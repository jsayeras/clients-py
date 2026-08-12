from __future__ import annotations

import random
import time
from typing import Any, BinaryIO

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from pydantic import BaseModel


DRIVE_API_SERVICE = "drive"
DRIVE_API_VERSION = "v3"

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

MAX_RETRIES = 4

RETRYABLE_STATUS_CODES = {
    429,
    500,
    502,
    503,
    504,
}


class DriveFolder(BaseModel):
    id: str
    name: str


class DriveFile(BaseModel):
    id: str
    name: str
    mime_type: str
    size: int | None = None
    web_url: str | None = None


class GoogleDriveClient:
    """
    Application-level Google Drive client.

    The client uses an OAuth access token obtained through
    the application's Google OAuth integration.

    The client exposes application-level models rather than
    Google Drive API response dictionaries.
    """

    def __init__(
        self,
        access_token: str,
    ) -> None:
        if not access_token:
            raise ValueError("Google Drive access token is required")

        credentials = Credentials(
            token=access_token,
        )

        self._service = build(
            DRIVE_API_SERVICE,
            DRIVE_API_VERSION,
            credentials=credentials,
            cache_discovery=False,
        )

    # --------------------------------------------------
    # Internal
    # --------------------------------------------------

    def _execute(
        self,
        request: Any,
    ) -> Any:
        """
        Execute a Google API request with exponential
        backoff for transient HTTP errors.
        """

        for attempt in range(MAX_RETRIES + 1):
            try:
                return request.execute()

            except HttpError as exc:
                status = getattr(
                    exc.resp,
                    "status",
                    None,
                )

                if status not in RETRYABLE_STATUS_CODES or attempt >= MAX_RETRIES:
                    raise

                delay = 2**attempt + random.uniform(0, 1)

                time.sleep(delay)

        raise RuntimeError("Google Drive request failed after retries")

    # --------------------------------------------------
    # Folders
    # --------------------------------------------------

    def get_folder(
        self,
        folder_id: str,
    ) -> DriveFolder | None:
        """
        Retrieve a folder by ID.

        Returns None when the folder does not exist,
        is inaccessible, or is trashed.
        """

        if not folder_id:
            raise ValueError("folder_id is required")

        try:
            response = self._execute(
                self._service.files().get(
                    fileId=folder_id,
                    fields=("id,name,mimeType,trashed"),
                )
            )

        except HttpError as exc:
            if (
                getattr(
                    exc.resp,
                    "status",
                    None,
                )
                == 404
            ):
                return None

            raise

        if response.get("trashed"):
            return None

        if response.get("mimeType") != FOLDER_MIME_TYPE:
            raise ValueError(f"Drive item {folder_id!r} is not a folder")

        return DriveFolder(
            id=response["id"],
            name=response["name"],
        )

    def find_folder(
        self,
        name: str,
        *,
        parent_id: str | None = None,
    ) -> DriveFolder | None:
        """
        Find a non-trashed folder by name.

        When parent_id is supplied, only direct children
        of that folder are searched.
        """

        if not name:
            raise ValueError("Folder name is required")

        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")

        query_parts = [
            f"name = '{escaped_name}'",
            f"mimeType = '{FOLDER_MIME_TYPE}'",
            "trashed = false",
        ]

        if parent_id:
            escaped_parent_id = parent_id.replace("\\", "\\\\").replace("'", "\\'")

            query_parts.append(f"'{escaped_parent_id}' in parents")

        query = " and ".join(query_parts)

        response = self._execute(
            self._service.files().list(
                q=query,
                spaces="drive",
                pageSize=100,
                fields=("files(id,name,mimeType,trashed)"),
                orderBy="createdTime",
            )
        )

        files = response.get(
            "files",
            [],
        )

        if not files:
            return None

        folder = files[0]

        return DriveFolder(
            id=folder["id"],
            name=folder["name"],
        )

    def get_or_create_folder(
        self,
        name: str,
        *,
        parent_id: str | None = None,
    ) -> DriveFolder:
        """
        Get an existing folder or create it.

        The lookup is restricted to the specified parent.
        """

        existing = self.find_folder(
            name,
            parent_id=parent_id,
        )

        if existing is not None:
            return existing

        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": FOLDER_MIME_TYPE,
        }

        if parent_id:
            metadata["parents"] = [
                parent_id,
            ]

        response = self._execute(
            self._service.files().create(
                body=metadata,
                fields="id,name,mimeType",
            )
        )

        return DriveFolder(
            id=response["id"],
            name=response["name"],
        )

    # --------------------------------------------------
    # Files
    # --------------------------------------------------

    def get_file(
        self,
        file_id: str,
    ) -> DriveFile | None:
        """
        Retrieve a file by ID.

        Returns None when the file does not exist,
        is inaccessible, or is trashed.
        """

        if not file_id:
            raise ValueError("file_id is required")

        try:
            response = self._execute(
                self._service.files().get(
                    fileId=file_id,
                    fields=("id,name,mimeType,size,webViewLink,trashed"),
                )
            )

        except HttpError as exc:
            if (
                getattr(
                    exc.resp,
                    "status",
                    None,
                )
                == 404
            ):
                return None

            raise

        if response.get("trashed"):
            return None

        size = response.get("size")

        return DriveFile(
            id=response["id"],
            name=response["name"],
            mime_type=response["mimeType"],
            size=(int(size) if size is not None else None),
            web_url=response.get("webViewLink"),
        )

    def upload_file(
        self,
        *,
        filename: str,
        content: BinaryIO,
        mime_type: str,
        parent_id: str | None = None,
        chunk_size: int = 5 * 1024 * 1024,
    ) -> DriveFile:
        """
        Upload a file to Google Drive using a resumable upload.

        `content` must be a binary file-like object supporting
        read(), seek(), and tell().
        """

        if not filename:
            raise ValueError("filename is required")

        if not mime_type:
            raise ValueError("mime_type is required")

        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")

        if parent_id:
            parent = self.get_folder(parent_id)

            if parent is None:
                raise ValueError(
                    f"Parent folder {parent_id!r} does not exist or is inaccessible"
                )

        metadata: dict[str, Any] = {
            "name": filename,
        }

        if parent_id:
            metadata["parents"] = [
                parent_id,
            ]

        media = MediaIoBaseUpload(
            content,
            mimetype=mime_type,
            chunksize=chunk_size,
            resumable=True,
        )

        request = self._service.files().create(
            body=metadata,
            media_body=media,
            fields=("id,name,mimeType,size,webViewLink"),
        )

        response = self._execute_upload(request)

        size = response.get("size")

        return DriveFile(
            id=response["id"],
            name=response["name"],
            mime_type=response["mimeType"],
            size=(int(size) if size is not None else None),
            web_url=response.get("webViewLink"),
        )

    def _execute_upload(
        self,
        request: Any,
    ) -> dict[str, Any]:
        """
        Execute a resumable upload.

        The Google API client handles the individual
        resumable upload chunks. This method handles
        transient HTTP failures while advancing the
        upload.
        """

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = None

                while response is None:
                    _, response = request.next_chunk()

                return response

            except HttpError as exc:
                status = getattr(
                    exc.resp,
                    "status",
                    None,
                )

                if status not in RETRYABLE_STATUS_CODES or attempt >= MAX_RETRIES:
                    raise

                delay = 2**attempt + random.uniform(0, 1)

                time.sleep(delay)

        raise RuntimeError("Google Drive upload failed after retries")

    def list_folders(
        self,
        *,
        parent_id: str | None = None,
    ) -> list[DriveFolder]:
        """
        List non-trashed folders accessible to the user.

        When parent_id is provided, only direct child folders
        are returned.
        """

        query_parts = [
            f"mimeType = '{FOLDER_MIME_TYPE}'",
            "trashed = false",
        ]

        if parent_id:
            query_parts.append(f"'{parent_id}' in parents")

        query = " and ".join(query_parts)

        folders: list[DriveFolder] = []
        page_token: str | None = None

        while True:
            response = self._execute(
                self._service.files().list(
                    q=query,
                    spaces="drive",
                    pageSize=100,
                    pageToken=page_token,
                    fields=("nextPageToken,files(id,name,mimeType)"),
                    orderBy="name",
                )
            )

            for item in response.get("files", []):
                folders.append(
                    DriveFolder(
                        id=item["id"],
                        name=item["name"],
                    )
                )

            page_token = response.get("nextPageToken")

            if not page_token:
                break

        return folders

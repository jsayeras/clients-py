from __future__ import annotations

from .errors import GmailClientError



import logging
from collections.abc import Iterable, Iterator
from itertools import islice
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from .models import (
    GmailBatchError,
    GmailHistoryResponse,
    GmailLabel,
    GmailMessageRef,
)

logger = logging.getLogger(__name__)

GMAIL_API_NAME = "gmail"
GMAIL_API_VERSION = "v1"

DEFAULT_MAX_RESULTS = 100
DEFAULT_BATCH_SIZE = 20
MAX_PAGE_SIZE = 500

DEFAULT_METADATA_HEADERS = (
    "Subject",
    "From",
    "To",
    "Date",
)


class GmailClient:
    """
    Thin, typed wrapper around the Gmail API.

    OAuth credential acquisition is deliberately handled outside
    this class.
    """

    def __init__(
        self,
        credentials: str | Credentials,
    ) -> None:

        if isinstance(credentials, str):
            credentials = Credentials(
                token=credentials,
            )
        self._service: Resource = build(
            GMAIL_API_NAME,
            GMAIL_API_VERSION,
            credentials=credentials,
            cache_discovery=False,
        )

    @staticmethod
    def _translate_error(exc: HttpError) -> GmailClientError:
        """
        Translate a Google API HttpError into a client-level exception.
        """
        status_code = getattr(exc.resp, "status", None)

        return GmailClientError(
            "Gmail API request failed",
            status_code=status_code,
        )

    @property
    def service(self) -> Resource:
        return self._service

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def list_messages(
        self,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        query: str | None = None,
    ) -> list[GmailMessageRef]:
        """
        List Gmail messages with automatic pagination.
        """

        if max_results <= 0:
            return []

        messages: list[GmailMessageRef] = []
        page_token: str | None = None

        while len(messages) < max_results:
            page_size = min(
                MAX_PAGE_SIZE,
                max_results - len(messages),
            )

            response = (
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=page_size,
                    pageToken=page_token,
                )
                .execute()
            )

            messages.extend(
                GmailMessageRef.model_validate(message)
                for message in response.get("messages", [])
            )

            page_token = response.get("nextPageToken")

            if not page_token:
                break

        return messages[:max_results]

    def get_message(
        self,
        message_id: str,
        *,
        format: str = "metadata",
        metadata_headers: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """
        Retrieve a raw Gmail message.

        Message parsing should remain outside the client.
        """

        if not message_id:
            raise ValueError("message_id cannot be empty")

        kwargs: dict[str, Any] = {
            "userId": "me",
            "id": message_id,
            "format": format,
        }

        if metadata_headers is not None:
            kwargs["metadataHeaders"] = list(metadata_headers)

        return (
            self.service.users()
            .messages()
            .get(**kwargs)
            .execute()
        )

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def get_latest_history_id(self) -> int | None:
        """
        Return the latest mailbox history ID.
        """

        messages = self.list_messages(max_results=1)

        if not messages:
            return None

        message = self.get_message(
            messages[0].id,
            format="minimal",
        )

        history_id = message.get("historyId")

        if history_id is None:
            return None

        return int(history_id)

    def list_history(
        self,
        history_id: int,
    ) -> GmailHistoryResponse:
        """
        Return mailbox changes since history_id.
        """

        if history_id <= 0:
            raise ValueError(
                "history_id must be greater than zero",
            )

        response = (
            self.service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=str(history_id),
                historyTypes=["messageAdded"],
                fields=(
                    "history("
                    "id,"
                    "messagesAdded("
                    "message(id,threadId)"
                    ")"
                    "),"
                    "nextPageToken,"
                    "historyId"
                ),
            )
            .execute()
        )

        return GmailHistoryResponse.model_validate(response)

    @staticmethod
    def extract_message_ids(
        history: GmailHistoryResponse,
    ) -> list[str]:
        """
        Extract unique message IDs from a history response.
        """

        ids: set[str] = set()

        for record in history.history:
            for added in record.messages_added:
                ids.add(added.message.id)

        return list(ids)

    # ------------------------------------------------------------------
    # Batch messages
    # ------------------------------------------------------------------

    @staticmethod
    def _chunks(
        iterable: Iterable[str],
        size: int,
    ) -> Iterator[list[str]]:
        if size <= 0:
            raise ValueError("size must be greater than zero")

        iterator = iter(iterable)

        while chunk := list(islice(iterator, size)):
            yield chunk

    def batch_get_messages(
        self,
        message_ids: Iterable[str],
        *,
        format: str = "metadata",
        batch_size: int = DEFAULT_BATCH_SIZE,
        metadata_headers: Iterable[str] = DEFAULT_METADATA_HEADERS,
    ) -> list[dict[str, Any]]:
        """
        Retrieve multiple Gmail messages using HTTP batch requests.

        Returns raw Gmail API responses. Conversion into the application's
        Email model belongs in the service layer.
        """

        messages: list[dict[str, Any]] = []

        for chunk in self._chunks(
            message_ids,
            batch_size,
        ):
            responses: list[dict[str, Any]] = []
            errors: list[GmailBatchError] = []

            def callback(
                request_id: str,
                response: dict[str, Any] | None,
                exception: Exception | None,
            ) -> None:
                if exception is not None:
                    errors.append(
                        GmailBatchError(
                            message_id=request_id,
                            error=str(exception),
                        )
                    )
                    return

                if response is not None:
                    responses.append(response)

            batch = self.service.new_batch_http_request(
                callback=callback,
            )

            for message_id in chunk:
                request = (
                    self.service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=message_id,
                        format=format,
                        metadataHeaders=list(metadata_headers),
                    )
                )

                batch.add(
                    request,
                    request_id=message_id,
                )

            try:
                batch.execute()
            except HttpError:
                logger.exception(
                    "Gmail batch request failed for %d messages",
                    len(chunk),
                )
                raise

            for error in errors:
                logger.warning(
                    "Failed to retrieve Gmail message %s: %s",
                    error.message_id,
                    error.error,
                )

            messages.extend(responses)

        logger.info(
            "Successfully retrieved %d Gmail messages",
            len(messages),
        )

        return messages

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    def get_labels(self) -> list[GmailLabel]:
        """
        Return all Gmail labels.
        """

        response = (
            self.service.users()
            .labels()
            .list(userId="me")
            .execute()
        )

        return [
            GmailLabel.model_validate(label)
            for label in response.get("labels", [])
        ]

    def get_label_by_name(
        self,
        name: str,
    ) -> GmailLabel | None:
        """
        Find a Gmail label by name.
        """

        if not name:
            raise ValueError("Label name cannot be empty")

        return next(
            (
                label
                for label in self.get_labels()
                if label.name == name
            ),
            None,
        )

    def create_label(
        self,
        name: str,
        *,
        message_list_visibility: str = "show",
        label_list_visibility: str = "labelShow",
    ) -> GmailLabel:
        """
        Create a Gmail label.
        """

        if not name:
            raise ValueError("Label name cannot be empty")

        response = (
            self.service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": label_list_visibility,
                    "messageListVisibility": message_list_visibility,
                },
            )
            .execute()
        )

        return GmailLabel.model_validate(response)

    def get_or_create_label(
        self,
        name: str,
        *,
        message_list_visibility: str = "show",
        label_list_visibility: str = "labelShow",
    ) -> GmailLabel:
        """
        Return an existing label or create it.
        """

        label = self.get_label_by_name(name)

        if label is not None:
            return label

        return self.create_label(
            name,
            message_list_visibility=message_list_visibility,
            label_list_visibility=label_list_visibility,
        )

    def add_labels(
        self,
        message_id: str,
        labels: Iterable[str],
    ) -> None:
        """
        Add Gmail label IDs to a message.
        """

        label_ids = list(labels)

        if not label_ids:
            return

        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={
                "addLabelIds": label_ids,
            },
        ).execute()

from __future__ import annotations


class GmailClientError(Exception):
    """
    Base exception raised by the Gmail client.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code

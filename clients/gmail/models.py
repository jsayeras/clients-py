from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GmailMessageRef(BaseModel):
    """Minimal Gmail message returned by messages.list."""

    model_config = ConfigDict(extra="ignore")

    id: str
    thread_id: str = Field(alias="threadId")


class GmailLabel(BaseModel):
    """Gmail label."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    type: str


class GmailHistoryMessage(BaseModel):
    """Message reference contained in a history event."""

    model_config = ConfigDict(extra="ignore")

    id: str
    thread_id: str | None = Field(
        default=None,
        alias="threadId",
    )


class GmailMessagesAdded(BaseModel):
    """A messageAdded history event."""

    model_config = ConfigDict(extra="ignore")

    message: GmailHistoryMessage


class GmailHistoryRecord(BaseModel):
    """A single Gmail history record."""

    model_config = ConfigDict(extra="ignore")

    id: str
    messages_added: list[GmailMessagesAdded] = Field(
        default_factory=list,
        alias="messagesAdded",
    )


class GmailHistoryResponse(BaseModel):
    """Response returned by Gmail history.list."""

    model_config = ConfigDict(extra="ignore")

    history: list[GmailHistoryRecord] = Field(
        default_factory=list,
    )
    next_page_token: str | None = Field(
        default=None,
        alias="nextPageToken",
    )
    history_id: int | None = Field(
        default=None,
        alias="historyId",
    )


class GmailBatchError(BaseModel):
    """Error encountered while retrieving a message in a batch."""

    message_id: str
    error: str


class GmailBatchResult[T](BaseModel):
    """Result of a Gmail batch message request."""

    messages: list[T] = Field(default_factory=list)
    errors: list[GmailBatchError] = Field(default_factory=list)

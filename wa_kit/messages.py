"""Inbound message model — the one shape every transport normalises to."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def as_utc(dt: datetime) -> datetime:
    """Normalise to an aware UTC datetime; a naive datetime is taken to already be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class MessageType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    LOCATION = "location"
    INTERACTIVE = "interactive"
    #: An emoji reaction to an earlier message. Not a conversational turn: the agent ignores it.
    REACTION = "reaction"
    UNSUPPORTED = "unsupported"


class Media(BaseModel):
    media_id: str
    mime_type: str | None = None
    caption: str | None = None
    filename: str | None = None
    sha256: str | None = None


class Location(BaseModel):
    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None


class InboundMessage(BaseModel):
    """A single user → business message, independent of transport."""

    message_id: str
    wa_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    type: MessageType = MessageType.TEXT
    text: str | None = None
    media: Media | None = None
    location: Location | None = None
    interactive_reply_id: str | None = None
    profile_name: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def content(self) -> str:
        """Best-effort textual content for the flow engine."""
        if self.text:
            return self.text
        if self.media and self.media.caption:
            return self.media.caption
        if self.location:
            return f"{self.location.latitude},{self.location.longitude}"
        return ""

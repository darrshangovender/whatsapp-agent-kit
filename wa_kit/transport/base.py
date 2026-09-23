"""Transport protocol — the only surface the rest of the kit uses to talk to WhatsApp."""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

MediaType = Literal["image", "document", "audio", "video"]


class SendResult(BaseModel):
    message_id: str
    ok: bool = True
    raw: dict[str, Any] = Field(default_factory=dict)


class TransportError(RuntimeError):
    """Raised when the transport gives up on a send."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@runtime_checkable
class Transport(Protocol):
    async def send_text(self, to: str, body: str) -> SendResult: ...

    async def send_template(
        self,
        to: str,
        name: str,
        language: str,
        body_params: list[str],
    ) -> SendResult: ...

    async def send_media(
        self,
        to: str,
        media_type: MediaType,
        *,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
    ) -> SendResult: ...

    async def mark_read(self, message_id: str) -> None: ...

    def verify_webhook(self, signature: str | None, body: bytes) -> bool: ...

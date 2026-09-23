"""In-memory transport: records every outbound call and fabricates inbound messages."""

from __future__ import annotations

import itertools
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wa_kit.messages import InboundMessage, MessageType, utcnow
from wa_kit.transport._signing import sign_body, verify_signature
from wa_kit.transport.base import MediaType, SendResult


@dataclass
class SentMessage:
    kind: str  # "text" | "template" | "media"
    to: str
    body: str | None = None
    template: str | None = None
    language: str | None = None
    params: list[str] = field(default_factory=list)
    media_type: str | None = None
    link: str | None = None
    media_id: str | None = None
    caption: str | None = None
    at: datetime = field(default_factory=utcnow)


class MockTransport:
    """Records sends; ``fail_next`` lets tests inject a transport failure."""

    def __init__(self, app_secret: str = "test-app-secret") -> None:
        self.app_secret = app_secret
        self.sent: list[SentMessage] = []
        self.read_receipts: list[str] = []
        self.fail_next: deque[Exception] = deque()
        self._ids = itertools.count(1)
        self._inbound_ids = itertools.count(1)

    # -- outbound -------------------------------------------------------------

    def _record(self, msg: SentMessage) -> SendResult:
        if self.fail_next:
            raise self.fail_next.popleft()
        self.sent.append(msg)
        return SendResult(message_id=f"wamid.mock.{next(self._ids)}")

    async def send_text(self, to: str, body: str) -> SendResult:
        return self._record(SentMessage(kind="text", to=to, body=body))

    async def send_template(
        self, to: str, name: str, language: str, body_params: list[str]
    ) -> SendResult:
        return self._record(
            SentMessage(
                kind="template", to=to, template=name, language=language, params=list(body_params)
            )
        )

    async def send_media(
        self,
        to: str,
        media_type: MediaType,
        *,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
    ) -> SendResult:
        return self._record(
            SentMessage(
                kind="media",
                to=to,
                media_type=media_type,
                link=link,
                media_id=media_id,
                caption=caption,
            )
        )

    async def mark_read(self, message_id: str) -> None:
        self.read_receipts.append(message_id)

    def verify_webhook(self, signature: str | None, body: bytes) -> bool:
        return verify_signature(self.app_secret, signature, body)

    # -- helpers for tests / examples -------------------------------------------

    def sign(self, body: bytes) -> str:
        """Value for the ``X-Hub-Signature-256`` header of a scripted webhook POST."""
        return sign_body(self.app_secret, body)

    def texts_to(self, wa_id: str) -> list[str]:
        return [m.body or "" for m in self.sent if m.kind == "text" and m.to == wa_id]

    def last_text(self, wa_id: str) -> str | None:
        texts = self.texts_to(wa_id)
        return texts[-1] if texts else None

    def clear(self) -> None:
        self.sent.clear()
        self.read_receipts.clear()

    def inbound(
        self,
        wa_id: str,
        text: str,
        *,
        at: datetime | None = None,
        message_id: str | None = None,
        profile_name: str | None = None,
    ) -> InboundMessage:
        """Fabricate a text ``InboundMessage`` as the webhook parser would produce it."""
        return InboundMessage(
            message_id=message_id or f"wamid.in.{next(self._inbound_ids)}",
            wa_id=wa_id,
            timestamp=at or utcnow(),
            type=MessageType.TEXT,
            text=text,
            profile_name=profile_name,
        )

    @staticmethod
    def meta_payload(
        wa_id: str,
        text: str,
        *,
        message_id: str = "wamid.HBgL",
        profile_name: str = "Test User",
        timestamp: int | None = None,
        phone_number_id: str = "1234567890",
    ) -> dict[str, Any]:
        """A raw Meta webhook body for a single inbound text message."""
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "27600000000",
                                    "phone_number_id": phone_number_id,
                                },
                                "contacts": [{"profile": {"name": profile_name}, "wa_id": wa_id}],
                                "messages": [
                                    {
                                        "from": wa_id,
                                        "id": message_id,
                                        "timestamp": str(timestamp or int(time.time())),
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }

    def meta_body(self, wa_id: str, text: str, **kw: Any) -> tuple[bytes, dict[str, str]]:
        """``(body_bytes, headers)`` ready for a signed webhook POST."""
        body = json.dumps(self.meta_payload(wa_id, text, **kw)).encode("utf-8")
        return body, {"X-Hub-Signature-256": self.sign(body), "Content-Type": "application/json"}

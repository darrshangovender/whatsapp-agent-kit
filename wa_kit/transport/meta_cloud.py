"""WhatsApp Cloud API (Graph v20) transport over httpx."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from wa_kit.transport._signing import verify_signature
from wa_kit.transport.base import MediaType, SendResult, TransportError

log = logging.getLogger("wa_kit.transport.meta")

RETRYABLE = {429, 500, 502, 503, 504}


class MetaCloudTransport:
    """Sends via ``POST /{version}/{phone_number_id}/messages``.

    - The ``httpx.AsyncClient`` is created lazily on first send, so constructing the
      transport (e.g. at import time in a server module) never opens sockets.
    - 429 and 5xx responses are retried with exponential backoff, honouring
      ``Retry-After`` when Meta sends one. Other 4xx fail immediately.
    - ``verify_webhook`` checks ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body
      with the app secret) using a constant-time comparison.
    """

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        app_secret: str,
        *,
        api_version: str = "v20.0",
        base_url: str = "https://graph.facebook.com",
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.app_secret = app_secret
        self.api_version = api_version
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._client = client
        self._sleep = sleep

    # -- client ---------------------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    @property
    def client_created(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def messages_url(self) -> str:
        return f"/{self.api_version}/{self.phone_number_id}/messages"

    # -- sending ---------------------------------------------------------------

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: TransportError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.client.post(
                    self.messages_url, json=payload, headers=self.headers
                )
            except httpx.TransportError as exc:  # network-level: retry
                last_error = TransportError(f"network error: {exc}")
                delay = self._delay(attempt, None)
            else:
                if resp.status_code < 400:
                    return self._json(resp)
                body = self._json(resp)
                last_error = TransportError(
                    f"Cloud API {resp.status_code}: {body}", status=resp.status_code, body=body
                )
                if resp.status_code not in RETRYABLE:
                    raise last_error
                delay = self._delay(attempt, resp.headers.get("Retry-After"))
            if attempt < self.max_retries:
                log.warning("send failed (%s); retrying in %.2fs", last_error, delay)
                await self._sleep(delay)
        assert last_error is not None
        raise last_error

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.backoff_cap)
            except ValueError:
                pass
        return min(self.backoff_base * (2**attempt), self.backoff_cap)

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            return {"text": resp.text}
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _result(data: dict[str, Any]) -> SendResult:
        messages = data.get("messages") or [{}]
        return SendResult(message_id=str(messages[0].get("id", "")), ok=True, raw=data)

    def _base(self, to: str) -> dict[str, Any]:
        return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}

    async def send_text(self, to: str, body: str) -> SendResult:
        payload = self._base(to) | {"type": "text", "text": {"preview_url": False, "body": body}}
        return self._result(await self._post(payload))

    async def send_template(
        self, to: str, name: str, language: str, body_params: list[str]
    ) -> SendResult:
        components: list[dict[str, Any]] = []
        if body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                }
            )
        payload = self._base(to) | {
            "type": "template",
            "template": {"name": name, "language": {"code": language}, "components": components},
        }
        return self._result(await self._post(payload))

    async def send_media(
        self,
        to: str,
        media_type: MediaType,
        *,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
    ) -> SendResult:
        if not (link or media_id) or (link and media_id):
            raise ValueError("send_media needs exactly one of link= or media_id=")
        media: dict[str, Any] = {"link": link} if link else {"id": media_id}
        if caption:
            media["caption"] = caption
        payload = self._base(to) | {"type": media_type, media_type: media}
        return self._result(await self._post(payload))

    async def mark_read(self, message_id: str) -> None:
        await self._post({"messaging_product": "whatsapp", "status": "read", "message_id": message_id})

    # -- webhooks ---------------------------------------------------------------

    def verify_webhook(self, signature: str | None, body: bytes) -> bool:
        return verify_signature(self.app_secret, signature, body)

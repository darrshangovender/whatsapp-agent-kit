"""Starlette webhook: GET verification, POST inbound → parse → dedup → queue → 200."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from wa_kit.messages import InboundMessage, Location, Media, MessageType, utcnow
from wa_kit.transport.base import Transport

log = logging.getLogger("wa_kit.webhook")

Handler = Callable[[InboundMessage], Awaitable[Any]]


# --- parsing ------------------------------------------------------------------------


def _timestamp(raw: str | int | None) -> datetime:
    """Meta sends unix seconds as a string. A missing/garbled value falls back to now — loudly,
    because the 24h window is computed from it."""
    if raw is None:
        return utcnow()
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        log.warning("unparseable message timestamp %r; using receive time", raw)
        return utcnow()


def parse_message(msg: dict[str, Any], contacts: dict[str, str]) -> InboundMessage:
    """Normalise one entry of ``value.messages[]`` into an ``InboundMessage``."""
    mtype = msg.get("type", "")
    base = {
        "message_id": msg["id"],
        "wa_id": msg["from"],
        "timestamp": _timestamp(msg.get("timestamp")),
        "profile_name": contacts.get(msg["from"]),
        "raw": msg,
    }
    if mtype == "text":
        return InboundMessage(**base, type=MessageType.TEXT, text=msg.get("text", {}).get("body"))
    if mtype in ("image", "document"):
        m = msg.get(mtype, {})
        media = Media(
            media_id=m.get("id", ""),
            mime_type=m.get("mime_type"),
            caption=m.get("caption"),
            filename=m.get("filename"),
            sha256=m.get("sha256"),
        )
        return InboundMessage(**base, type=MessageType(mtype), media=media, text=media.caption)
    if mtype == "location":
        loc = msg.get("location", {})
        return InboundMessage(
            **base,
            type=MessageType.LOCATION,
            location=Location(
                latitude=float(loc.get("latitude", 0.0)),
                longitude=float(loc.get("longitude", 0.0)),
                name=loc.get("name"),
                address=loc.get("address"),
            ),
        )
    if mtype == "interactive":
        inter = msg.get("interactive", {})
        reply = inter.get(inter.get("type", ""), {})  # button_reply | list_reply
        return InboundMessage(
            **base,
            type=MessageType.INTERACTIVE,
            text=reply.get("title"),
            interactive_reply_id=reply.get("id"),
        )
    if mtype == "reaction":
        return InboundMessage(**base, type=MessageType.REACTION)
    return InboundMessage(**base, type=MessageType.UNSUPPORTED)


def parse_payload(payload: dict[str, Any]) -> list[InboundMessage]:
    """Extract every inbound message from a Meta webhook body. Status updates are ignored."""
    out: list[InboundMessage] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return out
    for entry in _dicts(payload.get("entry")):
        for change in _dicts(entry.get("changes")):
            if change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            contacts = {
                c.get("wa_id", ""): (c.get("profile") or {}).get("name")
                for c in _dicts(value.get("contacts"))
            }
            for msg in _dicts(value.get("messages")):
                try:
                    out.append(parse_message(msg, contacts))
                except (KeyError, ValueError, TypeError, AttributeError) as exc:
                    log.warning("skipping malformed message: %r", exc)
    return out


def _dicts(items: Any) -> list[dict[str, Any]]:
    """The dict elements of a list-ish value; anything else (None, str, ...) is ignored."""
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


# --- idempotency ------------------------------------------------------------------


class DedupStore:
    """SQLite table of seen ``message_id``s. ``mark()`` is atomic: True only on first sight."""

    def __init__(
        self, path_or_conn: str | sqlite3.Connection = ":memory:", *, ttl_days: int = 7
    ) -> None:
        if isinstance(path_or_conn, sqlite3.Connection):
            self._conn = path_or_conn
        else:
            self._conn = sqlite3.connect(path_or_conn, check_same_thread=False)
        self._lock = threading.Lock()
        self.ttl_days = ttl_days
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS seen_messages "
                "(message_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)"
            )
            self._conn.commit()

    def mark(self, message_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO seen_messages (message_id, seen_at) VALUES (?, ?)",
                (message_id, utcnow().isoformat()),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def seen(self, message_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return row is not None

    def purge(self, now: datetime | None = None) -> int:
        from datetime import timedelta

        cutoff = ((now or utcnow()) - timedelta(days=self.ttl_days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM seen_messages WHERE seen_at < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount


# --- worker -----------------------------------------------------------------------


async def run_worker(queue: asyncio.Queue[InboundMessage], handler: Handler) -> None:
    """Drain ``queue`` forever, one message at a time. Handler exceptions are logged, not fatal."""
    while True:
        msg = await queue.get()
        try:
            await handler(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("handler failed for %s from %s", msg.message_id, msg.wa_id)
        finally:
            queue.task_done()


# --- app -----------------------------------------------------------------------


def create_app(
    *,
    transport: Transport,
    verify_token: str,
    handler: Handler,
    dedup: DedupStore | None = None,
    queue: asyncio.Queue[InboundMessage] | None = None,
    path: str = "/webhook",
    start_worker: bool = True,
) -> Starlette:
    """Build the Starlette app.

    The POST route does the minimum before acking: signature check, JSON parse,
    dedup insert, ``queue.put_nowait``. The handler runs in a background worker
    started by the app lifespan (disable with ``start_worker=False`` to drain
    ``app.state.queue`` yourself, e.g. in tests).
    """
    dedup = dedup or DedupStore()
    queue = queue if queue is not None else asyncio.Queue()

    expected_token = verify_token.encode("utf-8")

    async def verify(request: Request) -> Response:
        q = request.query_params
        token = q.get("hub.verify_token", "").encode("utf-8", "replace")
        if q.get("hub.mode") == "subscribe" and hmac.compare_digest(token, expected_token):
            return PlainTextResponse(q.get("hub.challenge", ""))
        return PlainTextResponse("verification failed", status_code=403)

    async def inbound(request: Request) -> Response:
        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if not transport.verify_webhook(signature, body):
            return JSONResponse({"error": "bad signature"}, status_code=403)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "expected a JSON object"}, status_code=400)

        queued = duplicates = 0
        for msg in parse_payload(payload):
            if dedup.mark(msg.message_id):
                queue.put_nowait(msg)
                queued += 1
            else:
                duplicates += 1
        return JSONResponse({"queued": queued, "duplicates": duplicates})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        task = asyncio.create_task(run_worker(queue, handler)) if start_worker else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = Starlette(
        routes=[
            Route(path, verify, methods=["GET"]),
            Route(path, inbound, methods=["POST"]),
            Route("/health", lambda r: JSONResponse({"ok": True}), methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    app.state.queue = queue
    app.state.dedup = dedup
    return app

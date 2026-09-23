"""Per-conversation state in SQLite, plus the 24-hour customer-service-window rule."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from wa_kit.messages import as_utc, utcnow

Role = Literal["user", "assistant", "system"]

#: Meta's rule: free-form messages are only allowed within 24h of the user's last inbound.
SERVICE_WINDOW = timedelta(hours=24)


def _iso_utc(dt: datetime) -> str:
    """ISO-8601 in UTC, so the ``last_seen`` column compares correctly as a string.

    ``"…T12:00:00+02:00" < "…T11:00:00+00:00"`` is False lexicographically but True in
    time; normalising every stored and compared value to ``+00:00`` removes the trap.
    """
    return as_utc(dt).isoformat()


class HistoryEntry(BaseModel):
    role: Role
    content: str
    at: datetime = Field(default_factory=utcnow)


class ConversationState(BaseModel):
    wa_id: str
    stage: str | None = None
    slots: dict[str, Any] = Field(default_factory=dict)
    history: list[HistoryEntry] = Field(default_factory=list)
    handoff: bool = False
    opted_out: bool = False
    consent_recorded: bool = False
    clarify_count: int = 0
    profile_name: str | None = None
    last_inbound_at: datetime | None = None
    last_seen: datetime | None = None

    def add_history(self, role: Role, content: str, at: datetime | None = None) -> None:
        self.history.append(HistoryEntry(role=role, content=content, at=at or utcnow()))

    def can_send_freeform(self, now: datetime | None = None) -> bool:
        """True while inside Meta's 24h customer-service window."""
        if self.last_inbound_at is None:
            return False
        return as_utc(now or utcnow()) - as_utc(self.last_inbound_at) < SERVICE_WINDOW

    def reset_flow(self) -> None:
        self.stage = None
        self.slots = {}
        self.clarify_count = 0


class ConversationStore:
    """SQLite-backed store. ``path=":memory:"`` for tests; a file path for real deployments.

    One connection is shared behind a lock: writes are tiny, and a single-process
    asyncio worker is the intended deployment (see README → Limitations).
    """

    def __init__(self, path: str = ":memory:", *, clock: Callable[[], datetime] = utcnow) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    wa_id           TEXT PRIMARY KEY,
                    data            TEXT NOT NULL,
                    last_seen       TEXT,
                    last_inbound_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_conversations_last_seen ON conversations(last_seen);
                """
            )
            self._conn.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        """Shared connection, so ``DedupStore`` can live in the same file."""
        return self._conn

    # -- CRUD -------------------------------------------------------------------

    def get(self, wa_id: str) -> ConversationState:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM conversations WHERE wa_id = ?", (wa_id,)
            ).fetchone()
        if row is None:
            return ConversationState(wa_id=wa_id)
        return ConversationState.model_validate_json(row["data"])

    def exists(self, wa_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM conversations WHERE wa_id = ?", (wa_id,)
            ).fetchone()
        return row is not None

    def save(self, state: ConversationState) -> None:
        if state.last_seen is None:
            state.last_seen = self._clock()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversations (wa_id, data, last_seen, last_inbound_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(wa_id) DO UPDATE SET
                    data = excluded.data,
                    last_seen = excluded.last_seen,
                    last_inbound_at = excluded.last_inbound_at
                """,
                (
                    state.wa_id,
                    state.model_dump_json(),
                    _iso_utc(state.last_seen),
                    _iso_utc(state.last_inbound_at) if state.last_inbound_at else None,
                ),
            )
            self._conn.commit()

    def delete(self, wa_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM conversations WHERE wa_id = ?", (wa_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def all(self) -> Iterator[ConversationState]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM conversations ORDER BY wa_id").fetchall()
        for row in rows:
            yield ConversationState.model_validate_json(row["data"])

    # -- window + retention ---------------------------------------------------

    def touch_inbound(self, wa_id: str, at: datetime | None = None) -> ConversationState:
        state = self.get(wa_id)
        at = at or self._clock()
        state.last_inbound_at = at
        state.last_seen = at
        self.save(state)
        return state

    def can_send_freeform(self, wa_id: str, now: datetime | None = None) -> bool:
        return self.get(wa_id).can_send_freeform(now or self._clock())

    def purge_older_than(self, days: int, now: datetime | None = None) -> int:
        """Delete every conversation whose ``last_seen`` is older than ``days``. Returns count."""
        cutoff = (now or self._clock()) - timedelta(days=days)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conversations WHERE last_seen IS NULL OR last_seen < ?",
                (_iso_utc(cutoff),),
            )
            self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

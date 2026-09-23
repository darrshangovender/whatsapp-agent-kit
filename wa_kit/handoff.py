"""Human handoff: mark, notify, pause the bot until ``resume()``."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field

from wa_kit.compliance import normalise_text
from wa_kit.conversation import ConversationState, ConversationStore
from wa_kit.messages import utcnow

log = logging.getLogger("wa_kit.handoff")

DEFAULT_TRIGGERS: frozenset[str] = frozenset(
    {"human", "agent", "speak to someone", "talk to someone", "real person", "speak to a person"}
)


class Escalation(BaseModel):
    wa_id: str
    reason: str
    stage: str | None = None
    last_message: str | None = None
    profile_name: str | None = None
    slots: dict[str, Any] = Field(default_factory=dict)
    transcript: list[str] = Field(default_factory=list)
    at: datetime = Field(default_factory=utcnow)


@runtime_checkable
class EscalationSink(Protocol):
    async def notify(self, escalation: Escalation) -> None: ...


class LogSink:
    """Logs each escalation and keeps them in ``events`` (handy for tests and demos)."""

    def __init__(self) -> None:
        self.events: list[Escalation] = []

    async def notify(self, escalation: Escalation) -> None:
        self.events.append(escalation)
        log.warning("HANDOFF %s (%s) at stage %s", escalation.wa_id, escalation.reason, escalation.stage)


class WebhookSink:
    """POSTs the escalation as JSON to ``url`` (Slack incoming webhook, CRM, pager...)."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def notify(self, escalation: Escalation) -> None:
        resp = await self.client.post(
            self.url, json=escalation.model_dump(mode="json"), headers=self.headers
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class HandoffManager:
    def __init__(
        self,
        store: ConversationStore,
        sinks: list[EscalationSink] | None = None,
        *,
        triggers: frozenset[str] | set[str] = DEFAULT_TRIGGERS,
        reply: str = "I'm connecting you to a person now — someone will reply here shortly.",
        transcript_tail: int = 10,
    ) -> None:
        self.store = store
        self.sinks: list[EscalationSink] = sinks if sinks is not None else [LogSink()]
        self.triggers = frozenset(normalise_text(t).lower() for t in triggers)
        self.reply = reply
        self.transcript_tail = transcript_tail

    def wants_human(self, text: str) -> bool:
        norm = normalise_text(text).lower()
        if norm in self.triggers:
            return True
        return any(" " in t and t in norm for t in self.triggers)

    def is_paused(self, wa_id: str) -> bool:
        return self.store.get(wa_id).handoff

    async def escalate(
        self, state: ConversationState, reason: str, last_message: str | None = None
    ) -> Escalation:
        state.handoff = True
        self.store.save(state)
        esc = Escalation(
            wa_id=state.wa_id,
            reason=reason,
            stage=state.stage,
            last_message=last_message,
            profile_name=state.profile_name,
            slots=dict(state.slots),
            transcript=[f"{h.role}: {h.content}" for h in state.history[-self.transcript_tail :]],
        )
        for sink in self.sinks:
            try:
                await sink.notify(esc)
            except Exception:  # a dead sink must never block the user-facing reply
                log.exception("escalation sink %r failed", sink)
        return esc

    def resume(self, wa_id: str, *, reset_flow: bool = False) -> ConversationState:
        state = self.store.get(wa_id)
        state.handoff = False
        if reset_flow:
            state.reset_flow()
        self.store.save(state)
        return state

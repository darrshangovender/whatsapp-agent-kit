"""The orchestrator: compliance → handoff → flow → transport, per inbound message."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from wa_kit.compliance import Compliance
from wa_kit.conversation import ConversationState, ConversationStore
from wa_kit.flow import Flow, FlowRunner
from wa_kit.handoff import HandoffManager
from wa_kit.messages import InboundMessage, MessageType, as_utc, utcnow
from wa_kit.transport.base import Transport

log = logging.getLogger("wa_kit.agent")


class Agent:
    """Wire a flow to a transport with the compliance and handoff layers in between.

    Every inbound message goes through, in order:

    0. reactions (👍 on an earlier message) are not turns and are dropped here;
    1. touch the 24h window and store the (PII-redacted) user turn — persisted at once,
       so a later send failure cannot lose the window;
    2. opt-out / opt-in keywords — honoured before anything else;
    3. if the conversation is handed off, stay silent (a human owns it);
    4. handoff trigger words → escalate;
    5. otherwise advance the flow; an escalation from the flow is honoured;
    6. send replies, store the assistant turns (redacted), persist state.
    """

    def __init__(
        self,
        *,
        store: ConversationStore,
        transport: Transport,
        flow: Flow,
        compliance: Compliance | None = None,
        handoff: HandoffManager | None = None,
        max_clarify: int = 2,
        mark_read: bool = True,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.transport = transport
        self.compliance = compliance or Compliance(store, clock=clock)
        self.handoff = handoff or HandoffManager(store)
        self.runner = FlowRunner(flow, max_clarify=max_clarify, redact=self.compliance.redact)
        self.mark_read = mark_read
        self._clock = clock

    async def handle(self, inbound: InboundMessage) -> list[str]:
        """Process one message and return the replies that were sent."""
        if inbound.type is MessageType.REACTION:
            log.debug("ignoring reaction %s from %s", inbound.message_id, inbound.wa_id)
            return []

        state = self.store.get(inbound.wa_id)
        text = inbound.content
        now = self._clock()

        # Meta does not guarantee delivery order; the window is the LATEST inbound.
        seen_at = as_utc(inbound.timestamp)
        if state.last_inbound_at is None or seen_at > as_utc(state.last_inbound_at):
            state.last_inbound_at = seen_at
        state.last_seen = now
        if inbound.profile_name:
            state.profile_name = inbound.profile_name
        state.add_history("user", self.compliance.redact(text), at=now)
        self.store.save(state)

        if self.mark_read:
            try:
                await self.transport.mark_read(inbound.message_id)
            except Exception:
                log.warning("mark_read failed for %s", inbound.message_id, exc_info=True)

        replies: list[str] = []
        if self.compliance.is_opt_out(text):
            replies = [self.compliance.opt_out(state)]
        elif state.opted_out:
            if self.compliance.is_opt_in(text):
                replies = [self.compliance.opt_in(state)]
            else:
                self.store.save(state)
                return []
        elif state.handoff:
            self.store.save(state)
            return []
        elif self.handoff.wants_human(text):
            await self._escalate(state, "user_requested", text)
            replies = [self.handoff.reply]
        else:
            result = await self.runner.step(state, text)
            replies = list(result.replies)
            if result.escalate:
                await self._escalate(state, result.reason or "flow", text)
                replies.append(self.handoff.reply)

        await self._send(state, replies, now)
        self.store.save(state)
        return replies

    async def _escalate(self, state: ConversationState, reason: str, text: str) -> None:
        # Sinks are external (Slack, CRM); the triggering message is redacted like the history.
        await self.handoff.escalate(state, reason, self.compliance.redact(text))

    async def _send(self, state: ConversationState, replies: list[str], now: datetime) -> None:
        for body in replies:
            await self.transport.send_text(state.wa_id, body)
            state.add_history("assistant", self.compliance.redact(body), at=now)

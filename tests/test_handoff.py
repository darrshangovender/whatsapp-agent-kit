from __future__ import annotations

import httpx
import pytest

from wa_kit import Escalation, HandoffManager, LogSink, WebhookSink
from wa_kit.handoff import EscalationSink

from .conftest import USER, VALID_SA_ID, decision


@pytest.mark.parametrize("text", ["HUMAN", "human", "Agent", "agent!", "I want to speak to someone please"])
def test_trigger_words(store, text):
    assert HandoffManager(store).wants_human(text)


@pytest.mark.parametrize("text", ["humanity", "my agent number is 5", "hi"])
def test_non_triggers(store, text):
    assert not HandoffManager(store).wants_human(text)


@pytest.mark.parametrize("text", ["human…", "Agent \U0001F64F", "«real person»", "speak-to-someone!"])
def test_trigger_words_tolerate_unicode_punctuation(store, text):
    assert HandoffManager(store).wants_human(text)


async def test_escalation_last_message_is_redacted(agent, transport, sink):
    await agent.handle(transport.inbound(USER, "hi"))
    await agent.handle(transport.inbound(USER, f"speak to someone, my ID is {VALID_SA_ID}"))
    esc = sink.events[0]
    assert esc.reason == "user_requested"
    assert VALID_SA_ID not in esc.last_message and "[SA_ID]" in esc.last_message
    assert all(VALID_SA_ID not in line for line in esc.transcript)


async def test_agent_escalates_on_keyword_and_pauses(agent, transport, sink):
    await agent.handle(transport.inbound(USER, "hi"))
    replies = await agent.handle(transport.inbound(USER, "AGENT"))
    assert replies == [agent.handoff.reply]
    assert agent.handoff.is_paused(USER)
    assert len(sink.events) == 1
    esc = sink.events[0]
    assert isinstance(esc, Escalation)
    assert esc.wa_id == USER and esc.reason == "user_requested" and esc.stage == "suburb"
    assert esc.last_message == "AGENT"
    assert esc.transcript[-1] == "user: AGENT"

    before = len(transport.sent)
    assert await agent.handle(transport.inbound(USER, "hello?")) == []
    assert len(transport.sent) == before  # bot is silent while handed off
    assert agent.store.get(USER).history[-1].content == "hello?"  # but still logged


async def test_resume_reactivates_bot_at_same_stage(agent, transport):
    await agent.handle(transport.inbound(USER, "hi"))
    await agent.handle(transport.inbound(USER, "human"))
    agent.handoff.resume(USER)
    assert not agent.handoff.is_paused(USER)
    replies = await agent.handle(transport.inbound(USER, "Glenwood"))
    assert replies == ["What's wrong?"]
    assert agent.store.get(USER).slots["suburb"] == "Glenwood"


async def test_resume_with_reset_restarts_flow(agent, transport):
    await agent.handle(transport.inbound(USER, "hi"))
    await agent.handle(transport.inbound(USER, "Glenwood"))
    await agent.handle(transport.inbound(USER, "human"))
    agent.handoff.resume(USER, reset_flow=True)
    replies = await agent.handle(transport.inbound(USER, "back"))
    assert replies == ["Hi there.", "Which suburb?"]


async def test_llm_needs_human_escalates_through_agent(agent, transport, sink, model):
    model._script = [decision("Let me get a person.", needs_human=True)]
    await agent.handle(transport.inbound(USER, "hi"))
    await agent.handle(transport.inbound(USER, "Glenwood"))
    replies = await agent.handle(transport.inbound(USER, "gas leak"))
    assert replies == ["Let me get a person.", agent.handoff.reply]
    assert sink.events[0].reason == "llm_needs_human"
    assert sink.events[0].slots["suburb"] == "Glenwood"


async def test_clarify_limit_escalates_through_agent(agent, transport, sink):
    await agent.handle(transport.inbound(USER, "hi"))
    for _ in range(2):
        await agent.handle(transport.inbound(USER, "?"))
    replies = await agent.handle(transport.inbound(USER, "?"))
    assert replies == [agent.handoff.reply]
    assert sink.events[0].reason == "clarify_limit"


async def test_multiple_sinks_and_failing_sink_does_not_block(store):
    class Boom:
        async def notify(self, escalation: Escalation) -> None:
            raise RuntimeError("sink down")

    log = LogSink()
    sinks: list[EscalationSink] = [Boom(), log]
    hm = HandoffManager(store, sinks)
    state = store.get(USER)
    esc = await hm.escalate(state, "test")
    assert log.events == [esc]
    assert store.get(USER).handoff


async def test_webhook_sink_posts_json(store):
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200)

    sink = WebhookSink("https://hooks.example/esc", headers={"X-Key": "k"}, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    hm = HandoffManager(store, [sink])
    await hm.escalate(store.get(USER), "user_requested", "help")
    assert len(posted) == 1
    assert posted[0].headers["X-Key"] == "k"
    body = posted[0].read()
    assert b'"wa_id":"' + USER.encode() + b'"' in body
    assert b'"reason":"user_requested"' in body


async def test_webhook_sink_raises_on_http_error(store):
    sink = WebhookSink("https://hooks.example/esc", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(httpx.HTTPStatusError):
        await sink.notify(Escalation(wa_id=USER, reason="x"))


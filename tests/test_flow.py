from __future__ import annotations

import pytest

from wa_kit import (
    ConversationState,
    Flow,
    FlowRunner,
    LLMDecision,
    LLMStage,
    MockModel,
    Stage,
    min_length,
    one_of,
)
from wa_kit.flow import DecisionParseError, parse_decision

from .conftest import USER, build_flow, decision


def state() -> ConversationState:
    return ConversationState(wa_id=USER)


async def test_first_message_enters_start_and_passes_through_greeting(flow):
    r = await FlowRunner(flow).step(state(), "hi")
    assert r.replies == ["Hi there.", "Which suburb?"]


async def test_slot_fill_advances_and_stores_value(flow):
    s = state()
    runner = FlowRunner(flow)
    await runner.step(s, "hi")
    r = await runner.step(s, "Glenwood")
    assert s.slots["suburb"] == "Glenwood"
    assert s.stage == "problem"
    assert r.replies == ["What's wrong?"]


async def test_validator_reject_reprompts_without_advancing(flow):
    s = state()
    runner = FlowRunner(flow)
    await runner.step(s, "hi")
    r = await runner.step(s, "ab")
    assert s.stage == "suburb" and "suburb" not in s.slots
    assert r.replies == ["Sorry, I didn't understand that. Which suburb?"]
    assert s.clarify_count == 1


async def test_clarify_twice_then_escalate(flow):
    s = state()
    runner = FlowRunner(flow, max_clarify=2)
    await runner.step(s, "hi")
    r1 = await runner.step(s, "?")
    r2 = await runner.step(s, "?")
    r3 = await runner.step(s, "?")
    assert not r1.escalate and not r2.escalate
    assert r3.escalate and r3.reason == "clarify_limit" and r3.replies == []


async def test_custom_error_prompt_used():
    f = Flow("f", [Stage("a", prompt="A?", slot="a", validator=one_of("x"), error_prompt="Only x.", next=None)])
    s = state()
    runner = FlowRunner(f)
    await runner.step(s, "go")
    r = await runner.step(s, "y")
    assert r.replies == ["Only x."]


async def test_dynamic_next_routes_on_state():
    f = Flow(
        "f",
        [
            Stage("a", prompt="A?", slot="a", next=lambda st: "urgent" if st.slots["a"] == "burst" else "calm"),
            Stage("urgent", prompt="Urgent path", terminal=True),
            Stage("calm", prompt="Calm path", terminal=True),
        ],
    )
    s = state()
    runner = FlowRunner(f)
    await runner.step(s, "go")
    r = await runner.step(s, "burst")
    assert r.replies == ["Urgent path"] and s.stage == "urgent"


async def test_prompt_formats_with_slots_and_callables():
    f = Flow(
        "f",
        [
            Stage("a", prompt="Name?", slot="name", next="b"),
            Stage("b", prompt="Hi {name}, you said {missing}", slot="x", next="c"),
            Stage("c", prompt=lambda st: f"callable {st.slots['name']}", terminal=True),
        ],
    )
    s = state()
    runner = FlowRunner(f)
    await runner.step(s, "go")
    r = await runner.step(s, "Thandi")
    assert r.replies == ["Hi Thandi, you said {missing}"]
    r = await runner.step(s, "x")
    assert r.replies == ["callable Thandi"]


async def test_terminal_stage_restarts_on_next_message(model):
    f = build_flow(model)
    s = state()
    runner = FlowRunner(f)
    for text in ("hi", "Glenwood", "leak", "1"):
        await runner.step(s, text)
    assert s.stage == "done"
    r = await runner.step(s, "hello again")
    assert r.replies == ["Hi there.", "Which suburb?"]
    assert s.slots == {} and s.stage == "suburb"


def test_flow_validation_errors():
    with pytest.raises(ValueError):
        Flow("f", [])
    with pytest.raises(ValueError):
        Flow("f", [Stage("a"), Stage("a")])
    with pytest.raises(ValueError):
        Flow("f", [Stage("a", next="nope")])
    with pytest.raises(TypeError):
        LLMStage("x")
    # validator helpers
    assert min_length(3)("abc") and not min_length(3)(" ab ")
    assert one_of("1", "Two")("two") and not one_of("1")("3")


# --- LLMStage ---------------------------------------------------------------------


def llm_flow(model: MockModel) -> Flow:
    return Flow(
        "f",
        [
            LLMStage("classify", prompt="Tell me.", slot="problem", next="after", model=model),
            Stage("after", prompt="Next step.", terminal=True),
        ],
    )


async def test_llm_stage_merges_slots_and_advances():
    model = MockModel([decision("Got it.", intent="repair", slots={"urgency": "high"})])
    s = state()
    runner = FlowRunner(llm_flow(model))
    await runner.step(s, "hi")
    r = await runner.step(s, "burst geyser")
    assert r.replies == ["Got it.", "Next step."]
    assert s.slots == {"urgency": "high", "problem": "burst geyser", "intent": "repair"}
    assert r.decision is not None and r.decision.intent == "repair"
    assert model.call_count == 1


async def test_llm_stage_retries_once_on_schema_failure_then_succeeds():
    model = MockModel(['{"intent": "x", "reply": 5}', decision("Fixed.")])
    s = state()
    runner = FlowRunner(llm_flow(model))
    await runner.step(s, "hi")
    r = await runner.step(s, "leak")
    assert r.replies[0] == "Fixed."
    assert model.call_count == 2
    retry_messages = model.calls[1]["messages"]
    assert retry_messages[-2]["role"] == "assistant"
    assert "rejected" in retry_messages[-1]["content"]


async def test_llm_stage_falls_back_after_two_failures():
    model = MockModel(["not json at all", "```json\n{\"intent\": 1}\n```"])
    s = state()
    runner = FlowRunner(llm_flow(model))
    await runner.step(s, "hi")
    r = await runner.step(s, "leak")
    assert r.used_fallback
    assert r.replies == ["Sorry, I didn't quite catch that. Could you say it another way?"]
    assert s.stage == "classify" and "problem" not in s.slots
    assert model.call_count == 2


async def test_llm_provider_exception_gives_fallback_then_escalates(caplog):
    """A dead provider must not leave the user in silence or crash the worker."""
    calls = 0

    def boom(messages, system):
        nonlocal calls
        calls += 1
        raise ConnectionError("provider down")

    s = state()
    runner = FlowRunner(llm_flow(MockModel(boom)), max_clarify=2)
    await runner.step(s, "hi")
    with caplog.at_level("ERROR", logger="wa_kit.flow"):
        r1 = await runner.step(s, "leak")
        r2 = await runner.step(s, "leak")
        r3 = await runner.step(s, "leak")
    assert r1.used_fallback and r1.replies == [runner.flow.stage("classify").fallback_reply]
    assert r2.used_fallback and not r2.escalate
    assert r3.escalate and r3.reason == "llm_unparseable"
    assert s.stage == "classify" and "problem" not in s.slots
    assert calls == 3 and "model call failed" in caplog.text


async def test_llm_stage_needs_human_escalates_without_advancing():
    model = MockModel([decision("Let me get someone.", needs_human=True)])
    s = state()
    runner = FlowRunner(llm_flow(model))
    await runner.step(s, "hi")
    r = await runner.step(s, "gas leak")
    assert r.escalate and r.reason == "llm_needs_human"
    assert r.replies == ["Let me get someone."]
    assert s.stage == "classify"


async def test_llm_stage_receives_redacted_history_and_schema():
    model = MockModel([decision()])
    s = state()
    s.add_history("user", "my number is 0821234567")
    runner = FlowRunner(llm_flow(model), redact=lambda t: t.replace("0821234567", "[PHONE]"))
    await runner.step(s, "hi")
    await runner.step(s, "call 0821234567")
    call = model.calls[0]
    contents = [m["content"] for m in call["messages"]]
    assert all("0821234567" not in c for c in contents)
    assert any("[PHONE]" in c for c in contents)
    assert "slots_extracted" in call["system"]


def test_parse_decision_handles_fences_and_prose():
    d = parse_decision('Sure!\n```json\n{"intent":"a","reply":"b","slots_extracted":{},"needs_human":true}\n```')
    assert d == LLMDecision(intent="a", reply="b", needs_human=True)
    with pytest.raises(DecisionParseError):
        parse_decision("no braces here")
    with pytest.raises(DecisionParseError):
        parse_decision('{"intent":"a","reply":"b","extra":1}')  # extra="forbid"
    with pytest.raises(DecisionParseError):
        parse_decision('{"intent":"a",}')

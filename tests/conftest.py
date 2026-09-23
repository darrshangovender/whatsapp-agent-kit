from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from wa_kit import (
    Agent,
    Compliance,
    ConversationStore,
    Flow,
    HandoffManager,
    LLMStage,
    LogSink,
    MockModel,
    MockTransport,
    Stage,
    min_length,
    one_of,
)

USER = "27821234567"
VALID_SA_ID = "8001015009087"  # passes date, citizenship and Luhn checks
INVALID_LUHN_SA_ID = "8001015009088"  # same digits, wrong check digit


def decision(
    reply: str = "Noted.",
    intent: str = "repair",
    slots: dict[str, str] | None = None,
    needs_human: bool = False,
) -> str:
    return json.dumps(
        {
            "intent": intent,
            "reply": reply,
            "slots_extracted": slots or {},
            "needs_human": needs_human,
        }
    )


class Clock:
    """Controllable clock; ``now`` is tz-aware UTC."""

    def __init__(self, at: datetime | None = None) -> None:
        self.now = at or datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> ConversationStore:
    return ConversationStore(":memory:", clock=clock)


@pytest.fixture
def transport() -> MockTransport:
    return MockTransport()


@pytest.fixture
def model() -> MockModel:
    return MockModel([decision(slots={"problem": "leak", "urgency": "normal"})], loop=True)


def build_flow(model: MockModel) -> Flow:
    return Flow(
        "booking",
        [
            Stage("greet", prompt="Hi there.", next="suburb"),
            Stage(
                "suburb",
                prompt="Which suburb?",
                slot="suburb",
                validator=min_length(3),
                next="problem",
            ),
            LLMStage("problem", prompt="What's wrong?", slot="problem", next="slot", model=model),
            Stage("slot", prompt="1 or 2?", slot="slot_choice", validator=one_of("1", "2"), next="done"),
            Stage("done", prompt="Booked.", terminal=True),
        ],
    )


@pytest.fixture
def flow(model: MockModel) -> Flow:
    return build_flow(model)


@pytest.fixture
def sink() -> LogSink:
    return LogSink()


@pytest.fixture
def agent(
    store: ConversationStore,
    transport: MockTransport,
    flow: Flow,
    sink: LogSink,
    clock: Clock,
) -> Agent:
    return Agent(
        store=store,
        transport=transport,
        flow=flow,
        compliance=Compliance(store, clock=clock),
        handoff=HandoffManager(store, [sink]),
        clock=clock,
    )

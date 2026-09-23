"""A plumber's booking assistant, end to end, entirely offline.

greet → suburb → problem (LLMStage classifies urgency) → offer slot → confirm.
An emergency keyword, "speak to someone", or an LLM ``needs_human`` hands off.

Run:  python examples/booking_bot.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wa_kit import (  # noqa: E402
    Agent,
    Compliance,
    ConversationState,
    ConversationStore,
    Flow,
    HandoffManager,
    LLMStage,
    LogSink,
    MockModel,
    MockTransport,
    Stage,
    is_yes,
    min_length,
    one_of,
    yes_no,
)
from wa_kit.models import Message  # noqa: E402

BUSINESS = "Dlamini Plumbing"
SLOTS = {"1": "tomorrow 08:00-10:00", "2": "tomorrow 13:00-15:00"}

HANDOFF_TRIGGERS = frozenset(
    {"human", "agent", "speak to someone", "talk to someone", "emergency", "flooding"}
)


def classify(messages: list[Message], system: str | None) -> str:
    """Stand-in for a real model: keyword urgency classifier that returns the strict JSON."""
    text = messages[-1]["content"].lower()
    urgent = any(w in text for w in ("burst", "flood", "no water", "gushing", "sewage"))
    needs_human = "gas" in text  # not our trade; a person must decide
    problem = messages[-1]["content"].strip()
    return json.dumps(
        {
            "intent": "gas_query" if needs_human else ("emergency_repair" if urgent else "repair"),
            "reply": (
                "That involves gas — I'll get a qualified person to talk to you."
                if needs_human
                else "Got it — that sounds urgent, we'll prioritise it."
                if urgent
                else "Thanks, noted."
            ),
            "slots_extracted": {"problem": problem, "urgency": "high" if urgent else "normal"},
            "needs_human": needs_human,
        }
    )


def build_flow(model: MockModel) -> Flow:
    def offer_prompt(state: ConversationState) -> str:
        lead = "Because it's urgent, the earliest we can do is:" if state.slots.get(
            "urgency"
        ) == "high" else "We can come:"
        options = "\n".join(f"{k}) {v}" for k, v in SLOTS.items())
        return f"{lead}\n{options}\nReply 1 or 2."

    def after_confirm(state: ConversationState) -> str:
        return "done" if is_yes(str(state.slots.get("confirmed", ""))) else "offer_slot"

    def confirm_prompt(state: ConversationState) -> str:
        chosen = SLOTS.get(str(state.slots.get("slot_choice")), "?")
        return (
            f"To confirm: {state.slots.get('problem')} in {state.slots.get('suburb')}, "
            f"{chosen}. Reply YES to book or NO to pick another time."
        )

    return Flow(
        "booking",
        [
            Stage("greet", prompt=f"Hi! I'm the booking assistant for {BUSINESS}.", next="suburb"),
            Stage(
                "suburb",
                prompt="Which suburb are you in?",
                slot="suburb",
                validator=min_length(3),
                error_prompt="Please tell me the suburb, e.g. 'Umhlanga'.",
                next="problem",
            ),
            LLMStage(
                "problem",
                prompt="What's the problem? (a sentence is fine)",
                slot="problem",
                next="offer_slot",
                model=model,
                system_prompt=(
                    f"You classify plumbing requests for {BUSINESS} in Durban. "
                    "Extract 'problem' and 'urgency' (high|normal). Set needs_human for "
                    "anything outside plumbing or that needs a quote."
                ),
            ),
            Stage(
                "offer_slot",
                prompt=offer_prompt,
                slot="slot_choice",
                validator=one_of("1", "2"),
                next="confirm",
            ),
            Stage(
                "confirm",
                prompt=confirm_prompt,
                slot="confirmed",
                validator=yes_no,
                next=after_confirm,
            ),
            Stage(
                "done",
                prompt="Booked! You'll get a reminder an hour before. Reply anytime to start over.",
                terminal=True,
            ),
        ],
    )


def build_agent(
    *, transport: MockTransport | None = None, model: MockModel | None = None
) -> tuple[Agent, MockTransport, LogSink]:
    store = ConversationStore(":memory:")
    transport = transport or MockTransport()
    sink = LogSink()
    agent = Agent(
        store=store,
        transport=transport,
        flow=build_flow(model or MockModel(classify)),
        compliance=Compliance(store),
        handoff=HandoffManager(store, [sink], triggers=HANDOFF_TRIGGERS),
        mark_read=False,
    )
    return agent, transport, sink


async def converse(agent: Agent, transport: MockTransport, wa_id: str, lines: list[str]) -> None:
    for line in lines:
        print(f"  {wa_id} > {line}")
        replies = await agent.handle(transport.inbound(wa_id, line))
        for r in replies:
            print("  bot   < " + r.replace("\n", "\n          "))
        if not replies:
            print("  bot   < (silent — handed off or opted out)")


async def main(printer: Callable[[str], None] = print) -> None:
    agent, transport, sink = build_agent()

    printer("=== 1. Happy path: leaking tap in Glenwood ===")
    await converse(
        agent, transport, "27821110001", ["hi", "Glenwood", "kitchen tap dripping", "2", "yes"]
    )

    printer("\n=== 2. Urgent: burst geyser, user wants a person mid-flow ===")
    await converse(
        agent,
        transport,
        "27821110002",
        ["hello", "Umhlanga", "burst geyser, water everywhere", "speak to someone", "hello?"],
    )

    printer("\n=== 3. LLM says needs_human (gas), then agent resumes the bot ===")
    await converse(agent, transport, "27821110003", ["hi", "Durban North", "gas geyser pilot out"])
    agent.handoff.resume("27821110003", reset_flow=True)
    printer("  (human resolved it; resume(wa_id))")
    await converse(agent, transport, "27821110003", ["thanks, also a slow drain", "Durban North"])

    printer("\n=== 4. Clarify twice then escalate; PII is redacted from history ===")
    await converse(agent, transport, "27821110004", ["hi", "?", "??", "?"])
    agent.handoff.resume("27821110004")
    printer("  (resume(wa_id) — still at the suburb stage)")
    await converse(
        agent, transport, "27821110004", ["Pinetown, my ID is 8001015009087 call 082 555 1234"]
    )
    last_user = [h for h in agent.store.get("27821110004").history if h.role == "user"][-1]
    printer("  stored user turn: " + last_user.content)

    printer("\n=== 5. Opt-out in isiZulu ===")
    await converse(agent, transport, "27821110005", ["hi", "yeka", "hello?"])

    printer(f"\nEscalations notified: {len(sink.events)}")
    for e in sink.events:
        printer(f"  - {e.wa_id} reason={e.reason} stage={e.stage} slots={e.slots}")
    printer(f"Outbound messages sent: {len(transport.sent)}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # em dashes on a cp1252 Windows console
    asyncio.run(main())

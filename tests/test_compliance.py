from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from wa_kit import (
    Compliance,
    ConsentRequired,
    ConversationState,
    PIIDetector,
    QuietHours,
    is_valid_sa_id,
)
from wa_kit.compliance import luhn_ok, normalise_keyword

from .conftest import INVALID_LUHN_SA_ID, USER, VALID_SA_ID, Clock

JHB = ZoneInfo("Africa/Johannesburg")


# --- opt-out ----------------------------------------------------------------------


@pytest.mark.parametrize("word", ["STOP", "UNSUBSCRIBE", "HOU OP", "YEKA", "OPT OUT"])
def test_each_opt_out_keyword(store, word):
    assert Compliance(store).is_opt_out(word)


@pytest.mark.parametrize("text", ["stop", "Stop!", "  hou   op ", "Yeka.", "unsubscribe"])
def test_opt_out_is_case_and_punctuation_insensitive(store, text):
    assert Compliance(store).is_opt_out(text)


@pytest.mark.parametrize("text", ["please stop calling", "stopwatch", "yekani", "hi"])
def test_non_opt_out(store, text):
    assert not Compliance(store).is_opt_out(text)


@pytest.mark.parametrize(
    "text",
    [
        "stop…",  # iOS turns "..." into an ellipsis
        "Stop \U0001F6D1",  # emoji
        "STOP ❤️",  # emoji with a variation selector
        "opt-out",  # hyphen must read as a word break, not be deleted into OPTOUT
        "«hou op»",  # guillemets
        "yeka’",  # curly apostrophe
        "unsubscribe​",  # zero-width space
    ],
)
def test_opt_out_tolerates_unicode_punctuation_and_emoji(store, text):
    assert Compliance(store).is_opt_out(text)


async def test_opt_out_is_honoured_before_any_model_call(agent, transport, model):
    await agent.handle(transport.inbound(USER, "hi"))
    await agent.handle(transport.inbound(USER, "Glenwood"))  # now at the LLM stage
    calls_before = model.call_count
    replies = await agent.handle(transport.inbound(USER, "Stop…"))
    assert replies == [agent.compliance.opt_out_reply]
    assert model.call_count == calls_before


def test_normalise_keyword():
    assert normalise_keyword(" Hou  op! ") == "HOU OP"


async def test_agent_opt_out_then_silence_then_start(agent, transport):
    await agent.handle(transport.inbound(USER, "hi"))
    replies = await agent.handle(transport.inbound(USER, "yeka"))
    assert replies == [agent.compliance.opt_out_reply]
    st = agent.store.get(USER)
    assert st.opted_out and st.stage is None
    assert await agent.handle(transport.inbound(USER, "hello?")) == []
    replies = await agent.handle(transport.inbound(USER, "START"))
    assert replies == [agent.compliance.opt_in_reply]
    assert not agent.store.get(USER).opted_out


# --- quiet hours -----------------------------------------------------------------


def test_quiet_hours_across_midnight_boundary():
    q = QuietHours()  # 20:00 → 08:00 SAST
    # evening, before midnight
    assert q.is_quiet(datetime(2026, 3, 10, 20, 0, tzinfo=JHB))
    assert q.is_quiet(datetime(2026, 3, 10, 21, 30, tzinfo=JHB))
    assert q.is_quiet(datetime(2026, 3, 10, 23, 59, tzinfo=JHB))
    # after midnight, same window
    assert q.is_quiet(datetime(2026, 3, 11, 0, 0, tzinfo=JHB))
    assert q.is_quiet(datetime(2026, 3, 11, 1, 15, tzinfo=JHB))
    assert q.is_quiet(datetime(2026, 3, 11, 7, 59, tzinfo=JHB))
    # daytime and boundaries
    assert not q.is_quiet(datetime(2026, 3, 11, 8, 0, tzinfo=JHB))
    assert not q.is_quiet(datetime(2026, 3, 11, 12, 0, tzinfo=JHB))
    assert not q.is_quiet(datetime(2026, 3, 11, 19, 59, tzinfo=JHB))


def test_quiet_hours_converts_utc_to_tenant_timezone():
    q = QuietHours()  # SAST = UTC+2
    assert q.is_quiet(datetime(2026, 3, 10, 18, 30, tzinfo=UTC))  # 20:30 SAST
    assert not q.is_quiet(datetime(2026, 3, 10, 17, 30, tzinfo=UTC))  # 19:30 SAST
    assert q.is_quiet(datetime(2026, 3, 10, 5, 0, tzinfo=UTC))  # 07:00 SAST


def test_quiet_hours_same_day_window_and_disabled():
    q = QuietHours(start=time(13, 0), end=time(14, 0), tz="Europe/London")
    assert q.is_quiet(datetime(2026, 3, 10, 13, 30, tzinfo=ZoneInfo("Europe/London")))
    assert not q.is_quiet(datetime(2026, 3, 10, 14, 0, tzinfo=ZoneInfo("Europe/London")))
    assert not QuietHours(start=time(8), end=time(8)).is_quiet()


def test_next_allowed_crosses_midnight():
    q = QuietHours()
    nxt = q.next_allowed(datetime(2026, 3, 10, 22, 0, tzinfo=JHB))
    assert nxt == datetime(2026, 3, 11, 8, 0, tzinfo=JHB)
    nxt = q.next_allowed(datetime(2026, 3, 11, 3, 0, tzinfo=JHB))
    assert nxt == datetime(2026, 3, 11, 8, 0, tzinfo=JHB)
    noon = datetime(2026, 3, 11, 12, 0, tzinfo=JHB)
    assert q.next_allowed(noon) == noon


# --- PII / POPIA -----------------------------------------------------------------


def test_luhn_and_sa_id_validation():
    assert luhn_ok(VALID_SA_ID)
    assert is_valid_sa_id(VALID_SA_ID)
    assert not is_valid_sa_id(INVALID_LUHN_SA_ID)
    assert not is_valid_sa_id("8013015009087")  # month 13
    assert not is_valid_sa_id("8001015009287")  # citizenship digit 2
    assert not is_valid_sa_id("800101500908")  # 12 digits


def test_sa_id_date_must_exist_on_a_calendar():
    # All three pass Luhn and the citizenship check; only real dates are IDs.
    assert not is_valid_sa_id("8002305009084")  # 30 February
    assert is_valid_sa_id("0002295009084")  # 29 Feb 2000 (leap year)
    assert is_valid_sa_id("9602295009082")  # 29 Feb 1996 (leap year)
    assert PIIDetector().redact("ref 8002305009084") == "ref 8002305009084"
    assert PIIDetector().redact("id 0002295009084") == "id [SA_ID]"


def test_sa_id_redacted_only_when_luhn_valid():
    p = PIIDetector()
    assert p.redact(f"my id is {VALID_SA_ID} ok") == "my id is [SA_ID] ok"
    # a 13-digit run that fails Luhn is not an ID, and is too long for the account pattern
    assert p.redact(f"ref {INVALID_LUHN_SA_ID} ok") == f"ref {INVALID_LUHN_SA_ID} ok"


@pytest.mark.parametrize(
    "text",
    ["0821234567", "082 123 4567", "082-123-4567", "+27821234567", "+27 82 123 4567", "27821234567"],
)
def test_sa_phone_formats_redacted(text):
    assert PIIDetector().redact(f"call {text} now") == "call [PHONE] now"


def test_email_and_bank_account_redacted():
    p = PIIDetector()
    assert p.redact("mail thandi.m@example.co.za") == "mail [EMAIL]"
    assert p.redact("acc 62012345678 at FNB") == "acc [BANK_ACCOUNT] at FNB"
    assert p.redact("order 1234") == "order 1234"  # short numbers untouched
    kinds = [m.kind for m in p.detect(f"{VALID_SA_ID} / a@b.io / 0821234567")]
    assert kinds == ["sa_id", "email", "phone"]
    assert p.contains_pii("0821234567") and not p.contains_pii("hello")


async def test_history_never_contains_raw_pii(agent, transport):
    await agent.handle(transport.inbound(USER, "hi"))
    await agent.handle(transport.inbound(USER, f"Glenwood, ID {VALID_SA_ID}, cell 082 123 4567"))
    st = agent.store.get(USER)
    joined = " ".join(h.content for h in st.history)
    assert VALID_SA_ID not in joined and "082 123 4567" not in joined
    assert "[SA_ID]" in joined and "[PHONE]" in joined
    # the raw text still reached the flow as the slot value (the business needs it)
    assert st.slots["suburb"].startswith("Glenwood")


async def test_llm_never_receives_raw_pii(agent, transport, model):
    await agent.handle(transport.inbound(USER, "hi"))
    await agent.handle(transport.inbound(USER, "Glenwood"))
    await agent.handle(transport.inbound(USER, f"geyser burst, my id {VALID_SA_ID} email x@y.co.za"))
    for call in model.calls:
        for m in call["messages"]:
            assert VALID_SA_ID not in m["content"]
            assert "x@y.co.za" not in m["content"]


async def test_llm_never_receives_raw_pii_from_earlier_slots(agent, transport, model):
    """Slots keep the raw value for the business; the 'Known so far' prompt must not leak it."""
    await agent.handle(transport.inbound(USER, "hi"))
    await agent.handle(transport.inbound(USER, f"Glenwood, my ID is {VALID_SA_ID}, cell 0821234567"))
    assert VALID_SA_ID in agent.store.get(USER).slots["suburb"]  # raw slot, by design
    await agent.handle(transport.inbound(USER, "leaking tap"))  # LLM stage: slots go in the prompt
    assert model.call_count == 1
    contents = [m["content"] for m in model.calls[0]["messages"]]
    assert any("Known so far" in c for c in contents)
    for c in contents:
        assert VALID_SA_ID not in c and "0821234567" not in c
    assert any("[SA_ID]" in c and "[PHONE]" in c for c in contents)


async def test_inbound_touch_is_persisted_even_if_the_send_fails(agent, transport, clock: Clock):
    """A Meta outage must not lose the 24h window or the user's turn."""
    await agent.handle(transport.inbound(USER, "hi", at=clock.now))
    later = clock.now + timedelta(hours=1)
    transport.fail_next.append(RuntimeError("meta down"))
    with pytest.raises(RuntimeError):
        await agent.handle(transport.inbound(USER, "Glenwood", at=later))
    st = agent.store.get(USER)
    assert st.last_inbound_at == later
    assert st.history[-1].role == "user" and st.history[-1].content == "Glenwood"
    assert st.can_send_freeform(later + timedelta(hours=23))


# --- retention + consent -----------------------------------------------------------


def test_purge_older_than(store, clock: Clock):
    old = ConversationState(wa_id="old", last_seen=clock.now - timedelta(days=100))
    fresh = ConversationState(wa_id="fresh", last_seen=clock.now - timedelta(days=5))
    store.save(old)
    store.save(fresh)
    c = Compliance(store, clock=clock)
    assert c.purge_older_than(90) == 1
    assert not store.exists("old") and store.exists("fresh")
    assert c.purge_older_than(1) == 1
    assert not store.exists("fresh")


def test_consent_required_then_recorded(store):
    c = Compliance(store)
    with pytest.raises(ConsentRequired):
        c.require_consent(store.get(USER))
    c.record_consent(USER)
    c.require_consent(store.get(USER))  # no raise
    assert store.get(USER).consent_recorded


def test_redact_messages_helper(store):
    c = Compliance(store)
    out = c.redact_messages([{"role": "user", "content": "0821234567"}])
    assert out == [{"role": "user", "content": "[PHONE]"}]

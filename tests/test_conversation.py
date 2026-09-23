from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from wa_kit import ConversationState, ConversationStore

from .conftest import USER, Clock

T0 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
JHB = ZoneInfo("Africa/Johannesburg")


def test_new_conversation_defaults(store):
    s = store.get(USER)
    assert s.stage is None and s.slots == {} and s.history == []
    assert not s.handoff and not s.opted_out and not s.consent_recorded
    assert s.last_inbound_at is None
    assert not store.exists(USER)


def test_state_round_trips_through_sqlite(store):
    s = ConversationState(wa_id=USER, stage="suburb", slots={"suburb": "Glenwood", "n": 2})
    s.add_history("user", "hi", at=T0)
    s.handoff = True
    store.save(s)
    back = store.get(USER)
    assert back.stage == "suburb"
    assert back.slots == {"suburb": "Glenwood", "n": 2}
    assert back.history[0].content == "hi" and back.history[0].at == T0
    assert back.handoff is True
    assert store.exists(USER)


def test_state_persists_across_reopen(tmp_path):
    path = str(tmp_path / "wa.db")
    a = ConversationStore(path)
    a.touch_inbound(USER, T0)
    a.close()
    b = ConversationStore(path)
    assert b.get(USER).last_inbound_at == T0


def test_no_inbound_means_no_freeform(store):
    assert store.can_send_freeform(USER) is False


def test_window_open_at_23h59(store, clock: Clock):
    store.touch_inbound(USER, T0)
    clock.now = T0 + timedelta(hours=23, minutes=59)
    assert store.can_send_freeform(USER) is True


def test_window_closed_at_24h01_and_exactly_24h(store, clock: Clock):
    store.touch_inbound(USER, T0)
    clock.now = T0 + timedelta(hours=24, minutes=1)
    assert store.can_send_freeform(USER) is False
    clock.now = T0 + timedelta(hours=24)
    assert store.can_send_freeform(USER) is False
    # a new inbound message reopens it
    clock.now = T0 + timedelta(days=3)
    store.touch_inbound(USER, clock.now)
    assert store.can_send_freeform(USER) is True


def test_window_is_timezone_safe(store, clock: Clock):
    # last inbound recorded in SAST, checked against a UTC clock: 12:00+02:00 == 10:00Z
    store.touch_inbound(USER, datetime(2026, 3, 10, 12, 0, tzinfo=JHB))
    clock.now = datetime(2026, 3, 11, 9, 59, tzinfo=UTC)
    assert store.can_send_freeform(USER) is True
    clock.now = datetime(2026, 3, 11, 10, 0, tzinfo=UTC)
    assert store.can_send_freeform(USER) is False
    # a naive datetime is taken as UTC rather than raising TypeError
    assert ConversationState(wa_id="n", last_inbound_at=T0.replace(tzinfo=None)).can_send_freeform(
        T0 + timedelta(hours=1)
    )


def test_purge_compares_instants_not_offset_strings(store):
    # 12:00+02:00 is 10:00Z, i.e. older than an 11:00Z cutoff, even though the ISO string sorts later
    store.touch_inbound("sast", datetime(2026, 1, 1, 12, 0, tzinfo=JHB))
    store.touch_inbound("utc", datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    assert store.purge_older_than(0, now=datetime(2026, 1, 1, 11, 0, tzinfo=UTC)) == 1
    assert not store.exists("sast") and store.exists("utc")


async def test_agent_window_uses_latest_inbound_even_when_delivered_out_of_order(agent, transport):
    newer = T0 + timedelta(hours=2)
    await agent.handle(transport.inbound(USER, "hi", at=newer))
    await agent.handle(transport.inbound(USER, "Glenwood", at=T0))  # late delivery of an older message
    assert agent.store.get(USER).last_inbound_at == newer


def test_delete_and_all(store):
    store.save(ConversationState(wa_id="1"))
    store.save(ConversationState(wa_id="2"))
    assert [s.wa_id for s in store.all()] == ["1", "2"]
    assert store.delete("1") is True
    assert store.delete("1") is False
    assert [s.wa_id for s in store.all()] == ["2"]

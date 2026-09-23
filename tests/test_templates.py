from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wa_kit import (
    Compliance,
    OptedOutError,
    OutsideWindowError,
    QuietHoursError,
    Template,
    TemplateNotApproved,
    TemplateNotFound,
    TemplateParamError,
    TemplateRegistry,
    TemplateSender,
)

from .conftest import USER, Clock

T0 = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)  # 12:00 SAST — outside quiet hours


@pytest.fixture
def registry() -> TemplateRegistry:
    r = TemplateRegistry()
    r.register(Template(name="booking_confirm", body="Hi {name}, see you {when}.", params=["name", "when"]))
    r.register(Template(name="promo", body="Hi {name}, 20% off this week!", params=["name"], category="marketing"))
    r.register(Template(name="pending", body="Not yet.", approved=False))
    return r


@pytest.fixture
def sender(registry, transport, store, clock: Clock) -> TemplateSender:
    clock.now = T0
    return TemplateSender(registry, transport, store, compliance=Compliance(store, clock=clock), clock=clock)


def test_render_validates_params(registry):
    tpl = registry.get("booking_confirm")
    preview, ordered = tpl.render(name="Thandi", when="tomorrow 08:00")
    assert preview == "Hi Thandi, see you tomorrow 08:00."
    assert ordered == ["Thandi", "tomorrow 08:00"]
    with pytest.raises(TemplateParamError):
        tpl.render(name="Thandi")  # missing
    with pytest.raises(TemplateParamError):
        tpl.render(name="Thandi", when="x", extra="y")  # extra


def test_register_rejects_placeholder_mismatch():
    r = TemplateRegistry()
    with pytest.raises(TemplateParamError):
        r.register(Template(name="bad", body="Hi {name}", params=[]))
    with pytest.raises(TemplateNotFound):
        r.get("bad")
    assert "bad" not in r


async def test_send_template_inside_window(sender, transport, store):
    store.touch_inbound(USER, T0)
    res = await sender.send_template(USER, "booking_confirm", name="Thandi", when="08:00")
    assert res.message_id
    sent = transport.sent[-1]
    assert sent.kind == "template" and sent.template == "booking_confirm"
    assert sent.params == ["Thandi", "08:00"] and sent.language == "en"


async def test_freeform_allowed_at_23h59_refused_at_24h01(sender, transport, store, clock: Clock):
    store.touch_inbound(USER, T0)
    clock.now = T0 + timedelta(hours=23, minutes=59)
    await sender.send_text(USER, "free text")
    assert transport.last_text(USER) == "free text"
    clock.now = T0 + timedelta(hours=24, minutes=1)
    with pytest.raises(OutsideWindowError):
        await sender.send_text(USER, "free text 2")
    assert len(transport.sent) == 1


async def test_send_falls_back_to_template_outside_window(sender, transport, store, clock: Clock):
    store.touch_inbound(USER, T0)
    clock.now = T0 + timedelta(days=2)
    await sender.send(USER, "free text", template="booking_confirm", name="T", when="now")
    assert transport.sent[-1].kind == "template"


async def test_send_refuses_outside_window_without_approved_template(sender, transport, store, clock: Clock):
    store.touch_inbound(USER, T0)
    clock.now = T0 + timedelta(days=2)
    with pytest.raises(OutsideWindowError):
        await sender.send(USER, "free text")
    with pytest.raises(OutsideWindowError):
        await sender.send(USER, "free text", template="pending")
    with pytest.raises(OutsideWindowError):
        await sender.send(USER, "free text", template="does_not_exist")
    assert transport.sent == []


async def test_unapproved_template_refused_even_inside_window(sender, store):
    store.touch_inbound(USER, T0)
    with pytest.raises(TemplateNotApproved):
        await sender.send_template(USER, "pending")


async def test_marketing_requires_consent(sender, transport):
    with pytest.raises(PermissionError):
        await sender.send_template(USER, "promo", name="T")
    sender.compliance.record_consent(USER)
    await sender.send_template(USER, "promo", name="T")
    assert transport.sent[-1].template == "promo"


async def test_marketing_blocked_in_quiet_hours_unless_forced(sender, transport, clock: Clock):
    sender.compliance.record_consent(USER)
    clock.now = datetime(2026, 3, 10, 20, 0, tzinfo=UTC)  # 22:00 SAST
    with pytest.raises(QuietHoursError):
        await sender.send_template(USER, "promo", name="T")
    await sender.send_template(USER, "promo", force_quiet_hours=True, name="T")
    assert transport.sent[-1].template == "promo"


async def test_opted_out_user_gets_no_templates(sender, transport, store):
    st = store.get(USER)
    st.opted_out = True
    store.save(st)
    with pytest.raises(PermissionError):
        await sender.send_template(USER, "booking_confirm", name="T", when="x")
    assert transport.sent == []


async def test_opted_out_user_gets_no_freeform_text_even_inside_window(sender, transport, store):
    """Opt-out means silence on every proactive path, not just templates."""
    store.touch_inbound(USER, T0)
    st = store.get(USER)
    st.opted_out = True
    store.save(st)
    assert sender.window_open(USER)
    with pytest.raises(OptedOutError):
        await sender.send_text(USER, "just checking in")
    with pytest.raises(OptedOutError):
        await sender.send(USER, "just checking in", template="booking_confirm", name="T", when="x")
    assert transport.sent == []

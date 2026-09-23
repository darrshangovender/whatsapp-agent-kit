from __future__ import annotations

import json

import httpx
import pytest

from wa_kit import MetaCloudTransport, MockTransport, Transport, TransportError
from wa_kit.transport._signing import sign_body


def make_transport(responses: list[httpx.Response], **kw) -> tuple[MetaCloudTransport, list[httpx.Request], list[float]]:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(it)

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://graph.facebook.com")
    t = MetaCloudTransport("PNID", "TOKEN", "SECRET", client=client, sleep=fake_sleep, **kw)
    return t, requests, sleeps


def ok(mid: str = "wamid.out") -> httpx.Response:
    return httpx.Response(200, json={"messages": [{"id": mid}]})


def test_both_transports_satisfy_protocol_and_client_is_lazy():
    assert isinstance(MockTransport(), Transport)
    t = MetaCloudTransport("p", "t", "s")
    assert isinstance(t, Transport)
    assert not t.client_created  # constructing never opens a socket
    _ = t.client
    assert t.client_created


def test_verify_webhook_hmac():
    t = MetaCloudTransport("p", "t", "top-secret")
    body = b'{"object":"whatsapp_business_account"}'
    assert t.verify_webhook(sign_body("top-secret", body), body)
    assert not t.verify_webhook(sign_body("other-secret", body), body)
    assert not t.verify_webhook(None, body)
    assert not t.verify_webhook("md5=abc", body)
    # non-ASCII input must be a plain False, never a TypeError from compare_digest
    assert not t.verify_webhook("sha256=" + "é" * 64, body)
    assert not t.verify_webhook("sha256=\U0001F44D", body)
    # verification is over the raw bytes: a re-serialised body with different spacing fails
    assert not t.verify_webhook(sign_body("top-secret", body), b'{"object": "whatsapp_business_account"}')


async def test_send_text_payload_and_result():
    t, reqs, _ = make_transport([ok("wamid.123")])
    res = await t.send_text("27820000000", "hello")
    assert res.message_id == "wamid.123"
    req = reqs[0]
    assert req.url.path == "/v20.0/PNID/messages"
    assert req.headers["Authorization"] == "Bearer TOKEN"
    assert json.loads(req.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "27820000000",
        "type": "text",
        "text": {"preview_url": False, "body": "hello"},
    }


async def test_send_template_payload():
    t, reqs, _ = make_transport([ok()])
    await t.send_template("27820000000", "booking_confirm", "en", ["Thandi", "08:00"])
    body = json.loads(reqs[0].content)
    assert body["type"] == "template"
    assert body["template"]["name"] == "booking_confirm"
    assert body["template"]["language"] == {"code": "en"}
    assert body["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "Thandi"},
        {"type": "text", "text": "08:00"},
    ]


async def test_send_media_and_mark_read():
    t, reqs, _ = make_transport([ok(), ok()])
    await t.send_media("27820000000", "image", link="https://x/y.jpg", caption="quote")
    await t.mark_read("wamid.in")
    assert json.loads(reqs[0].content)["image"] == {"link": "https://x/y.jpg", "caption": "quote"}
    assert json.loads(reqs[1].content) == {"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.in"}
    with pytest.raises(ValueError):
        await t.send_media("x", "image")


async def test_retry_on_429_then_success_honours_retry_after():
    t, reqs, sleeps = make_transport(
        [httpx.Response(429, json={"error": "rate"}, headers={"Retry-After": "2"}), ok("wamid.r")]
    )
    res = await t.send_text("x", "y")
    assert res.message_id == "wamid.r"
    assert len(reqs) == 2
    assert sleeps == [2.0]


async def test_retry_on_5xx_with_exponential_backoff():
    t, reqs, sleeps = make_transport(
        [httpx.Response(503, text="down"), httpx.Response(500, text="down"), ok()],
        backoff_base=0.5,
    )
    await t.send_text("x", "y")
    assert len(reqs) == 3
    assert sleeps == [0.5, 1.0]


async def test_gives_up_after_max_retries():
    t, reqs, _ = make_transport([httpx.Response(502)] * 3, max_retries=2)
    with pytest.raises(TransportError) as exc:
        await t.send_text("x", "y")
    assert exc.value.status == 502
    assert len(reqs) == 3


async def test_non_retryable_4xx_fails_immediately():
    t, reqs, sleeps = make_transport([httpx.Response(400, json={"error": {"message": "bad"}})])
    with pytest.raises(TransportError) as exc:
        await t.send_text("x", "y")
    assert exc.value.status == 400
    assert len(reqs) == 1 and sleeps == []


async def test_mock_transport_records_everything():
    m = MockTransport()
    await m.send_text("1", "a")
    await m.send_template("1", "tpl", "en", ["x"])
    await m.send_media("1", "document", media_id="D", caption="c")
    await m.mark_read("wamid.z")
    assert [s.kind for s in m.sent] == ["text", "template", "media"]
    assert m.texts_to("1") == ["a"] and m.last_text("1") == "a"
    assert m.read_receipts == ["wamid.z"]
    m.fail_next.append(RuntimeError("down"))
    with pytest.raises(RuntimeError):
        await m.send_text("1", "b")

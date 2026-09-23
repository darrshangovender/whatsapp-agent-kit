from __future__ import annotations

import asyncio
import json
import time

import pytest
from starlette.testclient import TestClient

from wa_kit import DedupStore, InboundMessage, MockTransport, create_app, run_worker

VERIFY = "my-verify-token"


def make_app(transport: MockTransport, handler, **kw):
    return create_app(transport=transport, verify_token=VERIFY, handler=handler, start_worker=False, **kw)


async def noop(_: InboundMessage) -> None:
    return None


def test_verify_challenge_ok(transport):
    client = TestClient(make_app(transport, noop))
    r = client.get(
        "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "42"}
    )
    assert r.status_code == 200
    assert r.text == "42"


def test_verify_wrong_token_rejected(transport):
    client = TestClient(make_app(transport, noop))
    r = client.get(
        "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"}
    )
    assert r.status_code == 403


def test_bad_missing_or_tampered_signature_rejected(transport):
    app = make_app(transport, noop)
    client = TestClient(app)
    body, headers = transport.meta_body("27820000000", "hi")
    # wrong signature
    bad = dict(headers, **{"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert client.post("/webhook", content=body, headers=bad).status_code == 403
    # missing signature
    r = client.post("/webhook", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 403
    # valid signature, tampered body
    assert client.post("/webhook", content=body + b" ", headers=headers).status_code == 403
    assert app.state.queue.empty()


def test_valid_post_enqueues_message(transport):
    app = make_app(transport, noop)
    client = TestClient(app)
    body, headers = transport.meta_body("27820000000", "hello", message_id="wamid.1")
    r = client.post("/webhook", content=body, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"queued": 1, "duplicates": 0}
    msg = app.state.queue.get_nowait()
    assert msg.wa_id == "27820000000"
    assert msg.text == "hello"
    assert msg.message_id == "wamid.1"


def test_replay_is_deduplicated(transport):
    app = make_app(transport, noop)
    client = TestClient(app)
    body, headers = transport.meta_body("27820000000", "hello", message_id="wamid.same")
    first = client.post("/webhook", content=body, headers=headers).json()
    second = client.post("/webhook", content=body, headers=headers).json()
    assert first == {"queued": 1, "duplicates": 0}
    assert second == {"queued": 0, "duplicates": 1}
    assert app.state.queue.qsize() == 1


def test_dedup_store_mark_is_first_sight_only():
    d = DedupStore()
    assert d.mark("a") is True
    assert d.mark("a") is False
    assert d.seen("a") and not d.seen("b")


def test_ack_within_200ms_even_with_slow_handler(transport):
    async def slow(_: InboundMessage) -> None:
        await asyncio.sleep(1.0)

    app = create_app(transport=transport, verify_token=VERIFY, handler=slow)  # worker on
    with TestClient(app) as client:
        body, headers = transport.meta_body("27820000000", "hello", message_id="wamid.slow")
        t0 = time.perf_counter()
        r = client.post("/webhook", content=body, headers=headers)
        elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 0.2, f"ack took {elapsed:.3f}s"


def test_status_updates_are_ignored(transport):
    app = make_app(transport, noop)
    client = TestClient(app)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {"statuses": [{"id": "wamid.x", "status": "delivered"}]},
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode()
    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": transport.sign(body)})
    assert r.json() == {"queued": 0, "duplicates": 0}


def test_invalid_json_is_400(transport):
    client = TestClient(make_app(transport, noop))
    body = b"{not json"
    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": transport.sign(body)})
    assert r.status_code == 400


def test_non_ascii_signature_header_is_403_not_500(transport):
    """hmac.compare_digest raises TypeError on non-ASCII str; a header must never 500."""
    client = TestClient(make_app(transport, noop), raise_server_exceptions=False)
    body, _ = transport.meta_body("27820000000", "hi")
    latin1 = {b"X-Hub-Signature-256": b"sha256=" + b"\xe9" * 64}
    r = client.post("/webhook", content=body, headers=latin1)
    assert r.status_code == 403


def test_verify_token_with_non_ascii_query_is_403_not_500(transport):
    client = TestClient(make_app(transport, noop), raise_server_exceptions=False)
    r = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "é" * 8, "hub.challenge": "42"},
    )
    assert r.status_code == 403


def test_signed_non_object_json_is_400_not_500(transport):
    client = TestClient(make_app(transport, noop), raise_server_exceptions=False)
    for body in (b"[]", b'"str"', b"42", b"null"):
        r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": transport.sign(body)})
        assert r.status_code == 400, body


def test_odd_payload_shapes_are_ignored_not_fatal(transport):
    client = TestClient(make_app(transport, noop), raise_server_exceptions=False)
    payloads = [
        {"object": "whatsapp_business_account", "entry": "nope"},
        {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": []}]}]},
        {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {"messages": ["a string", 7, None]}}]}],
        },
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "contacts": [{"wa_id": "1", "profile": None}],
                                "messages": [{"from": "1", "id": "wamid.ok", "type": "text", "text": "not-a-dict"}],
                            },
                        }
                    ]
                }
            ],
        },
    ]
    for p in payloads:
        body = json.dumps(p).encode()
        r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": transport.sign(body)})
        assert r.status_code == 200, p
        assert r.json() == {"queued": 0, "duplicates": 0}, p


def test_reaction_and_sticker_are_acked_and_queued_without_crashing(transport):
    app = make_app(transport, noop)
    client = TestClient(app)
    for mid, extra in (
        ("wamid.react", {"type": "reaction", "reaction": {"message_id": "wamid.x", "emoji": "\U0001F44D"}}),
        ("wamid.stick", {"type": "sticker", "sticker": {"id": "S", "mime_type": "image/webp"}}),
        ("wamid.unsup", {"type": "unsupported", "errors": [{"code": 131051, "title": "Unsupported"}]}),
    ):
        p = transport.meta_payload("27820000000", "", message_id=mid)
        p["entry"][0]["changes"][0]["value"]["messages"][0] = {
            "from": "27820000000", "id": mid, "timestamp": "1700000000", **extra
        }
        body = json.dumps(p).encode()
        r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": transport.sign(body)})
        assert r.status_code == 200 and r.json()["queued"] == 1, mid
    assert app.state.queue.qsize() == 3


def test_unparseable_timestamp_falls_back_to_now_with_warning(transport, caplog):
    from wa_kit import parse_payload

    p = transport.meta_payload("27820000000", "hi", message_id="wamid.ts")
    p["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = "not-a-number"
    with caplog.at_level("WARNING", logger="wa_kit.webhook"):
        [m] = parse_payload(p)
    assert m.timestamp.tzinfo is not None
    assert "unparseable message timestamp" in caplog.text


async def test_worker_survives_handler_exception(transport):
    seen: list[str] = []

    async def handler(m: InboundMessage) -> None:
        seen.append(m.message_id)
        if m.message_id == "boom":
            raise RuntimeError("handler exploded")

    queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
    queue.put_nowait(transport.inbound("1", "a", message_id="boom"))
    queue.put_nowait(transport.inbound("1", "b", message_id="ok"))
    task = asyncio.create_task(run_worker(queue, handler))
    await asyncio.wait_for(queue.join(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == ["boom", "ok"]

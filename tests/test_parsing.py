from __future__ import annotations

from datetime import UTC, datetime

from wa_kit import MessageType, parse_payload

from .conftest import USER


def payload(msg: dict, name: str = "Thandi") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"profile": {"name": name}, "wa_id": "27820000001"}],
                            "messages": [{"from": "27820000001", "id": "wamid.t", "timestamp": "1700000000"} | msg],
                        },
                    }
                ]
            }
        ],
    }


def test_text_message():
    [m] = parse_payload(payload({"type": "text", "text": {"body": "Hi there"}}))
    assert m.type is MessageType.TEXT
    assert m.text == "Hi there"
    assert m.content == "Hi there"
    assert m.wa_id == "27820000001"
    assert m.profile_name == "Thandi"
    assert m.timestamp == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_image_message_with_caption():
    [m] = parse_payload(
        payload({"type": "image", "image": {"id": "MEDIA1", "mime_type": "image/jpeg", "caption": "the leak", "sha256": "abc"}})
    )
    assert m.type is MessageType.IMAGE
    assert m.media is not None and m.media.media_id == "MEDIA1"
    assert m.media.mime_type == "image/jpeg"
    assert m.content == "the leak"


def test_document_message():
    [m] = parse_payload(
        payload({"type": "document", "document": {"id": "DOC1", "mime_type": "application/pdf", "filename": "invoice.pdf"}})
    )
    assert m.type is MessageType.DOCUMENT
    assert m.media is not None and m.media.filename == "invoice.pdf"
    assert m.content == ""


def test_location_message():
    [m] = parse_payload(
        payload({"type": "location", "location": {"latitude": -29.85, "longitude": 31.02, "name": "Home", "address": "Durban"}})
    )
    assert m.type is MessageType.LOCATION
    assert m.location is not None
    assert (m.location.latitude, m.location.longitude) == (-29.85, 31.02)
    assert m.content == "-29.85,31.02"


def test_interactive_button_reply():
    [m] = parse_payload(
        payload({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "slot_1", "title": "Tomorrow 08:00"}}})
    )
    assert m.type is MessageType.INTERACTIVE
    assert m.interactive_reply_id == "slot_1"
    assert m.content == "Tomorrow 08:00"


def test_interactive_list_reply():
    [m] = parse_payload(
        payload({"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "opt_b", "title": "Option B", "description": "..."}}})
    )
    assert m.interactive_reply_id == "opt_b"
    assert m.text == "Option B"


def test_reaction_is_parsed_as_reaction_not_text():
    [m] = parse_payload(payload({"type": "reaction", "reaction": {"message_id": "wamid.x", "emoji": "\U0001F44D"}}))
    assert m.type is MessageType.REACTION
    assert m.text is None and m.content == ""
    assert m.raw["reaction"]["emoji"] == "\U0001F44D"


async def test_agent_ignores_reactions_entirely(agent, transport):
    """A thumbs-up on the bot's prompt is not an answer: no clarify, no escalation, no state."""
    await agent.handle(transport.inbound(USER, "hi"))
    before = agent.store.get(USER)
    sent_before = len(transport.sent)
    for i in range(3):
        [m] = parse_payload(
            payload({"type": "reaction", "reaction": {"message_id": "wamid.x", "emoji": "\U0001F44D"}})
        )
        m = m.model_copy(update={"wa_id": USER, "message_id": f"wamid.r{i}"})
        assert await agent.handle(m) == []
    after = agent.store.get(USER)
    assert len(transport.sent) == sent_before
    assert after.stage == before.stage == "suburb"
    assert after.clarify_count == 0 and not after.handoff
    assert len(after.history) == len(before.history)
    assert transport.read_receipts == ["wamid.in.1"]  # reactions are not marked read either


def test_unsupported_type_is_kept_but_flagged():
    [m] = parse_payload(payload({"type": "sticker", "sticker": {"id": "S"}}))
    assert m.type is MessageType.UNSUPPORTED
    assert m.content == ""
    assert m.raw["type"] == "sticker"


def test_non_whatsapp_object_yields_nothing():
    assert parse_payload({"object": "page", "entry": []}) == []


def test_malformed_message_is_skipped_not_fatal():
    body = payload({"type": "text", "text": {"body": "ok"}})
    body["entry"][0]["changes"][0]["value"]["messages"].append({"type": "text"})  # no id/from
    assert len(parse_payload(body)) == 1

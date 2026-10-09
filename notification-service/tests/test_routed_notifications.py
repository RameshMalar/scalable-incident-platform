"""Consumer-to-delivery migration tests; all HTTP and Blob calls are mocked."""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.consumer import NotificationConsumer
from app.evidence import EvidenceUnavailable
from common_event_model import IncidentEvent
from app.notification_models import NotificationEnvelope
from app.notifier import MockNotifier
from app.processor import InvalidIncident, NotificationProcessor
from tests.test_telegram_notifier import (
    FakeEvidenceStore,
    FakeResponse,
    form_fields,
    make_event,
    make_evidence,
    make_notifier,
)


SELECTED_CHAT = "-100987654"


def envelope_payload(**changes):
    payload = {
        "schema_version": 1,
        "subscription_id": "SUB-1",
        "channel": "telegram",
        "telegram_chat_id": SELECTED_CHAT,
        "incident": make_event().model_dump(mode="json"),
    }
    payload.update(changes)
    return payload


async def test_flat_message_still_uses_global_chat():
    notifier, session, _ = make_notifier([FakeResponse(200, '{"ok":true}')])
    await NotificationProcessor(notifier).process(make_event().model_dump_json())
    assert session.requests[0][1]["json"]["chat_id"] == "-100123"


async def test_routed_text_uses_selected_chat_even_with_different_global():
    notifier, session, _ = make_notifier([FakeResponse(200, '{"ok":true}')])
    await NotificationProcessor(notifier).process(json.dumps(envelope_payload()))
    assert session.requests[0][1]["json"]["chat_id"] == SELECTED_CHAT
    assert notifier.chat_id == "-100123"


async def test_routed_photo_preserves_evidence_and_caption_destination():
    store = FakeEvidenceStore(evidence=make_evidence())
    notifier, session, _ = make_notifier(
        [FakeResponse(200, '{"ok":true}')], evidence_store=store
    )
    await NotificationProcessor(notifier).process(json.dumps(envelope_payload()))
    assert store.requested == [make_event().evidence_uri]
    url, request = session.requests[0]
    assert url.endswith("/sendPhoto")
    fields = form_fields(request["data"])
    assert fields["chat_id"] == SELECTED_CHAT
    assert fields["photo"] == b"jpeg-bytes"
    assert "Event ID: EVT-001" in fields["caption"]
    assert notifier.chat_id == "-100123"


async def test_photo_failure_falls_back_to_same_selected_chat():
    notifier, session, _ = make_notifier(
        [FakeResponse(400, '{"ok":false}'), FakeResponse(200, '{"ok":true}')],
        evidence_store=FakeEvidenceStore(evidence=make_evidence()),
        min_send_interval_seconds=0,
    )
    await NotificationProcessor(notifier).process(json.dumps(envelope_payload()))
    assert session.requests[0][0].endswith("/sendPhoto")
    assert session.requests[1][0].endswith("/sendMessage")
    assert form_fields(session.requests[0][1]["data"])["chat_id"] == SELECTED_CHAT
    assert session.requests[1][1]["json"]["chat_id"] == SELECTED_CHAT


async def test_unavailable_blob_keeps_selected_text_destination():
    store = FakeEvidenceStore(error=EvidenceUnavailable("missing blob"))
    notifier, session, _ = make_notifier(
        [FakeResponse(200, '{"ok":true}')], evidence_store=store
    )
    await NotificationProcessor(notifier).process(json.dumps(envelope_payload()))
    assert store.requested == [make_event().evidence_uri]
    assert session.requests[0][0].endswith("/sendMessage")
    assert session.requests[0][1]["json"]["chat_id"] == SELECTED_CHAT


@pytest.mark.parametrize("photo", [False, True])
async def test_rate_limit_retries_retain_selected_destination(photo):
    responses = [
        FakeResponse(429, '{"parameters":{"retry_after":4}}'),
        FakeResponse(200, '{"ok":true}'),
    ]
    store = FakeEvidenceStore(evidence=make_evidence()) if photo else None
    notifier, session, clock = make_notifier(
        responses, evidence_store=store, min_send_interval_seconds=0,
        max_rate_limit_retries=1,
    )
    await NotificationProcessor(notifier).process(json.dumps(envelope_payload()))
    assert clock.sleeps == [4.0]
    assert len(session.requests) == 2
    for _, request in session.requests:
        fields = form_fields(request["data"]) if photo else request["json"]
        assert fields["chat_id"] == SELECTED_CHAT
    if photo:
        assert session.requests[0][1]["data"] is not session.requests[1][1]["data"]


async def test_routed_call_does_not_change_next_legacy_destination():
    notifier, session, _ = make_notifier(
        [FakeResponse(200, '{"ok":true}'), FakeResponse(200, '{"ok":true}')],
        min_send_interval_seconds=0,
    )
    processor = NotificationProcessor(notifier)
    await processor.process(json.dumps(envelope_payload()))
    await processor.process(make_event().model_dump_json())
    assert [request["json"]["chat_id"] for _, request in session.requests] == [
        SELECTED_CHAT, "-100123"
    ]


async def test_complete_incident_survives_and_mock_records_destination():
    notifier = MockNotifier()
    payload = envelope_payload()
    await NotificationProcessor(notifier).process(json.dumps(payload).encode("utf-8"))
    assert notifier.sent_events[0].model_dump() == IncidentEvent.model_validate(
        payload["incident"]
    ).model_dump()
    assert notifier.sent_destinations == [SELECTED_CHAT]
    assert notifier.sent_events[0].evidence_uri == payload["incident"]["evidence_uri"]


@pytest.mark.parametrize("changes", [
    {"schema_version": 2}, {"schema_version": True}, {"schema_version": "1"},
    {"subscription_id": " "}, {"channel": "email"},
    {"telegram_chat_id": ""}, {"telegram_chat_id": None},
    {"telegram_chat_id": 123}, {"telegram_chat_id": "@chat"},
    {"incident": {}}, {"incident": None}, {"unexpected": "field"},
])
async def test_invalid_envelopes_never_send_to_global_chat(changes):
    notifier, session, _ = make_notifier([])
    with pytest.raises(InvalidIncident):
        await NotificationProcessor(notifier).process(
            json.dumps(envelope_payload(**changes))
        )
    assert session.requests == []
    assert notifier.chat_id == "-100123"


@pytest.mark.parametrize("field", [
    "schema_version", "subscription_id", "channel", "telegram_chat_id", "incident"
])
async def test_incomplete_envelope_is_not_legacy(field):
    payload = envelope_payload()
    del payload[field]
    notifier = MockNotifier()
    with pytest.raises(InvalidIncident):
        await NotificationProcessor(notifier).process(json.dumps(payload))
    assert notifier.sent_events == []


@pytest.mark.parametrize("marker", [
    "schema_version", "subscription_id", "channel", "telegram_chat_id", "incident"
])
async def test_hybrid_flat_message_with_route_marker_cannot_downgrade(marker):
    payload = make_event().model_dump(mode="json")
    payload[marker] = envelope_payload()[marker]
    notifier = MockNotifier()
    with pytest.raises(InvalidIncident):
        await NotificationProcessor(notifier).process(json.dumps(payload))
    assert notifier.sent_events == []


@pytest.mark.parametrize("purpose,event_id,expected", [
    ("load_test", "EVT-1", False), (None, "EVT-LOAD-001", False),
    ("e2e_test", "EVT-1", True), (None, "EVT-OLD-1", True),
    ("incident", "EVT-LOAD-001", True),
])
async def test_routed_suppression_matches_legacy_semantics(purpose, event_id, expected):
    incident = make_event(event_id).model_dump(mode="json")
    if purpose is None:
        del incident["event_purpose"]
    else:
        incident["event_purpose"] = purpose
    notifier = MockNotifier()
    await NotificationProcessor(notifier).process(
        json.dumps(envelope_payload(incident=incident))
    )
    assert len(notifier.sent_events) == int(expected)


async def test_legacy_compatibility_can_be_disabled_without_disabling_routes():
    notifier = MockNotifier()
    processor = NotificationProcessor(notifier, accept_legacy=False)
    with pytest.raises(InvalidIncident):
        await processor.process(make_event().model_dump_json())
    await processor.process(json.dumps(envelope_payload()))
    assert notifier.sent_destinations == [SELECTED_CHAT]


async def test_invalid_routed_destination_rejected_before_evidence_io():
    store = FakeEvidenceStore(evidence=make_evidence())
    notifier, session, _ = make_notifier([], evidence_store=store)
    with pytest.raises(ValidationError):
        await notifier.notify(make_event(), telegram_chat_id="")
    assert session.requests == []
    assert store.requested == []


async def test_unsupported_version_is_dead_lettered_by_existing_consumer():
    notifier = MockNotifier()
    consumer = NotificationConsumer(
        processor=NotificationProcessor(notifier),
        fully_qualified_namespace="example.servicebus.windows.net",
    )
    consumer._receiver = AsyncMock()
    message = AsyncMock()
    message.body = [json.dumps(envelope_payload(schema_version=2)).encode()]
    await consumer.settle(message)
    consumer._receiver.dead_letter_message.assert_awaited_once()
    consumer._receiver.complete_message.assert_not_awaited()
    consumer._receiver.abandon_message.assert_not_awaited()
    assert notifier.sent_events == []


def test_envelope_model_preserves_existing_incident_contract():
    envelope = NotificationEnvelope.model_validate(envelope_payload())
    assert NotificationEnvelope.model_validate_json(
        envelope.model_dump_json()
    ).incident == make_event()

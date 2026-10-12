import pytest
from pydantic import ValidationError

from common_event_model import IncidentEvent
from app.notification_models import NotificationEnvelope, SubscriptionRecord


def incident_payload(**changes):
    payload = {
        "event_id": "EVT-001", "tenant_id": "TENANT-A", "site_id": "SITE-1",
        "source_id": "CAM-1", "source_name": "Gate camera",
        "source_type": "video_analytics", "analytics_type": "intrusion_detection",
        "event_type": "intrusion_detected", "severity": "high",
        "timestamp": "2026-10-07T10:00:00+08:00", "event_purpose": "incident",
        "confidence": 0.94, "description": "Restricted area intrusion",
        "evidence_uri": "https://example.blob.core.windows.net/evidence/a.jpg?x=1",
    }
    payload.update(changes)
    return payload


def subscription_document(**changes):
    document = {
        "id": "SUB-1", "tenant_id": "TENANT-A",
        "event_type": "intrusion_detected", "channel": "telegram",
        "telegram_chat_id": "-100123456", "enabled": True,
        "created_at": "2026-10-07T02:00:00Z",
        "updated_at": "2026-10-07T02:00:00Z",
        "_etag": "etag", "_rid": "rid", "_ts": 123,
    }
    document.update(changes)
    return document


def make_envelope():
    return NotificationEnvelope(
        subscription_id="SUB-1", channel="telegram",
        telegram_chat_id="-100123456",
        incident=IncidentEvent(**incident_payload()),
    )


def test_phase_one_id_and_metadata_compatibility():
    record = SubscriptionRecord.model_validate(subscription_document())
    assert record.id == "SUB-1"
    assert "created_at" not in record.model_dump()
    assert "_etag" not in record.model_dump()


def test_complete_incident_round_trip():
    envelope = make_envelope()
    restored = NotificationEnvelope.model_validate_json(envelope.model_dump_json())
    assert restored.incident.model_dump() == envelope.incident.model_dump()
    assert restored.incident.evidence_uri == incident_payload()["evidence_uri"]
    assert restored.telegram_chat_id == "-100123456"
    assert set(restored.model_dump()) == {
        "schema_version", "subscription_id", "channel", "telegram_chat_id", "incident"
    }
    assert restored.schema_version == 1


@pytest.mark.parametrize("changes", [
    {"schema_version": 2}, {"schema_version": True}, {"schema_version": "1"},
    {"channel": "email"}, {"telegram_chat_id": "@chat"},
    {"telegram_chat_id": 123}, {"subscription_id": " "},
    {"bot_token": "secret"}, {"incident": {}},
])
def test_invalid_envelope(changes):
    payload = {**make_envelope().model_dump(), **changes}
    with pytest.raises(ValidationError):
        NotificationEnvelope.model_validate(payload)


@pytest.mark.parametrize("field", [
    "id", "tenant_id", "event_type", "channel", "telegram_chat_id", "enabled"
])
def test_required_subscription_fields(field):
    document = subscription_document()
    del document[field]
    with pytest.raises(ValidationError):
        SubscriptionRecord.model_validate(document)


@pytest.mark.parametrize("changes", [
    {"enabled": "true"}, {"enabled": 1}, {"enabled": None},
    {"channel": "email"}, {"telegram_chat_id": ""},
    {"telegram_chat_id": "https://example.com"}, {"id": ""},
    {"tenant_id": " "}, {"event_type": ""},
])
def test_invalid_subscription_fields(changes):
    with pytest.raises(ValidationError):
        SubscriptionRecord.model_validate(subscription_document(**changes))

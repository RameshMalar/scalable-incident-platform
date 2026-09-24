import json

import pytest

from app.notifier import MockNotifier
from app.processor import InvalidIncident, NotificationProcessor


@pytest.mark.asyncio
async def test_valid_incident_is_sent_to_notifier():
    notifier = MockNotifier()
    processor = NotificationProcessor(notifier)

    payload = {
        "event_id": "EVT-001",
        "tenant_id": "TENANT-01",
        "site_id": "SITE-01",
        "source_id": "CAM-001",
        "source_name": "Restricted Area Camera",
        "source_type": "video_analytics",
        "analytics_type": "intrusion_detection",
        "event_type": "intrusion_detected",
        "severity": "high",
        "timestamp": "2026-09-12T20:00:00+08:00",
        "confidence": 0.95,
        "description": "Person detected in restricted area",
        "evidence_uri": "/evidence/EVT-001.jpg",
    }

    await processor.process(json.dumps(payload))

    assert len(notifier.sent_events) == 1
    assert notifier.sent_events[0].event_id == "EVT-001"
    assert notifier.sent_events[0].evidence_uri == "/evidence/EVT-001.jpg"


@pytest.mark.asyncio
async def test_load_test_incident_is_completed_without_notification():
    notifier = MockNotifier()
    processor = NotificationProcessor(notifier)

    payload = {
        "event_id": "EVT-001",
        "tenant_id": "TENANT-01",
        "site_id": "SITE-01",
        "source_id": "CAM-001",
        "source_name": "Load Test Camera",
        "source_type": "video_analytics",
        "analytics_type": "intrusion_detection",
        "event_type": "intrusion_detected",
        "severity": "high",
        "timestamp": "2026-09-12T20:00:00+08:00",
        "event_purpose": "load_test",
        "confidence": 0.95,
        "description": "Synthetic intrusion from the load simulator",
    }

    await processor.process(json.dumps(payload))

    assert notifier.sent_events == []


@pytest.mark.asyncio
async def test_legacy_load_test_incident_is_completed_without_notification():
    notifier = MockNotifier()
    processor = NotificationProcessor(notifier)

    payload = {
        "event_id": "EVT-LOAD-000000001",
        "tenant_id": "TENANT-01",
        "site_id": "SITE-01",
        "source_id": "CAM-001",
        "source_name": "Load Test Camera",
        "source_type": "video_analytics",
        "analytics_type": "intrusion_detection",
        "event_type": "intrusion_detected",
        "severity": "high",
        "timestamp": "2026-09-12T20:00:00+08:00",
        "confidence": 0.95,
        "description": "Legacy load event without event_purpose",
    }

    await processor.process(json.dumps(payload))

    assert notifier.sent_events == []


@pytest.mark.asyncio
async def test_invalid_json_is_rejected():
    notifier = MockNotifier()
    processor = NotificationProcessor(notifier)

    with pytest.raises(InvalidIncident):
        await processor.process("not-valid-json")
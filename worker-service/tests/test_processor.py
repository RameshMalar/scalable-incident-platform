import json
from datetime import datetime

import pytest

from app.models import IncidentEvent
from app.processor import (
    InMemoryDeduplicator,
    IncidentHandler,
    IncidentProcessor,
    InvalidIncident,
    Outcome,
)

pytestmark = pytest.mark.asyncio


def sample_event(**overrides) -> IncidentEvent:
    fields = {
        "event_id": "EVT-TEST0001",
        "tenant_id": "TENANT-01",
        "site_id": "SITE-01",
        "source_id": "CAM-001",
        "source_name": "Test Camera",
        "source_type": "video_analytics",
        "analytics_type": "intrusion_detection",
        "event_type": "intrusion_detected",
        "severity": "high",
        "timestamp": datetime(2026, 8, 21, 10, 0, 0),
        "confidence": 0.95,
        "description": "Person detected inside restricted area",
        "evidence_uri": "/evidence/test.jpg",
    }
    fields.update(overrides)
    return IncidentEvent(**fields)


def sample_body(**overrides) -> bytes:
    """The exact bytes the ingestion publisher puts on the queue."""
    return sample_event(**overrides).model_dump_json().encode("utf-8")


class RecordingHandler(IncidentHandler):
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.handled: list[IncidentEvent] = []

    async def handle(self, event: IncidentEvent) -> None:
        if self.fail:
            raise RuntimeError("downstream is unavailable")
        self.handled.append(event)


async def test_a_valid_incident_is_handled_once():
    handler = RecordingHandler()
    processor = IncidentProcessor(handler=handler)

    outcome = await processor.process(sample_body(), "EVT-TEST0001")

    assert outcome is Outcome.PROCESSED
    assert [event.event_id for event in handler.handled] == ["EVT-TEST0001"]


async def test_the_worker_round_trips_every_field_the_producer_sent():
    # The queue carries the Common Event Model as JSON, so a field the producer
    # sets and the worker cannot read is a silent data loss, not an error.
    handler = RecordingHandler()
    processor = IncidentProcessor(handler=handler)
    original = sample_event()

    await processor.process(original.model_dump_json().encode("utf-8"))

    assert handler.handled[0] == original


async def test_a_redelivered_incident_is_not_handled_twice():
    # Service Bus delivers at least once: the same incident arriving again
    # must not reach the handler a second time.
    handler = RecordingHandler()
    processor = IncidentProcessor(handler=handler)

    first = await processor.process(sample_body(), "EVT-TEST0001")
    second = await processor.process(sample_body(), "EVT-TEST0001")

    assert first is Outcome.PROCESSED
    assert second is Outcome.DUPLICATE
    assert len(handler.handled) == 1


async def test_distinct_incidents_are_all_handled():
    handler = RecordingHandler()
    processor = IncidentProcessor(handler=handler)

    await processor.process(sample_body(event_id="EVT-1"))
    await processor.process(sample_body(event_id="EVT-2"))

    assert [event.event_id for event in handler.handled] == ["EVT-1", "EVT-2"]


async def test_a_failed_handler_leaves_the_incident_unprocessed():
    # The dedupe entry is written only after the handler succeeds. If it were
    # written first, a crash midway through the handler would make the
    # redelivery look like a duplicate and the incident would vanish.
    handler = RecordingHandler(fail=True)
    processor = IncidentProcessor(handler=handler)

    with pytest.raises(RuntimeError):
        await processor.process(sample_body())

    handler.fail = False
    outcome = await processor.process(sample_body())

    assert outcome is Outcome.PROCESSED
    assert len(handler.handled) == 1


async def test_a_handler_failure_is_not_mistaken_for_a_bad_message():
    # An unavailable downstream is transient and deserves a redelivery.
    # Raising InvalidIncident here would dead-letter a perfectly good incident.
    processor = IncidentProcessor(handler=RecordingHandler(fail=True))

    with pytest.raises(RuntimeError):
        await processor.process(sample_body())


async def test_a_body_that_is_not_json_is_unprocessable():
    processor = IncidentProcessor(handler=RecordingHandler())

    with pytest.raises(InvalidIncident):
        await processor.process(b"not json at all", "EVT-BAD")


async def test_a_body_that_is_not_utf8_is_unprocessable():
    processor = IncidentProcessor(handler=RecordingHandler())

    with pytest.raises(InvalidIncident):
        await processor.process(b"\xff\xfe\x00binary", "EVT-BAD")


async def test_json_that_is_not_an_incident_is_unprocessable():
    processor = IncidentProcessor(handler=RecordingHandler())

    with pytest.raises(InvalidIncident):
        await processor.process(b'["a", "list", "not", "an", "object"]')


async def test_an_incident_missing_a_required_field_is_unprocessable():
    handler = RecordingHandler()
    processor = IncidentProcessor(handler=handler)
    payload = json.loads(sample_body())
    del payload["tenant_id"]

    with pytest.raises(InvalidIncident):
        await processor.process(json.dumps(payload).encode("utf-8"))

    assert handler.handled == []


async def test_a_confidence_outside_the_model_range_is_unprocessable():
    # The Common Event Model constrains confidence to 0.0-1.0. The worker
    # revalidates rather than trusting the producer: ingestion is not the only
    # thing that can put a message on this queue.
    processor = IncidentProcessor(handler=RecordingHandler())
    payload = json.loads(sample_body())
    payload["confidence"] = 4.2

    with pytest.raises(InvalidIncident):
        await processor.process(json.dumps(payload).encode("utf-8"))


async def test_a_string_body_is_accepted_as_well_as_bytes():
    handler = RecordingHandler()
    processor = IncidentProcessor(handler=handler)

    outcome = await processor.process(sample_body().decode("utf-8"))

    assert outcome is Outcome.PROCESSED


async def test_the_deduplicator_forgets_the_oldest_entries_first():
    # Bounded on purpose: an unbounded set in a long-lived replica is a memory
    # leak. Forgetting the oldest costs a reprocess, never a lost incident.
    dedupe = InMemoryDeduplicator(capacity=2)

    await dedupe.remember("EVT-1")
    await dedupe.remember("EVT-2")
    await dedupe.remember("EVT-3")

    assert not await dedupe.seen("EVT-1")
    assert await dedupe.seen("EVT-2")
    assert await dedupe.seen("EVT-3")

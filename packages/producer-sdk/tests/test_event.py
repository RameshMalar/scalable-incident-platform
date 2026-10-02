"""Construction and validation, before anything leaves the producer."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from common_event_model import IncidentEvent

from incident_producer import InvalidEvent, build, to_json, validate

REQUIRED = dict(
    tenant_id="TENANT-01",
    site_id="SITE-01",
    source_id="CAM-07",
    source_name="Loading bay camera CAM-07",
    analytics_type="intrusion_detection",
    event_type="intrusion_detected",
    severity="high",
)


def test_build_supplies_an_id_and_a_timestamp():
    event = build(**REQUIRED)

    assert event.event_id.startswith("EVT-")
    assert event.timestamp.tzinfo is not None
    assert event.source_type == "video_analytics"
    assert event.event_purpose == "incident"


def test_each_built_incident_gets_its_own_id():
    # The store keys on event_id, so two incidents sharing one would silently
    # become one.
    assert build(**REQUIRED).event_id != build(**REQUIRED).event_id


def test_a_given_id_is_kept():
    # What makes a retry safe: the caller re-sends the same incident, not a new one.
    assert build(**REQUIRED, event_id="EVT-FIXED").event_id == "EVT-FIXED"


def test_the_default_timestamp_carries_a_zone():
    # The model accepts a naive timestamp, so without this a producer outside UTC
    # would record a time nobody can place.
    assert build(**REQUIRED).timestamp.utcoffset() is not None


def test_optional_fields_pass_through():
    event = build(
        **REQUIRED,
        confidence=0.91,
        description="Perimeter breach at the loading bay",
        evidence_uri="https://account.blob.core.windows.net/evidence/x.jpg",
        event_purpose="e2e_test",
    )

    assert event.confidence == 0.91
    assert event.event_purpose == "e2e_test"


def test_a_bad_value_is_refused_at_construction():
    with pytest.raises(InvalidEvent):
        build(**REQUIRED, confidence=1.5)


def test_an_unknown_purpose_is_refused():
    with pytest.raises(InvalidEvent):
        build(**REQUIRED, event_purpose="whatever")


def test_an_unknown_analytics_type_is_accepted():
    # A new analytics pipeline must not need a release of this SDK either.
    assert build(**{**REQUIRED, "analytics_type": "no_such_analytics_yet"})


def test_validate_accepts_a_dict():
    event = validate(
        {
            **REQUIRED,
            "source_type": "video_analytics",
            "event_id": "EVT-1",
            "timestamp": "2026-10-02T02:01:20Z",
        }
    )

    assert isinstance(event, IncidentEvent)
    assert event.event_id == "EVT-1"


def test_validate_supplies_no_defaults_of_its_own():
    # `build` fills in event_id, timestamp and source_type; `validate` checks an
    # incident against the published contract and must not quietly complete it.
    # Otherwise a producer's incomplete payload passes here and fails at the API,
    # which is the opposite of validating in the producer.
    complete_except_source_type = {
        **REQUIRED,
        "event_id": "EVT-1",
        "timestamp": "2026-10-02T02:01:20Z",
    }

    with pytest.raises(InvalidEvent, match="source_type"):
        validate(complete_except_source_type)


def test_validate_passes_a_model_straight_through():
    event = build(**REQUIRED)

    assert validate(event) is event


def test_validate_names_the_problem():
    with pytest.raises(InvalidEvent, match="tenant_id"):
        validate({"event_id": "EVT-1"})


def test_to_json_serialises_the_timestamp_and_drops_unset_optionals():
    event = build(**REQUIRED, timestamp=datetime(2026, 10, 2, 2, 1, 20, tzinfo=timezone.utc))

    sent = json.loads(to_json(event))

    assert sent["timestamp"].startswith("2026-10-02T02:01:20")
    assert "confidence" not in sent
    assert "evidence_uri" not in sent

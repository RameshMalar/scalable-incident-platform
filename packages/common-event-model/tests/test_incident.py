"""The properties of the contract, asserted where the contract lives.

The services already test what they each do with an incident. These test what
the incident itself promises, so a change to the contract fails here rather than
in three service suites at once.
"""

import pytest
from pydantic import ValidationError

from common_event_model import IncidentEvent

REQUIRED = {
    "event_id": "EVT-0001",
    "tenant_id": "TENANT-01",
    "site_id": "SITE-01",
    "source_id": "CAM-07",
    "source_name": "Loading bay camera CAM-07",
    "source_type": "video_analytics",
    "analytics_type": "intrusion_detection",
    "event_type": "intrusion_detected",
    "severity": "high",
    "timestamp": "2026-10-02T02:01:20Z",
}


def test_the_required_fields_are_enough():
    event = IncidentEvent(**REQUIRED)

    assert event.event_id == "EVT-0001"
    assert event.timestamp.year == 2026
    assert event.event_purpose == "incident"
    assert event.confidence is None
    assert event.evidence_uri is None


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_every_required_field_is_required(missing):
    # One test per field rather than one test for the set, so a field that stops
    # being required names itself in the failure.
    with pytest.raises(ValidationError):
        IncidentEvent(**{k: v for k, v in REQUIRED.items() if k != missing})


def test_an_unknown_analytics_type_is_accepted():
    # The platform's extensibility claim, asserted in the contract that carries
    # it. A new analytics pipeline must not need a release of this library.
    event = IncidentEvent(**{**REQUIRED, "analytics_type": "no_such_analytics_yet"})

    assert event.analytics_type == "no_such_analytics_yet"


def test_an_unknown_event_purpose_is_rejected():
    # The one field the pipeline branches on, so an unrecognised value here would
    # mean an incident processed as something nobody wrote a rule for.
    with pytest.raises(ValidationError):
        IncidentEvent(**{**REQUIRED, "event_purpose": "whatever"})


@pytest.mark.parametrize("purpose", ["incident", "load_test", "e2e_test"])
def test_the_recognised_purposes(purpose):
    assert IncidentEvent(**{**REQUIRED, "event_purpose": purpose}).event_purpose == purpose


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_confidence_outside_zero_to_one_is_rejected(confidence):
    with pytest.raises(ValidationError):
        IncidentEvent(**{**REQUIRED, "confidence": confidence})


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_confidence_on_the_boundary_is_accepted(confidence):
    assert IncidentEvent(**{**REQUIRED, "confidence": confidence}).confidence == confidence


def test_a_timestamp_without_a_zone_is_not_silently_made_utc():
    # Worth pinning: a naive timestamp stays naive rather than being assumed to
    # be UTC, so a producer in another zone is a visible problem and not a
    # quietly wrong incident time.
    event = IncidentEvent(**{**REQUIRED, "timestamp": "2026-10-02T02:01:20"})

    assert event.timestamp.tzinfo is None

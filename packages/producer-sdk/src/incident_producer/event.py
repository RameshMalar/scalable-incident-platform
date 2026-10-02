"""Building and validating an incident before it is sent.

Validation happens in the producer, not only at the API, so a malformed incident
fails where the mistake was made and names the field -- rather than arriving as a
422 from a service the producer's author cannot see the source of.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from common_event_model import IncidentEvent
from pydantic import ValidationError


class InvalidEvent(ValueError):
    """The incident does not satisfy the Common Event Model."""


def build(
    *,
    tenant_id: str,
    site_id: str,
    source_id: str,
    source_name: str,
    analytics_type: str,
    event_type: str,
    severity: str,
    event_id: str | None = None,
    timestamp: datetime | None = None,
    source_type: str = "video_analytics",
    **extra: Any,
) -> IncidentEvent:
    """One validated incident, with the fields a producer should not have to supply.

    `event_id` defaults to a UUID because the store keys documents on it: two
    incidents sharing one id are the same incident, so generating it here rather
    than letting a caller forget it is what makes a retry safe.

    `timestamp` defaults to now, in UTC and with the offset present. The model
    accepts a naive timestamp, so a producer in another zone would otherwise be
    able to record a time nobody can place.
    """
    try:
        return IncidentEvent(
            event_id=event_id or f"EVT-{uuid.uuid4().hex[:12].upper()}",
            tenant_id=tenant_id,
            site_id=site_id,
            source_id=source_id,
            source_name=source_name,
            source_type=source_type,
            analytics_type=analytics_type,
            event_type=event_type,
            severity=severity,
            timestamp=timestamp or datetime.now(timezone.utc),
            **extra,
        )
    except ValidationError as exc:
        raise InvalidEvent(str(exc)) from exc


def validate(event: IncidentEvent | dict) -> IncidentEvent:
    """Whatever the caller has, as a validated incident.

    A dict is accepted because a producer reading incidents off a message bus or
    a config file has dicts, and making it build a model first would only move
    this call.
    """
    if isinstance(event, IncidentEvent):
        return event

    try:
        return IncidentEvent.model_validate(event)
    except ValidationError as exc:
        raise InvalidEvent(str(exc)) from exc


def to_json(event: IncidentEvent) -> bytes:
    """The wire form the ingestion API expects.

    `mode="json"` so the timestamp is serialised as a string rather than left as
    a datetime that `json.dumps` cannot encode. Unset optional fields are dropped
    rather than sent as null, which says the same thing in fewer bytes.
    """
    return json.dumps(event.model_dump(mode="json", exclude_none=True)).encode()

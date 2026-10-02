"""The Common Event Model.

One incident shape for every analytics type the platform carries. Most fields
are deliberately open strings: `analytics_type`, `event_type`, `source_type` and
`severity` take any value, because constraining them would mean editing and
redeploying this library -- and every service that installs it -- to accept a
kind of analytics that changes nothing about how an incident is handled.

`event_purpose` is the exception, and it is a `Literal` because the pipeline
genuinely branches on it: load-test events are persisted without paging anyone.
A value nothing acts on should not be enumerated; a value something acts on
should not be free.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class IncidentEvent(BaseModel):
    """
    Common Event Model for all analytics incident types.

    The same model can represent intrusion, PPE, crowd
    detection and future analytics types.
    """

    event_id: str = Field(
        description="Unique identifier for this incident, assigned by the producer."
    )
    tenant_id: str = Field(
        description="Customer/tenant this incident belongs to."
    )
    site_id: str = Field(
        description="Site (building, facility, location) the incident occurred at."
    )

    source_id: str = Field(
        description="Identifier of the specific device that raised the incident, e.g. a camera ID."
    )
    source_name: str = Field(
        description="Human-readable name of the source device."
    )
    source_type: str = Field(
        description="Category of source device, e.g. 'video_analytics'."
    )

    analytics_type: str = Field(
        description="Analytics pipeline that produced this incident, e.g. 'intrusion_detection', 'PPE_detection', 'crowd_detection'."
    )
    event_type: str = Field(
        description="Specific event raised within the analytics type, e.g. 'intrusion_detected', 'helmet_missing'."
    )

    severity: str = Field(
        description="Severity of the incident, e.g. 'low', 'medium', 'high'."
    )
    timestamp: datetime = Field(
        description="Time the incident occurred, as reported by the source."
    )

    event_purpose: Literal["incident", "load_test", "e2e_test"] = Field(
        default="incident",
        description=(
            "Processing purpose for this event. Load-test events exercise "
            "ingestion, queues, and persistence without paging people; "
            "e2e_test events exercise the notification path at low volume."
        ),
    )

    confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Confidence score from the analytics model, between 0.0 and 1.0.",
    )

    description: Optional[str] = Field(
        default=None,
        description="Human-readable description of the incident.",
    )

    evidence_uri: Optional[str] = Field(
        default=None,
        description=(
            "Reference to evidence associated with the incident, such as an "
            "image or video clip. Private Azure Blob references can be "
            "resolved by the notification service with its managed identity."
        ),
    )

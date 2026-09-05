"""The Common Event Model.

This module is the platform's contract between analytics producers and
downstream consumers, so every service that speaks it carries the same file:
`ingestion-service/app/models.py` and `worker-service/app/models.py` are
byte-identical copies, and CI fails the build if the two ever drift. Edit one,
copy it over the other.

They are copies rather than a shared package because each service builds from
its own directory as its Docker context; sharing the model as a real package
means moving both builds to a repository-root context, which is a change to the
deployment pipeline rather than to the model.
"""

from datetime import datetime
from typing import Optional

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
            "image or video clip. Currently a dummy URI; will point to Azure "
            "Blob Storage."
        ),
    )
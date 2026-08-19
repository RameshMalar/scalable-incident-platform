from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class IncidentEvent(BaseModel):
    """
    Common Event Model for all analytics incident types.

    The same model can represent intrusion, PPE, crowd
    detection and future analytics types.
    """

    event_id: str
    tenant_id: str
    site_id: str

    source_id: str
    source_name: str
    source_type: str

    analytics_type: str
    event_type: str

    severity: str
    timestamp: datetime

    confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0
    )

    description: Optional[str] = None

    # Reference to evidence associated with the incident.
    # Sprint 1 uses a dummy URI.
    # Later this can point to Azure Blob Storage.
    evidence_uri: Optional[str] = None
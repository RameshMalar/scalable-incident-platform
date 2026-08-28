from datetime import datetime
import os
import random
import uuid

from fastapi import Depends, FastAPI, Request, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.models import IncidentEvent
from app.publisher import (
    EventPublisher,
    MockEventPublisher,
    PublishError,
    ServiceBusEventPublisher,
)


app = FastAPI(
    title="Scalable Incident Platform - Ingestion Service",
    description="Common ingestion API for multiple analytics event types",
    version="1.0.0"
)

templates = Jinja2Templates(directory="app/templates")

app.mount(
    "/static",
    StaticFiles(directory="app/static"),
    name="static"
)


# Demo-only storage backing the dashboard below. Not part of the real
# ingestion path -- incidents are published, not stored, by create_incident.
events = []


def _build_default_publisher() -> EventPublisher:
    if os.environ.get("EVENT_PUBLISHER") == "servicebus":
        return ServiceBusEventPublisher()
    return MockEventPublisher()


_default_publisher = _build_default_publisher()


def get_publisher() -> EventPublisher:
    return _default_publisher


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "events": events
        }
    )


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "incident-ingestion-service"
    }

@app.post(
    "/api/v1/incidents",
    status_code=202,
    summary="Submit an incident",
    description=(
        "Validates an IncidentEvent against the Common Event Model and "
        "publishes it for downstream processing. A 202 response means the "
        "event was handed off successfully -- it does not mean it has been "
        "stored or acted on. Ingestion does not persist incidents; that is "
        "the responsibility of downstream consumers reading from the "
        "publish target (Azure Service Bus in production)."
    ),
)
async def create_incident(
    event: IncidentEvent,
    response: Response,
    publisher: EventPublisher = Depends(get_publisher),
):
    try:
        await publisher.publish(event)
    except PublishError as exc:
        response.status_code = 502
        return {
            "status": "error",
            "event_id": event.event_id,
            "message": f"Failed to publish incident: {exc}",
        }

    return {
        "status": "accepted",
        "event_id": event.event_id,
        "tenant_id": event.tenant_id,
        "site_id": event.site_id,
        "analytics_type": event.analytics_type,
        "event_type": event.event_type,
        "message": "Incident received successfully"
    }


def _record_demo_event(event: IncidentEvent) -> None:
    event_data = event.model_dump()
    event_data["timestamp"] = event.timestamp.isoformat()
    events.insert(0, event_data)


@app.post(
    "/api/v1/simulate/{analytics_type}",
    summary="Generate a sample incident (demo/dev only)",
    description=(
        "Generates and publishes a realistic sample incident for "
        "'intrusion', 'ppe', or 'crowd' without needing a real analytics "
        "producer. Not part of the real ingestion contract -- intended for "
        "local development and demos, and its output also populates the "
        "in-memory dashboard at '/'."
    ),
)
async def simulate_event(analytics_type: str):
    analytics_samples = {
    "intrusion": {
        "analytics_type": "intrusion_detection",
        "event_type": "intrusion_detected",
        "description": "Person detected inside restricted area",
        "severity": "high",
        "evidence_uri": "/evidence/intrusion_sample.jpg"
    },
    "ppe": {
        "analytics_type": "PPE_detection",
        "event_type": "helmet_missing",
        "description": "Worker detected without helmet",
        "severity": "medium",
        "evidence_uri": "/evidence/ppe_sample.jpg"
    },
    "crowd": {
        "analytics_type": "crowd_detection",
        "event_type": "crowd_threshold_exceeded",
        "description": "Crowd threshold exceeded in monitored area",
        "severity": "high",
        "evidence_uri": "/evidence/crowd_sample.jpg"
    }
}

    if analytics_type not in analytics_samples:
        return {
            "status": "error",
            "message": "Unsupported analytics type"
        }

    sample = analytics_samples[analytics_type]

    event = IncidentEvent(
        event_id=f"EVT-{uuid.uuid4().hex[:8].upper()}",
        tenant_id="TENANT-01",
        site_id="SITE-01",
        source_id=f"CAM-{random.randint(1, 20):03}",
        source_name="Demo Camera",
        source_type="video_analytics",
        analytics_type=sample["analytics_type"],
        event_type=sample["event_type"],
        severity=sample["severity"],
        timestamp=datetime.now(),
        confidence=round(random.uniform(0.80, 0.99), 2),
        description=sample["description"],
        #image_path=sample["image_path"]
        evidence_uri=sample["evidence_uri"]

    )

    publisher = get_publisher()
    try:
        await publisher.publish(event)
    except PublishError as exc:
        return {
            "status": "error",
            "event_id": event.event_id,
            "message": f"Failed to publish incident: {exc}",
        }

    _record_demo_event(event)

    return {
        "status": "accepted",
        "event_id": event.event_id,
        "tenant_id": event.tenant_id,
        "site_id": event.site_id,
        "analytics_type": event.analytics_type,
        "event_type": event.event_type,
        "message": "Incident received successfully"
    }
from datetime import datetime
import random
import uuid

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.models import IncidentEvent


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


# Temporary in-memory storage for Sprint 1 demo
events = []


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

# Temporary debug endpoint - remove after testing
@app.get("/api/v1/debug/events")
def debug_events():
    return {
        "count": len(events),
        "events": events
    }


@app.post("/api/v1/incidents")
def create_incident(event: IncidentEvent):

    event_data = event.model_dump()

    # Convert datetime to string for display
    event_data["timestamp"] = event.timestamp.isoformat()

    events.insert(0, event_data)

    return {
        "status": "accepted",
        "event_id": event.event_id,
        "tenant_id": event.tenant_id,
        "site_id": event.site_id,
        "analytics_type": event.analytics_type,
        "event_type": event.event_type,
        "message": "Incident received successfully"
    }


@app.post("/api/v1/simulate/{analytics_type}")
def simulate_event(analytics_type: str):
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

    return create_incident(event)
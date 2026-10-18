from contextlib import asynccontextmanager
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import random
import uuid

from fastapi import Depends, FastAPI, Request, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from common_event_model import IncidentEvent

from app.auth import (
    Producer,
    auth_mode,
    build_verifier,
    enforce_tenant,
    get_producer,
)
from app.publisher import (
    EventPublisher,
    MockEventPublisher,
    PublishError,
    ServiceBusEventPublisher,
)


# Uvicorn configures its own loggers but leaves the root logger at WARNING, so
# without this the publish trail -- "publishing event_id=...", "published
# event_id=..." -- is dropped before it ever reaches stdout, and with it the
# Log Analytics workspace the container streams to.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("app.main")


def _build_default_publisher() -> EventPublisher:
    if os.environ.get("EVENT_PUBLISHER") == "servicebus":
        return ServiceBusEventPublisher()
    return MockEventPublisher()


def _publisher_name(publisher: EventPublisher) -> str:
    return "servicebus" if isinstance(publisher, ServiceBusEventPublisher) else "mock"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One publisher for the life of the process, so the Service Bus connection
    # and sender are established once rather than per request.
    publisher = _build_default_publisher()
    await publisher.start()
    app.state.publisher = publisher

    verifier = build_verifier()
    verifier.warm()
    app.state.verifier = verifier

    logger.info(
        "ingestion ready publisher=%s auth=%s tenants=%d",
        _publisher_name(publisher),
        auth_mode(verifier),
        len(getattr(verifier, "tenant_map", {})),
    )
    try:
        yield
    finally:
        await publisher.close()


app = FastAPI(
    title="Scalable Incident Platform - Ingestion Service",
    description="Common ingestion API for multiple analytics event types",
    version="1.0.0",
    lifespan=lifespan,
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


def get_publisher(request: Request) -> EventPublisher:
    return request.app.state.publisher


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "events": events
        }
    )


@app.get(
    "/health",
    summary="Liveness probe",
    description=(
        "Answers one question: is this process alive and serving? It "
        "deliberately does not touch Service Bus. Liveness failure restarts "
        "the container, and restarting a healthy replica because the broker is "
        "having a bad minute turns a messaging outage into an outage of its "
        "own. Reaching the queue is what /ready is for.\n\n"
        "It does report which publisher the process started with, because a "
        "deployed service answering 'mock' would accept incidents and drop "
        "them; the deployment pipeline asserts on that field."
    ),
)
def health_check(request: Request):
    return {
        "status": "healthy",
        "service": "incident-ingestion-service",
        "publisher": _publisher_name(request.app.state.publisher),
        "auth": auth_mode(request.app.state.verifier),
    }


@app.get(
    "/ready",
    summary="Readiness probe",
    description=(
        "Answers a different question: can this replica actually publish? It "
        "asks the credential for a Service Bus token, which is the cheapest "
        "proof that an identity is attached, resolves, and can reach the token "
        "endpoint.\n\n"
        "Checking that the sender object exists would prove nothing -- "
        "constructing it does no network I/O, so a process pointed at a "
        "namespace that does not exist starts cleanly and looks healthy. A "
        "replica that answers 503 here is kept out of rotation until it can do "
        "the job, rather than accepting incidents and failing them with a 502."
    ),
)
async def readiness_check(request: Request, response: Response):
    publisher = request.app.state.publisher
    ready, detail = await publisher.ready()

    if not ready:
        response.status_code = 503

    return {
        "status": "ready" if ready else "not-ready",
        "service": "incident-ingestion-service",
        "publisher": _publisher_name(publisher),
        "detail": detail,
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
    producer: Producer | None = Depends(get_producer),
):
    # Authentication proves the caller holds a valid token. It does not prove
    # they may speak for the tenant named in the body -- tenant_id is an
    # ordinary field of the Common Event Model, so without this an
    # authenticated producer could attribute incidents to another customer.
    enforce_tenant(producer, event.tenant_id)

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
        "event_purpose": event.event_purpose,
        "message": "Incident received successfully"
    }


def _record_demo_event(event: IncidentEvent) -> None:
    event_data = event.model_dump()
    event_data["timestamp"] = event.timestamp.isoformat()
    events.insert(0, event_data)


# Blobs from evidence_images/manifest.proposed.json, already in the evidence
# container through the Evidence batch workflow; scripts/sync-simulated-evidence.py
# writes the list. Each simulated incident references one at random, so a demo
# shows a different photograph each time on the dashboard's Evidence page and in
# the Telegram alert, and the camera named is the one printed on that image. The
# set has no PPE image yet, so a simulated PPE incident keeps its placeholder and
# alerts as text.
SIMULATED_EVIDENCE = json.loads(
    Path(__file__).with_name("simulated_evidence.json").read_text()
)


def _simulated_evidence(
    analytics_type: str, placeholder: str
) -> tuple[str | None, str]:
    """Return the camera and evidence reference for a simulated incident.

    A blob reference has to name the deployment's storage account, which only
    the container app is told. Without it -- a local run -- the placeholder path
    stands and no camera is imposed.
    """
    endpoint = os.environ.get("EVIDENCE_BLOB_ENDPOINT", "").strip().rstrip("/")
    candidates = SIMULATED_EVIDENCE.get(analytics_type)

    if not endpoint or not candidates:
        return None, placeholder

    evidence = random.choice(candidates)
    container = os.environ.get("EVIDENCE_CONTAINER", "evidence").strip()
    return evidence["source_id"], f"{endpoint}/{container}/{evidence['blob']}"


@app.post(
    "/api/v1/simulate/{analytics_type}",
    summary="Generate a sample incident (demo/dev only)",
    description=(
        "Generates and publishes a realistic sample incident for "
        "'intrusion', 'ppe', 'crowd', 'fire' or 'smoke' without needing a real analytics "
        "producer. Not part of the real ingestion contract -- intended for "
        "local development and demos, and its output also populates the "
        "in-memory dashboard at '/'."
    ),
)
async def simulate_event(
    analytics_type: str,
    publisher: EventPublisher = Depends(get_publisher),
):
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
    },
    "fire": {
        "analytics_type": "fire_smoke_detection",
        "event_type": "fire_detected",
        "description": "Fire detected in monitored area",
        "severity": "high",
        "evidence_uri": "/evidence/fire_sample.jpg"
    },
    "smoke": {
        "analytics_type": "fire_smoke_detection",
        "event_type": "smoke_detected",
        "description": "Smoke detected in monitored area",
        "severity": "high",
        "evidence_uri": "/evidence/smoke_sample.jpg"
    }
}

    if analytics_type not in analytics_samples:
        return {
            "status": "error",
            "message": "Unsupported analytics type"
        }

    sample = analytics_samples[analytics_type]
    source_id, evidence_uri = _simulated_evidence(
        analytics_type, sample["evidence_uri"]
    )

    event = IncidentEvent(
        event_id=f"EVT-{uuid.uuid4().hex[:8].upper()}",
        tenant_id="TENANT-01",
        site_id="SITE-01",
        source_id=source_id or f"CAM-{random.randint(1, 20):03}",
        source_name="Demo Camera",
        source_type="video_analytics",
        analytics_type=sample["analytics_type"],
        event_type=sample["event_type"],
        severity=sample["severity"],
        timestamp=datetime.now(),
        event_purpose="e2e_test",
        confidence=round(random.uniform(0.80, 0.99), 2),
        description=sample["description"],
        evidence_uri=evidence_uri
    )

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
        "event_purpose": event.event_purpose,
        "message": "Incident received successfully"
    }

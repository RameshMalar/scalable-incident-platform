import pytest
from fastapi.testclient import TestClient

from app.main import _build_default_publisher, app, get_publisher
from app.models import IncidentEvent
from app.publisher import MockEventPublisher, ServiceBusEventPublisher


@pytest.fixture
def client():
    # As a context manager so the lifespan runs: that is what builds the
    # publisher and puts it on app.state, exactly as it does in the container.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def mock_publisher():
    publisher = MockEventPublisher()
    app.dependency_overrides[get_publisher] = lambda: publisher
    yield publisher
    app.dependency_overrides.pop(get_publisher, None)


def sample_event(**overrides):
    event = {
        "event_id": "EVT-TEST0001",
        "tenant_id": "TENANT-01",
        "site_id": "SITE-01",
        "source_id": "CAM-001",
        "source_name": "Test Camera",
        "source_type": "video_analytics",
        "analytics_type": "intrusion_detection",
        "event_type": "intrusion_detected",
        "severity": "high",
        "timestamp": "2026-08-21T10:00:00",
        "confidence": 0.95,
        "description": "Person detected inside restricted area",
        "evidence_uri": "/evidence/test.jpg",
    }
    event.update(overrides)
    return event


def test_health_reports_service_identity_and_active_publisher(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "incident-ingestion-service",
        # No EVENT_PUBLISHER and no AUTH_MODE in the test environment, so the
        # process falls back to the mock publisher and open access, and says so
        # in both cases. The deployment pipeline asserts the opposite of each.
        "publisher": "mock",
        "auth": "disabled",
    }


def test_ready_reports_ready_when_the_publisher_can_reach_its_target(client):
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["publisher"] == "mock"


def test_ready_answers_503_when_the_publisher_cannot_publish(client):
    class NotReadyPublisher(MockEventPublisher):
        async def ready(self):
            return False, "could not acquire a Service Bus token: no identity"

    client.app.state.publisher = NotReadyPublisher()

    response = client.get("/ready")

    # 503 keeps the replica out of rotation. Accepting the incident and failing
    # it with a 502 would be the worse answer.
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not-ready"
    assert "no identity" in body["detail"]


def test_liveness_does_not_depend_on_the_publisher_being_reachable(client):
    # Liveness failure restarts the container, so a broker outage must not trip
    # it -- otherwise a messaging problem becomes a restart loop.
    class NotReadyPublisher(MockEventPublisher):
        async def ready(self):
            return False, "broker unreachable"

    client.app.state.publisher = NotReadyPublisher()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_servicebus_configuration_selects_the_real_publisher(monkeypatch):
    # The regression this guards: the deployed service ran without
    # EVENT_PUBLISHER set, quietly answered 202 to every incident and published
    # none of them.
    monkeypatch.setenv("EVENT_PUBLISHER", "servicebus")
    monkeypatch.setenv(
        "SERVICE_BUS_FQDN", "incident-bus-test.servicebus.windows.net"
    )

    assert isinstance(_build_default_publisher(), ServiceBusEventPublisher)


def test_incident_is_accepted_and_echoed_back(client):
    response = client.post("/api/v1/incidents", json=sample_event())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["event_id"] == "EVT-TEST0001"
    assert body["analytics_type"] == "intrusion_detection"
    assert body["event_purpose"] == "incident"


def test_incident_is_published_as_a_valid_common_event_model_message(
    client, mock_publisher
):
    response = client.post("/api/v1/incidents", json=sample_event())

    assert response.status_code == 202
    assert len(mock_publisher.published_events) == 1

    published = mock_publisher.published_events[0]
    assert isinstance(published, IncidentEvent)
    assert published.event_id == "EVT-TEST0001"
    assert published.tenant_id == "TENANT-01"
    assert published.analytics_type == "intrusion_detection"
    assert published.event_purpose == "incident"


def test_incident_publish_failure_returns_an_explicit_error(client):
    app.dependency_overrides[get_publisher] = lambda: MockEventPublisher(fail=True)

    response = client.post("/api/v1/incidents", json=sample_event())

    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "error"
    assert body["event_id"] == "EVT-TEST0001"
    assert "message" in body


def test_common_event_model_rejects_confidence_above_one(client):
    response = client.post(
        "/api/v1/incidents",
        json=sample_event(confidence=1.5),
    )

    assert response.status_code == 422


def test_common_event_model_requires_tenant_id(client):
    payload = sample_event()
    del payload["tenant_id"]

    response = client.post("/api/v1/incidents", json=payload)

    assert response.status_code == 422


def test_the_same_model_carries_every_analytics_type(client, mock_publisher):
    for analytics_type in ("intrusion", "ppe", "crowd"):
        response = client.post(f"/api/v1/simulate/{analytics_type}")

        assert response.status_code == 200
        assert response.json()["status"] == "accepted"

    published = [e.analytics_type for e in mock_publisher.published_events]
    assert published == ["intrusion_detection", "PPE_detection", "crowd_detection"]
    assert {e.event_purpose for e in mock_publisher.published_events} == {"e2e_test"}


def test_invalid_event_purpose_is_rejected(client):
    response = client.post(
        "/api/v1/incidents",
        json=sample_event(event_purpose="bulk_test"),
    )

    assert response.status_code == 422


def test_unsupported_analytics_type_is_reported(client):
    response = client.post("/api/v1/simulate/not-a-real-analytics")

    assert response.json()["status"] == "error"


def test_debug_events_endpoint_is_removed(client):
    response = client.get("/api/v1/debug/events")

    assert response.status_code == 404


def test_an_anonymous_incident_is_refused_once_authentication_is_on(client):
    # The gap this closes: the deployed API answered 202 to anyone on the
    # internet, for any tenant they cared to name.
    from app.auth import TokenVerifier

    client.app.state.verifier = TokenVerifier(
        tenant_id="a-tenant",
        audience="api://an-api",
        tenant_map={},
        jwk_client=object(),
    )

    response = client.post("/api/v1/incidents", json=sample_event())

    assert response.status_code == 401
    assert "Bearer" in response.headers.get("WWW-Authenticate", "")

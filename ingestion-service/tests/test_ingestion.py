from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


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


def test_health_reports_service_identity():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "incident-ingestion-service",
    }


def test_incident_is_accepted_and_echoed_back():
    response = client.post("/api/v1/incidents", json=sample_event())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["event_id"] == "EVT-TEST0001"
    assert body["analytics_type"] == "intrusion_detection"


def test_common_event_model_rejects_confidence_above_one():
    response = client.post(
        "/api/v1/incidents",
        json=sample_event(confidence=1.5),
    )

    assert response.status_code == 422


def test_common_event_model_requires_tenant_id():
    payload = sample_event()
    del payload["tenant_id"]

    response = client.post("/api/v1/incidents", json=payload)

    assert response.status_code == 422


def test_the_same_model_carries_every_analytics_type():
    for analytics_type in ("intrusion", "ppe", "crowd"):
        response = client.post(f"/api/v1/simulate/{analytics_type}")

        assert response.status_code == 200
        assert response.json()["status"] == "accepted"


def test_unsupported_analytics_type_is_reported():
    response = client.post("/api/v1/simulate/not-a-real-analytics")

    assert response.json()["status"] == "error"

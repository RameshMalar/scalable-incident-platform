from unittest.mock import AsyncMock

from azure.core.exceptions import ServiceRequestError
from fastapi.testclient import TestClient

from app.auth import DevelopmentVerifier
from app.main import create_app
from app.store import InMemorySubscriptionStore
from tests.conftest import headers


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "healthy", "service": "subscription-service", "store": "memory", "auth": "entra"}
    assert client.get("/ready").json()["status"] == "ready"


def test_readiness_failure_does_not_fail_liveness(client, store):
    store.ready = AsyncMock(side_effect=ServiceRequestError("private endpoint detail"))
    assert client.get("/ready").status_code == 503
    assert client.get("/ready").json()["status"] == "not-ready"
    assert "private endpoint" not in client.get("/ready").text
    assert client.get("/health").status_code == 200


def test_storage_outage_returns_sanitized_503(client, store, payload):
    store.create = AsyncMock(side_effect=ServiceRequestError("secret diagnostic"))
    response = client.post("/api/v1/subscriptions", json=payload, headers=headers())
    assert response.status_code == 503
    assert response.json() == {"detail": "subscription store unavailable"}


def test_lifecycle():
    store = InMemorySubscriptionStore()
    store.start, store.close = AsyncMock(), AsyncMock()
    with TestClient(create_app(store, DevelopmentVerifier("TENANT-DEV"))) as client:
        assert client.get("/health").status_code == 200
        store.start.assert_awaited_once()
    store.close.assert_awaited_once()

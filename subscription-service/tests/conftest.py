import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import TenantContext
from app.main import create_app
from app.store import InMemorySubscriptionStore


class FakeVerifier:
    mode = "entra"

    def verify(self, token):
        if token not in {"A", "B", "reader", "producer"}:
            raise HTTPException(401, "invalid token")
        permissions = {"Subscriptions.Read", "Subscriptions.Write"}
        if token == "reader":
            permissions = {"Subscriptions.Read"}
        if token == "producer":
            permissions = {"Incidents.Write"}
        return TenantContext(token, "TENANT-B" if token == "B" else "TENANT-A", frozenset(permissions))


@pytest.fixture
def store():
    return InMemorySubscriptionStore()


@pytest.fixture
def client(store):
    with TestClient(create_app(store, FakeVerifier())) as client:
        yield client


@pytest.fixture
def payload():
    return {"event_type": "intrusion_detected", "channel": "telegram", "telegram_chat_id": "-100123456"}


def headers(tenant="A"):
    return {"Authorization": f"Bearer {tenant}"}

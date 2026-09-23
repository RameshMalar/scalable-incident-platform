from app.main import build_handler, handler_name
from app.notification_handler import PersistAndNotifyHandler
from app.notification_publisher import MockNotificationPublisher
from app.processor import LoggingIncidentHandler
from app.store import CosmosIncidentStore


def test_the_worker_logs_incidents_when_no_store_is_configured(monkeypatch):
    monkeypatch.delenv("INCIDENT_STORE", raising=False)
    monkeypatch.delenv("NOTIFICATION_PUBLISHER", raising=False)

    handler = build_handler()

    assert isinstance(handler, LoggingIncidentHandler)
    assert handler_name(handler) == "logging"


def test_cosmos_configuration_selects_the_real_store(monkeypatch):
    # This guards against a deployed worker that consumes incidents but
    # accidentally persists none of them.
    monkeypatch.setenv("INCIDENT_STORE", "cosmos")
    monkeypatch.delenv("NOTIFICATION_PUBLISHER", raising=False)
    monkeypatch.setenv(
        "COSMOS_ENDPOINT",
        "https://incident-cosmos-test.documents.azure.com:443/",
    )

    handler = build_handler()

    # Cosmos is now wrapped by the notification pipeline rather than being
    # returned directly as the top-level handler.
    assert isinstance(handler, PersistAndNotifyHandler)
    assert isinstance(handler.store, CosmosIncidentStore)

    # Without NOTIFICATION_PUBLISHER=servicebus, local/test execution uses the
    # mock publisher and therefore requires no Service Bus credentials.
    assert isinstance(handler.publisher, MockNotificationPublisher)

    assert handler_name(handler) == "cosmos+notification"
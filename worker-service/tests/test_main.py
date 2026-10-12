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
    assert handler.router.reader.endpoint == handler.store.endpoint
    assert handler.router.reader.container_name == "subscriptions"


def test_subscription_container_configuration(monkeypatch):
    monkeypatch.setenv("INCIDENT_STORE", "cosmos")
    monkeypatch.setenv("COSMOS_ENDPOINT", "https://test.invalid")
    monkeypatch.setenv("COSMOS_DATABASE", "configured-db")
    monkeypatch.setenv("COSMOS_SUBSCRIPTIONS_CONTAINER", "configured-subscriptions")
    monkeypatch.delenv("NOTIFICATION_PUBLISHER", raising=False)
    handler = build_handler()
    assert handler.router.reader.database_name == "configured-db"
    assert handler.router.reader.container_name == "configured-subscriptions"


async def test_startup_failure_closes_handler_and_consumer(monkeypatch):
    from unittest.mock import AsyncMock
    import pytest
    import app.main as main_module

    handler, consumer = AsyncMock(), AsyncMock()
    handler.start.side_effect = RuntimeError("startup failed")
    monkeypatch.setattr(main_module, "build_handler", lambda: handler)
    monkeypatch.setattr(main_module, "IncidentConsumer", lambda **kwargs: consumer)
    monkeypatch.setattr(main_module, "install_shutdown_handler", lambda stop: None)
    with pytest.raises(RuntimeError, match="startup failed"):
        await main_module.run()
    handler.close.assert_awaited_once()
    consumer.close.assert_awaited_once()

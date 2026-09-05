import pytest

from app.main import build_handler, handler_name
from app.processor import LoggingIncidentHandler
from app.store import CosmosIncidentStore


def test_the_worker_logs_incidents_when_no_store_is_configured(monkeypatch):
    monkeypatch.delenv("INCIDENT_STORE", raising=False)

    handler = build_handler()

    assert isinstance(handler, LoggingIncidentHandler)
    assert handler_name(handler) == "logging"


def test_cosmos_configuration_selects_the_real_store(monkeypatch):
    # The failure this guards is the one the ingestion service already had once:
    # a deployed service that consumes the queue, looks entirely healthy, and
    # persists nothing.
    monkeypatch.setenv("INCIDENT_STORE", "cosmos")
    monkeypatch.setenv(
        "COSMOS_ENDPOINT", "https://incident-cosmos-test.documents.azure.com:443/"
    )

    handler = build_handler()

    assert isinstance(handler, CosmosIncidentStore)
    assert handler_name(handler) == "cosmos"

from unittest.mock import AsyncMock

import pytest

from common_event_model import IncidentEvent
from app.notification_models import InvalidSubscription
from app.subscription_router import SubscriptionRouter
from app.subscription_store import InMemorySubscriptionReader
from tests.test_notification_models import incident_payload, subscription_document


async def test_matching_route_and_complete_incident():
    event = IncidentEvent(**incident_payload())
    router = SubscriptionRouter(InMemorySubscriptionReader([subscription_document()]), policies=AsyncMock(approved=AsyncMock(return_value=True)))
    routes = await router.route(event)
    assert len(routes) == 1
    route = routes[0]
    assert route.subscription_id == "SUB-1"
    assert route.telegram_chat_id == subscription_document()["telegram_chat_id"]
    assert route.incident.model_dump() == event.model_dump()
    assert route.incident.model_fields_set == event.model_fields_set
    assert route.incident is not event
    assert route.incident.evidence_uri == event.evidence_uri
    assert route.schema_version == 1


@pytest.mark.parametrize("changes", [
    {"event_type": "helmet_missing"}, {"tenant_id": "TENANT-B"},
    {"enabled": False}, {"event_type": "*"},
    {"event_type": "Intrusion_detected"}, {"event_type": "intrusion_detected "},
    {"tenant_id": "tenant-a"},
])
async def test_nonmatching_document_cannot_route_even_from_custom_reader(changes):
    reader = AsyncMock()
    reader.find_matches.return_value = [subscription_document(**changes)]
    assert await SubscriptionRouter(reader, policies=AsyncMock(approved=AsyncMock(return_value=True))).route(IncidentEvent(**incident_payload())) == []
    reader.find_matches.assert_awaited_once_with("TENANT-A", "intrusion_detected")


@pytest.mark.parametrize("purpose,event_id,eligible", [
    ("load_test", "EVT-1", False), ("e2e_test", "EVT-1", True),
    (None, "EVT-LOAD-001", False), (None, "EVT-OLD-1", True),
    ("incident", "EVT-LOAD-001", True), ("e2e_test", "EVT-LOAD-001", True),
])
async def test_existing_purpose_and_legacy_policy(purpose, event_id, eligible):
    payload = incident_payload(event_id=event_id)
    if purpose is None:
        del payload["event_purpose"]
    else:
        payload["event_purpose"] = purpose
    reader = AsyncMock()
    reader.find_matches.return_value = [subscription_document()]
    routes = await SubscriptionRouter(reader, policies=AsyncMock(approved=AsyncMock(return_value=True))).route(IncidentEvent(**payload))
    assert len(routes) == (1 if eligible else 0)
    if eligible:
        reader.find_matches.assert_awaited_once()
    else:
        reader.find_matches.assert_not_awaited()


async def test_multiple_subscriptions_to_same_destination_are_not_deduplicated():
    documents = [subscription_document(id=f"SUB-{number}") for number in range(3)]
    routes = await SubscriptionRouter(InMemorySubscriptionReader(documents), policies=AsyncMock(approved=AsyncMock(return_value=True))).route(
        IncidentEvent(**incident_payload())
    )
    assert [route.subscription_id for route in routes] == ["SUB-0", "SUB-1", "SUB-2"]
    assert {route.telegram_chat_id for route in routes} == {"-100123456"}


async def test_empty_store():
    assert await SubscriptionRouter(InMemorySubscriptionReader(), policies=AsyncMock(approved=AsyncMock(return_value=True))).route(
        IncidentEvent(**incident_payload())
    ) == []


async def test_lookup_failure_propagates():
    reader = AsyncMock()
    reader.find_matches.side_effect = RuntimeError("Cosmos unavailable")
    with pytest.raises(RuntimeError, match="Cosmos unavailable"):
        await SubscriptionRouter(reader, policies=AsyncMock(approved=AsyncMock(return_value=True))).route(IncidentEvent(**incident_payload()))


@pytest.mark.parametrize("document", [
    None, {}, subscription_document(channel="email"),
    subscription_document(enabled="true"), subscription_document(telegram_chat_id="bad"),
    subscription_document(id=None),
])
async def test_invalid_later_document_does_not_block_valid_routes(document):
    reader = AsyncMock()
    reader.find_matches.return_value = [subscription_document(), document]
    routes = await SubscriptionRouter(reader, policies=AsyncMock(approved=AsyncMock(return_value=True))).route(IncidentEvent(**incident_payload()))
    assert [route.subscription_id for route in routes] == ["SUB-1"]


async def test_asterisk_only_matches_literal_asterisk_event():
    router = SubscriptionRouter(InMemorySubscriptionReader([subscription_document(event_type="*")]), policies=AsyncMock(approved=AsyncMock(return_value=True)))
    assert await router.route(IncidentEvent(**incident_payload())) == []
    assert len(await router.route(IncidentEvent(**incident_payload(event_type="*")))) == 1


async def test_valid_malformed_valid_router_preserves_policy(caplog):
    reader = AsyncMock()
    reader.find_matches.return_value = [subscription_document(id='FIRST'),
        {'tenant_id': 'SECRET', 'telegram_chat_id': 'SECRET-CHAT'},
        subscription_document(tenant_id=None), {},
        subscription_document(id='DENIED'), subscription_document(id='LAST')]
    policy=AsyncMock(approved=AsyncMock(side_effect=[True,False,True]))
    routes=await SubscriptionRouter(reader,policies=policy).route(IncidentEvent(**incident_payload()))
    assert [r.subscription_id for r in routes]==['FIRST','LAST']
    assert policy.approved.await_count==3
    assert caplog.text.count('skipping malformed')==3
    assert 'SECRET' not in caplog.text


async def test_unexpected_validation_exception_is_not_swallowed(monkeypatch):
    import app.subscription_router as module
    reader=AsyncMock();reader.find_matches.return_value=[subscription_document()]
    def broken(document):raise RuntimeError('unexpected validation failure')
    monkeypatch.setattr(module,'validate_subscription',broken)
    with pytest.raises(RuntimeError):
        await SubscriptionRouter(reader).route(IncidentEvent(**incident_payload()))

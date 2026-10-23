"""Account-owned subscriptions route only while their owner remains eligible."""
from unittest.mock import AsyncMock, Mock

import pytest
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError

from common_event_model import IncidentEvent
from app.owner_eligibility import ACCESS_PARTITION
from app.subscription_router import SubscriptionRouter
from app.subscription_store import CosmosSubscriptionReader, InMemorySubscriptionReader, MATCH_QUERY
from tests.test_notification_models import incident_payload, subscription_document

OWNER = "00000000-0000-4000-8000-000000000101"
CHAT = "-100123456"


def records(**access_changes):
    access = {"id": "access:" + OWNER, "record_type": "platform_access", "object_id": OWNER, "role": "SOC Viewer",
              "tenants": ["TENANT-A"], "status": "active", "version": 1}
    access.update(access_changes)
    return {"access:" + OWNER: access,
            "telegram-owner:" + OWNER: {"record_type": "telegram_owner", "object_id": OWNER, "chat_id": CHAT}}


def router(platform):
    reader = InMemorySubscriptionReader([subscription_document(owner_object_id=OWNER)], platform)
    return SubscriptionRouter(reader, policies=AsyncMock(approved=AsyncMock(return_value=True)))


async def test_eligible_owner_routes():
    routes = await router(records()).route(IncidentEvent(**incident_payload()))
    assert [route.telegram_chat_id for route in routes] == [CHAT]


@pytest.mark.parametrize("platform", [
    records(status="disabled"), records(status="revoked"), records(tenants=["TENANT-B"]),
    records(role="Owner"), records(object_id="someone-else"), {},
    {k: v for k, v in records().items() if k.startswith("access:")},
    {**records(), "telegram-owner:" + OWNER: {"record_type": "telegram_owner", "object_id": OWNER, "chat_id": "-1"}},
])
async def test_ineligible_owner_is_not_routed(platform):
    assert await router(platform).route(IncidentEvent(**incident_payload())) == []


async def test_legacy_subscriptions_never_read_platform_records():
    reader = InMemorySubscriptionReader([subscription_document()])
    reader.read_platform_record = AsyncMock(side_effect=AssertionError("unexpected"))
    routes = await SubscriptionRouter(reader, policies=AsyncMock(approved=AsyncMock(return_value=True))).route(IncidentEvent(**incident_payload()))
    assert len(routes) == 1


async def test_lookup_failure_propagates_to_retry():
    reader = InMemorySubscriptionReader([subscription_document(owner_object_id=OWNER)])
    reader.read_platform_record = AsyncMock(side_effect=RuntimeError("cosmos down"))
    with pytest.raises(RuntimeError):
        await SubscriptionRouter(reader, policies=AsyncMock(approved=AsyncMock(return_value=True))).route(IncidentEvent(**incident_payload()))


async def test_cosmos_reader_reads_reserved_partition_and_rejects_missing_container():
    reader = CosmosSubscriptionReader(endpoint="https://example.documents.azure.com", credential=object(),
                                      client_factory=Mock())
    container = Mock(read_item=AsyncMock(return_value={"id": "access:x"}),
                     read=AsyncMock(return_value={"partitionKey": {"paths": ["/tenant_id"]}}))
    reader._container = container
    assert await reader.read_platform_record("access:x") == {"id": "access:x"}
    container.read_item.assert_awaited_once_with(item="access:x", partition_key=ACCESS_PARTITION)
    container.read_item.side_effect = CosmosResourceNotFoundError()
    assert await reader.read_platform_record("access:x") is None
    container.read.side_effect = CosmosResourceNotFoundError()
    with pytest.raises(CosmosResourceNotFoundError):
        await reader.read_platform_record("access:x")
    container.read_item.side_effect = CosmosHttpResponseError(status_code=429, message="throttled")
    with pytest.raises(CosmosHttpResponseError):
        await reader.read_platform_record("access:x")


def test_match_query_projects_owner():
    assert "c.owner_object_id" in MATCH_QUERY

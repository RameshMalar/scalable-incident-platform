from unittest.mock import AsyncMock
import pytest
from common_event_model import IncidentEvent
from app.destination_policy import PolicyUnavailable, CosmosDestinationPolicy
from app.subscription_router import SubscriptionRouter
from app.subscription_store import InMemorySubscriptionReader
from app.notification_handler import PersistAndNotifyHandler
from app.processor import Outcome
from tests.test_notification_models import incident_payload, subscription_document


@pytest.mark.asyncio
async def test_default_deny_direct_storage_modification():
    router=SubscriptionRouter(InMemorySubscriptionReader([subscription_document()]))
    assert await router.route(IncidentEvent(**incident_payload())) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('approval',[False,True])
async def test_worker_independent_authorization(approval):
    policies=AsyncMock(approved=AsyncMock(return_value=approval))
    router=SubscriptionRouter(InMemorySubscriptionReader([subscription_document()]),policies)
    routes=await router.route(IncidentEvent(**incident_payload()))
    assert len(routes)==int(approval)
    policies.approved.assert_awaited_once_with('TENANT-A',subscription_document()['telegram_chat_id'])


@pytest.mark.asyncio
async def test_revoked_between_selection_and_publication():
    policies=AsyncMock(approved=AsyncMock(side_effect=[True,False]))
    router=SubscriptionRouter(InMemorySubscriptionReader([subscription_document()]),policies)
    publisher=AsyncMock();store=AsyncMock(handle=AsyncMock(return_value=Outcome.PROCESSED))
    handler=PersistAndNotifyHandler(store,publisher,router)
    assert await handler.handle(IncidentEvent(**incident_payload())) == Outcome.PROCESSED
    publisher.publish.assert_not_awaited()
    assert policies.approved.await_count==2


@pytest.mark.asyncio
@pytest.mark.parametrize('checks',[[PolicyUnavailable('unavailable')],[True,PolicyUnavailable('unavailable')]])
async def test_outage_propagates_without_publication(checks,caplog):
    router=SubscriptionRouter(InMemorySubscriptionReader([subscription_document()]),AsyncMock(approved=AsyncMock(side_effect=checks)))
    publisher=AsyncMock();store=AsyncMock(handle=AsyncMock(return_value=Outcome.PROCESSED))
    with pytest.raises(PolicyUnavailable):await PersistAndNotifyHandler(store,publisher,router).handle(IncidentEvent(**incident_payload()))
    publisher.publish.assert_not_awaited()
    assert 'publication blocked' in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('changes,expected', [({},True),({'enabled':False},False),({'tenant_id':'TENANT-B'},False),({'telegram_chat_id':'-999'},False)])
async def test_cosmos_partitioned_read_only_policy(changes, expected):
    from unittest.mock import Mock
    record = {'id':'telegram:-100001','tenant_id':'TENANT-A','channel':'telegram','telegram_chat_id':'-100001','enabled':True} | changes
    container = Mock(read=AsyncMock(return_value={"id":"approved-destinations","partitionKey":{"paths":["/tenant_id"],"kind":"Hash"}}), read_item=AsyncMock(return_value=record))
    client = Mock(); client.get_database_client.return_value.get_container_client.return_value = container
    policy = CosmosDestinationPolicy(endpoint='https://synthetic.invalid', credential=object(), client_factory=lambda *a,**k:client)
    assert await policy.approved('TENANT-A','-100001') is expected
    container.read_item.assert_awaited_once_with(item='telegram:-100001',partition_key='TENANT-A')
    container.create_item.assert_not_called()


@pytest.mark.asyncio
async def test_missing_outage_and_corrupt_policy():
    from unittest.mock import Mock
    from azure.cosmos.exceptions import CosmosResourceNotFoundError
    from azure.core.exceptions import ServiceRequestError
    container=Mock(read=AsyncMock(return_value={"id":"approved-destinations","partitionKey":{"paths":["/tenant_id"],"kind":"Hash"}}),read_item=AsyncMock(side_effect=CosmosResourceNotFoundError()))
    client=Mock();client.get_database_client.return_value.get_container_client.return_value=container
    policy=CosmosDestinationPolicy(endpoint='https://synthetic.invalid',credential=object(),client_factory=lambda *a,**k:client)
    assert not await policy.approved('TENANT-A','-100001')
    container.read_item.side_effect=ServiceRequestError('secret')
    with pytest.raises(PolicyUnavailable,match='^destination policy unavailable$'):
        await policy.approved('TENANT-A','-100001')
    container.read_item.side_effect=None;container.read_item.return_value={'enabled':'true'}
    with pytest.raises(PolicyUnavailable):await policy.approved('TENANT-A','-100001')


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["missing_container", "deleted_during_lookup", "wrong_partition", "wrong_container", "forbidden", "throttled", "network", "missing_record"])
async def test_container_failures_are_not_destination_denials(scenario):
    from unittest.mock import Mock
    from azure.cosmos.exceptions import CosmosResourceNotFoundError, CosmosHttpResponseError
    from azure.core.exceptions import ServiceRequestError
    metadata = {"id": "approved-destinations", "partitionKey": {"paths": ["/tenant_id"], "kind": "Hash"}}
    container = Mock(read=AsyncMock(return_value=metadata), read_item=AsyncMock(side_effect=CosmosResourceNotFoundError()))
    client = Mock()
    client.get_database_client.return_value.get_container_client.return_value = container
    policy = CosmosDestinationPolicy(endpoint="https://synthetic.invalid", credential=object(), client_factory=lambda *a, **k: client)
    if scenario == "missing_container": container.read.side_effect = CosmosResourceNotFoundError()
    if scenario == "deleted_during_lookup": container.read.side_effect = [metadata, CosmosResourceNotFoundError()]
    if scenario == "wrong_partition": metadata["partitionKey"]["paths"] = ["/other"]
    if scenario == "wrong_container": metadata["id"] = "other"
    if scenario == "forbidden": container.read_item.side_effect = CosmosHttpResponseError(status_code=403, message="private")
    if scenario == "throttled": container.read_item.side_effect = CosmosHttpResponseError(status_code=429, message="private")
    if scenario == "network": container.read.side_effect = ServiceRequestError("private")
    if scenario == "missing_record":
        assert await policy.approved("TENANT-A", "-100001") is False
        assert container.read.await_count == 2
    else:
        with pytest.raises(PolicyUnavailable, match="^destination policy unavailable$"):
            await policy.approved("TENANT-A", "-100001")
    if scenario in {"missing_container", "wrong_partition", "wrong_container", "network"}:
        container.read_item.assert_not_awaited()
    container.create_item.assert_not_called()

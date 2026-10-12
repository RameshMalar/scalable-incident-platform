from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.destination_policy import CosmosDestinationPolicy, PolicyUnavailable
from tests.conftest import FakeVerifier, headers


@pytest.mark.parametrize("approval", [False, True])
def test_post_requires_tenant_approval(store, payload, approval):
    policy = AsyncMock(approved=AsyncMock(return_value=approval))
    with TestClient(create_app(store, FakeVerifier(), policy)) as client:
        response = client.post('/api/v1/subscriptions', json=payload, headers=headers())
        assert response.status_code == (201 if approval else 403)
        policy.approved.assert_awaited_once_with('TENANT-A', payload['telegram_chat_id'])
        if not approval:
            assert response.json() == {'detail': 'destination not approved'}
            assert not store.items
        assert client.post('/api/v1/destinations', json={}, headers=headers()).status_code == 404


def test_revocation_read_disable_reenable_delete(store, payload):
    policy = AsyncMock(approved=AsyncMock(return_value=True))
    with TestClient(create_app(store, FakeVerifier(), policy)) as client:
        item = client.post('/api/v1/subscriptions', json=payload, headers=headers()).json()
        url = '/api/v1/subscriptions/' + item['id']
        policy.approved.return_value = False
        assert client.get(url, headers=headers()).status_code == 200
        assert client.get('/api/v1/subscriptions', headers=headers()).status_code == 200
        assert client.patch(url, json={'event_type':'changed'}, headers=headers()).status_code == 403
        assert client.patch(url, json={'enabled':False}, headers=headers()).status_code == 200
        assert client.patch(url, json={'enabled':True}, headers=headers()).status_code == 403
        policy.approved.side_effect = PolicyUnavailable('private diagnostics')
        assert client.patch(url, json={'enabled':True}, headers=headers()).json() == {'detail':'destination policy unavailable'}
        assert client.delete(url, headers=headers()).status_code == 204


def test_policy_outage_and_disabled_creation(store, payload):
    policy = AsyncMock(approved=AsyncMock(side_effect=PolicyUnavailable('private diagnostics')))
    with TestClient(create_app(store, FakeVerifier(), policy)) as client:
        response = client.post('/api/v1/subscriptions', json=payload, headers=headers())
        assert response.status_code == 503
        assert 'private' not in response.text
        assert client.post('/api/v1/subscriptions', json=payload | {'enabled':False}, headers=headers()).status_code == 201


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


def test_approval_for_other_tenant_does_not_authorize_caller(store, payload):
    policy=AsyncMock(approved=AsyncMock(side_effect=lambda tenant,chat: tenant=='TENANT-B'))
    with TestClient(create_app(store,FakeVerifier(),policy)) as client:
        assert client.post('/api/v1/subscriptions',json=payload,headers=headers('A')).status_code==403
        assert client.post('/api/v1/subscriptions',json=payload,headers=headers('B')).status_code==201


def test_patch_destination_change_checks_final_enabled_record(store,payload):
    policy=AsyncMock(approved=AsyncMock(side_effect=lambda tenant,chat: chat==payload['telegram_chat_id']))
    with TestClient(create_app(store,FakeVerifier(),policy)) as client:
        item=client.post('/api/v1/subscriptions',json=payload,headers=headers()).json()
        url='/api/v1/subscriptions/'+item['id']
        assert client.patch(url,json={'telegram_chat_id':'-100000999'},headers=headers()).status_code==403
        assert client.get(url,headers=headers()).json()['telegram_chat_id']==payload['telegram_chat_id']
        assert client.patch(url,json={'telegram_chat_id':'-100000999','enabled':False},headers=headers()).status_code==200


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

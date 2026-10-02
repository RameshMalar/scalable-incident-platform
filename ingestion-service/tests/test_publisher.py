import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.servicebus import TransportType
from azure.servicebus.exceptions import ServiceBusError
from common_event_model import IncidentEvent

from app.publisher import MockEventPublisher, PublishError, ServiceBusEventPublisher

pytestmark = pytest.mark.asyncio

NAMESPACE = "incident-bus-test.servicebus.windows.net"


def sample_event(**overrides):
    fields = {
        "event_id": "EVT-TEST0001",
        "tenant_id": "TENANT-01",
        "site_id": "SITE-01",
        "source_id": "CAM-001",
        "source_name": "Test Camera",
        "source_type": "video_analytics",
        "analytics_type": "intrusion_detection",
        "event_type": "intrusion_detected",
        "severity": "high",
        "timestamp": datetime(2026, 8, 21, 10, 0, 0),
        "confidence": 0.95,
        "description": "Person detected inside restricted area",
        "evidence_uri": "/evidence/test.jpg",
    }
    fields.update(overrides)
    return IncidentEvent(**fields)


def fake_credential(token_expires_in=3600, get_token=None):
    credential = MagicMock()
    credential.close = AsyncMock()
    token = MagicMock()
    token.expires_on = time.time() + token_expires_in
    credential.get_token = get_token or AsyncMock(return_value=token)
    return credential


def wire_client(mock_client_cls, sender):
    """Point the patched ServiceBusClient at `sender` and hand back the client."""
    client = MagicMock()
    client.get_queue_sender.return_value = sender
    client.close = AsyncMock()
    mock_client_cls.return_value = client
    return client


def fake_sender(send_messages=None):
    sender = MagicMock()
    sender.send_messages = send_messages or AsyncMock()
    sender.close = AsyncMock()
    return sender


def build_publisher(**overrides):
    kwargs = {
        "fully_qualified_namespace": NAMESPACE,
        "queue_name": "incident-events",
        "credential": fake_credential(),
    }
    kwargs.update(overrides)
    return ServiceBusEventPublisher(**kwargs)


async def test_mock_publisher_records_published_events():
    publisher = MockEventPublisher()
    event = sample_event()

    await publisher.publish(event)

    assert publisher.published_events == [event]


async def test_mock_publisher_raises_publish_error_when_configured_to_fail():
    publisher = MockEventPublisher(fail=True)
    event = sample_event()

    with pytest.raises(PublishError):
        await publisher.publish(event)

    assert publisher.published_events == []


@patch("app.publisher.ServiceBusClient")
async def test_service_bus_publisher_sends_to_a_queue_not_a_topic(mock_client_cls):
    # The namespace provisions a queue, `incident-events`. A topic of that name
    # does not exist, so publishing to one would fail against real infrastructure.
    sender = fake_sender()
    client = wire_client(mock_client_cls, sender)

    publisher = build_publisher(timeout_seconds=2.5)
    await publisher.publish(sample_event())

    client.get_queue_sender.assert_called_once_with(queue_name="incident-events")
    assert not client.get_topic_sender.called
    sender.send_messages.assert_called_once()


@patch("app.publisher.ServiceBusClient")
async def test_service_bus_publisher_authenticates_with_a_credential(mock_client_cls):
    # Local auth is disabled on the namespace, so there is no connection string
    # to fall back to -- the client must be constructed with a credential.
    sender = fake_sender()
    wire_client(mock_client_cls, sender)
    credential = fake_credential()

    publisher = build_publisher(credential=credential)
    await publisher.publish(sample_event())

    _, kwargs = mock_client_cls.call_args
    assert kwargs["fully_qualified_namespace"] == NAMESPACE
    assert kwargs["credential"] is credential
    assert not mock_client_cls.from_connection_string.called


@patch("app.publisher.ServiceBusClient")
async def test_service_bus_publisher_carries_the_event_id_as_message_id(
    mock_client_cls,
):
    sender = fake_sender()
    wire_client(mock_client_cls, sender)

    publisher = build_publisher()
    await publisher.publish(sample_event())

    message = sender.send_messages.call_args.args[0]
    assert message.message_id == "EVT-TEST0001"
    assert message.content_type == "application/json"


@patch("app.publisher.ServiceBusClient")
async def test_service_bus_publisher_reuses_one_sender_across_messages(
    mock_client_cls,
):
    # One AMQP handshake per request would dominate the latency and throughput
    # numbers the platform is measured on.
    sender = fake_sender()
    client = wire_client(mock_client_cls, sender)

    publisher = build_publisher()
    await publisher.publish(sample_event(event_id="EVT-1"))
    await publisher.publish(sample_event(event_id="EVT-2"))
    await publisher.publish(sample_event(event_id="EVT-3"))

    assert mock_client_cls.call_count == 1
    client.get_queue_sender.assert_called_once()
    assert sender.send_messages.call_count == 3


# --- concurrency -----------------------------------------------------------
#
# An AMQP link is a state machine, and a sender is not safe to use from two
# coroutines at once: concurrent sends interleave frames on it, the SDK
# detaches the link mid-send and shuts the handler down, and every request
# after that fails against a half-dismantled object. Against the deployed
# service this failed 94 of 100 requests at five incidents a second, while
# every check that published one at a time had been passing for weeks. These
# tests are what stop a future refactor from quietly going back to one sender.


def tracking_client(mock_client_cls, send=None):
    """A client whose senders record whether two sends ever overlap."""
    senders = []
    overlaps = []

    def build_sender(queue_name=None):
        sender = MagicMock()
        busy = []

        async def send_messages(message):
            if busy:
                overlaps.append(sender)
            busy.append(message)
            try:
                if send is not None:
                    await send(message)
                else:
                    await asyncio.sleep(0.01)
            finally:
                busy.pop()

        sender.send_messages = send_messages
        sender.close = AsyncMock()
        senders.append(sender)
        return sender

    client = MagicMock()
    client.get_queue_sender.side_effect = build_sender
    client.close = AsyncMock()
    mock_client_cls.return_value = client
    return senders, overlaps


@patch("app.publisher.ServiceBusClient")
async def test_a_sender_is_never_used_by_two_incidents_at_once(mock_client_cls):
    senders, overlaps = tracking_client(mock_client_cls)

    publisher = build_publisher(pool_size=4)
    await asyncio.gather(
        *(publisher.publish(sample_event(event_id=f"EVT-{i:04d}")) for i in range(20))
    )

    assert overlaps == []


@patch("app.publisher.ServiceBusClient")
async def test_concurrency_is_bounded_by_the_pool_rather_than_the_traffic(
    mock_client_cls,
):
    # Twenty incidents at once must not open twenty links. Each is a
    # connection to the namespace, and a replica under load would otherwise
    # open one per in-flight request.
    senders, _ = tracking_client(mock_client_cls)

    publisher = build_publisher(pool_size=4)
    await asyncio.gather(
        *(publisher.publish(sample_event(event_id=f"EVT-{i:04d}")) for i in range(20))
    )

    assert len(senders) == 4


@patch("app.publisher.ServiceBusClient")
async def test_a_replica_that_sees_no_concurrency_opens_no_extra_links(
    mock_client_cls,
):
    senders, _ = tracking_client(mock_client_cls)

    publisher = build_publisher(pool_size=8)
    for index in range(5):
        await publisher.publish(sample_event(event_id=f"EVT-{index:04d}"))

    assert len(senders) == 1


@patch("app.publisher.ServiceBusClient")
async def test_a_sender_that_failed_is_discarded_not_handed_to_the_next_caller(
    mock_client_cls,
):
    # Whatever broke the link is still broken for the next request, and the
    # SDK's own recovery is what produced the AttributeError seen in
    # production.
    senders, _ = tracking_client(mock_client_cls)
    failing = AsyncMock(side_effect=ServiceBusError("link detached"))

    publisher = build_publisher(pool_size=2)
    await publisher.start()
    senders[0].send_messages = failing

    with pytest.raises(PublishError):
        await publisher.publish(sample_event())

    senders[0].close.assert_awaited_once()

    await publisher.publish(sample_event(event_id="EVT-AFTER"))

    assert len(senders) == 2
    assert failing.await_count == 1


@patch("app.publisher.ServiceBusClient")
async def test_repeated_failures_do_not_drain_the_pool(mock_client_cls):
    # A slot returned empty is a slot that refills. Returning nothing at all
    # would shrink the pool on every failure until the replica accepted no
    # incidents and reported nothing wrong.
    tracking_client(mock_client_cls, send=AsyncMock(side_effect=ServiceBusError("down")))

    publisher = build_publisher(pool_size=2)

    for index in range(6):
        with pytest.raises(PublishError):
            await publisher.publish(sample_event(event_id=f"EVT-{index:04d}"))

    assert publisher._pool.qsize() == 2


@patch("app.publisher.ServiceBusClient")
async def test_a_cancelled_request_gives_its_slot_back(mock_client_cls):
    # A client that hangs up mid-publish cancels the handler. The link it was
    # using is half-written and goes, but the capacity it held must not.
    async def never(message):
        await asyncio.sleep(10)

    tracking_client(mock_client_cls, send=never)

    publisher = build_publisher(pool_size=2)
    await publisher.start()

    task = asyncio.create_task(publisher.publish(sample_event()))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert publisher._pool.qsize() == 2


@patch("app.publisher.ServiceBusClient")
async def test_a_timed_out_send_does_not_poison_the_next_one(mock_client_cls):
    # The timeout cancels a send that is part-way onto the wire, which is
    # itself enough to leave the link unusable.
    slow = True

    async def send(message):
        if slow:
            await asyncio.sleep(10)

    senders, _ = tracking_client(mock_client_cls, send=send)

    publisher = build_publisher(pool_size=2, timeout_seconds=0.05)
    await publisher.start()

    with pytest.raises(PublishError):
        await publisher.publish(sample_event())

    senders[0].close.assert_awaited_once()

    slow = False
    await publisher.publish(sample_event(event_id="EVT-AFTER"))


async def test_a_pool_of_no_senders_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        ServiceBusEventPublisher(fully_qualified_namespace=NAMESPACE, pool_size=-1)


@patch("app.publisher.ServiceBusClient")
async def test_closing_releases_every_sender_in_the_pool(mock_client_cls):
    senders, _ = tracking_client(mock_client_cls)

    publisher = build_publisher(pool_size=4)
    await asyncio.gather(
        *(publisher.publish(sample_event(event_id=f"EVT-{i:04d}")) for i in range(8))
    )
    await publisher.close()

    assert len(senders) == 4
    for sender in senders:
        sender.close.assert_awaited_once()


@patch("app.publisher.ServiceBusClient")
async def test_start_opens_the_sender_before_the_first_incident(mock_client_cls):
    sender = fake_sender()
    client = wire_client(mock_client_cls, sender)

    publisher = build_publisher()
    await publisher.start()

    client.get_queue_sender.assert_called_once_with(queue_name="incident-events")

    await publisher.publish(sample_event())
    assert mock_client_cls.call_count == 1


@patch("app.publisher.ServiceBusClient")
async def test_close_releases_the_sender_client_and_owned_credential(mock_client_cls):
    sender = fake_sender()
    client = wire_client(mock_client_cls, sender)
    credential = fake_credential()

    publisher = build_publisher(credential=credential)
    await publisher.start()
    await publisher.close()

    sender.close.assert_awaited_once()
    client.close.assert_awaited_once()
    # The credential was supplied by the caller, so it is not ours to close.
    assert not credential.close.await_count


@patch("app.publisher.ServiceBusClient")
async def test_service_bus_publisher_wraps_send_failures_as_publish_error(
    mock_client_cls,
):
    sender = fake_sender(
        send_messages=AsyncMock(side_effect=ServiceBusError("upstream failure"))
    )
    wire_client(mock_client_cls, sender)

    publisher = build_publisher()

    with pytest.raises(PublishError):
        await publisher.publish(sample_event())


@patch("app.publisher.ServiceBusClient")
async def test_service_bus_publisher_wraps_timeout_as_publish_error(mock_client_cls):
    async def slow_send(*args, **kwargs):
        await asyncio.sleep(10)

    sender = fake_sender(send_messages=slow_send)
    wire_client(mock_client_cls, sender)

    publisher = build_publisher(timeout_seconds=0.05)

    with pytest.raises(PublishError):
        await publisher.publish(sample_event())


async def test_service_bus_publisher_requires_a_namespace(monkeypatch):
    monkeypatch.delenv("SERVICE_BUS_FQDN", raising=False)

    with pytest.raises(ValueError):
        ServiceBusEventPublisher(fully_qualified_namespace=None)


@patch("app.publisher.ServiceBusClient")
async def test_transport_defaults_to_websocket_and_is_switchable(
    mock_client_cls, monkeypatch
):
    # Native AMQP needs 5671, which firewalls block; the deployed service sets
    # SERVICE_BUS_TRANSPORT=amqp to opt back into it.
    monkeypatch.delenv("SERVICE_BUS_TRANSPORT", raising=False)
    wire_client(mock_client_cls, fake_sender())
    await build_publisher().start()
    assert mock_client_cls.call_args.kwargs["transport_type"] == (
        TransportType.AmqpOverWebsocket
    )

    mock_client_cls.reset_mock()
    monkeypatch.setenv("SERVICE_BUS_TRANSPORT", "amqp")
    wire_client(mock_client_cls, fake_sender())
    await build_publisher().start()
    assert mock_client_cls.call_args.kwargs["transport_type"] == TransportType.Amqp


# --- readiness -------------------------------------------------------------
#
# The probe exists because "the sender object exists" proves nothing: building
# a client and sender does no network I/O, so a process aimed at a namespace
# that does not exist starts cleanly and reports itself healthy.


async def test_mock_publisher_is_always_ready():
    ready, detail = await MockEventPublisher().ready()

    assert ready is True
    assert detail == "ready"


@patch("app.publisher.ServiceBusClient")
async def test_ready_asks_the_credential_for_a_service_bus_token(mock_client_cls):
    wire_client(mock_client_cls, fake_sender())
    credential = fake_credential()

    publisher = build_publisher(credential=credential)
    ready, detail = await publisher.ready()

    assert ready is True
    assert detail == "ready"
    credential.get_token.assert_awaited_once_with(
        "https://servicebus.azure.net/.default"
    )


@patch("app.publisher.ServiceBusClient")
async def test_ready_is_false_when_no_token_can_be_minted(mock_client_cls):
    wire_client(mock_client_cls, fake_sender())
    credential = fake_credential(
        get_token=AsyncMock(side_effect=RuntimeError("no managed identity"))
    )

    ready, detail = await build_publisher(credential=credential).ready()

    assert ready is False
    assert "no managed identity" in detail


@patch("app.publisher.ServiceBusClient")
async def test_ready_is_false_rather_than_hanging_when_the_token_stalls(
    mock_client_cls,
):
    async def never(*args, **kwargs):
        await asyncio.sleep(10)

    wire_client(mock_client_cls, fake_sender())
    publisher = build_publisher(
        credential=fake_credential(get_token=never), readiness_timeout_seconds=0.05
    )

    ready, detail = await publisher.ready()

    assert ready is False
    assert "timed out" in detail


@patch("app.publisher.ServiceBusClient")
async def test_ready_reuses_a_live_token_instead_of_asking_every_probe(
    mock_client_cls,
):
    # The probe runs every ten seconds; it must not become a token request
    # every ten seconds.
    wire_client(mock_client_cls, fake_sender())
    credential = fake_credential()
    publisher = build_publisher(credential=credential)

    for _ in range(5):
        assert (await publisher.ready())[0] is True

    assert credential.get_token.await_count == 1


@patch("app.publisher.ServiceBusClient")
async def test_a_token_near_expiry_is_refreshed(mock_client_cls):
    wire_client(mock_client_cls, fake_sender())
    # Inside the five-minute refresh margin, so the cache must not be trusted.
    credential = fake_credential(token_expires_in=60)
    publisher = build_publisher(credential=credential)

    await publisher.ready()
    await publisher.ready()

    assert credential.get_token.await_count == 2


@patch("app.publisher.ServiceBusClient")
async def test_closing_forgets_the_cached_token(mock_client_cls):
    wire_client(mock_client_cls, fake_sender())
    publisher = build_publisher(credential=fake_credential())

    await publisher.ready()
    await publisher.close()

    assert publisher._token_expires_at == 0.0


@patch("app.publisher.ServiceBusClient")
async def test_readiness_detail_stays_short_enough_to_log_every_ten_seconds(
    mock_client_cls,
):
    # A failed DefaultAzureCredential explains itself across every credential it
    # tried -- roughly two kilobytes. Returned and logged every ten seconds,
    # that is a Log Analytics bill for one repeated message.
    sprawling = "no managed identity\n" + "\n".join(
        f"\tAttempted {name}Credential: unavailable for a long-winded reason"
        for name in ("Environment", "WorkloadIdentity", "ManagedIdentity", "AzureCli")
    )
    wire_client(mock_client_cls, fake_sender())
    credential = fake_credential(get_token=AsyncMock(side_effect=RuntimeError(sprawling)))

    ready, detail = await build_publisher(credential=credential).ready()

    assert ready is False
    assert len(detail) < 260
    assert "no managed identity" in detail
    assert "Attempted" not in detail


@patch("app.publisher.ServiceBusClient")
async def test_a_replica_that_stays_unready_logs_once_not_every_probe(
    mock_client_cls, caplog
):
    wire_client(mock_client_cls, fake_sender())
    credential = fake_credential(get_token=AsyncMock(side_effect=RuntimeError("down")))
    publisher = build_publisher(credential=credential)

    with caplog.at_level("WARNING", logger="app.publisher"):
        for _ in range(4):
            await publisher.ready()

    assert len([r for r in caplog.records if "not ready" in r.message]) == 1

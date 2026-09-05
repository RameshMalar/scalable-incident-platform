import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.servicebus import TransportType
from azure.servicebus.exceptions import ServiceBusError

from app.consumer import IncidentConsumer
from app.processor import IncidentProcessor, Outcome
from tests.test_processor import RecordingHandler, sample_body

pytestmark = pytest.mark.asyncio

NAMESPACE = "incident-bus-test.servicebus.windows.net"


def fake_credential():
    credential = MagicMock()
    credential.close = AsyncMock()
    return credential


def fake_receiver():
    receiver = MagicMock()
    receiver.receive_messages = AsyncMock(return_value=[])
    receiver.complete_message = AsyncMock()
    receiver.abandon_message = AsyncMock()
    receiver.dead_letter_message = AsyncMock()
    receiver.close = AsyncMock()
    return receiver


def fake_message(body: bytes, message_id: str = "EVT-TEST0001", delivery_count: int = 1):
    """A received message whose body is a generator, as the SDK returns it."""
    message = MagicMock()
    message.message_id = message_id
    message.body = iter([body])
    message.delivery_count = delivery_count
    return message


def build_consumer(handler=None, receiver=None):
    consumer = IncidentConsumer(
        processor=IncidentProcessor(handler=handler or RecordingHandler()),
        fully_qualified_namespace=NAMESPACE,
        queue_name="incident-events",
        credential=fake_credential(),
        max_wait_time=0.01,
    )
    consumer._receiver = receiver or fake_receiver()
    return consumer


def wire_client(mock_client_cls, receiver):
    client = MagicMock()
    client.get_queue_receiver.return_value = receiver
    client.close = AsyncMock()
    mock_client_cls.return_value = client
    return client


async def test_a_processed_incident_is_completed():
    receiver = fake_receiver()
    consumer = build_consumer(receiver=receiver)

    outcome = await consumer.settle(fake_message(sample_body()))

    assert outcome is Outcome.PROCESSED
    receiver.complete_message.assert_awaited_once()
    assert not receiver.abandon_message.await_count
    assert not receiver.dead_letter_message.await_count


async def test_a_duplicate_is_completed_rather_than_redelivered():
    # The incident is already done with. Abandoning it would send it round the
    # loop five more times and then dead-letter a message nothing is wrong with.
    receiver = fake_receiver()
    consumer = build_consumer(receiver=receiver)

    await consumer.settle(fake_message(sample_body()))
    outcome = await consumer.settle(fake_message(sample_body()))

    assert outcome is Outcome.DUPLICATE
    assert receiver.complete_message.await_count == 2
    assert not receiver.dead_letter_message.await_count


async def test_an_unprocessable_message_is_dead_lettered_immediately():
    # Retrying malformed JSON five times only delays the inevitable while
    # holding a delivery slot the rest of the queue could use.
    receiver = fake_receiver()
    consumer = build_consumer(receiver=receiver)

    await consumer.settle(fake_message(b"not json at all", message_id="EVT-BAD"))

    receiver.dead_letter_message.assert_awaited_once()
    assert receiver.dead_letter_message.await_args.kwargs["reason"] == "invalid-incident"
    assert not receiver.complete_message.await_count
    assert not receiver.abandon_message.await_count


async def test_the_dead_letter_description_stays_within_the_broker_limit():
    # Service Bus rejects a description over 4096 characters, and a rejected
    # settlement turns an unprocessable message into a stuck one.
    receiver = fake_receiver()
    consumer = build_consumer(receiver=receiver)

    await consumer.settle(fake_message(b'{"event_id": ' + b'"x"' * 9000 + b"}"))

    description = receiver.dead_letter_message.await_args.kwargs["error_description"]
    assert len(description) <= 4096


async def test_a_transient_failure_is_abandoned_for_redelivery():
    # Abandoning is what lets the queue's maxDeliveryCount do its job: five
    # transient failures and Service Bus dead-letters the message itself.
    receiver = fake_receiver()
    consumer = build_consumer(handler=RecordingHandler(fail=True), receiver=receiver)

    await consumer.settle(fake_message(sample_body()))

    receiver.abandon_message.assert_awaited_once()
    assert not receiver.complete_message.await_count
    assert not receiver.dead_letter_message.await_count


async def test_a_lock_lost_while_settling_does_not_kill_the_worker():
    # The broker will redeliver the message. Letting the error escape would
    # take down the receive loop, and with it every other message in flight.
    receiver = fake_receiver()
    receiver.complete_message = AsyncMock(side_effect=ServiceBusError("lock lost"))
    consumer = build_consumer(receiver=receiver)

    outcome = await consumer.settle(fake_message(sample_body()))

    assert outcome is Outcome.PROCESSED


async def test_run_settles_the_batch_it_already_holds_before_stopping():
    # Those messages are locked. Dropping them on shutdown means waiting out
    # the lock before anything else can have them.
    stop = asyncio.Event()
    receiver = fake_receiver()
    receives = []

    async def receive(**kwargs):
        receives.append(kwargs)
        stop.set()
        return [
            fake_message(sample_body(event_id="EVT-1"), "EVT-1"),
            fake_message(sample_body(event_id="EVT-2"), "EVT-2"),
        ]

    receiver.receive_messages = receive
    consumer = build_consumer(receiver=receiver)

    await consumer.run(stop)

    assert len(receives) == 1
    assert receiver.complete_message.await_count == 2


async def test_run_survives_a_broker_error_instead_of_exiting():
    stop = asyncio.Event()
    receiver = fake_receiver()
    attempts = []

    async def receive(**kwargs):
        attempts.append(kwargs)
        stop.set()
        raise ServiceBusError("broker unavailable")

    receiver.receive_messages = receive
    consumer = build_consumer(receiver=receiver)

    await consumer.run(stop)

    assert len(attempts) == 1


async def test_run_receives_in_batches():
    stop = asyncio.Event()
    receiver = fake_receiver()
    receives = []

    async def receive(**kwargs):
        receives.append(kwargs)
        stop.set()
        return []

    receiver.receive_messages = receive
    consumer = IncidentConsumer(
        fully_qualified_namespace=NAMESPACE,
        credential=fake_credential(),
        max_message_count=7,
        max_wait_time=0.01,
    )
    consumer._receiver = receiver

    await consumer.run(stop)

    assert receives[0]["max_message_count"] == 7


@patch("app.consumer.ServiceBusClient")
async def test_the_consumer_reads_from_a_queue_with_a_credential(mock_client_cls):
    # Local auth is disabled on the namespace, so there is no connection string
    # to fall back to -- the client must be constructed with a credential.
    receiver = fake_receiver()
    client = wire_client(mock_client_cls, receiver)
    credential = fake_credential()

    consumer = IncidentConsumer(
        fully_qualified_namespace=NAMESPACE,
        queue_name="incident-events",
        credential=credential,
    )
    await consumer.start()

    client.get_queue_receiver.assert_called_once_with(queue_name="incident-events")
    assert mock_client_cls.call_args.kwargs["credential"] is credential
    assert not mock_client_cls.from_connection_string.called


@patch("app.consumer.ServiceBusClient")
async def test_the_consumer_reuses_one_receiver(mock_client_cls):
    receiver = fake_receiver()
    client = wire_client(mock_client_cls, receiver)

    consumer = IncidentConsumer(
        fully_qualified_namespace=NAMESPACE, credential=fake_credential()
    )
    await consumer.start()
    await consumer.start()

    assert mock_client_cls.call_count == 1
    client.get_queue_receiver.assert_called_once()


@patch("app.consumer.ServiceBusClient")
async def test_close_releases_the_receiver_client_and_owned_credential(mock_client_cls):
    receiver = fake_receiver()
    client = wire_client(mock_client_cls, receiver)
    credential = fake_credential()

    consumer = IncidentConsumer(
        fully_qualified_namespace=NAMESPACE, credential=credential
    )
    await consumer.start()
    await consumer.close()

    receiver.close.assert_awaited_once()
    client.close.assert_awaited_once()
    # The credential was supplied by the caller, so it is not ours to close.
    assert not credential.close.await_count


async def test_the_consumer_requires_a_namespace(monkeypatch):
    monkeypatch.delenv("SERVICE_BUS_FQDN", raising=False)

    with pytest.raises(ValueError):
        IncidentConsumer(fully_qualified_namespace=None)


@patch("app.consumer.ServiceBusClient")
async def test_transport_defaults_to_websocket_and_is_switchable(
    mock_client_cls, monkeypatch
):
    # Native AMQP needs 5671, which firewalls block; the deployed worker sets
    # SERVICE_BUS_TRANSPORT=amqp to opt back into it.
    monkeypatch.delenv("SERVICE_BUS_TRANSPORT", raising=False)
    wire_client(mock_client_cls, fake_receiver())
    await IncidentConsumer(
        fully_qualified_namespace=NAMESPACE, credential=fake_credential()
    ).start()
    assert mock_client_cls.call_args.kwargs["transport_type"] == (
        TransportType.AmqpOverWebsocket
    )

    mock_client_cls.reset_mock()
    monkeypatch.setenv("SERVICE_BUS_TRANSPORT", "amqp")
    wire_client(mock_client_cls, fake_receiver())
    await IncidentConsumer(
        fully_qualified_namespace=NAMESPACE, credential=fake_credential()
    ).start()
    assert mock_client_cls.call_args.kwargs["transport_type"] == TransportType.Amqp

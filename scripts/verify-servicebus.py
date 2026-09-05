#!/usr/bin/env python3
"""Self-contained smoke test for the incident-events queue and its dead-letter
subqueue.

It needs neither the ingestion service nor the processing worker. It plays both
sides itself: publish one message, confirm it lands in the queue, then
deliberately dead-letter it and confirm it lands in the dead-letter subqueue.
That was how the two paths this queue is responsible for -- "reaches the queue"
and "reaches the DLQ" -- were proven before either service existed, and it
remains the way to test the queue in isolation when a service is misbehaving and
the question is which side the fault is on.

Auth is the same managed-identity story as the running services: no connection
string, no SAS key. Locally that resolves to your `az login`; in a container it
resolves to the assigned identity.

Usage:
    az login                       # once, locally
    pip install -r scripts/requirements.txt
    export SERVICE_BUS_FQDN=incident-bus-xxxx.servicebus.windows.net
    python scripts/verify-servicebus.py

Discover the host from a deployed environment with:
    az deployment group show -g "$RESOURCE_GROUP" -n core-<sha> \
      --query properties.outputs.serviceBusFqdn.value -o tsv
"""

import os
import sys
import uuid

from azure.identity import DefaultAzureCredential
from azure.servicebus import (
    ServiceBusClient,
    ServiceBusMessage,
    ServiceBusSubQueue,
    TransportType,
)


QUEUE = os.environ.get("SERVICE_BUS_QUEUE", "incident-events")

# Plain AMQP needs port 5671, which campus and corporate firewalls routinely
# block -- the symptom is a connection timeout with amqp:socket-error. AMQP over
# WebSocket tunnels through 443 instead, so it is the safe default for a script
# meant to run from anyone's laptop. Set SERVICE_BUS_TRANSPORT=amqp to force the
# native port where the network allows it.
TRANSPORT = (
    TransportType.Amqp
    if os.environ.get("SERVICE_BUS_TRANSPORT", "").lower() == "amqp"
    else TransportType.AmqpOverWebsocket
)


def main() -> int:
    fqdn = os.environ.get("SERVICE_BUS_FQDN")
    if not fqdn:
        print(
            "SERVICE_BUS_FQDN is not set. Export the namespace host, e.g.\n"
            "  export SERVICE_BUS_FQDN=incident-bus-xxxx.servicebus.windows.net",
            file=sys.stderr,
        )
        return 2

    marker = uuid.uuid4().hex
    credential = DefaultAzureCredential()

    with ServiceBusClient(fqdn, credential, transport_type=TRANSPORT) as client:
        # 1. Publish a uniquely tagged test message.
        with client.get_queue_sender(QUEUE) as sender:
            sender.send_messages(
                ServiceBusMessage(
                    f'{{"test":"smoke","marker":"{marker}"}}',
                    content_type="application/json",
                    application_properties={"marker": marker},
                )
            )
        print(f"[1/3] published test message  marker={marker}  queue={QUEUE}")

        # 2. Receive it from the main queue -- proof it reached the queue -- then
        #    dead-letter it by hand to stand in for a worker that gives up.
        with client.get_queue_receiver(QUEUE, max_wait_time=30) as receiver:
            got = receiver.receive_messages(max_message_count=1, max_wait_time=30)
            if not got:
                print(
                    "[2/3] FAILED: nothing arrived in the queue within 30s",
                    file=sys.stderr,
                )
                return 1
            msg = got[0]
            print(f"[2/3] received from queue      body={str(msg)}")
            receiver.dead_letter_message(
                msg,
                reason="smoke-test",
                error_description="deliberately dead-lettered by verify-servicebus.py",
            )
            print("      dead-lettered it on purpose (simulated processing failure)")

        # 3. Read the dead-letter subqueue and confirm our message is the one
        #    sitting there.
        with client.get_queue_receiver(
            QUEUE, sub_queue=ServiceBusSubQueue.DEAD_LETTER, max_wait_time=30
        ) as dlq:
            dead = dlq.receive_messages(max_message_count=5, max_wait_time=30)
            match = next(
                (m for m in dead if m.application_properties.get(b"marker") == marker.encode()),
                None,
            )
            if match is None:
                print(
                    "[3/3] FAILED: message did not reach the dead-letter subqueue",
                    file=sys.stderr,
                )
                return 1
            reason = match.dead_letter_reason
            print(f"[3/3] found in DLQ             reason={reason}")
            dlq.complete_message(match)  # tidy up so reruns start clean

    print("\nOK: publish -> queue -> dead-letter -> DLQ all verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

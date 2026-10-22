"""Send-time recheck of a routed envelope against current subscription state.

The Worker decided eligibility when it published the envelope, but a message
can wait in the queue (or be retried) after the subscriber unsubscribed or
lost access. With NOTIFICATION_ELIGIBILITY=enforce, every routed send first
re-reads, from the subscriptions container:

  * the subscription itself: it must still exist, be enabled, and still target
    this tenant, event type and chat;
  * for an account-owned subscription, the same owner rule as the Worker
    (worker-service/app/owner_eligibility.py): active access that still
    assigns the tenant, and a verified binding to this chat.

Ineligible -> the message completes without sending. A read failure raises, so
the consumer abandons the message and Service Bus retries, then dead-letters;
an outage never turns into a send. Legacy flat messages are not affected.

The default, off, keeps today's behaviour. Enforcing needs the Notification
identity to hold Cosmos DB Built-in Data Reader on the subscriptions container,
which it does not have yet.

A pre-send check narrows but cannot close the window for a send already in
flight when the unsubscribe lands.
"""

import logging
import os

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential

logger = logging.getLogger("app.delivery_eligibility")

ACCESS_PARTITION = "__platform_access__"
ROLES = frozenset({"Platform Admin", "SOC Operator", "SOC Viewer"})


def access_record_allows(record, owner_object_id, tenant_id):
    return (isinstance(record, dict) and record.get("record_type") == "platform_access"
            and record.get("object_id") == owner_object_id and record.get("status") == "active"
            and record.get("role") in ROLES and isinstance(record.get("tenants"), list)
            and tenant_id in record["tenants"])


def binding_allows(record, owner_object_id, chat_id):
    return (isinstance(record, dict) and record.get("record_type") == "telegram_owner"
            and record.get("object_id") == owner_object_id and record.get("chat_id") == chat_id)


def subscription_allows(record, envelope):
    incident = envelope.incident
    return (isinstance(record, dict) and record.get("id") == envelope.subscription_id
            and record.get("tenant_id") == incident.tenant_id and record.get("channel") == "telegram"
            and record.get("enabled") is True and record.get("event_type") == incident.event_type
            and record.get("telegram_chat_id") == envelope.telegram_chat_id)


class DeliveryEligibility:
    """Rule over a reader with read(partition, id) -> dict | None."""

    def __init__(self, reader):
        self.reader = reader

    async def eligible(self, envelope) -> bool:
        tenant_id = envelope.incident.tenant_id
        subscription = await self.reader.read(tenant_id, envelope.subscription_id)
        if not subscription_allows(subscription, envelope):
            return False
        owner = subscription.get("owner_object_id")
        if owner is None:
            return True
        if not access_record_allows(await self.reader.read(ACCESS_PARTITION, "access:" + owner), owner, tenant_id):
            return False
        return binding_allows(await self.reader.read(ACCESS_PARTITION, "telegram-owner:" + owner), owner, envelope.telegram_chat_id)

    async def close(self):
        await self.reader.close()


class CosmosRecordReader:
    def __init__(self, endpoint=None, database=None, container=None, credential=None, client_factory=None):
        self.endpoint = endpoint or os.environ.get("COSMOS_ENDPOINT")
        if not self.endpoint:
            raise ValueError("COSMOS_ENDPOINT is required when NOTIFICATION_ELIGIBILITY=enforce")
        self.database = database or os.environ.get("COSMOS_DATABASE", "incidents")
        self.container_name = container or os.environ.get("COSMOS_SUBSCRIPTIONS_CONTAINER", "subscriptions")
        self.credential = credential
        self.owns_credential = credential is None
        self.client_factory = client_factory or CosmosClient
        self.client = None
        self.container = None

    async def read(self, partition, document_id):
        if self.container is None:
            self.credential = self.credential or DefaultAzureCredential()
            self.client = self.client_factory(self.endpoint, credential=self.credential)
            self.container = self.client.get_database_client(self.database).get_container_client(self.container_name)
        try:
            return await self.container.read_item(item=document_id, partition_key=partition)
        except CosmosResourceNotFoundError:
            # A deleted container and a deleted item raise the same error;
            # only a confirmed container lets "absent" mean "unsubscribed".
            metadata = await self.container.read()
            if metadata.get("partitionKey", {}).get("paths") != ["/tenant_id"]:
                raise
            return None

    async def close(self):
        try:
            if self.client is not None:
                await self.client.close()
        finally:
            self.client = self.container = None
            if self.owns_credential and self.credential is not None:
                await self.credential.close()
                self.credential = None


def build_eligibility():
    mode = os.environ.get("NOTIFICATION_ELIGIBILITY", "off").lower()
    if mode == "off":
        return None
    if mode != "enforce":
        raise ValueError("NOTIFICATION_ELIGIBILITY must be off or enforce")
    return DeliveryEligibility(CosmosRecordReader())

"""Persists incidents in Cosmos DB.

Authentication is the same managed-identity story as the queue: `core.bicep`
sets `disableLocalAuth: true` on the account, so there are no keys and the only
way in is a credential holding the Cosmos **Data Contributor** role. That role
is granted to the worker identity alone -- ingestion publishes to the queue and
never touches the store, which keeps the write path single-writer.
"""

import asyncio
import logging
import os

from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceExistsError
from azure.identity.aio import DefaultAzureCredential
from common_event_model import IncidentEvent

from app.processor import IncidentHandler, Outcome

logger = logging.getLogger("app.store")


class CosmosIncidentStore(IncidentHandler):
    """Writes each incident once, and knows when it has seen one before.

    The document id is the producer's `event_id`, so uniqueness is the store's
    own constraint rather than something the worker has to remember. That is
    what retires the in-memory deduplicator: a redelivery -- after a lock
    expiry, a crash, or a scale-in -- collides no matter which replica handles
    it, where a per-process set only caught the ones that came back to the same
    replica.

    It creates rather than upserts, deliberately. An upsert would succeed on a
    repeat and overwrite the stored incident, so duplicates would be invisible
    and the DUPLICATE outcome could never be reported. Creating and catching the
    conflict makes the duplicate an observable fact. Incidents are immutable
    events, so there is nothing an update would legitimately change.

    Partitioned on `tenant_id`, which is how incidents are read back: a customer
    looks at their own sites.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        database_name: str | None = None,
        container_name: str | None = None,
        credential=None,
    ):
        self.endpoint = endpoint or os.environ.get("COSMOS_ENDPOINT")
        self.database_name = database_name or os.environ.get(
            "COSMOS_DATABASE", "incidents"
        )
        self.container_name = container_name or os.environ.get(
            "COSMOS_CONTAINER", "incidents"
        )

        if not self.endpoint:
            raise ValueError(
                "COSMOS_ENDPOINT is required for CosmosIncidentStore, e.g. "
                "https://incident-cosmos-xxxx.documents.azure.com:443/"
            )

        self._credential = credential
        self._owns_credential = credential is None
        self._client: CosmosClient | None = None
        self._container = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            self._open()

    def _open(self) -> None:
        if self._container is not None:
            return

        if self._credential is None:
            self._credential = DefaultAzureCredential()

        self._client = CosmosClient(self.endpoint, credential=self._credential)
        self._container = self._client.get_database_client(
            self.database_name
        ).get_container_client(self.container_name)

        logger.info(
            "cosmos store ready endpoint=%s database=%s container=%s",
            self.endpoint,
            self.database_name,
            self.container_name,
        )

    async def handle(self, event: IncidentEvent) -> Outcome:
        if self._container is None:
            async with self._lock:
                self._open()

        document = event.model_dump(mode="json")
        # Cosmos keys every document on `id`. Using the producer's event id
        # makes the store's uniqueness constraint the deduplication.
        document["id"] = event.event_id

        try:
            await self._container.create_item(document)
        except CosmosResourceExistsError:
            logger.info(
                "duplicate incident already stored event_id=%s tenant_id=%s",
                event.event_id,
                event.tenant_id,
            )
            return Outcome.DUPLICATE
        except (ClientAuthenticationError, ServiceRequestError) as exc:
            # Transient or credential-shaped: the caller abandons the message
            # and the broker redelivers it. Raising rather than swallowing is
            # what keeps maxDeliveryCount meaningful.
            logger.error(
                "could not store incident event_id=%s: %s", event.event_id, exc
            )
            raise

        logger.info(
            "stored incident event_id=%s tenant_id=%s site_id=%s "
            "analytics_type=%s event_type=%s severity=%s",
            event.event_id,
            event.tenant_id,
            event.site_id,
            event.analytics_type,
            event.event_type,
            event.severity,
        )
        return Outcome.PROCESSED

    async def close(self) -> None:
        async with self._lock:
            self._container = None
            if self._client is not None:
                await self._client.close()
                self._client = None
            if self._credential is not None and self._owns_credential:
                await self._credential.close()
                self._credential = None

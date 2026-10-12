"""Read-only operator destination policy. No write/approval API is exposed."""
import os
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential


class PolicyUnavailable(RuntimeError):
    """Authorization could not be established; callers must not deliver."""


class DenyDestinationPolicy:
    async def approved(self, tenant_id, chat_id):
        return False
    async def close(self):
        pass


class CosmosDestinationPolicy:
    def __init__(self, endpoint=None, database=None, container=None, credential=None, client_factory=None):
        self.endpoint = endpoint or os.environ.get("COSMOS_ENDPOINT")
        self.database = database or os.environ.get("COSMOS_DATABASE", "incidents")
        self.container = container or os.environ.get("COSMOS_DESTINATIONS_CONTAINER", "approved-destinations")
        self.credential = credential
        self.owns_credential = credential is None
        self.client_factory = client_factory or CosmosClient
        self.client = None

    async def approved(self, tenant_id, chat_id):
        try:
            if not self.endpoint:
                raise PolicyUnavailable("destination policy unavailable")
            if self.client is None:
                credential = self.credential or DefaultAzureCredential()
                self.credential = credential
                self.client = self.client_factory(self.endpoint, credential=credential)
            repository = self.client.get_database_client(self.database).get_container_client(self.container)
            await self._verify_container(repository)
            try:
                record = await repository.read_item(item="telegram:" + chat_id, partition_key=tenant_id)
            except CosmosResourceNotFoundError:
                # Item and parent-resource 404s use the same SDK exception.
                # Recheck after the failed read to catch deletion during lookup.
                await self._verify_container(repository)
                return False
        except Exception:
            raise PolicyUnavailable("destination policy unavailable") from None
        if not isinstance(record, dict) or not all(key in record for key in ("id", "tenant_id", "channel", "telegram_chat_id", "enabled")) or type(record["enabled"]) is not bool:
            raise PolicyUnavailable("destination policy unavailable")
        return (record["id"] == "telegram:" + chat_id and record["tenant_id"] == tenant_id
                and record["channel"] == "telegram" and record["telegram_chat_id"] == chat_id
                and record["enabled"] is True)

    async def _verify_container(self, repository):
        metadata = await repository.read()
        if (not isinstance(metadata, dict) or metadata.get("id") != self.container
                or metadata.get("partitionKey", {}).get("paths") != ["/tenant_id"]
                or metadata.get("partitionKey", {}).get("kind") != "Hash"):
            raise PolicyUnavailable("destination policy unavailable")

    async def close(self):
        try:
            if self.client is not None:
                await self.client.close()
        finally:
            self.client = None
            if self.owns_credential and self.credential is not None:
                await self.credential.close()
                self.credential = None

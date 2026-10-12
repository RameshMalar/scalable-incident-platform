"""Read-only evidence prerequisite; never creates routes or sends notifications."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys


def app_env(app):
    containers = app["properties"]["template"]["containers"]
    if len(containers) != 1:
        raise ValueError("unknown workload configuration")
    return {e["name"]: e.get("value") for e in containers[0].get("env", [])}


def verify_tenant(mapping, client_id, tenant):
    entries = [entry.strip().split(":", 1) for entry in mapping.split(",") if entry.strip()]
    if not tenant or any(len(entry) != 2 for entry in entries):
        raise ValueError("invalid tenant configuration")
    values = [value.strip() for key, value in entries if key.strip() == client_id]
    if values != [tenant]:
        raise ValueError("deploy producer must map to the evidence tenant")


async def require_route(reader, policy, tenant):
    records = await reader.find_matches(tenant, "intrusion_detected")
    matches = []
    for record in records:
        if (record.tenant_id == tenant and record.event_type == "intrusion_detected"
                and record.enabled is True and record.channel == "telegram"
                and await policy.approved(tenant, record.telegram_chat_id) is True):
            matches.append(record)
    # One explicit route makes the photo objective unambiguous and avoids fanout.
    if len(matches) != 1:
        raise ValueError("exactly one enabled, approved evidence subscription is required")


def azure_app(name):
    result = subprocess.run(["az", "containerapp", "show", "--resource-group",
                             os.environ["RESOURCE_GROUP"], "--name", name, "-o", "json"],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


async def main():
    # Use the same production validation/read-only adapters as Worker.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker-service"))
    from app.subscription_store import CosmosSubscriptionReader
    from app.destination_policy import CosmosDestinationPolicy
    tenant = os.environ["CD_TENANT_ID"]
    worker = app_env(azure_app("incident-worker"))
    ingestion = app_env(azure_app("incident-ingestion"))
    notification = app_env(azure_app("incident-notification"))
    verify_tenant(ingestion["TENANT_MAP"], os.environ["DEPLOY_CLIENT_ID"], tenant)
    if (worker.get("NOTIFICATION_QUEUE", "notification-events") != notification.get("SERVICE_BUS_QUEUE", "notification-events")
            or worker.get("SERVICE_BUS_FQDN") != notification.get("SERVICE_BUS_FQDN")):
        raise ValueError("notification queue wiring differs")
    reader = CosmosSubscriptionReader(endpoint=worker["COSMOS_ENDPOINT"],
                                     database_name=worker.get("COSMOS_DATABASE", "incidents"),
                                     container_name=worker.get("COSMOS_SUBSCRIPTIONS_CONTAINER", "subscriptions"))
    policy = CosmosDestinationPolicy(endpoint=worker["COSMOS_ENDPOINT"],
                                    database=worker.get("COSMOS_DATABASE", "incidents"),
                                    container=worker.get("COSMOS_DESTINATIONS_CONTAINER", "approved-destinations"))
    try:
        await require_route(reader, policy, tenant)
    finally:
        try:
            await reader.close()
        finally:
            await policy.close()
    print("Evidence routing prerequisite verified; no records written")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        # No Cosmos records, destination identifiers or CLI stderr in logs.
        print("::error::Evidence routing unavailable: verify tenant mapping, matching subscription, destination approval and read permissions", file=sys.stderr)
        sys.exit(1)

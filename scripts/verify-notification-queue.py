"""Read-only Azure preflight. Never provisions resources or switches workloads."""
import json
import os
import re
from decimal import Decimal
import subprocess
import sys
from uuid import UUID

LEGACY = "notification-events"
ALLOWED = {LEGACY, "notification-events-dedup-v1", "notification-events-dedup-v2"}
SENDER = "69a216fc-b8fb-44d8-bc22-1f3c2cd27a39"
RECEIVER = "4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0"
OWNER = "090c5cfd-751d-490a-894a-3ce6f1109419"


def selected(config):
    name = config.get("SELECTED_NOTIFICATION_QUEUE", "") or LEGACY
    if name not in ALLOWED:
        raise ValueError("Unapproved notification queue name")
    if name != LEGACY and (config.get("QUEUE_SELECTION_APPROVED") != "true" or not config.get("QUEUE_REVIEW_REFERENCE", "").strip()):
        raise ValueError("New queue selection requires persistent approval and a run review reference")
    if config.get("EXPECTED_NOTIFICATION_QUEUE", name) != name:
        raise ValueError("Queue selection changed between production approvals")
    return name


def properties(record):
    # Service Bus CLI flattens ARM properties; raw ARM fixtures remain supported.
    return record.get("properties", record)


def duration_seconds(value):
    match = re.fullmatch(r"P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?", value or "")
    if not match or not any(match.groups()):
        raise ValueError("Unsupported duplicate detection duration")
    return sum(Decimal(v or "0") * factor for v, factor in zip(match.groups(), [86400, 3600, 60, 1]))


def validate(config, namespace, queue, principals, assignments, revisions, scaler_queues):
    name = selected(config)
    if namespace["sku"]["tier"] not in {"Standard", "Premium"} or properties(namespace).get("disableLocalAuth") is not True:
        raise ValueError("Namespace must support deduplication and disable local authentication")
    if queue["name"] != name:
        raise ValueError("Queue lookup mismatch")
    queue_properties = properties(queue)
    if name != LEGACY and (queue_properties.get("requiresDuplicateDetection") is not True or duration_seconds(queue_properties.get("duplicateDetectionHistoryTimeWindow")) != 86400 or queue_properties.get("enablePartitioning", False)):
        raise ValueError("Selected queue must have nonpartitioned one-day duplicate detection")
    if len(set(principals.values())) != 3:
        raise ValueError("Application and scaler identities must be separate")
    for principal in principals.values():
        if UUID(principal).int == 0:
            raise ValueError("Invalid identity object id")
    for identity, role in [("worker", SENDER), ("notification", RECEIVER), ("scaler", OWNER)]:
        if not any(a.get("principalId", "").lower() == principals[identity].lower() and a.get("roleDefinitionId", "").lower().endswith("/" + role) and a.get("scope", "").lower() == queue["id"].lower() for a in assignments):
            raise ValueError("Missing exact queue-scoped permission for " + identity)
    # Include every active revision, not just the latest template. Refuse to
    # deploy if an old revision could still publish/consume on another queue.
    for service in ("worker", "notification"):
        if not revisions[service] or any(value != name for value in revisions[service]):
            raise ValueError("Manual quiesced cutover required: active " + service + " queue differs")
    if not scaler_queues or any(value != name for value in scaler_queues):
        raise ValueError("Notification scaler queue differs")
    return name


def az(*args):
    # CLI failures (including forbidden reads) fail closed. No mutation commands.
    return json.loads(subprocess.check_output(["az", *args, "-o", "json"], text=True))


def revision_wiring(config, name, bus, service, template, identities):
    containers = template["containers"]
    if len(containers) != 1:
        raise ValueError("Unknown multi-container workload configuration")
    container = containers[0]
    env = {}
    for entry in container.get("env", []):
        if entry["name"] in env:
            raise ValueError("Duplicate workload environment variable")
        env[entry["name"]] = entry.get("value")
    queue_key = "NOTIFICATION_QUEUE" if service == "worker" else "SERVICE_BUS_QUEUE"
    if queue_key not in env:
        # Only a separately reviewed immutable image can establish historical
        # code defaults. Attachment or a mutable tag is not sufficient evidence.
        approved = config.get("LEGACY_" + service.upper() + "_DEFAULT_IMAGE", "")
        if name != LEGACY or not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", approved) or container.get("image") != approved:
            raise ValueError("Implicit legacy default requires a reviewed immutable image")
        effective = LEGACY
    else:
        effective = env[queue_key]
    if effective != name:
        raise ValueError("Active revision queue differs")
    if env.get("SERVICE_BUS_FQDN", "").lower() != (bus + ".servicebus.windows.net").lower():
        raise ValueError("Active revision namespace differs")
    if env.get("AZURE_CLIENT_ID", "").lower() != identities[service]["clientId"].lower():
        raise ValueError("Active revision application identity differs")
    scaler_queues = []
    if service == "notification":
        rules = template["scale"].get("rules", [])
        for rule in rules:
            custom = rule.get("custom", {})
            if custom.get("type") != "azure-servicebus":
                raise ValueError("Unknown Notification scaling rule")
            metadata = custom["metadata"]
            if metadata.get("namespace", "").lower() != bus.lower() or custom.get("identity", "").lower() != identities["scaler"]["id"].lower() or custom.get("auth"):
                raise ValueError("Active revision scaler namespace or identity differs")
            scaler_queues.append(metadata.get("queueName"))
        if not scaler_queues or any(q != name for q in scaler_queues):
            raise ValueError("Active revision scaler queue differs")
    return effective, scaler_queues


def main():
    config = os.environ
    name = selected(config)
    group = config["RESOURCE_GROUP"]
    bus = config["SERVICE_BUS_NAMESPACE"]
    namespace = az("servicebus", "namespace", "show", "-g", group, "-n", bus)
    queue = az("servicebus", "queue", "show", "-g", group, "--namespace-name", bus, "-n", name)
    identities = {}
    for key, identity in [("worker", "incident-worker-id"), ("notification", "incident-notification-id"), ("scaler", "incident-notification-scaler-id")]:
        record = az("identity", "show", "-g", group, "-n", identity)
        UUID(record["clientId"])
        identities[key] = record
    assignments = az("role", "assignment", "list", "--scope", queue["id"], "--all")
    revisions = {}
    scaler_queues = []
    for service in ("worker", "notification"):
        workload = az("containerapp", "show", "-g", group, "-n", "incident-" + service)
        attached = {key.lower() for key in workload["identity"]["userAssignedIdentities"]}
        required = [service, "scaler"] if service == "notification" else [service]
        if any(identities[key]["id"].lower() not in attached for key in required):
            raise ValueError("Expected identity is not attached")
        # Check the current template as well as all active revision templates.
        revision_wiring(config, name, bus, service, workload["properties"]["template"], identities)
        active = az("containerapp", "revision", "list", "-g", group, "-n", "incident-" + service)
        revisions[service] = []
        for revision in active:
            if revision["properties"].get("active"):
                effective, scalers = revision_wiring(config, name, bus, service, revision["properties"]["template"], identities)
                revisions[service].append(effective)
                scaler_queues.extend(scalers)
    validate(config, namespace, queue, {k: v["principalId"] for k, v in identities.items()}, assignments, revisions, scaler_queues)
    if config.get("GITHUB_OUTPUT"):
        with open(config["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("name=" + name + "\n")
    print("Notification queue selection and deployed wiring verified")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        print("::error::Notification queue preflight failed: " + str(error), file=sys.stderr)
        sys.exit(1)

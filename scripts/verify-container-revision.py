"""Validate supplied revision JSON locally; no cloud clients or requests."""
import json
from pathlib import Path
import sys


def validate(revision, expected_image, service):
    props = revision.get("properties", {})
    if props.get("active") is not True:
        raise ValueError("Expected revision is not active")
    if props.get("provisioningState") != "Provisioned" or props.get("healthState") != "Healthy":
        raise ValueError("Expected revision is not provisioned and healthy")
    if props.get("runningState") not in {"Running", "RunningAtMaxScale"} or props.get("replicas", 0) < 1:
        raise ValueError("Expected revision has no running replica")
    containers = props.get("template", {}).get("containers", [])
    if len(containers) != 1 or containers[0].get("image") != expected_image:
        raise ValueError("Running revision does not use the approved immutable image")
    env = {entry["name"]: entry.get("value") for entry in containers[0].get("env", [])}
    if service == "worker":
        for name, expected in {"INCIDENT_STORE": "cosmos", "NOTIFICATION_PUBLISHER": "servicebus"}.items():
            if env.get(name) != expected:
                raise ValueError(f"Worker {name} is not configured correctly")
        required = ["COSMOS_ENDPOINT", "COSMOS_DATABASE", "COSMOS_CONTAINER", "COSMOS_SUBSCRIPTIONS_CONTAINER", "NOTIFICATION_QUEUE"]
    elif service == "notification":
        if env.get("NOTIFIER") != "telegram":
            raise ValueError("Notification provider is not configured")
        required = []
    else:
        raise ValueError("Unknown revision service")
    for name in required + ["AZURE_CLIENT_ID", "SERVICE_BUS_FQDN", "SERVICE_BUS_QUEUE"]:
        if not env.get(name):
            raise ValueError(f"Missing {name} in the running revision")


if __name__ == "__main__":
    try:
        validate(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2], sys.argv[3])
    except (ValueError, KeyError, TypeError) as error:
        print(f"Revision readiness check failed: {error}", file=sys.stderr)
        raise SystemExit(1)
    print("Expected image and configuration are on an active, healthy, running revision; live E2E is not verified")

#!/usr/bin/env python3
"""Demonstrate that a new analytics type needs no change to the platform.

The proposal's last success criterion is "easy integration of new AI analytics
without major architectural changes". That is a claim about the architecture, and
a claim is not evidence, so this script produces the evidence: it ingests an
analytics type the platform has never seen and follows it through every layer,
with nothing rebuilt, redeployed or reconfigured in between.

What makes the claim true is visible in the Common Event Model. `analytics_type`
is a plain `str` -- no Literal, no Enum, no pattern -- and so are `event_type`,
`source_type` and `severity`. Only `event_purpose` is constrained, because the
pipeline genuinely branches on it. Downstream, nothing maps the value to
behaviour: the worker logs it, the notifier interpolates it into message text,
and the dashboard derives its filter options from whatever is in the store
(`sorted(df["analytics_type"].unique())`). Service Bus carries queues rather than
filtered topic subscriptions, so there is no routing rule to add, and Cosmos
partitions on `/tenant_id` and enforces no schema, so there is no migration.

The one place a hardcoded list does exist is `/api/v1/simulate/{analytics_type}`
in the Ingestion Service, which serves three canned samples and rejects anything
else. That endpoint is a demo shortcut, not the ingestion path, and this script
deliberately uses the real one so the difference is not glossed over.

Run `--check` first to confirm the type really is unknown, then without it to
ingest and trace.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RESOURCE_GROUP = "rg-incident-platform"
INGESTION_APP = "incident-ingestion"

DEFAULT_TYPE = "abandoned_object_detection"
DEFAULT_EVENT_TYPE = "object_left_unattended"

REQUEST_TIMEOUT_SECONDS = 60
WARMUP_ATTEMPTS = 10
WARMUP_INTERVAL_SECONDS = 6

# The worker consumes from Service Bus, so the document arrives after the API has
# already answered. Long enough for a cold worker replica to start.
STORE_ATTEMPTS = 20
STORE_INTERVAL_SECONDS = 6

# Log Analytics ingestion lags behind the services by a minute or two, so the
# trace at the end is polled rather than read once.
LOG_ATTEMPTS = 10
LOG_INTERVAL_SECONDS = 20


class Failed(Exception):
    """A step did not produce the evidence it was supposed to."""


def az(*args: str) -> str:
    result = subprocess.run(["az", *args], capture_output=True, text=True, check=False)

    if result.returncode != 0:
        raise Failed(
            f"az {' '.join(args)} failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    return result.stdout.strip()


@lru_cache(maxsize=1)
def deployed_config() -> dict[str, str]:
    env = json.loads(
        az(
            "containerapp", "show", "-g", RESOURCE_GROUP, "-n", INGESTION_APP,
            "--query", "properties.template.containers[0].env", "-o", "json",
        )
        or "[]"
    )
    audience = next(
        (e.get("value") for e in env if e.get("name") == "AUTH_AUDIENCE"), None
    )

    fqdn = az(
        "containerapp", "show", "-g", RESOURCE_GROUP, "-n", INGESTION_APP,
        "--query", "properties.configuration.ingress.fqdn", "-o", "tsv",
    )

    if not audience or not fqdn:
        raise Failed("the ingestion deployment is missing AUTH_AUDIENCE or ingress")

    if "/" in fqdn or ":" in fqdn:
        raise Failed(f"the ingress fqdn is not a bare hostname: {fqdn!r}")

    cosmos = az(
        "cosmosdb", "list", "-g", RESOURCE_GROUP,
        "--query", "[0].documentEndpoint", "-o", "tsv",
    )

    return {
        "audience": audience,
        "url": f"https://{fqdn}/api/v1/incidents",
        "health": f"https://{fqdn}/health",
        "cosmos": cosmos,
    }


@lru_cache(maxsize=1)
def container():
    """Cosmos container client, imported late so --help needs no dependencies.

    Cached because step 5 polls for the document in a loop. Rebuilding the client
    each time would add its own `az` calls to every iteration, stretching the
    interval the loop then reports as elapsed time.
    """
    try:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise Failed(
            f"{exc}. Install the script dependencies: "
            "pip install -r scripts/requirements.txt"
        ) from exc

    endpoint = deployed_config()["cosmos"]
    client = CosmosClient(endpoint, credential=DefaultAzureCredential())

    return client.get_database_client("incidents").get_container_client("incidents")


def count_in_store(analytics_type: str) -> int:
    rows = list(
        container().query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.analytics_type = @t",
            parameters=[{"name": "@t", "value": analytics_type}],
            enable_cross_partition_query=True,
        )
    )

    return rows[0] if rows else 0


def mentions_in_source(analytics_type: str) -> list[str]:
    """Where the type string appears in tracked source, excluding this script.

    This is the actual evidence for "no code change": if the platform had been
    taught about the type, the teaching would be here.
    """
    result = subprocess.run(
        ["git", "grep", "-l", "--fixed-strings", analytics_type, "--",
         "*.py", "*.bicep", "*.yml", "*.yaml", "*.json"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )

    this_file = f"scripts/{Path(__file__).name}"

    return [
        line for line in result.stdout.split()
        if line and line != this_file
    ]


def trace_in_logs(event_id: str) -> list[tuple[str, str]]:
    """Every log line the platform wrote about this event, oldest first.

    Best-effort. Log Analytics ingestion lags by a minute or two, so an empty
    result here means "not yet", not "it did not happen" -- which is why this
    step reports rather than fails.
    """
    # The id is built by this script from a timestamp, but it is still being
    # pasted into a query, so require the shape that makes quoting moot.
    if not re.fullmatch(r"[A-Za-z0-9-]+", event_id):
        raise Failed(f"refusing to build a query around {event_id!r}")

    workspace = az(
        "monitor", "log-analytics", "workspace", "list", "-g", RESOURCE_GROUP,
        "--query", "[0].customerId", "-o", "tsv",
    )

    query = (
        "ContainerAppConsoleLogs_CL "
        "| where TimeGenerated > ago(1h) "
        f"| where Log_s has '{event_id}' "
        "| project TimeGenerated, ContainerAppName_s, Log_s "
        "| order by TimeGenerated asc"
    )

    raw = az(
        "monitor", "log-analytics", "query", "-w", workspace,
        "--analytics-query", query,
        "--query", "[].[ContainerAppName_s, Log_s]", "-o", "json",
    )

    return [(row[0], row[1]) for row in json.loads(raw or "[]")]


def existing_types() -> list[str]:
    return sorted(
        row
        for row in container().query_items(
            query="SELECT DISTINCT VALUE c.analytics_type FROM c",
            enable_cross_partition_query=True,
        )
        if row
    )


def wake(health_url: str) -> None:
    for _ in range(WARMUP_ATTEMPTS):
        try:
            with urllib.request.urlopen(health_url, timeout=WARMUP_INTERVAL_SECONDS) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass

        time.sleep(WARMUP_INTERVAL_SECONDS)


def post(url: str, token: str, event: dict) -> tuple[int, str]:
    if not url.startswith("https://"):
        raise Failed(f"refusing to send a bearer token over {url!r}")

    request = urllib.request.Request(
        url,
        data=json.dumps(event).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as r:
            return r.status, r.read().decode()[:300]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def build_event(analytics_type: str, event_type: str) -> dict:
    return {
        "event_id": f"EVT-NEWTYPE-{int(time.time())}",
        "tenant_id": "TENANT-DEV",
        "site_id": "SITE-DEV-CONCOURSE",
        "source_id": "CAM-99",
        "source_name": "Concourse camera CAM-99 - West Atrium",
        "source_type": "video_analytics",
        "analytics_type": analytics_type,
        "event_type": event_type,
        "severity": "medium",
        "timestamp": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        # Labelled for what it is. The purpose field is the one field the
        # pipeline does branch on, and mislabelling test traffic as a real
        # incident in a shared alert channel is not worth a tidier screenshot.
        "event_purpose": "e2e_test",
        "confidence": 0.88,
        "description": (
            "Analytics type the platform has never seen, ingested with no code "
            "change, rebuild or redeployment."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analytics-type", default=DEFAULT_TYPE)
    parser.add_argument("--event-type", default=DEFAULT_EVENT_TYPE)
    parser.add_argument(
        "--check", action="store_true",
        help="report whether the type is unknown, and ingest nothing",
    )
    args = parser.parse_args()

    new_type = args.analytics_type

    try:
        print(f"Analytics type under test: {new_type}\n")

        print("1. Does the platform's source mention this type?")
        mentions = mentions_in_source(new_type)

        if mentions:
            print(f"   It does, in {len(mentions)} file(s):")
            for path in mentions:
                print(f"     {path}")
            print(
                "   The demonstration is weaker with a type the code already "
                "names. Pass --analytics-type with something genuinely new."
            )
        else:
            print(
                "   No. The string appears in no tracked .py, .bicep, .yml or "
                ".json file, so nothing was written to accommodate it."
            )

        print("\n2. How many incidents of this type does the store hold?")
        before = count_in_store(new_type)
        print(f"   {before}")

        known = existing_types()
        print(f"\n3. Types the store currently knows ({len(known)}):")
        for name in known:
            print(f"     {name}")

        if args.check:
            print(
                "\n--check given, so nothing was ingested. Re-run without it to "
                "publish one event of this type and trace it through."
            )
            return 0

        config = deployed_config()
        event = build_event(new_type, args.event_type)

        print(f"\n4. Ingesting {event['event_id']} through the real API")
        print(f"   POST {config['url']}")

        token = az(
            "account", "get-access-token",
            "--resource", config["audience"], "--query", "accessToken", "-o", "tsv",
        )

        wake(config["health"])
        status, body = post(config["url"], token, event)

        if status != 202:
            raise Failed(f"the API answered {status or 'no response'}: {body}")

        print(
            "   202 Accepted. The API applied no validation specific to the "
            "type, because there is none to apply."
        )

        print("\n5. Waiting for the worker to persist it")
        stored = None

        for attempt in range(1, STORE_ATTEMPTS + 1):
            rows = list(
                container().query_items(
                    query="SELECT * FROM c WHERE c.id = @id",
                    parameters=[{"name": "@id", "value": event["event_id"]}],
                    enable_cross_partition_query=True,
                )
            )

            if rows:
                stored = rows[0]
                print(f"   Stored after {attempt * STORE_INTERVAL_SECONDS}s or less.")
                break

            time.sleep(STORE_INTERVAL_SECONDS)

        if stored is None:
            raise Failed(
                "the document never appeared. The API accepted the event, so "
                "look at the worker's logs and the incident-events queue."
            )

        for field in ("analytics_type", "event_type", "severity", "tenant_id"):
            print(f"     {field} = {stored.get(field)}")

        print(
            "   Cosmos enforces no schema and partitions on /tenant_id, not on "
            "the type, so this needed no migration and creates no new partition."
        )

        after = existing_types()
        print(f"\n6. Types the store knows now ({len(after)}):")
        for name in after:
            marker = "  <- new, and it will appear in the dashboard's" if name == new_type else ""
            print(f"     {name}{marker}")
            if marker:
                print("        Analytics type filter and charts without being listed anywhere")

        print(
            f"\n   The dashboard builds that filter from the store at render "
            f"time, so {new_type} is already selectable."
        )

        print("\n7. What each service logged about this event")
        trace = []

        for attempt in range(1, LOG_ATTEMPTS + 1):
            trace = trace_in_logs(event["event_id"])

            # The notifier is the last service to speak, so its line is how we
            # know the whole chain has been ingested and not just the start.
            if any("telegram" in line.lower() for _, line in trace):
                break

            if attempt < LOG_ATTEMPTS:
                time.sleep(LOG_INTERVAL_SECONDS)

        if trace:
            # Printed in full even though the worker's line is long. That line
            # names the new type inside the store, which is the evidence this
            # whole script exists to produce, and a tidier column would cut it.
            for service, line in trace:
                print(f"     {service:22} {line.strip()}")
        else:
            print(
                "     Nothing yet. Log Analytics ingestion lags a few minutes; "
                "the document in step 5 is the proof either way."
            )

        if any("telegram" in line.lower() for _, line in trace):
            print(
                "\n   Delivered. The notifier interpolates whatever the event "
                "says, so the alert names the new type without being taught it."
            )

        print(
            "\nNothing was rebuilt, redeployed or reconfigured between step 1 "
            "and step 7."
        )

        return 0

    except Failed as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Publish the evidence manifest's events through the ingestion API.

`upload-evidence-batch.py` puts the images in the blob container. Nothing then
tells the platform they exist: an incident only reaches Cosmos, and therefore the
dashboard's Evidence page and the Telegram alerts, by being posted to the
ingestion API. This script does that, from the same manifest, so the events in
the store and the blobs they reference come from one description rather than two.

Two properties of the pipeline decide how this script behaves.

**The store is idempotent, notifications are not.** The worker keys each Cosmos
document on the producer's `event_id`, so re-publishing creates no duplicate
document. But `worker-service/app/notification_handler.py` deliberately notifies
on a duplicate as well as a new incident, because a delivery that failed the
first time should get another chance. So a second run sends a second round of
Telegram messages. That is why publishing needs `--publish` stated explicitly and
why `--limit` exists.

**Every event here notifies.** The manifest classifies these as `e2e_test`, which
`should_notify` lets through -- only `load_test` is held back. Each of these
events carries an `evidence_uri`, so each one becomes a `sendPhoto`. Forty-three
of them is forty-three photographs in whichever chat the bot posts to.

Authentication is the operator path from `grant-operator-access.sh`: the Azure
CLI is pre-authorized on the API, and the caller holds the `Incidents.Write`
application role individually, so `az account get-access-token` returns a token
the API accepts. If that returns `AADSTS65001`, the operator setup has not been
run for this account.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "evidence_images/manifest.proposed.json"

RESOURCE_GROUP = "rg-incident-platform"
INGESTION_APP = "incident-ingestion"
DASHBOARD_APP = "incident-dashboard"

REQUEST_TIMEOUT_SECONDS = 60

# The ingestion service scales to zero, so the first caller after an idle period
# pays for the replica to start. Waking it on `/health` first keeps that cost off
# the first event, which would otherwise look like a failure of that event.
WARMUP_ATTEMPTS = 10
WARMUP_INTERVAL_SECONDS = 6


class Refused(Exception):
    """A precondition failed, so nothing is published."""


def az(*args: str) -> str:
    """Run the Azure CLI and return stdout, or raise with its stderr."""
    result = subprocess.run(
        ["az", *args], capture_output=True, text=True, check=False
    )

    if result.returncode != 0:
        raise Refused(
            f"az {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )

    return result.stdout.strip()


def container_app_env(app: str) -> dict[str, str]:
    raw = az(
        "containerapp", "show", "-g", RESOURCE_GROUP, "-n", app,
        "--query", "properties.template.containers[0].env", "-o", "json",
    )

    return {
        entry["name"]: entry.get("value", "")
        for entry in json.loads(raw or "[]")
        if "name" in entry
    }


def deployed_config() -> dict[str, str]:
    """Read the values the running platform actually uses.

    Taken from the deployment rather than from flags or from the manifest's own
    `storage` block, because the point of the check below is to catch a manifest
    that describes a different account from the one serving the dashboard.
    """
    ingestion = container_app_env(INGESTION_APP)
    dashboard = container_app_env(DASHBOARD_APP)

    fqdn = az(
        "containerapp", "show", "-g", RESOURCE_GROUP, "-n", INGESTION_APP,
        "--query", "properties.configuration.ingress.fqdn", "-o", "tsv",
    )

    missing = [
        name
        for name, value in (
            ("AUTH_AUDIENCE", ingestion.get("AUTH_AUDIENCE")),
            ("ingress fqdn", fqdn),
            ("EVIDENCE_BLOB_ENDPOINT", dashboard.get("EVIDENCE_BLOB_ENDPOINT")),
        )
        if not value
    ]

    if missing:
        raise Refused(f"the deployment is missing {', '.join(missing)}")

    # The hostname comes from the Azure CLI rather than from a flag, but it is
    # still interpolated into a URL, and a bearer token is sent to whatever that
    # URL resolves to. Require a bare hostname so the scheme cannot be
    # substituted from below.
    if "/" in fqdn or ":" in fqdn:
        raise Refused(f"the ingress fqdn is not a bare hostname: {fqdn!r}")

    return {
        "audience": ingestion["AUTH_AUDIENCE"],
        "url": f"https://{fqdn}/api/v1/incidents",
        "blob_endpoint": dashboard["EVIDENCE_BLOB_ENDPOINT"],
        "container": dashboard.get("EVIDENCE_CONTAINER") or "evidence",
    }


def check_events(events: list[dict], config: dict[str, str]) -> None:
    """Refuse the whole batch rather than publish part of a bad manifest.

    An event whose `evidence_uri` names a different account or container is not
    merely useless -- the notification service and the dashboard both validate
    the host and container before fetching, so the incident would be stored and
    alerted with an image that can never load, and nothing downstream would say
    why.
    """
    expected_host = urlparse(config["blob_endpoint"]).netloc.lower()
    problems: list[str] = []

    seen: set[str] = set()
    for event in events:
        event_id = event.get("event_id", "<no event_id>")

        if event_id in seen:
            problems.append(f"{event_id}: appears more than once in the manifest")

        seen.add(event_id)

        uri = event.get("evidence_uri")
        if not uri:
            problems.append(f"{event_id}: has no evidence_uri")
            continue

        parsed = urlparse(uri)
        container = parsed.path.lstrip("/").partition("/")[0]

        if parsed.scheme.lower() != "https":
            problems.append(f"{event_id}: evidence_uri is not https")
        if parsed.netloc.lower() != expected_host:
            problems.append(
                f"{event_id}: evidence_uri host {parsed.netloc} "
                f"is not the deployed {expected_host}"
            )
        if container != config["container"]:
            problems.append(
                f"{event_id}: evidence_uri container {container!r} "
                f"is not the deployed {config['container']!r}"
            )

    if problems:
        raise Refused(
            "the manifest does not match the deployment, so nothing was "
            "published:\n  " + "\n  ".join(problems)
        )


def get_token(audience: str) -> str:
    try:
        return az(
            "account", "get-access-token",
            "--resource", audience, "--query", "accessToken", "-o", "tsv",
        )
    except Refused as exc:
        raise Refused(
            f"{exc}\n\nIf this says AADSTS65001, the Azure CLI has not been "
            "pre-authorized for this account -- run scripts/grant-operator-access.sh."
        ) from exc


def publish(url: str, token: str, event: dict) -> tuple[int, str]:
    if not url.startswith("https://"):
        raise Refused(f"refusing to send a bearer token over {url!r}")

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
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode()[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:200]
    # A read timeout arrives as OSError rather than URLError, and it is the
    # ambiguous case: the API may have accepted the event before the socket gave
    # up. Reported as a failure so the run is not called clean, but re-running is
    # safe -- the worker keys on event_id.
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def wake(url: str) -> None:
    """Wait for a replica to be serving before the batch starts."""
    health = url.replace("/api/v1/incidents", "/health")

    for attempt in range(1, WARMUP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(health, timeout=WARMUP_INTERVAL_SECONDS) as response:
                if response.status == 200:
                    if attempt > 1:
                        print(f"Awake after {attempt} attempts.")
                    return
        except (urllib.error.URLError, OSError):
            pass

        time.sleep(WARMUP_INTERVAL_SECONDS)

    print(
        "Warning: the service never answered its health check. Publishing "
        "anyway; individual events may time out.",
        file=sys.stderr,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="publish only the first N events, for a smaller first run",
    )
    parser.add_argument(
        "--publish", action="store_true",
        help="actually send. Without this, the run reports what it would send.",
    )
    args = parser.parse_args()

    try:
        manifest = json.loads(args.manifest.read_text())
        events = [image["event"] for image in manifest["images"]]

        config = deployed_config()
        check_events(events, config)
    except Refused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"Could not read {args.manifest}: {exc}", file=sys.stderr)
        return 1

    selected = events[: args.limit] if args.limit else events

    print(f"Manifest:   {args.manifest.relative_to(ROOT)}")
    print(f"Target:     {config['url']}")
    print(f"Evidence:   {config['blob_endpoint']}{config['container']}")
    print(f"Events:     {len(selected)} of {len(events)}")

    if not args.publish:
        print("\nDry run. Nothing was sent. These would be:")
        for event in selected:
            print(
                f"  {event['event_id']}  {event['analytics_type']:<22} "
                f"{event['severity']:<7} {event.get('event_purpose', 'incident')}"
            )
        print(
            f"\nRe-run with --publish to send. Each event becomes a Telegram "
            f"photo, so {len(selected)} messages will arrive in the bot's chat."
        )
        return 0

    try:
        token = get_token(config["audience"])
    except Refused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1

    wake(config["url"])

    accepted = 0
    failures: list[tuple[str, int, str]] = []

    print()
    for event in selected:
        status, body = publish(config["url"], token, event)

        if status == 202:
            accepted += 1
            print(f"  202  {event['event_id']}")
        else:
            failures.append((event["event_id"], status, body))
            print(f"  {status or 'ERR':<3}  {event['event_id']}  {body}")

    print(f"\nAccepted {accepted} of {len(selected)}.")

    if failures:
        print(f"{len(failures)} failed.", file=sys.stderr)
        return 1

    print(
        "The API has accepted these; the worker stores them and the "
        "notification service alerts on them asynchronously. Give it a minute "
        "before checking the dashboard."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

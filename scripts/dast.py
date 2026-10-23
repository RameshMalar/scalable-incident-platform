#!/usr/bin/env python3
"""Helpers around OWASP ZAP for the DAST workflow (.github/workflows/dast.yml).

ZAP scans the deployed services from the outside, as an anonymous client would
see them: no bearer token and no Azure credential. This script does the four
things the scan must not be trusted to do by itself:

  targets    accept only this platform's own Container Apps, so a typo in a
             workflow input cannot aim an active scanner at someone else's site
  preflight  refuse to scan a service that does not reject anonymous calls
  spec       fetch a service's OpenAPI document and drop the paths an active
             scan must never touch
  gate       summarise ZAP's JSON reports and fail on any High-risk alert

Why a path is dropped: POST /api/v1/simulate/{analytics_type} takes no token
and publishes a real incident, which reaches the operators' Telegram chat. An
active scan sends thousands of requests. zap-api-scan.py attacks exactly the
operations in the definition it imports and runs no spider, so removing the
path from that definition keeps the scan away from it.

Standard library only: the workflow and the tests that guard it install
nothing.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

APPS = {
    "ingestion": "incident-ingestion",
    "subscription": "incident-subscription",
    "dashboard": "incident-dashboard",
}

# Paths an active scan must never reach, matched as prefixes.
EXCLUDED_PREFIXES = ("/api/v1/simulate",)

RISKS = {"3": "High", "2": "Medium", "1": "Low", "0": "Informational"}

TIMEOUT_SECONDS = 30


class Refused(Exception):
    pass


def check_target(role, url):
    """Return the normalised base URL, or refuse anything but our own app."""
    if role not in APPS:
        raise Refused(f"unknown role {role!r}")
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    pattern = rf"{re.escape(APPS[role])}\.[a-z0-9-]+\.[a-z0-9-]+\.azurecontainerapps\.io"
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.port
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or not re.fullmatch(pattern, host)
    ):
        raise Refused(
            f"{role} target must be https://{APPS[role]}.<environment>.<region>"
            f".azurecontainerapps.io with no path, got {url!r}"
        )
    return f"https://{host}"


def http_get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "dast-preflight"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise Refused(f"request to {url} failed: {reason}") from error


def preflight(role, base, get=http_get):
    """Refuse to scan unless the service turns anonymous callers away.

    The Ingestion API's anonymous write is probed separately by the reviewed
    scripts/verify-anonymous-ingestion.sh; here it must report Entra auth.
    """
    if role == "ingestion":
        status, body = get(f"{base}/health")
        try:
            auth = json.loads(body).get("auth")
        except (ValueError, AttributeError):
            auth = None
        if status != 200 or auth != "entra":
            raise Refused(f"/health answered {status} with auth={auth!r}; expected 200 and 'entra'")
    elif role == "subscription":
        status, _ = get(f"{base}/api/v1/subscriptions")
        if status != 401:
            raise Refused(f"anonymous GET /api/v1/subscriptions answered {status}; expected 401")
    else:
        raise Refused(f"no preflight defined for {role!r}")


def filter_spec(document):
    """Return (document without excluded paths, removed paths); refuse if any survive."""
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise Refused("OpenAPI document has no paths")
    kept = {p: v for p, v in paths.items() if not p.startswith(EXCLUDED_PREFIXES)}
    removed = sorted(set(paths) - set(kept))
    if any(p.startswith(EXCLUDED_PREFIXES) for p in kept) or not kept:
        raise Refused("filtered OpenAPI document is unsafe or empty")
    return {**document, "paths": kept}, removed


def write_spec(role, base, out, get=http_get):
    status, body = get(f"{base}/openapi.json")
    if status != 200:
        raise Refused(f"{base}/openapi.json answered {status}")
    document, removed = filter_spec(json.loads(body))
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
    print(f"{role}: scanning {sorted(document['paths'])}")
    print(f"{role}: excluded {removed or 'nothing'}")


def summarise(reports):
    """Return (markdown summary, number of High-risk alerts) for ZAP JSON reports."""
    rows, findings, high = [], [], 0
    for path in reports:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
        sites = report.get("site") or []
        if not sites:
            raise Refused(f"{path} records no scanned site")
        for site in sites:
            counts = dict.fromkeys(RISKS.values(), 0)
            for alert in site.get("alerts", []):
                risk = RISKS.get(str(alert.get("riskcode")), "Informational")
                counts[risk] += 1
                if risk in ("High", "Medium"):
                    findings.append(f"- **{risk}** · {alert.get('alert', alert.get('name'))} · {site.get('@name')}")
            high += counts["High"]
            rows.append(f"| {site.get('@name')} | " + " | ".join(str(counts[r]) for r in RISKS.values()) + " |")
    lines = [
        "## DAST (OWASP ZAP)",
        "",
        "| Target | High | Medium | Low | Informational |",
        "| --- | --- | --- | --- | --- |",
        *rows,
        "",
        "Counts are distinct alert types. The workflow fails on any High.",
    ]
    if findings:
        lines += ["", "### High and Medium", "", *findings]
    return "\n".join(lines) + "\n", high


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("targets", help="validate INGESTION_URL, SUBSCRIPTION_URL, DASHBOARD_URL")
    check = commands.add_parser("preflight")
    check.add_argument("role", choices=["ingestion", "subscription"])
    check.add_argument("url")
    spec = commands.add_parser("spec")
    spec.add_argument("role", choices=["ingestion", "subscription"])
    spec.add_argument("url")
    spec.add_argument("out")
    gate = commands.add_parser("gate")
    gate.add_argument("reports", nargs="+")
    args = parser.parse_args(argv)

    try:
        if args.command == "targets":
            exported = []
            for role in APPS:
                raw = os.environ.get(f"{role.upper()}_URL", "").strip()
                if not raw:
                    if role == "ingestion":
                        raise Refused("INGESTION_URL is required")
                    continue
                exported.append(f"DAST_{role.upper()}={check_target(role, raw)}")
            print("\n".join(exported))
            if os.environ.get("GITHUB_ENV"):
                with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as handle:
                    handle.write("\n".join(exported) + "\n")
        elif args.command == "preflight":
            preflight(args.role, check_target(args.role, args.url))
            print(f"{args.role}: anonymous callers are rejected")
        elif args.command == "spec":
            write_spec(args.role, check_target(args.role, args.url), args.out)
        elif args.command == "gate":
            summary, high = summarise(args.reports)
            print(summary)
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
                    handle.write(summary)
            if high:
                raise Refused(f"{high} High-risk alert type(s); see the reports")
    except Refused as refusal:
        print(f"::error::{refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

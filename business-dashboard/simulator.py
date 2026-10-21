"""Simulate Event: one test incident through the real, authorized ingestion path.

The dashboard does not call the anonymous demo route `/api/v1/simulate`. It
submits a Common Event Model incident to `POST /api/v1/incidents` -- the same
endpoint every producer uses -- with an Entra token for its own managed
identity. Ingestion then applies its existing controls unchanged: the token
must carry `Incidents.Write`, and TENANT_MAP pins the dashboard identity to one
tenant, so the event is refused if it names any other.

Every simulated event is `event_purpose="e2e_test"`: stored and shown as a TEST,
and delivered only to Telegram subscribers of that tenant and event type.
"""
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from analytics_catalog import SEVERITIES

EVIDENCE_FILE = Path(__file__).with_name("simulated_evidence.json")
SCOPE = re.compile(r"^api://[0-9a-fA-F-]{36}/\.default$")
TENANT = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,39}$")
SITE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,39}$")
MAX_RESPONSE_BYTES = 65536


DEMO_PREFIX = "DEMO-"


def demo_mode(env=os.environ) -> bool:
    """Demo mode unless the real ingestion path is explicitly selected.

    In demo mode an incident lives only in the signed-in browser session: it is
    never sent to ingestion, never stored in Cosmos DB and never alerts anyone.
    """
    return env.get("DASHBOARD_SIMULATOR_MODE", "demo").strip().lower() != "live"


def demo_evidence_uris(pools: dict, endpoint: str, container: str) -> frozenset:
    """Evidence a demo incident may show: only the packaged pool's photographs."""
    endpoint = (endpoint or "").strip().rstrip("/")
    if not endpoint:
        return frozenset()
    return frozenset(f"{endpoint}/{container or 'evidence'}/{item['blob']}"
                     for items in pools.values() for item in items)


class SimulatorUnavailable(Exception):
    """Simulation is switched off or misconfigured; never attempt a request."""


@dataclass(frozen=True)
class SimulatorConfig:
    origin: str
    scope: str
    tenant: str


def simulator_config(env=os.environ) -> SimulatorConfig:
    if env.get("ENABLE_DASHBOARD_SIMULATOR", "").lower() != "true":
        raise SimulatorUnavailable("Simulate Event is not enabled for this deployment.")
    origin = env.get("SIMULATOR_INGESTION_ORIGIN", "").rstrip("/")
    parsed = urlparse(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise SimulatorUnavailable("SIMULATOR_INGESTION_ORIGIN must be an HTTPS origin without a path.")
    scope = env.get("SIMULATOR_API_SCOPE", "")
    if not SCOPE.fullmatch(scope):
        raise SimulatorUnavailable("SIMULATOR_API_SCOPE must be api://<application id>/.default.")
    tenant = env.get("SIMULATOR_TENANT", "")
    if not TENANT.fullmatch(tenant):
        raise SimulatorUnavailable("SIMULATOR_TENANT must name one platform tenant.")
    return SimulatorConfig(origin, scope, tenant)


def load_evidence_pools(path=EVIDENCE_FILE) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_event(*, analytics_type: str, event_type: str, severity: str, description: str, tenant_id: str,
                site_id: str, evidence_pool: list | None, evidence_endpoint: str, evidence_container: str,
                now: datetime | None = None, choose=secrets.choice, id_prefix: str = "EVT-SIM-") -> dict:
    """A Common Event Model incident, marked as a test."""
    if severity not in SEVERITIES:
        raise ValueError("Unsupported severity.")
    if not SITE.fullmatch(site_id):
        raise ValueError("Unsupported site.")
    endpoint = (evidence_endpoint or "").strip().rstrip("/")
    source_id, evidence_uri = "CAM-SIM-01", None
    if evidence_pool and endpoint:
        # A real evidence photograph already in the private container, so the
        # Evidence page and the Telegram alert both show an image.
        picked = choose(evidence_pool)
        source_id = picked["source_id"]
        evidence_uri = f"{endpoint}/{evidence_container or 'evidence'}/{picked['blob']}"
    return {
        "event_id": id_prefix + secrets.token_hex(4).upper(),
        "tenant_id": tenant_id,
        "site_id": site_id,
        "source_id": source_id,
        "source_name": f"Camera {source_id}",
        "source_type": "video_analytics",
        "analytics_type": analytics_type,
        "event_type": event_type,
        "severity": severity,
        "timestamp": (now or datetime.now(timezone.utc)).isoformat(),
        "event_purpose": "e2e_test",
        "description": description,
        "evidence_uri": evidence_uri,
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def publish(origin: str, token: str, event: dict, opener=None) -> str:
    """Exactly one POST; no retry or redirect. Acceptance is not persistence."""
    if not token or "\r" in token or "\n" in token:
        raise ValueError("An access token is required.")
    request = Request(origin.rstrip("/") + "/api/v1/incidents", data=json.dumps(event).encode("utf-8"), method="POST",
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    # Never retry an ambiguous timeout: ingestion may already have published.
    with (opener or build_opener(_NoRedirect())).open(request, timeout=10) as response:
        if response.status not in (200, 202):
            raise ValueError("Ingestion did not accept the event.")
        payload = json.loads(response.read(MAX_RESPONSE_BYTES))
    if not isinstance(payload, dict) or payload.get("status") != "accepted" or payload.get("event_id") != event["event_id"]:
        raise ValueError("Ingestion did not confirm this event.")
    return event["event_id"]

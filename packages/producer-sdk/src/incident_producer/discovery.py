"""Finding the endpoint and audience of a deployed ingestion service.

A convenience for our own operators and scripts, not part of what a producer
does. A third-party producer is *given* a URL and an audience; it has no Azure
CLI, no access to our resource group, and no business reading our deployment.

It is here because both scripts in `scripts/` had their own copy, including the
same hostname check, and a security check worth making once is worth not
rewriting.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

DEFAULT_RESOURCE_GROUP = "rg-incident-platform"
DEFAULT_APP = "incident-ingestion"


class DiscoveryFailed(RuntimeError):
    """The deployment could not be read, so nothing was assumed about it."""


@dataclass(frozen=True)
class Deployment:
    """Where the ingestion service is, and what audience its tokens are for."""

    endpoint: str
    audience: str

    @classmethod
    def discover(
        cls,
        resource_group: str = DEFAULT_RESOURCE_GROUP,
        app: str = DEFAULT_APP,
    ) -> "Deployment":
        """Read both from the running Container App via the Azure CLI."""
        env = json.loads(
            _az(
                "containerapp", "show", "-g", resource_group, "-n", app,
                "--query", "properties.template.containers[0].env", "-o", "json",
            )
            or "[]"
        )
        audience = next(
            (e.get("value") for e in env if e.get("name") == "AUTH_AUDIENCE"), None
        )

        fqdn = _az(
            "containerapp", "show", "-g", resource_group, "-n", app,
            "--query", "properties.configuration.ingress.fqdn", "-o", "tsv",
        )

        if not audience or not fqdn:
            raise DiscoveryFailed(
                f"{app} is missing AUTH_AUDIENCE or has no ingress configured"
            )

        # The hostname comes from the CLI rather than from a flag, but it is still
        # interpolated into a URL that a bearer token is sent to. Requiring a bare
        # hostname means no scheme, port or path can be substituted from below.
        if "/" in fqdn or ":" in fqdn:
            raise DiscoveryFailed(f"the ingress fqdn is not a bare hostname: {fqdn!r}")

        return cls(endpoint=f"https://{fqdn}", audience=audience)


def _az(*args: str) -> str:
    result = subprocess.run(["az", *args], capture_output=True, text=True, check=False)

    if result.returncode != 0:
        raise DiscoveryFailed(
            f"az {' '.join(args)} failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    return result.stdout.strip()

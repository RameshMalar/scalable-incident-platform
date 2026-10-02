"""Where the bearer token comes from.

A token source is any callable returning a string, so this package needs no Azure
SDK and a producer is not told how to authenticate. Three ways in:

    producer = IncidentProducer(endpoint, token_source=AzureCliToken(audience))

    # A managed identity or a client secret, if the producer already has
    # azure-identity. No adapter needed -- this is the whole integration.
    credential = DefaultAzureCredential()
    producer = IncidentProducer(
        endpoint,
        token_source=lambda: credential.get_token(f"{audience}/.default").token,
    )

    producer = IncidentProducer(endpoint, token_source=lambda: "...")
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime
from typing import Callable

TokenSource = Callable[[], str]


class AuthenticationFailed(RuntimeError):
    """No token could be obtained, so nothing was sent."""


class AzureCliToken:
    """Tokens from `az account get-access-token`, for operators and scripts.

    For a person at a terminal, not for a deployed producer: it shells out to the
    Azure CLI and inherits whoever is signed in. A service should pass its own
    credential's token instead.

    Cached until shortly before expiry. The CLI call takes the better part of a
    second, and a batch of incidents would otherwise pay it once per event.
    """

    # Refreshed early, so a token cannot expire between the check and the request
    # it is used for.
    EXPIRY_MARGIN_SECONDS = 300

    def __init__(self, audience: str):
        self._audience = audience
        self._token: str | None = None
        self._expires_at = 0.0

    def __call__(self) -> str:
        if self._token and time.time() < self._expires_at - self.EXPIRY_MARGIN_SECONDS:
            return self._token

        result = subprocess.run(
            [
                "az", "account", "get-access-token",
                "--resource", self._audience,
                "--query", "[accessToken,expiresOn]",
                "-o", "tsv",
            ],
            capture_output=True, text=True, check=False,
        )

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()

            # Worth naming, because the message Entra returns does not say what to
            # do about it and the answer is a one-off grant, not a code change.
            if "AADSTS65001" in detail:
                detail += (
                    "\n\nThe Azure CLI has not been pre-authorized for this API. "
                    "Run scripts/grant-operator-access.sh once."
                )

            raise AuthenticationFailed(f"az account get-access-token failed: {detail}")

        token, _, expires_on = result.stdout.strip().partition("\t")

        if not token:
            raise AuthenticationFailed("the Azure CLI returned an empty token")

        self._token = token
        self._expires_at = _parse_expiry(expires_on)

        return token


def _parse_expiry(expires_on: str) -> float:
    """When the token expires, as a monotonic-ish deadline for the cache.

    The CLI's format has changed between versions and differs by platform, so an
    unreadable value is treated as "expires soon" rather than as an error: a token
    that is re-fetched too often costs a second, and one assumed to live forever
    costs a failed delivery.
    """
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(expires_on.strip(), fmt).timestamp()
        except ValueError:
            continue

    return time.time() + AzureCliToken.EXPIRY_MARGIN_SECONDS + 60

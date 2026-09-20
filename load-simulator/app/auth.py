"""Gets each simulated producer its own access token.

The ingestion API refuses anonymous writes and confines every producer to one
tenant, resolved from the token's client id. So a load generator cannot be a
loop around `curl`: it has to authenticate as a registered producer, and if the
run is to say anything about tenant isolation under load, as several of them at
once, each with its own identity and its own tenant.

Three ways to get those tokens, for three situations:

`StaticTokenProvider` takes one token from outside, for a first local run.

`ClientCredentialsTokenProvider` is what an off-platform producer would use: an
app registration and a client secret.

`ManagedIdentityTokenProvider` is what the deployed simulator uses. Each
producer is a user-assigned managed identity holding `Incidents.Write`, so the
in-region load run needs no client secret anywhere -- not in the repository,
not in a GitHub secret, and not in the job's environment. That matches how the
rest of the platform authenticates and is the reason the deployed simulator
prefers it.

Tokens are cached per producer and renewed a minute before expiry. Requesting
one per incident would measure Entra's latency rather than the platform's, and
would rate-limit the run long before the platform did.
"""

import asyncio
import time
from dataclasses import dataclass

import httpx

TOKEN_ENDPOINT = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

# Container Apps injects these two, the same contract App Service uses. Their
# presence is also how the simulator can tell it is running in the cloud.
IDENTITY_ENDPOINT_ENV = "IDENTITY_ENDPOINT"
IDENTITY_HEADER_ENV = "IDENTITY_HEADER"
IDENTITY_API_VERSION = "2019-08-01"

# Renew this many seconds before the token actually expires, so a request in
# flight cannot be the one that discovers it is too late.
RENEWAL_MARGIN_SECONDS = 60

DEFAULT_LIFETIME_SECONDS = 3600.0


class TokenError(RuntimeError):
    """The directory refused to issue a token for this producer."""


@dataclass(frozen=True)
class Producer:
    """A simulated analytics producer.

    `tenant_id` is the customer this producer is registered to speak for, and
    must match its entry in the ingestion service's TENANT_MAP. Sending a
    different one is how the simulator can prove isolation still holds under
    load: the platform answers 403 rather than accepting the incident.

    `client_id` is an app registration's id under client credentials and a
    managed identity's client id in the cloud. Both arrive in the token as the
    claim the platform maps to a tenant, so the platform cannot tell -- and
    does not need to tell -- which of the two a producer used.
    """

    name: str
    tenant_id: str
    client_id: str = ""
    client_secret: str = ""


class StaticTokenProvider:
    """Hands out one token supplied from outside.

    For the first local runs: `az account get-access-token --resource api://...`
    produces a token for your own user, which carries the same right a producer
    does. It cannot demonstrate multi-tenant isolation, because every request
    then arrives with the same client id.
    """

    def __init__(self, token: str):
        if not token:
            raise ValueError("a static token provider needs a token")
        self._token = token

    async def token_for(self, producer: Producer) -> str:
        return self._token


class CachingTokenProvider:
    """Caches one token per producer and renews it before it expires."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._cache: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def token_for(self, producer: Producer) -> str:
        cached = self._cache.get(producer.client_id)
        if cached and cached[1] > self._clock():
            return cached[0]

        # One request per producer, not one per caller: at 100 requests a
        # second every in-flight request would otherwise ask for the same
        # token at the same moment.
        lock = self._locks.setdefault(producer.client_id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(producer.client_id)
            if cached and cached[1] > self._clock():
                return cached[0]

            token, expires_in = await self._request_token(producer)
            self._cache[producer.client_id] = (
                token,
                self._clock() + max(expires_in - RENEWAL_MARGIN_SECONDS, 1),
            )
            return token

    async def _request_token(self, producer: Producer) -> tuple[str, float]:
        raise NotImplementedError


class ClientCredentialsTokenProvider(CachingTokenProvider):
    """Exchanges each producer's client credentials for an access token.

    The scope is `<audience>/.default`, which yields a v2.0 token. The API
    accepts v1.0 as well, so either would work -- this is the shape a real
    service-to-service producer would use.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        directory_tenant_id: str,
        audience: str,
        clock=time.monotonic,
    ):
        super().__init__(clock=clock)
        self._client = client
        self._endpoint = TOKEN_ENDPOINT.format(tenant_id=directory_tenant_id)
        self._scope = f"{audience.rstrip('/')}/.default"

    async def _request_token(self, producer: Producer) -> tuple[str, float]:
        if not producer.client_id or not producer.client_secret:
            raise TokenError(
                f"producer {producer.name} has no client id or secret to authenticate with"
            )

        response = await self._client.post(
            self._endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": producer.client_id,
                "client_secret": producer.client_secret,
                "scope": self._scope,
            },
        )
        if response.status_code != 200:
            # The body names the missing consent or role, and without it a
            # failed run looks like a platform fault rather than a directory
            # one.
            raise TokenError(
                f"the directory refused a token for {producer.name}: "
                f"{response.status_code} {response.text}"
            )

        body = response.json()
        token = body.get("access_token")
        if not token:
            raise TokenError(
                f"the directory returned no access token for {producer.name}"
            )
        return token, float(body.get("expires_in", DEFAULT_LIFETIME_SECONDS))


class ManagedIdentityTokenProvider(CachingTokenProvider):
    """Asks the platform for a token on each producer identity's behalf.

    Container Apps exposes a local endpoint that mints tokens for the
    identities attached to the container, selected by client id. Nothing here
    holds a credential: the secret is the environment's, not ours.

    `resource` rather than `scope` yields a v1.0 token, which the ingestion
    service accepts alongside v2.0 -- and which names the identity in `appid`,
    the claim TENANT_MAP is keyed on.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        identity_endpoint: str,
        identity_header: str,
        audience: str,
        clock=time.monotonic,
        wall_clock=time.time,
    ):
        super().__init__(clock=clock)
        self._client = client
        self._endpoint = identity_endpoint
        self._header = identity_header
        self._audience = audience
        self._wall_clock = wall_clock

    async def _request_token(self, producer: Producer) -> tuple[str, float]:
        if not producer.client_id:
            raise TokenError(
                f"producer {producer.name} has no managed identity client id"
            )

        params = {
            "resource": self._audience,
            "api-version": IDENTITY_API_VERSION,
            "client_id": producer.client_id,
        }
        try:
            response = await self._client.get(
                self._endpoint,
                params=params,
                headers={"X-IDENTITY-HEADER": self._header},
            )
        except httpx.HTTPError as exc:
            raise TokenError(
                f"the identity endpoint could not be reached for {producer.name}: {exc}"
            ) from exc

        if response.status_code != 200:
            # Almost always one of two things, and the body says which: the
            # identity is not attached to this job, or it has not been granted
            # Incidents.Write on the API registration.
            raise TokenError(
                f"the identity endpoint refused a token for {producer.name}: "
                f"{response.status_code} {response.text}"
            )

        body = response.json()
        token = body.get("access_token")
        if not token:
            raise TokenError(f"the identity endpoint returned no token for {producer.name}")

        return token, self._lifetime(body)

    def _lifetime(self, body: dict) -> float:
        """Seconds of validity left, from whichever field the host returned.

        `expires_on` is epoch seconds here but a formatted date on some hosts,
        so an unparseable value falls back to a nominal lifetime rather than
        failing the run -- the renewal margin covers the difference.
        """
        raw = body.get("expires_on", body.get("expires_in"))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_LIFETIME_SECONDS

        # expires_in is a duration, expires_on an absolute time. Anything
        # larger than a plausible lifetime is the latter.
        if value > 86_400:
            return max(value - self._wall_clock(), RENEWAL_MARGIN_SECONDS + 1)
        return value

"""Authenticates producers and confines each one to its own tenant.

Two separate controls, and it is worth being precise about which is which.

*Authentication* asks whether the caller holds a token this platform issued
rights against: signed by our Entra tenant, addressed to this API, not expired,
and carrying the `Incidents.Write` role. Without it the API was open to the
internet -- anyone could POST an incident.

*Tenant isolation* asks a second question that authentication alone does not
answer: whether this caller may speak for the tenant named in the body. The
Common Event Model carries `tenant_id` as an ordinary field, so an authenticated
producer for one customer could otherwise submit incidents attributed to
another. The token's `appid` identifies the producer; the mapping from producer
to tenant lives here, and the request is rejected when the body disagrees.

That mapping is configuration today. It is the tenant registry the Subscription
API takes over, and the shape is deliberately the same: producer identity in,
tenant out.
"""

import logging
import os
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("app.auth")

ENTRA = "entra"
DISABLED = "disabled"

# Entra publishes v2.0 tokens under this issuer. Pinning it means a token signed
# by some other tenant -- valid, but not ours -- is rejected rather than merely
# well-formed.
ISSUER_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/v2.0"
JWKS_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"


class AuthError(HTTPException):
    def __init__(self, detail: str, status_code: int = status.HTTP_401_UNAUTHORIZED):
        super().__init__(
            status_code=status_code,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


@dataclass(frozen=True)
class Producer:
    """Who is calling, and which tenant they are allowed to speak for."""

    client_id: str
    tenant_id: str


def _parse_tenant_map(raw: str | None) -> dict[str, str]:
    """Read `appid:tenant,appid:tenant` into a lookup.

    Malformed entries are dropped with a warning rather than failing startup: a
    typo in one producer's mapping should cost that producer its access, not
    take the whole ingestion service down.
    """
    mapping: dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        client_id, separator, tenant_id = entry.partition(":")
        if not separator or not client_id.strip() or not tenant_id.strip():
            logger.warning("ignoring malformed TENANT_MAP entry: %r", entry)
            continue
        mapping[client_id.strip()] = tenant_id.strip()
    return mapping


class TokenVerifier:
    """Validates an Entra access token and resolves the producer behind it."""

    def __init__(
        self,
        tenant_id: str | None = None,
        audience: str | None = None,
        required_role: str | None = None,
        tenant_map: dict[str, str] | None = None,
        jwk_client=None,
    ):
        self.tenant_id = tenant_id or os.environ.get("AUTH_TENANT_ID")
        self.audience = audience or os.environ.get("AUTH_AUDIENCE")
        self.required_role = required_role or os.environ.get(
            "AUTH_REQUIRED_ROLE", "Incidents.Write"
        )
        self.tenant_map = (
            tenant_map
            if tenant_map is not None
            else _parse_tenant_map(os.environ.get("TENANT_MAP"))
        )

        if not self.tenant_id or not self.audience:
            raise ValueError(
                "AUTH_TENANT_ID and AUTH_AUDIENCE are required when "
                "AUTH_MODE=entra"
            )

        self.issuer = ISSUER_TEMPLATE.format(tenant_id=self.tenant_id)
        self._jwk_client = jwk_client

    def _keys(self):
        # PyJWKClient caches the signing keys after the first fetch, so this is
        # a network call once rather than once per request. It is warmed at
        # startup so the first real incident does not pay for it.
        if self._jwk_client is None:
            self._jwk_client = jwt.PyJWKClient(
                JWKS_TEMPLATE.format(tenant_id=self.tenant_id), cache_keys=True
            )
        return self._jwk_client

    def warm(self) -> None:
        try:
            self._keys().fetch_data()
        except Exception as exc:
            # Not fatal: the first request will retry. Worth a line, because a
            # cold JWKS fetch failing at startup usually means egress is blocked.
            logger.warning("could not pre-fetch signing keys: %s", exc)

    def verify(self, token: str) -> Producer:
        try:
            signing_key = self._keys().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("the access token has expired")
        except jwt.InvalidAudienceError:
            raise AuthError("the access token was issued for a different API")
        except jwt.InvalidIssuerError:
            raise AuthError("the access token was issued by a different tenant")
        except jwt.PyJWTError as exc:
            raise AuthError(f"the access token is not valid: {exc}")

        # Producers are services, so the right is granted as an application role.
        # A delegated scope is accepted too, which is how a human operator or a
        # test client presents the same right.
        granted = set(claims.get("roles") or [])
        granted |= set((claims.get("scp") or "").split())
        if self.required_role not in granted:
            raise AuthError(
                f"the access token does not carry {self.required_role}",
                status.HTTP_403_FORBIDDEN,
            )

        client_id = claims.get("azp") or claims.get("appid")
        if not client_id:
            raise AuthError("the access token does not identify a client")

        tenant_id = self.tenant_map.get(client_id)
        if not tenant_id:
            raise AuthError(
                "this client is not registered to publish for any tenant",
                status.HTTP_403_FORBIDDEN,
            )

        return Producer(client_id=client_id, tenant_id=tenant_id)


class OpenAccess:
    """Stands in for the verifier when authentication is switched off.

    Local development runs with no Entra tenant and no token. The deployed
    service must never reach this -- /health reports the mode and the deployment
    pipeline fails on anything but `entra`, which is the same guard the
    publisher already carries and for the same reason.
    """

    tenant_map: dict[str, str] = {}

    def warm(self) -> None:
        return None

    def verify(self, token: str) -> Producer:  # pragma: no cover - not reached
        raise AuthError("authentication is disabled")


def build_verifier():
    if os.environ.get("AUTH_MODE") == ENTRA:
        return TokenVerifier()
    return OpenAccess()


def auth_mode(verifier) -> str:
    return ENTRA if isinstance(verifier, TokenVerifier) else DISABLED


_bearer = HTTPBearer(auto_error=False)


def get_producer(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Producer | None:
    """Resolve the caller, or None when authentication is switched off."""
    verifier = request.app.state.verifier
    if isinstance(verifier, OpenAccess):
        return None

    if credentials is None or not credentials.credentials:
        raise AuthError("an Authorization: Bearer token is required")

    return verifier.verify(credentials.credentials)


def enforce_tenant(producer: Producer | None, claimed_tenant_id: str) -> None:
    """Reject an authenticated producer speaking for someone else's tenant."""
    if producer is None:
        return

    if producer.tenant_id != claimed_tenant_id:
        logger.warning(
            "tenant mismatch: client_id=%s is registered for %s but submitted %s",
            producer.client_id,
            producer.tenant_id,
            claimed_tenant_id,
        )
        raise AuthError(
            f"this producer may not publish incidents for {claimed_tenant_id}",
            status.HTTP_403_FORBIDDEN,
        )

"""Entra token validation and server-controlled client-to-tenant binding.

Like ingestion, this supports v1/v2 tokens and application roles or delegated
scopes. A shared frontend client requires additional user authorization before
it can serve multiple platform tenants.
"""

import logging
import os
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("app.auth")


@dataclass(frozen=True)
class TenantContext:
    client_id: str
    tenant_id: str
    permissions: frozenset[str]


def parse_tenant_map(raw: str) -> dict[str, str]:
    mapping = {}
    for entry in raw.split(","):
        if not entry.strip():
            continue
        client, separator, tenant = entry.partition(":")
        if not separator or not client.strip() or not tenant.strip():
            raise ValueError("TENANT_MAP must contain clientId:tenant entries")
        if client.strip() in mapping:
            raise ValueError("TENANT_MAP contains a duplicate client")
        mapping[client.strip()] = tenant.strip()
    return mapping


def unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


class TokenVerifier:
    mode = "entra"

    def __init__(self, directory_tenant: str, audience: str, tenant_map: dict[str, str], jwk_client=None):
        if not directory_tenant or not audience or not tenant_map:
            raise ValueError("AUTH_TENANT_ID, AUTH_AUDIENCE and TENANT_MAP are required")
        self.issuers = {
            f"https://login.microsoftonline.com/{directory_tenant}/v2.0",
            f"https://sts.windows.net/{directory_tenant}/",
        }
        self.audiences = [audience]
        if audience.startswith("api://"):
            self.audiences.append(audience[len("api://"):])
        self.tenant_map = tenant_map
        self.keys = jwk_client or jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{directory_tenant}/discovery/v2.0/keys",
            cache_keys=True,
        )

    def verify(self, token: str) -> TenantContext:
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.audiences,
                options={"require": ["exp", "iss", "aud"], "verify_iss": False},
            )
        except jwt.PyJWTError:
            raise unauthorized("invalid or expired access token") from None
        if claims["iss"] not in self.issuers:
            raise unauthorized("access token issued by a different directory")
        client_id = claims.get("azp") or claims.get("appid")
        tenant = self.tenant_map.get(client_id)
        if not tenant:
            raise HTTPException(403, "client is not registered for a platform tenant")
        permissions = frozenset(claims.get("roles") or []) | frozenset((claims.get("scp") or "").split())
        return TenantContext(client_id, tenant, permissions)


class DevelopmentVerifier:
    mode = "development"

    def __init__(self, tenant_id: str):
        if not tenant_id:
            raise ValueError("DEV_TENANT_ID is required in development mode")
        self.context = TenantContext("local-development", tenant_id, frozenset({"Subscriptions.Read", "Subscriptions.Write"}))


def build_verifier():
    mode = os.environ.get("AUTH_MODE", "entra")
    if mode == "development":
        logger.warning("development authentication enabled; do not expose this service publicly")
        return DevelopmentVerifier(os.environ.get("DEV_TENANT_ID", ""))
    if mode != "entra":
        raise ValueError("AUTH_MODE must be entra or development")
    return TokenVerifier(
        os.environ.get("AUTH_TENANT_ID", ""), os.environ.get("AUTH_AUDIENCE", ""),
        parse_tenant_map(os.environ.get("TENANT_MAP", "")),
    )


bearer = HTTPBearer(auto_error=False)


def get_context(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> TenantContext:
    verifier = request.app.state.verifier
    if isinstance(verifier, DevelopmentVerifier):
        return verifier.context
    if credentials is None:
        raise unauthorized("an Authorization: Bearer token is required")
    return verifier.verify(credentials.credentials)


def require_read(context: TenantContext = Depends(get_context)) -> TenantContext:
    if not ({"Subscriptions.Read", "Subscriptions.Write"} & context.permissions):
        raise HTTPException(403, "Subscriptions.Read permission is required")
    return context


def require_write(context: TenantContext = Depends(get_context)) -> TenantContext:
    if "Subscriptions.Write" not in context.permissions:
        raise HTTPException(403, "Subscriptions.Write permission is required")
    return context

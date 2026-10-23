"""Delegated-user Entra tokens for the account and access-management routes.

This is deliberately separate from app.auth. The legacy subscription routes
bind a *client application* to one tenant through TENANT_MAP; that binding says
nothing about which person is calling, so it must never authorize a user. These
routes accept only a delegated token a signed-in person obtained through an
approved dashboard client, identify that person by object ID, and leave every
application permission to the central access registry.
"""

import os
from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth import unauthorized

MAX_TOKEN_LENGTH = 65536


@dataclass(frozen=True)
class UserIdentity:
    object_id: str
    directory_id: str


class UserTokenVerifier:
    mode = "entra"

    def __init__(self, directory_tenant: str, audience: str, client_ids, scope: str = "user_impersonation", jwk_client=None):
        UUID(directory_tenant)
        if not audience or not scope or not client_ids:
            raise ValueError("AUTH_AUDIENCE, USER_AUTH_SCOPE and USER_AUTH_CLIENT_IDS are required")
        for client in client_ids:
            UUID(client)
        self.directory_id = directory_tenant
        self.issuers = [
            f"https://login.microsoftonline.com/{directory_tenant}/v2.0",
            f"https://sts.windows.net/{directory_tenant}/",
        ]
        self.audiences = [audience, audience.removeprefix("api://")]
        self.client_ids = frozenset(client_ids)
        self.scope = scope
        self.keys = jwk_client or jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{directory_tenant}/discovery/v2.0/keys",
            cache_keys=True,
        )

    def verify(self, token: str) -> UserIdentity:
        try:
            if not token or len(token) > MAX_TOKEN_LENGTH:
                raise ValueError("invalid token")
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.audiences, issuer=self.issuers,
                options={"require": ["exp", "iss", "aud", "tid", "oid"]},
            )
        except (jwt.PyJWTError, ValueError, TypeError, RecursionError):
            raise unauthorized("invalid or expired access token") from None
        scopes = claims.get("scp")
        client = claims.get("azp") or claims.get("appid")
        user = claims["oid"]
        # An application-only token (client credentials) has no scp and is not
        # a person, even when it carries every application role.
        if (claims["tid"] != self.directory_id or claims.get("idtyp") == "app"
                or not isinstance(scopes, str) or self.scope not in scopes.split()
                or client not in self.client_ids or not isinstance(user, str)):
            raise HTTPException(403, "a delegated user token from an approved client is required")
        try:
            UUID(user)
        except ValueError:
            raise HTTPException(403, "a delegated user token from an approved client is required") from None
        return UserIdentity(user, self.directory_id)


class DevelopmentUserVerifier:
    """Local only: a fixed person, mirroring app.auth.DevelopmentVerifier."""
    mode = "development"

    def __init__(self, object_id: str, directory_id: str):
        UUID(object_id)
        UUID(directory_id)
        self.identity = UserIdentity(object_id, directory_id)

    def verify(self, token: str) -> UserIdentity:
        return self.identity


def build_user_verifier():
    mode = os.environ.get("AUTH_MODE", "entra")
    if mode == "development":
        return DevelopmentUserVerifier(os.environ.get("DEV_USER_OBJECT_ID", ""), os.environ.get("ACCESS_DIRECTORY_ID", ""))
    if mode != "entra":
        raise ValueError("AUTH_MODE must be entra or development")
    return UserTokenVerifier(
        os.environ.get("AUTH_TENANT_ID", ""), os.environ.get("AUTH_AUDIENCE", ""),
        [value.strip() for value in os.environ.get("USER_AUTH_CLIENT_IDS", "").split(",") if value.strip()],
        os.environ.get("USER_AUTH_SCOPE", "user_impersonation"),
    )


bearer = HTTPBearer(auto_error=False)


def get_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> UserIdentity:
    verifier = getattr(request.app.state, "user_verifier", None)
    if verifier is None:
        raise HTTPException(404, "not found")
    if isinstance(verifier, DevelopmentUserVerifier):
        return verifier.identity
    if credentials is None:
        raise unauthorized("an Authorization: Bearer token is required")
    return verifier.verify(credentials.credentials)

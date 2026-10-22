"""Person-facing routes: central access (central-access-v1) and Telegram
self-service (self-service-v1). Both are off unless explicitly enabled, and
when off every route answers 404 exactly as before this module existed.

    ENABLE_CENTRAL_ACCESS=true          access/me, identity receipt, Admin routes
    ENABLE_TELEGRAM_SELF_SERVICE=true   /me/telegram routes and the bot webhook
                                        (requires central access)

Callers authenticate with a delegated user token (app.user_auth), never with
the client-wide TENANT_MAP binding the legacy subscription routes use.
"""

import json
import logging
import os
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints

from app.access_registry import (
    AccessRegistry, RegistryConflict, RegistryDenied, RegistryUnavailable, grant_response,
)
from app.access_roles import InvalidAccess
from app.platform_documents import CosmosPlatformDocuments, InMemoryPlatformDocuments
from app.telegram_self_service import DEFAULT_EVENT_TYPES, OwnershipRejected, RateLimited, TelegramSelfService
from app.user_auth import UserIdentity, build_user_verifier, get_user

logger = logging.getLogger("app.account_routes")
MAX_WEBHOOK_BYTES = 65536
NO_STORE = {"Cache-Control": "no-store"}

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Tenant = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
Tenants = Annotated[list[Tenant], Field(min_length=1, max_length=20)]
RoleName = Literal["Platform Admin", "SOC Operator", "SOC Viewer"]


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_id: str
    role: RoleName
    tenants: Tenants
    approval_reference: Text


class AccessChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: StrictInt
    reason: Text
    role: RoleName | None = None
    tenants: Tenants | None = None
    status: Literal["active", "disabled", "revoked"] | None = None


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    event_types: Annotated[list[Annotated[str, StringConstraints(max_length=100)]], Field(max_length=50)]


def _enabled(name):
    return os.environ.get(name, "").lower() == "true"


def build_account_features(store, policy):
    """(user_verifier, registry, telegram) from the environment; all None when off."""
    if not _enabled("ENABLE_CENTRAL_ACCESS"):
        if _enabled("ENABLE_TELEGRAM_SELF_SERVICE"):
            raise ValueError("ENABLE_TELEGRAM_SELF_SERVICE requires ENABLE_CENTRAL_ACCESS")
        return None, None, None
    directory = os.environ.get("ACCESS_DIRECTORY_ID", "")
    if os.environ.get("AUTH_MODE", "entra") == "entra" and directory != os.environ.get("AUTH_TENANT_ID"):
        raise ValueError("ACCESS_DIRECTORY_ID must equal AUTH_TENANT_ID")
    documents = InMemoryPlatformDocuments() if store.mode == "memory" else CosmosPlatformDocuments(store)
    tenants = [value.strip() for value in os.environ.get("ACCESS_PLATFORM_TENANTS", "").split(",") if value.strip()]
    registry = AccessRegistry(documents, directory, tenants)
    telegram = None
    if _enabled("ENABLE_TELEGRAM_SELF_SERVICE"):
        events = [value.strip() for value in os.environ.get("TELEGRAM_EVENT_TYPES", "").split(",") if value.strip()]
        telegram = TelegramSelfService(
            registry, documents, store, policy, os.environ.get("TELEGRAM_BOT_USERNAME", ""),
            os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""), events or DEFAULT_EVENT_TYPES)
    return build_user_verifier(), registry, telegram


def registry_of(request: Request) -> AccessRegistry:
    registry = request.app.state.registry
    if registry is None:
        raise HTTPException(404, "not found")
    return registry


def telegram_of(request: Request) -> TelegramSelfService:
    telegram = request.app.state.telegram
    if telegram is None:
        raise HTTPException(404, "not found")
    return telegram


def register_account_routes(app: FastAPI) -> None:
    @app.exception_handler(RegistryDenied)
    async def denied(request: Request, exc: RegistryDenied):
        return JSONResponse(status_code=403, content={"detail": "access denied"}, headers=NO_STORE)

    @app.exception_handler(RegistryConflict)
    async def conflict(request: Request, exc: RegistryConflict):
        return JSONResponse(status_code=409, content={"detail": "access record changed; refresh and retry"}, headers=NO_STORE)

    @app.exception_handler(RegistryUnavailable)
    async def unavailable(request: Request, exc: RegistryUnavailable):
        logger.error("central access registry unavailable")
        return JSONResponse(status_code=503, content={"detail": "access registry unavailable"}, headers=NO_STORE)

    @app.exception_handler(RateLimited)
    async def limited(request: Request, exc: RateLimited):
        return JSONResponse(status_code=429, content={"detail": "too many requests; retry later"},
                            headers={**NO_STORE, "Retry-After": "60"})

    def invalid(exc):
        raise HTTPException(422, "invalid access request") from exc

    @app.get("/api/v1/access/me")
    async def access_me(response: Response, user: UserIdentity = Depends(get_user), registry: AccessRegistry = Depends(registry_of)):
        response.headers.update(NO_STORE)
        # Resolved fresh on every call; no cache and no fallback grant.
        return grant_response(await registry.resolve(user.object_id))

    @app.post("/api/v1/access/identity-proof")
    async def identity_proof(response: Response, user: UserIdentity = Depends(get_user), registry: AccessRegistry = Depends(registry_of)):
        response.headers.update(NO_STORE)
        return await registry.record_identity_receipt(user.object_id)

    @app.get("/api/v1/admin/access/users")
    async def list_access(response: Response, cursor: str | None = Query(None, max_length=36), limit: int = Query(50, ge=1, le=100),
                          user: UserIdentity = Depends(get_user), registry: AccessRegistry = Depends(registry_of)):
        response.headers.update(NO_STORE)
        try:
            return await registry.users(user.object_id, cursor, limit)
        except ValueError as exc:
            invalid(exc)

    @app.post("/api/v1/admin/access/users", status_code=201)
    async def register_user(payload: Registration, response: Response, user: UserIdentity = Depends(get_user), registry: AccessRegistry = Depends(registry_of)):
        response.headers.update(NO_STORE)
        try:
            grant = await registry.register(user.object_id, payload.object_id, payload.role, payload.tenants, payload.approval_reference)
        except (ValueError, InvalidAccess) as exc:
            invalid(exc)
        return grant.public()

    @app.patch("/api/v1/admin/access/users/{object_id}")
    async def change_user(object_id: str, payload: AccessChange, response: Response, user: UserIdentity = Depends(get_user), registry: AccessRegistry = Depends(registry_of)):
        response.headers.update(NO_STORE)
        changes = payload.model_dump(exclude_unset=True, exclude={"expected_version", "reason"})
        try:
            grant = await registry.change(user.object_id, object_id, payload.expected_version, changes, payload.reason)
        except (ValueError, InvalidAccess) as exc:
            invalid(exc)
        return grant.public()

    @app.get("/api/v1/admin/telegram/pending-destinations")
    async def pending_destinations(response: Response, tenant_id: Tenant = Query(...), user: UserIdentity = Depends(get_user), telegram: TelegramSelfService = Depends(telegram_of)):
        response.headers.update(NO_STORE)
        return await telegram.pending_destinations(user.object_id, tenant_id)

    @app.get("/api/v1/me/telegram")
    async def telegram_status(response: Response, tenant_id: Tenant = Query(...), user: UserIdentity = Depends(get_user), telegram: TelegramSelfService = Depends(telegram_of)):
        response.headers.update(NO_STORE)
        return await telegram.status(user.object_id, tenant_id)

    @app.post("/api/v1/me/telegram/challenges", status_code=201)
    async def telegram_challenge(response: Response, tenant_id: Tenant = Query(...), user: UserIdentity = Depends(get_user), telegram: TelegramSelfService = Depends(telegram_of)):
        response.headers.update(NO_STORE)
        return await telegram.challenge(user.object_id, tenant_id)

    @app.put("/api/v1/me/telegram/preferences")
    async def telegram_preferences(payload: Preferences, response: Response, tenant_id: Tenant = Query(...), user: UserIdentity = Depends(get_user), telegram: TelegramSelfService = Depends(telegram_of)):
        response.headers.update(NO_STORE)
        try:
            return await telegram.save_preferences(user.object_id, tenant_id, payload.enabled, payload.event_types)
        except ValueError as exc:
            invalid(exc)

    @app.post("/api/v1/telegram/webhook")
    async def telegram_webhook(request: Request, telegram: TelegramSelfService = Depends(telegram_of)):
        # Telegram proves the call is genuine only through the secret header
        # configured with setWebhook; there is no bearer token here.
        if not telegram.webhook_authentic(request.headers.get("X-Telegram-Bot-Api-Secret-Token")):
            telegram.admit_unauthenticated()
            raise HTTPException(401, "webhook not authenticated")
        declared = request.headers.get("content-length")
        if declared is None or not declared.isdigit() or int(declared) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "update too large")
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "update too large")
        try:
            update = json.loads(body)
        except (ValueError, RecursionError):
            raise HTTPException(400, "invalid update") from None
        if not telegram.admit_update(update):
            # One sender over its limit: acknowledge so Telegram stops
            # redelivering, but do no lookup or write for it.
            logger.warning("telegram sender over rate limit; update dropped")
            return {"ok": True}
        try:
            await telegram.confirm_ownership(update)
        except OwnershipRejected:
            # Acknowledge so Telegram does not redeliver an update that can
            # never succeed; the user simply requests a new link.
            logger.info("telegram ownership confirmation rejected")
        # Returning nothing sends nothing: linking never messages the user.
        return {"ok": True}

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from azure.core.exceptions import AzureError

from app.auth import TenantContext, build_verifier, require_read, require_write
from app.models import Subscription, SubscriptionCreate, SubscriptionPage, SubscriptionPatch
from app.store import SubscriptionStore, build_store
from app.destination_policy import CosmosDestinationPolicy, PolicyUnavailable

logger = logging.getLogger("app.main")
MAX_SUBSCRIPTION_OFFSET = 10000


def get_store(request: Request) -> SubscriptionStore:
    return request.app.state.store


def create_app(store=None, verifier=None, policy=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.verifier = verifier if verifier is not None else build_verifier()
        app.state.store = store if store is not None else build_store()
        app.state.policy = policy if policy is not None else CosmosDestinationPolicy()
        try:
            await app.state.store.start()
            yield
        finally:
            try:
                await app.state.store.close()
            finally:
                await app.state.policy.close()

    app = FastAPI(title="Scalable Incident Platform - Subscription Service", version="1.0.0", lifespan=lifespan)

    async def authorize(subscription, tenant_id):
        if not subscription.enabled:
            return
        if (await app.state.policy.approved(tenant_id, subscription.telegram_chat_id)) is not True:
            raise HTTPException(403, "destination not approved")

    @app.exception_handler(PolicyUnavailable)
    async def policy_error(request: Request, exc: PolicyUnavailable):
        logger.error("destination policy unavailable")
        return JSONResponse(status_code=503, content={"detail": "destination policy unavailable"})

    @app.exception_handler(AzureError)
    async def storage_error(request: Request, exc: AzureError):
        logger.error("subscription storage operation failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "subscription store unavailable"})

    @app.get("/health")
    async def health(request: Request):
        return {"status": "healthy", "service": "subscription-service", "store": request.app.state.store.mode, "auth": request.app.state.verifier.mode}

    @app.get("/ready")
    async def ready(response: Response, store: SubscriptionStore = Depends(get_store)):
        try:
            available = await store.ready()
        except Exception:
            available = False
        if not available:
            response.status_code = 503
        return {"status": "ready" if available else "not-ready", "service": "subscription-service", "store": store.mode}

    @app.post("/api/v1/subscriptions", response_model=Subscription, status_code=201)
    async def create_subscription(payload: SubscriptionCreate, response: Response, context: TenantContext = Depends(require_write), store: SubscriptionStore = Depends(get_store)):
        now = datetime.now(timezone.utc)
        subscription = Subscription(**payload.model_dump(), id=f"SUB-{uuid4()}", tenant_id=context.tenant_id, created_at=now, updated_at=now)
        await authorize(subscription, context.tenant_id)
        result = await store.create(subscription)
        response.headers["Location"] = f"/api/v1/subscriptions/{result.id}"
        return result

    @app.get("/api/v1/subscriptions", response_model=SubscriptionPage)
    async def list_subscriptions(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=MAX_SUBSCRIPTION_OFFSET), context: TenantContext = Depends(require_read), store: SubscriptionStore = Depends(get_store)):
        items = await store.list(context.tenant_id, limit + 1, offset)
        next_offset = offset + limit
        # Do not advertise a continuation that request validation will reject.
        has_next_page = len(items) > limit and next_offset <= MAX_SUBSCRIPTION_OFFSET
        return SubscriptionPage(items=items[:limit], next_offset=next_offset if has_next_page else None)

    @app.get("/api/v1/subscriptions/{subscription_id}", response_model=Subscription)
    async def get_subscription(subscription_id: str, context: TenantContext = Depends(require_read), store: SubscriptionStore = Depends(get_store)):
        result = await store.get(context.tenant_id, subscription_id)
        if result is None:
            raise HTTPException(404, "subscription not found")
        return result

    @app.patch("/api/v1/subscriptions/{subscription_id}", response_model=Subscription)
    async def patch_subscription(subscription_id: str, payload: SubscriptionPatch, context: TenantContext = Depends(require_write), store: SubscriptionStore = Depends(get_store)):
        existing = await store.get(context.tenant_id, subscription_id)
        if existing is None:
            raise HTTPException(404, "subscription not found")
        updated = existing.model_copy(update={**payload.model_dump(exclude_unset=True), "updated_at": datetime.now(timezone.utc)})
        await authorize(updated, context.tenant_id)
        result = await store.replace(updated)
        if result is None:
            raise HTTPException(404, "subscription not found")
        return result

    @app.delete("/api/v1/subscriptions/{subscription_id}", status_code=204)
    async def delete_subscription(subscription_id: str, context: TenantContext = Depends(require_write), store: SubscriptionStore = Depends(get_store)):
        if not await store.delete(context.tenant_id, subscription_id):
            raise HTTPException(404, "subscription not found")
        return Response(status_code=204)

    return app


app = create_app()

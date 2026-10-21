"""Central access and Telegram self-service routes. No network, no Telegram."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import jwt
import pytest
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosResourceNotFoundError
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.access_registry import AccessRegistry
from app.access_roles import BOOTSTRAP_ADMIN_ID
from app.account_routes import build_account_features
from app.auth import TenantContext, parse_tenant_map
from app.main import create_app
from app.platform_documents import (
    ACCESS_PARTITION, CosmosPlatformDocuments, DocumentConflict, DocumentsUnavailable, InMemoryPlatformDocuments,
)
from app.rate_limit import SlidingWindowLimiter
from app.store import InMemorySubscriptionStore
from app.telegram_self_service import TelegramSelfService
from app.user_auth import UserTokenVerifier

DIRECTORY = "00000000-0000-4000-8000-0000000000d1"
CLIENT = "00000000-0000-4000-8000-0000000000c1"
AUDIENCE = "api://subscription-test"
VIEWER = "00000000-0000-4000-8000-000000000101"
OTHER = "00000000-0000-4000-8000-000000000102"
SECRET = "w" * 40
TENANTS = ("TENANT-01", "TENANT-02", "TENANT-03")
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(user=BOOTSTRAP_ADMIN_ID, **changes):
    claims = dict(iss=f"https://login.microsoftonline.com/{DIRECTORY}/v2.0", aud=AUDIENCE, exp=int(time.time()) + 300,
                  tid=DIRECTORY, oid=user, azp=CLIENT, scp="user_impersonation")
    claims.update(changes)
    return {"Authorization": "Bearer " + jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")}


class ClientVerifier:
    mode = "entra"

    def verify(self, value):
        if value != "client":
            raise HTTPException(401, "invalid token")
        return TenantContext("client", "TENANT-01", frozenset({"Subscriptions.Read", "Subscriptions.Write"}))


class Policy:
    def __init__(self):
        self.approved_chats = set()

    async def approved(self, tenant_id, chat_id):
        return (tenant_id, chat_id) in self.approved_chats

    async def close(self):
        pass


@pytest.fixture
def env():
    documents, store, policy = InMemoryPlatformDocuments(), InMemorySubscriptionStore(), Policy()
    keys = SimpleNamespace(get_signing_key_from_jwt=lambda value: SimpleNamespace(key=KEY.public_key()))
    users = UserTokenVerifier(DIRECTORY, AUDIENCE, [CLIENT], jwk_client=keys)
    registry = AccessRegistry(documents, DIRECTORY, TENANTS)
    now = [1000.0]
    telegram = TelegramSelfService(registry, documents, store, policy, "IncidentTestBot", SECRET, limit_clock=lambda: now[0])
    app = create_app(store, ClientVerifier(), policy, account=(users, registry, telegram))
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, documents=documents, store=store, policy=policy, registry=registry, now=now)


def register(env, user=VIEWER, role="SOC Viewer", tenants=("TENANT-01",)):
    assert env.client.post("/api/v1/access/identity-proof", headers=token(user)).status_code == 200
    response = env.client.post("/api/v1/admin/access/users", headers=token(), json=dict(
        object_id=user, role=role, tenants=list(tenants), approval_reference="TICKET-1"))
    assert response.status_code == 201, response.text
    return response.json()


def link(env, user, chat, tenant="TENANT-01"):
    deep_link = env.client.post(f"/api/v1/me/telegram/challenges?tenant_id={tenant}", headers=token(user)).json()["deep_link"]
    return env.client.post("/api/v1/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
                           json=update(chat, "/start " + deep_link.split("start=")[1]))


def update(chat, text, chat_type="private", sender=None):
    return {"update_id": 1, "message": {"message_id": 1, "date": 0, "text": text,
            "from": {"id": sender or chat, "is_bot": False, "first_name": "T"}, "chat": {"id": chat, "type": chat_type}}}


def status(env, user, tenant="TENANT-01"):
    return env.client.get(f"/api/v1/me/telegram?tenant_id={tenant}", headers=token(user))


def test_routes_are_404_when_not_enabled(monkeypatch):
    monkeypatch.delenv("ENABLE_CENTRAL_ACCESS", raising=False)
    monkeypatch.delenv("ENABLE_TELEGRAM_SELF_SERVICE", raising=False)
    store = InMemorySubscriptionStore()
    assert build_account_features(store, Policy()) == (None, None, None)
    with TestClient(create_app(store, ClientVerifier(), Policy())) as client:
        for method, path in [("get", "/api/v1/access/me"), ("post", "/api/v1/access/identity-proof"),
                             ("get", "/api/v1/admin/access/users"), ("get", "/api/v1/me/telegram?tenant_id=TENANT-01"),
                             ("post", "/api/v1/telegram/webhook")]:
            assert getattr(client, method)(path, headers=token()).status_code == 404


def test_self_service_requires_central_access(monkeypatch):
    monkeypatch.delenv("ENABLE_CENTRAL_ACCESS", raising=False)
    monkeypatch.setenv("ENABLE_TELEGRAM_SELF_SERVICE", "true")
    with pytest.raises(ValueError):
        build_account_features(InMemorySubscriptionStore(), Policy())


def test_reserved_partition_cannot_be_bound_to_a_client():
    with pytest.raises(ValueError):
        parse_tenant_map("client-a:" + ACCESS_PARTITION)


@pytest.mark.parametrize("changes,status_code", [
    ({"scp": None}, 403), ({"scp": "Subscriptions.Read"}, 403), ({"idtyp": "app"}, 403),
    ({"azp": "00000000-0000-4000-8000-0000000000ff"}, 403), ({"oid": "not-a-uuid"}, 403),
    ({"aud": "api://other"}, 401), ({"exp": 1}, 401), ({"iss": "https://evil.example/v2.0"}, 401),
    ({"tid": "00000000-0000-4000-8000-0000000000ee"}, 403),
])
def test_only_delegated_users_from_approved_clients(env, changes, status_code):
    assert env.client.get("/api/v1/access/me", headers=token(**changes)).status_code == status_code


def test_client_token_is_not_a_user(env):
    assert env.client.get("/api/v1/access/me", headers={"Authorization": "Bearer client"}).status_code == 401


def test_bootstrap_admin_is_seeded_once_and_immutable(env):
    me = env.client.get("/api/v1/access/me", headers=token())
    assert me.status_code == 200 and me.headers["cache-control"] == "no-store"
    assert me.json()["role"] == "Platform Admin" and me.json()["tenants"] == list(TENANTS)
    assert "Access.Manage" in me.json()["permissions"] and me.json()["contract"] == "central-access-v1"
    assert env.client.get("/api/v1/access/me", headers=token()).json()["version"] == 1
    response = env.client.patch(f"/api/v1/admin/access/users/{BOOTSTRAP_ADMIN_ID}", headers=token(),
                                json={"expected_version": 1, "reason": "x", "status": "disabled"})
    assert response.status_code == 403


def test_unknown_user_denied_and_receipt_grants_nothing(env):
    assert env.client.get("/api/v1/access/me", headers=token(VIEWER)).status_code == 403
    assert env.client.post("/api/v1/access/identity-proof", headers=token(VIEWER)).status_code == 200
    assert env.client.get("/api/v1/access/me", headers=token(VIEWER)).status_code == 403


def test_registration_requires_fresh_receipt_and_original_admin(env):
    body = dict(object_id=VIEWER, role="SOC Viewer", tenants=["TENANT-01"], approval_reference="TICKET-1")
    assert env.client.post("/api/v1/admin/access/users", headers=token(), json=body).status_code == 403
    env.client.post("/api/v1/access/identity-proof", headers=token(VIEWER))
    receipt = env.documents.items["identity-receipt:" + VIEWER]
    receipt["expires_at"] = int(time.time()) - 1
    assert env.client.post("/api/v1/admin/access/users", headers=token(), json=body).status_code == 403
    register(env)
    assert "identity-receipt:" + VIEWER not in env.documents.items  # consumed
    assert env.client.post("/api/v1/admin/access/users", headers=token(), json={**body, "extra": 1}).status_code == 422
    # A Viewer cannot manage access, and nobody but the original Admin registers.
    assert env.client.get("/api/v1/admin/access/users", headers=token(VIEWER)).status_code == 403
    register(env, OTHER, "Platform Admin", TENANTS)
    env.client.post("/api/v1/access/identity-proof", headers=token("00000000-0000-4000-8000-000000000103"))
    denied = env.client.post("/api/v1/admin/access/users", headers=token(OTHER), json={**body, "object_id": "00000000-0000-4000-8000-000000000103"})
    assert denied.status_code == 403


def test_out_of_scope_tenant_rejected(env):
    env.client.post("/api/v1/access/identity-proof", headers=token(VIEWER))
    response = env.client.post("/api/v1/admin/access/users", headers=token(), json=dict(
        object_id=VIEWER, role="SOC Viewer", tenants=["TENANT-99"], approval_reference="x"))
    assert response.status_code in (403, 422)
    assert env.client.get("/api/v1/access/me", headers=token(VIEWER)).status_code == 403


def test_change_uses_versions_audits_and_never_reverses_revocation(env):
    register(env)
    path = f"/api/v1/admin/access/users/{VIEWER}"
    stale = env.client.patch(path, headers=token(), json={"expected_version": 9, "reason": "x", "status": "disabled"})
    assert stale.status_code == 409
    assert env.client.patch(path, headers=token(), json={"expected_version": 1, "reason": "leave", "status": "disabled"}).json()["version"] == 2
    assert env.client.get("/api/v1/access/me", headers=token(VIEWER)).status_code == 403
    assert env.client.patch(path, headers=token(), json={"expected_version": 2, "reason": "back", "status": "active"}).status_code == 200
    assert env.client.patch(path, headers=token(), json={"expected_version": 3, "reason": "gone", "status": "revoked"}).status_code == 200
    assert env.client.patch(path, headers=token(), json={"expected_version": 4, "reason": "undo", "status": "active"}).status_code == 403
    audits = [item for item in env.documents.items.values() if item["record_type"] == "access_audit"]
    assert [item["action"] for item in audits].count("change") == 3 and all(item["tenant_id"] == ACCESS_PARTITION for item in audits)


def test_listing_pages_and_hides_private_fields(env):
    register(env)
    page = env.client.get("/api/v1/admin/access/users?limit=1", headers=token()).json()
    assert page["contract"] == "central-access-v1" and len(page["items"]) == 1 and page["next_cursor"]
    rest = env.client.get(f"/api/v1/admin/access/users?cursor={page['next_cursor']}", headers=token()).json()
    assert {item["object_id"] for item in page["items"] + rest["items"]} == {VIEWER, BOOTSTRAP_ADMIN_ID}
    assert set(rest["items"][0]) == {"directory_id", "object_id", "role", "tenants", "status", "version"}


def test_registry_outage_is_503_without_fallback(env):
    env.documents.fail = True
    assert env.client.get("/api/v1/access/me", headers=token()).status_code == 503
    assert env.client.get("/api/v1/me/telegram?tenant_id=TENANT-01", headers=token()).status_code == 503


def test_webhook_authentication_and_bounds(env):
    register(env)
    assert env.client.post("/api/v1/telegram/webhook", json=update(5, "/start x")).status_code == 401
    wrong = {"X-Telegram-Bot-Api-Secret-Token": "x" * 40}
    assert env.client.post("/api/v1/telegram/webhook", headers=wrong, json=update(5, "/start x")).status_code == 401
    big = json.dumps(update(5, "a" * 70000))
    response = env.client.post("/api/v1/telegram/webhook", content=big, headers={
        "X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"})
    assert response.status_code == 413


def test_ownership_requires_private_sender_and_is_single_use(env):
    register(env)
    deep_link = env.client.post("/api/v1/me/telegram/challenges?tenant_id=TENANT-01", headers=token(VIEWER)).json()["deep_link"]
    challenge = "/start " + deep_link.split("start=")[1]
    hook = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    for forged in (update(-1001, challenge, "group", 77), update(77, challenge, sender=78), update(77, "/start " + "A" * 43)):
        assert env.client.post("/api/v1/telegram/webhook", headers=hook, json=forged).json() == {"ok": True}
        assert status(env, VIEWER).json()["verified"] is False
    assert env.client.post("/api/v1/telegram/webhook", headers=hook, json=update(77, challenge)).status_code == 200
    assert status(env, VIEWER).json()["verified"] is True
    # Replay by another sender cannot move the binding.
    env.client.post("/api/v1/telegram/webhook", headers=hook, json=update(88, challenge))
    assert env.documents.items["telegram-owner:" + VIEWER]["chat_id"] == "77"


def test_expired_challenge_rejected(env):
    register(env)
    deep_link = env.client.post("/api/v1/me/telegram/challenges?tenant_id=TENANT-01", headers=token(VIEWER)).json()["deep_link"]
    env.documents.items["telegram-challenge:" + VIEWER]["expires_at"] = int(time.time()) - 1
    env.client.post("/api/v1/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
                    json=update(77, "/start " + deep_link.split("start=")[1]))
    assert status(env, VIEWER).json()["verified"] is False


def test_one_telegram_account_cannot_serve_two_people(env):
    register(env)
    register(env, OTHER)
    link(env, VIEWER, 77)
    link(env, OTHER, 77)
    assert status(env, OTHER).json()["verified"] is False


def test_opt_in_requires_ownership_and_approved_destination(env):
    register(env)
    prefs = {"enabled": True, "event_types": ["intrusion_detected"]}
    url = "/api/v1/me/telegram/preferences?tenant_id=TENANT-01"
    assert env.client.put(url, headers=token(VIEWER), json=prefs).status_code == 403
    link(env, VIEWER, 77)
    assert status(env, VIEWER).json()["verified"] is True
    assert env.client.put(url, headers=token(VIEWER), json=prefs).status_code == 403  # not approved
    env.policy.approved_chats.add(("TENANT-01", "77"))
    assert env.client.put(url, headers=token(VIEWER), json={**prefs, "event_types": ["made_up"]}).status_code == 422
    assert env.client.put(url, headers=token(VIEWER), json={**prefs, "chat_id": "1"}).status_code == 422
    saved = env.client.put(url, headers=token(VIEWER), json=prefs).json()
    assert saved["enabled"] is True and saved["event_types"] == ["intrusion_detected"] and "chat" not in json.dumps(saved)
    again = env.client.put(url, headers=token(VIEWER), json=prefs).json()
    assert again == saved and len(env.store.items) == 1  # idempotent
    # Tenant outside the user's assignment is denied.
    assert env.client.put("/api/v1/me/telegram/preferences?tenant_id=TENANT-02", headers=token(VIEWER), json=prefs).status_code == 403


def test_legacy_routes_hide_owner_and_cannot_repoint_owned_subscriptions(env):
    register(env)
    link(env, VIEWER, 77)
    env.policy.approved_chats.add(("TENANT-01", "77"))
    env.client.put("/api/v1/me/telegram/preferences?tenant_id=TENANT-01", headers=token(VIEWER),
                   json={"enabled": True, "event_types": ["intrusion_detected"]})
    client = {"Authorization": "Bearer client"}
    items = env.client.get("/api/v1/subscriptions", headers=client).json()["items"]
    assert len(items) == 1 and "owner_object_id" not in items[0]
    path = "/api/v1/subscriptions/" + items[0]["id"]
    assert "owner_object_id" not in env.client.get(path, headers=client).json()
    assert env.client.patch(path, headers=client, json={"telegram_chat_id": "99"}).status_code == 403
    assert env.client.delete(path, headers=client).status_code == 204


def test_rebinding_a_new_chat_strands_old_subscriptions(env):
    register(env)
    link(env, VIEWER, 77)
    env.policy.approved_chats |= {("TENANT-01", "77"), ("TENANT-01", "78")}
    url = "/api/v1/me/telegram/preferences?tenant_id=TENANT-01"
    env.client.put(url, headers=token(VIEWER), json={"enabled": True, "event_types": ["fire_detected"]})
    link(env, VIEWER, 78)
    assert status(env, VIEWER).json()["enabled"] is False
    assert "telegram-chat:77" not in env.documents.items and "telegram-chat:78" in env.documents.items
    assert env.client.put(url, headers=token(VIEWER), json={"enabled": True, "event_types": ["fire_detected"]}).json()["enabled"] is True
    assert [item.telegram_chat_id for item in env.store.items.values()] == ["78"]


async def test_cosmos_documents_translate_conflicts_and_refuse_missing_container():
    container = Mock(read_item=AsyncMock(side_effect=CosmosResourceNotFoundError()),
                     read=AsyncMock(return_value={"partitionKey": {"paths": ["/id"]}}))
    documents = CosmosPlatformDocuments(SimpleNamespace(container=container))
    with pytest.raises(DocumentsUnavailable):
        await documents.read("access:x")
    container.read.return_value = {"partitionKey": {"paths": ["/tenant_id"]}}
    assert await documents.read("access:x") is None
    error = CosmosBatchOperationError(error_index=0, headers={}, status_code=400, message="m",
                                      operation_responses=[{"statusCode": 412}])
    container.execute_item_batch = AsyncMock(side_effect=error)
    with pytest.raises(DocumentConflict):
        await documents.batch([("replace", {"id": "a", "_etag": "1"}, "1"), ("delete", "b", "2")])
    sent = container.execute_item_batch.await_args.kwargs
    assert sent["partition_key"] == ACCESS_PARTITION
    assert sent["batch_operations"][0] == ("replace", ("a", {"id": "a"}), {"if_match_etag": "1"})
    with pytest.raises(DocumentsUnavailable):
        await CosmosPlatformDocuments(SimpleNamespace(container=None)).read("x")


# --- rate limits (local only; the clock is simulated) ---------------------------

def test_sliding_window_limiter_expires_and_bounds_keys():
    now = [0.0]
    limiter = SlidingWindowLimiter(2, 10, max_keys=2, clock=lambda: now[0])
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")
    now[0] = 10.01
    assert limiter.allow("a")
    limiter.allow("b"), limiter.allow("c")
    assert list(limiter.events) == ["b", "c"]  # least recently used key forgotten
    with pytest.raises(ValueError):
        SlidingWindowLimiter(0, 10)


def test_challenge_requests_are_limited_per_user(env):
    register(env)
    register(env, OTHER)
    url = "/api/v1/me/telegram/challenges?tenant_id=TENANT-01"
    assert [env.client.post(url, headers=token(VIEWER)).status_code for _ in range(6)] == [201] * 5 + [429]
    limited = env.client.post(url, headers=token(VIEWER))
    assert limited.headers["retry-after"] == "60" and "start=" not in limited.text
    assert env.client.post(url, headers=token(OTHER)).status_code == 201  # per person
    env.now[0] += 901
    assert env.client.post(url, headers=token(VIEWER)).status_code == 201


def test_webhook_sender_flood_is_dropped_without_lookups(env):
    register(env)
    hook = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    for _ in range(10):
        assert env.client.post("/api/v1/telegram/webhook", headers=hook, json=update(55, "/start " + "A" * 43)).status_code == 200
    deep_link = env.client.post("/api/v1/me/telegram/challenges?tenant_id=TENANT-01", headers=token(VIEWER)).json()["deep_link"]
    genuine = update(55, "/start " + deep_link.split("start=")[1])
    assert env.client.post("/api/v1/telegram/webhook", headers=hook, json=genuine).json() == {"ok": True}
    assert status(env, VIEWER).json()["verified"] is False  # dropped, not processed
    assert env.client.post("/api/v1/telegram/webhook", headers=hook, json=update(56, "hello")).status_code == 200
    env.now[0] += 61
    env.client.post("/api/v1/telegram/webhook", headers=hook, json=genuine)
    assert status(env, VIEWER).json()["verified"] is True


def test_webhook_global_saturation_returns_429(env):
    hook = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    codes = [env.client.post("/api/v1/telegram/webhook", headers=hook, json=update(1000 + n, "hi")).status_code
             for n in range(121)]
    assert codes[:120] == [200] * 120 and codes[120] == 429


def test_unauthenticated_webhook_flood_returns_429(env):
    codes = [env.client.post("/api/v1/telegram/webhook", json=update(5, "x")).status_code for _ in range(31)]
    assert codes[:30] == [401] * 30 and codes[30] == 429


# --- destination review for Platform Admins (approval stays with the operator) --

def test_pending_destinations_lists_only_verified_authorized_unapproved(env):
    register(env)
    register(env, OTHER, tenants=("TENANT-02",))
    link(env, VIEWER, 77)
    link(env, OTHER, 88, tenant="TENANT-02")
    before = dict(env.documents.items)
    page = env.client.get("/api/v1/admin/telegram/pending-destinations?tenant_id=TENANT-01", headers=token())
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    body = page.json()
    assert body["contract"] == "destination-review-v1" and [item["object_id"] for item in body["items"]] == [VIEWER]
    assert body["items"][0]["chat_hint"] == "…77"
    assert body["items"][0]["approval_record"] == {"id": "telegram:77", "tenant_id": "TENANT-01", "channel": "telegram",
                                                   "telegram_chat_id": "77", "enabled": True}
    audits = [item for key, item in env.documents.items.items() if key not in before]
    assert len(audits) == 1 and audits[0]["action"] == "destination_review" and audits[0]["disclosed_object_ids"] == [VIEWER]
    # Approved destinations drop off; the API itself approved nothing.
    env.policy.approved_chats.add(("TENANT-01", "77"))
    assert env.client.get("/api/v1/admin/telegram/pending-destinations?tenant_id=TENANT-01", headers=token()).json()["items"] == []


def test_pending_destinations_denied_to_non_admins_and_skips_revoked(env):
    register(env)
    link(env, VIEWER, 77)
    url = "/api/v1/admin/telegram/pending-destinations?tenant_id=TENANT-01"
    assert env.client.get(url, headers=token(VIEWER)).status_code == 403
    env.client.patch(f"/api/v1/admin/access/users/{VIEWER}", headers=token(), json={"expected_version": 1, "reason": "x", "status": "revoked"})
    assert env.client.get(url, headers=token()).json()["items"] == []


def test_pending_review_is_withheld_when_audit_cannot_be_written(env):
    register(env)
    link(env, VIEWER, 77)
    original = env.documents.batch

    async def no_audit(operations):
        if any(op[0] == "create" and op[1].get("action") == "destination_review" for op in operations):
            raise RuntimeError("cosmos down")
        return await original(operations)
    env.documents.batch = no_audit
    response = env.client.get("/api/v1/admin/telegram/pending-destinations?tenant_id=TENANT-01", headers=token())
    assert response.status_code == 503 and "77" not in response.text

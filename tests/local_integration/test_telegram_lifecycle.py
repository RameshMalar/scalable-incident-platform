"""Telegram Subscription API lifecycle across the real services, locally.

Subscription API (routes, registry, self-service) -> Worker routing ->
notification-events envelope -> Notification processor -> mocked Telegram.

Entra tokens are genuine RS256 JWTs validated by the real verifier against a
test key. Telegram delivery is MockNotifier; the bot webhook is called as
Telegram would call it. Nothing leaves the process: sockets to anything but
loopback fail, no Azure credential is constructed, and no live incident or
Telegram message is produced.
"""
import importlib
import importlib.util
import ipaddress
import json
import socket
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


def load(directory, alias, names):
    """Import one service's `app` package under an alias so all three coexist."""
    saved = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
    for key in saved:
        del sys.modules[key]
    package = types.ModuleType("app")
    package.__path__ = [str(ROOT / directory / "app")]
    sys.modules["app"] = package
    try:
        modules = {name: importlib.import_module("app." + name) for name in names}
    finally:
        for key in list(sys.modules):
            if key == "app" or key.startswith("app."):
                sys.modules[alias + key[3:]] = sys.modules.pop(key)
        sys.modules.update(saved)
    return SimpleNamespace(**modules)


S = load("subscription-service", "lifecycle_subscription", [
    "main", "store", "access_registry", "access_roles", "platform_documents", "telegram_self_service", "user_auth", "auth"])
W = load("worker-service", "lifecycle_worker", [
    "notification_handler", "subscription_router", "subscription_store", "notification_publisher", "processor", "owner_eligibility"])
N = load("notification-service", "lifecycle_notification", ["processor", "notifier", "delivery_eligibility", "consumer"])

from common_event_model import IncidentEvent  # noqa: E402  (after service loading)

DIRECTORY = "00000000-0000-4000-8000-0000000000d1"
CLIENT = "00000000-0000-4000-8000-0000000000c1"
AUDIENCE = "api://subscription-test"
ADMIN = S.access_roles.BOOTSTRAP_ADMIN_ID
ALICE = "00000000-0000-4000-8000-00000000a11c"   # SOC Viewer, the subscriber under test
BOB = "00000000-0000-4000-8000-0000000000b0"     # verified and opted in, then access disabled
CAROL = "00000000-0000-4000-8000-0000000000ca"   # opted in on another tenant only
DAVE = "00000000-0000-4000-8000-0000000000da"    # signed in, never approved
ALICE_CHAT, BOB_CHAT, CAROL_CHAT, LEGACY_CHAT = 7001, 7002, 7003, "-1009000"
TENANTS = ("TENANT-01", "TENANT-02", "TENANT-03")
SECRET = "lifecycle-webhook-secret-" + "x" * 16
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def guard(original):
        def guarded(sock, address, *args, **kwargs):
            host = address[0] if isinstance(address, tuple) else address
            try:
                if ipaddress.ip_address(host).is_loopback:
                    return original(sock, address, *args, **kwargs)
            except ValueError:
                pass
            raise AssertionError(f"external network access attempted: {address!r}")
        return guarded
    monkeypatch.setattr(socket.socket, "connect", guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(socket.socket.connect_ex))
    monkeypatch.setattr(socket, "getaddrinfo", Mock(side_effect=AssertionError("DNS lookup attempted")))


def signed_in(user, **changes):
    """A delegated Entra access token as the dashboard would forward it."""
    claims = dict(iss=f"https://login.microsoftonline.com/{DIRECTORY}/v2.0", aud=AUDIENCE, exp=int(time.time()) + 300,
                  tid=DIRECTORY, oid=user, azp=CLIENT, scp="user_impersonation")
    claims.update(changes)
    return {"Authorization": "Bearer " + jwt.encode(claims, KEY, algorithm="RS256")}


class ClientVerifier:
    """Legacy client-credential caller for TENANT-01 (unchanged contract)."""
    mode = "entra"

    def verify(self, value):
        if value != "tenant-client":
            raise HTTPException(401, "invalid token")
        return S.auth.TenantContext("tenant-client", "TENANT-01", frozenset({"Subscriptions.Read", "Subscriptions.Write"}))


class DestinationPolicy:
    """Operator-approved destinations; shared by API and Worker as in production."""
    def __init__(self):
        self.approved_chats = set()

    async def approved(self, tenant_id, chat_id):
        return (tenant_id, chat_id) in self.approved_chats

    async def close(self):
        pass


class SharedContainer:
    """Read view the Worker and Notifier get of the one subscriptions container."""
    def __init__(self, store, documents):
        self.store, self.documents = store, documents
        self.fail = False

    def subscription_documents(self):
        return [item.model_dump(mode="json") for item in self.store.items.values()]

    async def read(self, partition, document_id):
        if self.fail:
            raise RuntimeError("cosmos unavailable")
        if partition == S.platform_documents.ACCESS_PARTITION:
            return await self.documents.read(document_id)
        item = self.store.items.get((partition, document_id))
        return item.model_dump(mode="json") if item is not None else None

    async def close(self):
        pass


@pytest.fixture
def platform():
    documents = S.platform_documents.InMemoryPlatformDocuments()
    store = S.store.InMemorySubscriptionStore()
    policy = DestinationPolicy()
    keys = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=KEY.public_key()))
    users = S.user_auth.UserTokenVerifier(DIRECTORY, AUDIENCE, [CLIENT], jwk_client=keys)
    registry = S.access_registry.AccessRegistry(documents, DIRECTORY, TENANTS)
    telegram = S.telegram_self_service.TelegramSelfService(registry, documents, store, policy, "IncidentAlertsBot", SECRET)
    app = S.main.create_app(store, ClientVerifier(), policy, account=(users, registry, telegram))
    with TestClient(app) as api:
        yield SimpleNamespace(api=api, store=store, documents=documents, policy=policy,
                              container=SharedContainer(store, documents))


# --- steps, each through the public HTTP contract -------------------------------

def approve(p, user, role, tenants):
    assert p.api.post("/api/v1/access/identity-proof", headers=signed_in(user)).status_code == 200
    response = p.api.post("/api/v1/admin/access/users", headers=signed_in(ADMIN), json=dict(
        object_id=user, role=role, tenants=list(tenants), approval_reference="SOC-APPROVAL-1"))
    assert response.status_code == 201, response.text


def telegram_status(p, user, tenant="TENANT-01"):
    return p.api.get(f"/api/v1/me/telegram?tenant_id={tenant}", headers=signed_in(user))


def save_preferences(p, user, enabled, events=("intrusion_detected",), tenant="TENANT-01"):
    return p.api.put(f"/api/v1/me/telegram/preferences?tenant_id={tenant}", headers=signed_in(user),
                     json={"enabled": enabled, "event_types": list(events)})


def bot_receives(p, chat_id, text, chat_type="private", sender=None):
    update = {"update_id": 1, "message": {"message_id": 1, "date": 0, "text": text,
              "from": {"id": sender or chat_id, "is_bot": False, "first_name": "Test"},
              "chat": {"id": chat_id, "type": chat_type}}}
    return p.api.post("/api/v1/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}, json=update)


def link_telegram(p, user, chat_id, tenant="TENANT-01"):
    response = p.api.post(f"/api/v1/me/telegram/challenges?tenant_id={tenant}", headers=signed_in(user))
    assert response.status_code == 201
    link = response.json()["deep_link"]
    assert link.startswith("https://t.me/IncidentAlertsBot?start=")
    assert bot_receives(p, chat_id, "/start " + link.split("start=", 1)[1]).json() == {"ok": True}


def admin_change(p, user, **changes):
    current = next(item for item in p.api.get("/api/v1/admin/access/users", headers=signed_in(ADMIN)).json()["items"]
                   if item["object_id"] == user)
    response = p.api.patch(f"/api/v1/admin/access/users/{user}", headers=signed_in(ADMIN),
                           json={"expected_version": current["version"], "reason": "lifecycle test", **changes})
    assert response.status_code == 200, response.text


def sample_incident(event_id, event_type="intrusion_detected", tenant="TENANT-01"):
    return IncidentEvent(event_id=event_id, tenant_id=tenant, site_id="SITE-01", source_id="CAM-001",
                         source_name="Test Camera", source_type="video_analytics", analytics_type="intrusion_detection",
                         event_type=event_type, severity="high", timestamp="2026-10-19T10:00:00+08:00",
                         event_purpose="e2e_test", confidence=0.9, description="Lifecycle test incident",
                         evidence_uri="https://evidence.example.test/test.jpg")


async def worker_publishes(p, incident):
    """Real Worker persist-then-route handler; returns the queued envelopes."""
    reader = W.subscription_store.InMemorySubscriptionReader(p.container.subscription_documents())
    reader.read_platform_record = lambda document_id: p.container.read(S.platform_documents.ACCESS_PARTITION, document_id)
    publisher = W.notification_publisher.MockNotificationPublisher()
    persisted = AsyncMock(handle=AsyncMock(return_value=W.processor.Outcome.PROCESSED))
    handler = W.notification_handler.PersistAndNotifyHandler(
        persisted, publisher, W.subscription_router.SubscriptionRouter(reader, policies=p.policy))
    await handler.handle(incident)
    persisted.handle.assert_awaited_once_with(incident)
    return [envelope.model_dump_json() for envelope in publisher.published_events]


async def notifier_delivers(p, queued):
    """Real Notification processor with send-time recheck; Telegram is mocked."""
    telegram = N.notifier.MockNotifier()
    processor = N.processor.NotificationProcessor(
        telegram, eligibility=N.delivery_eligibility.DeliveryEligibility(p.container))
    for body in queued:
        await processor.process(body)
    return telegram.sent_destinations


# --- the lifecycle ----------------------------------------------------------------

async def test_complete_telegram_subscription_lifecycle(platform):
    p = platform

    # 1. Sign-in through Entra. The approved Platform Admin resolves from the
    #    registry; an unapproved but genuinely signed-in user is denied.
    assert p.api.get("/api/v1/access/me", headers=signed_in(ADMIN)).json()["role"] == "Platform Admin"
    assert p.api.get("/api/v1/access/me", headers=signed_in(DAVE)).status_code == 403
    assert telegram_status(p, DAVE).status_code == 403
    assert p.api.get("/api/v1/access/me", headers=signed_in(ALICE, scp="")).status_code == 403  # not delegated
    approve(p, ALICE, "SOC Viewer", ["TENANT-01"])
    approve(p, BOB, "SOC Operator", ["TENANT-01"])
    approve(p, CAROL, "SOC Viewer", ["TENANT-02"])
    me = p.api.get("/api/v1/access/me", headers=signed_in(ALICE)).json()
    assert me["tenants"] == ["TENANT-01"] and "Subscriptions.SelfManage" in me["permissions"]

    # 2. Alice opts in. Consent alone cannot activate delivery before ownership.
    assert telegram_status(p, ALICE).json() == {
        "contract": "self-service-v1", "tenant_id": "TENANT-01", "verified": False, "destination_approved": False,
        "enabled": False, "event_types": [], "supported_event_types": sorted(S.telegram_self_service.DEFAULT_EVENT_TYPES)}
    assert save_preferences(p, ALICE, True).status_code == 403
    assert p.store.items == {}

    # 3. Ownership through secure bot linking; forged or group confirmations fail.
    response = p.api.post("/api/v1/me/telegram/challenges?tenant_id=TENANT-01", headers=signed_in(ALICE)).json()
    challenge = "/start " + response["deep_link"].split("start=", 1)[1]
    bot_receives(p, -100555, challenge, chat_type="group", sender=ALICE_CHAT)
    assert p.api.post("/api/v1/telegram/webhook", json={"message": {}}).status_code == 401  # no secret header
    assert telegram_status(p, ALICE).json()["verified"] is False
    bot_receives(p, ALICE_CHAT, challenge)
    assert telegram_status(p, ALICE).json()["verified"] is True
    bot_receives(p, 9999, challenge)  # replay from another account changes nothing
    assert p.documents.items["telegram-owner:" + ALICE]["chat_id"] == str(ALICE_CHAT)
    assert p.store.items == {}  # linking created no subscription and sent nothing

    # 4. Activation for the approved tenant, once the destination is approved.
    assert save_preferences(p, ALICE, True).status_code == 403
    p.policy.approved_chats |= {("TENANT-01", str(ALICE_CHAT)), ("TENANT-01", str(BOB_CHAT)),
                                ("TENANT-02", str(CAROL_CHAT)), ("TENANT-01", LEGACY_CHAT)}
    activated = save_preferences(p, ALICE, True).json()
    assert activated["enabled"] is True and activated["destination_approved"] is True
    assert str(ALICE_CHAT) not in json.dumps(activated)  # no private destination returned
    assert save_preferences(p, ALICE, True, tenant="TENANT-02").status_code == 403  # tenant not assigned

    # Other subscribers that must not receive Alice's TENANT-01 incident.
    link_telegram(p, BOB, BOB_CHAT)
    assert save_preferences(p, BOB, True).json()["enabled"] is True
    link_telegram(p, CAROL, CAROL_CHAT, tenant="TENANT-02")
    assert save_preferences(p, CAROL, True, tenant="TENANT-02").json()["enabled"] is True
    assert save_preferences(p, ALICE, True, events=("fire_detected",), tenant="TENANT-01").status_code == 200
    assert save_preferences(p, ALICE, True).status_code == 200  # back to intrusion only
    admin_change(p, BOB, status="disabled")
    # The pre-existing legacy client contract keeps working beside self-service.
    legacy = p.api.post("/api/v1/subscriptions", headers={"Authorization": "Bearer tenant-client"},
                        json={"event_type": "intrusion_detected", "telegram_chat_id": LEGACY_CHAT})
    assert legacy.status_code == 201 and "owner_object_id" not in legacy.json()

    # 5-6. An eligible test incident through the existing pipeline: only Alice
    #      (verified, active, authorized) and the operator's legacy route.
    queued = await worker_publishes(p, sample_incident("EVT-LIFE-001"))
    assert sorted(json.loads(body)["telegram_chat_id"] for body in queued) == sorted([str(ALICE_CHAT), LEGACY_CHAT])
    assert sorted(await notifier_delivers(p, queued)) == sorted([str(ALICE_CHAT), LEGACY_CHAT])
    assert await worker_publishes(p, sample_incident("EVT-LIFE-002", event_type="fire_detected")) == []
    load = sample_incident("EVT-LOAD-1").model_copy(update={"event_purpose": "load_test"})
    assert await worker_publishes(p, load) == []

    # 7. Unsubscribe: a message queued before it is not sent, and no new routes.
    queued_before = await worker_publishes(p, sample_incident("EVT-LIFE-003"))
    assert str(ALICE_CHAT) in {json.loads(body)["telegram_chat_id"] for body in queued_before}
    unsubscribed = save_preferences(p, ALICE, False)
    assert unsubscribed.status_code == 200 and unsubscribed.json()["enabled"] is False
    assert await notifier_delivers(p, queued_before) == [LEGACY_CHAT]
    after = await worker_publishes(p, sample_incident("EVT-LIFE-004"))
    assert [json.loads(body)["telegram_chat_id"] for body in after] == [LEGACY_CHAT]
    assert save_preferences(p, ALICE, False).json() == unsubscribed.json()  # idempotent
    assert p.api.get("/api/v1/access/me", headers=signed_in(ALICE)).status_code == 200  # dashboard access unchanged

    # 8a. Revocation of the user: queued messages are dropped, routing stops,
    #     and the account routes deny, even though Alice's token is still valid.
    assert save_preferences(p, ALICE, True).json()["enabled"] is True
    queued_before = await worker_publishes(p, sample_incident("EVT-LIFE-005"))
    assert str(ALICE_CHAT) in {json.loads(body)["telegram_chat_id"] for body in queued_before}
    admin_change(p, ALICE, status="revoked")
    assert await notifier_delivers(p, queued_before) == [LEGACY_CHAT]
    assert [json.loads(b)["telegram_chat_id"] for b in await worker_publishes(p, sample_incident("EVT-LIFE-006"))] == [LEGACY_CHAT]
    assert telegram_status(p, ALICE).status_code == 403
    assert p.api.get("/api/v1/access/me", headers=signed_in(ALICE)).status_code == 403

    # 8b. Removal of a tenant from a user: Carol's TENANT-02 delivery stops.
    queued_before = await worker_publishes(p, sample_incident("EVT-LIFE-007", tenant="TENANT-02"))
    assert [json.loads(b)["telegram_chat_id"] for b in queued_before] == [str(CAROL_CHAT)]
    admin_change(p, CAROL, tenants=["TENANT-03"])
    assert await notifier_delivers(p, queued_before) == []
    assert await worker_publishes(p, sample_incident("EVT-LIFE-008", tenant="TENANT-02")) == []


async def test_registry_outage_never_sends_and_returns_message_for_retry(platform):
    p = platform
    approve(p, ALICE, "SOC Viewer", ["TENANT-01"])
    link_telegram(p, ALICE, ALICE_CHAT)
    p.policy.approved_chats.add(("TENANT-01", str(ALICE_CHAT)))
    save_preferences(p, ALICE, True)
    queued = await worker_publishes(p, sample_incident("EVT-LIFE-101"))
    assert len(queued) == 1

    p.container.fail = True
    with pytest.raises(RuntimeError):
        await worker_publishes(p, sample_incident("EVT-LIFE-102"))  # Worker retries, publishes nothing

    telegram = N.notifier.MockNotifier()
    consumer = N.consumer.NotificationConsumer(
        N.processor.NotificationProcessor(telegram, eligibility=N.delivery_eligibility.DeliveryEligibility(p.container)),
        fully_qualified_namespace="test.servicebus.windows.net")
    consumer._receiver = Mock(abandon_message=AsyncMock(), complete_message=AsyncMock(), dead_letter_message=AsyncMock())
    message = Mock(body=queued[0].encode(), message_id="m1", delivery_count=1)
    await consumer.settle(message)
    consumer._receiver.abandon_message.assert_awaited_once_with(message)  # retried, later DLQ
    assert telegram.sent_events == []

    p.container.fail = False
    await consumer.settle(message)
    consumer._receiver.complete_message.assert_awaited_once_with(message)
    assert telegram.sent_destinations == [str(ALICE_CHAT)]


def test_one_role_model_shared_by_dashboard_and_api():
    dashboard = (ROOT / "business-dashboard" / "access_roles.py").read_bytes()
    assert (ROOT / "subscription-service" / "app" / "access_roles.py").read_bytes() == dashboard


@pytest.mark.parametrize("access,binding,tenant,chat", [
    ({"status": "active"}, {}, "TENANT-01", "1"), ({"status": "disabled"}, {}, "TENANT-01", "1"),
    ({"status": "revoked"}, {}, "TENANT-01", "1"), ({"tenants": ["TENANT-02"]}, {}, "TENANT-01", "1"),
    ({"role": "Owner"}, {}, "TENANT-01", "1"), ({"object_id": "x"}, {}, "TENANT-01", "1"),
    ({"record_type": "access_audit"}, {}, "TENANT-01", "1"), ({}, {"chat_id": "2"}, "TENANT-01", "1"),
    ({}, {"object_id": "x"}, "TENANT-01", "1"), ({}, {"record_type": "x"}, "TENANT-01", "1"),
    ({"tenants": "TENANT-01"}, {}, "TENANT-01", "1"), (None, {}, "TENANT-01", "1"),
])
def test_worker_and_notifier_apply_the_same_owner_rule(access, binding, tenant, chat):
    owner = "o"
    access_record = None if access is None else {"record_type": "platform_access", "object_id": owner, "role": "SOC Viewer",
                                                 "tenants": ["TENANT-01"], "status": "active", **access}
    binding_record = {"record_type": "telegram_owner", "object_id": owner, "chat_id": "1", **binding}
    for module in ("access_record_allows", "binding_allows"):
        assert getattr(W.owner_eligibility, module) is not getattr(N.delivery_eligibility, module)
    assert (W.owner_eligibility.access_record_allows(access_record, owner, tenant)
            == N.delivery_eligibility.access_record_allows(access_record, owner, tenant))
    assert (W.owner_eligibility.binding_allows(binding_record, owner, chat)
            == N.delivery_eligibility.binding_allows(binding_record, owner, chat))
    assert W.owner_eligibility.ROLES == N.delivery_eligibility.ROLES == frozenset(S.access_roles.Role)

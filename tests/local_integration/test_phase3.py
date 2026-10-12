"""Local cross-service verification; no credentials or external IO allowed."""
import importlib
import importlib.util
import ipaddress
import json
import socket
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


def load_service(directory, alias):
    # Production modules use absolute app imports. Isolate each package while
    # importing, then retain aliases so all three real services coexist.
    saved = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
    for k in saved:
        del sys.modules[k]
    spec = importlib.util.spec_from_file_location("app", ROOT / directory / "app" / "__init__.py", submodule_search_locations=[str(ROOT / directory / "app")])
    if spec is None or spec.loader is None:
        # Worker/notification app directories are namespace packages.
        import types
        package = types.ModuleType("app")
        package.__path__ = [str(ROOT / directory / "app")]
        sys.modules["app"] = package
    else:
        package = importlib.util.module_from_spec(spec)
        sys.modules["app"] = package
        if (ROOT / directory / "app" / "__init__.py").exists():
            spec.loader.exec_module(package)
    names = {
        "subscription-service": ["main", "auth", "store"],
        "worker-service": ["processor", "notification_handler", "subscription_router", "subscription_store", "notification_publisher"],
        "notification-service": ["processor", "telegram_notifier", "evidence"],
    }[directory]
    result = {name: importlib.import_module("app." + name) for name in names}
    for key in list(sys.modules):
        if key == "app" or key.startswith("app."):
            sys.modules[alias + key[3:]] = sys.modules.pop(key)
    sys.modules.update(saved)
    return result


S = load_service("subscription-service", "phase3_subscription")
W = load_service("worker-service", "phase3_worker")
N = load_service("notification-service", "phase3_notification")


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    # Windows asyncio builds its internal wakeup pipe from loopback sockets.
    # Permit only literal loopback addresses; never resolve external names.
    def guard(original):
        def guarded(sock, address, *args, **kwargs):
            try:
                allowed = ipaddress.ip_address(address[0]).is_loopback
            except (ValueError, TypeError):
                allowed = False
            if not allowed:
                raise AssertionError("External network prohibited in local verification")
            return original(sock, address, *args, **kwargs)
        return guarded
    monkeypatch.setattr(socket.socket, "connect", guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(socket.socket.connect_ex))


class LocalVerifier:
    mode = "local-test"

    def verify(self, token):
        if token not in {"A", "B"}:
            raise HTTPException(401, "invalid local token")
        return S["auth"].TenantContext(token, "TENANT-" + token, frozenset({"Subscriptions.Read", "Subscriptions.Write"}))


class SharedReader(W["subscription_store"].SubscriptionReader):
    def __init__(self, store, trace):
        self.store, self.trace = store, trace
        self.fail = False
        self.calls = []

    async def find_matches(self, tenant_id, event_type):
        self.trace.append("lookup")
        self.calls.append((tenant_id, event_type))
        if self.fail:
            raise RuntimeError("lookup failed")
        # Read live API state, never a copied seed list or unrelated mock store.
        records = [W["subscription_store"].validate_subscription(x.model_dump(mode="json")) for x in self.store.items.values()]
        return [r for r in records if W["subscription_store"].matches(r, tenant_id, event_type)]


class Persistence:
    def __init__(self, trace):
        self.trace, self.items, self.fail = trace, {}, False

    async def handle(self, event):
        self.trace.append("persist")
        if self.fail:
            raise RuntimeError("persist failed")
        key = (event.tenant_id, event.event_id)
        duplicate = key in self.items
        self.items[key] = event.model_dump(mode="json")
        return W["processor"].Outcome.DUPLICATE if duplicate else W["processor"].Outcome.PROCESSED


class Transport:
    def __init__(self):
        self.calls = []
        self.photo_fail = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def post(self, url, **kwargs):
        method = url.rsplit("/", 1)[-1]
        if "json" in kwargs:
            destination = kwargs["json"]["chat_id"]
        else:
            destination = next(value for options, _, value in kwargs["data"]._fields if options["name"] == "chat_id")
        self.calls.append((method, destination))
        response = TransportResponse(400 if method == "sendPhoto" and self.photo_fail else 200)
        return response


class TransportResponse:
    headers = {}

    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def text(self):
        return '{"ok":true}' if self.status == 200 else '{"ok":false}'


class Publisher:
    def __init__(self, trace):
        self.trace, self.messages, self.attempts = trace, [], []
        self.fail_at = None

    async def publish(self, route):
        self.trace.append("publish")
        message_id = W["notification_publisher"].route_message_id(route)
        self.attempts.append(message_id)
        if len(self.attempts) == self.fail_at:
            raise W["notification_publisher"].NotificationPublishError("synthetic publish failure")
        self.messages.append(json.loads(route.model_dump_json()))


@pytest.fixture
def harness():
    from types import SimpleNamespace
    trace = []
    store = S["store"].InMemorySubscriptionStore()
    reader, persistence, publisher, transport = SharedReader(store, trace), Persistence(trace), Publisher(trace), Transport()
    evidence = AsyncMock()
    evidence.fetch.return_value = N["evidence"].Evidence(b"synthetic-image", "synthetic.jpg", "image/jpeg")
    notifier = N["telegram_notifier"].TelegramNotifier(bot_token="synthetic-token", chat_id="-999999", min_send_interval_seconds=0, evidence_store=evidence, session_factory=lambda: transport)
    worker = W["processor"].IncidentProcessor(W["notification_handler"].PersistAndNotifyHandler(persistence, publisher, W["subscription_router"].SubscriptionRouter(reader, policies=AsyncMock(approved=AsyncMock(return_value=True)))))
    with TestClient(S["main"].create_app(store, LocalVerifier(), policy=AsyncMock(approved=AsyncMock(return_value=True)))) as client:
        yield SimpleNamespace(client=client, store=store, reader=reader, persistence=persistence, publisher=publisher, transport=transport, evidence=evidence, worker=worker, notification=N["processor"].NotificationProcessor(notifier), trace=trace)


def headers(tenant="A"):
    return {"Authorization": "Bearer " + tenant}


def subscribe(h, destination="-100000001"):
    response = h.client.post("/api/v1/subscriptions", headers=headers(), json={"event_type": "intrusion_detected", "channel": "telegram", "telegram_chat_id": destination, "enabled": True})
    assert response.status_code == 201
    data = response.json()
    assert data["id"]
    assert h.client.get("/api/v1/subscriptions/" + data["id"], headers=headers()).json() == data
    return data


def incident(event_id="EVT-E2E-1", tenant="TENANT-A", **changes):
    return dict(event_id=event_id, tenant_id=tenant, site_id="SITE-TEST", source_id="CAM-TEST", source_name="Synthetic camera", source_type="video_analytics", analytics_type="intrusion_detection", event_type="intrusion_detected", severity="low", timestamp="2026-10-09T00:00:00Z", event_purpose="e2e_test", confidence=0.99, description="Local verification", evidence_uri=None) | changes


async def deliver(h):
    for message in h.publisher.messages:
        await h.notification.process(json.dumps(message))


@pytest.mark.asyncio
async def test_a_subscriber(harness):
    h = harness
    sub = subscribe(h)
    payload = incident()
    await h.worker.process(json.dumps(payload))
    assert h.trace == ["persist", "lookup", "publish"]
    assert len(h.persistence.items) == len(h.publisher.messages) == 1
    envelope = h.publisher.messages[0]
    assert envelope["schema_version"] == 1
    assert envelope["subscription_id"] == sub["id"]
    assert envelope["telegram_chat_id"] == sub["telegram_chat_id"]
    assert envelope["incident"] == next(iter(h.persistence.items.values()))
    assert W["processor"].IncidentProcessor()._parse(json.dumps(payload), None).model_dump(mode="json") == envelope["incident"]
    await deliver(h)
    assert h.transport.calls == [("sendMessage", sub["telegram_chat_id"])]


@pytest.mark.asyncio
async def test_b_non_subscriber(harness):
    h = harness
    subscribe(h)
    await h.worker.process(json.dumps(incident(tenant="TENANT-B")))
    assert h.trace == ["persist", "lookup"]
    assert len(h.persistence.items) == 1
    assert h.reader.calls == [("TENANT-B", "intrusion_detected")]
    assert not h.publisher.messages
    await deliver(h)
    assert not h.transport.calls


@pytest.mark.asyncio
async def test_c_disabled_live_api_state(harness):
    h = harness
    sub = subscribe(h)
    await h.worker.process(json.dumps(incident()))
    assert len(h.publisher.messages) == 1
    path = "/api/v1/subscriptions/" + sub["id"]
    assert h.client.patch(path, headers=headers(), json={"enabled": False}).status_code == 200
    assert h.client.get(path, headers=headers()).json()["enabled"] is False
    h.publisher.messages.clear()
    h.trace.clear()
    await h.worker.process(json.dumps(incident("EVT-E2E-DISABLED")))
    assert len(h.persistence.items) == 2
    assert h.trace == ["persist", "lookup"]
    assert not h.publisher.messages
    await deliver(h)
    assert not h.transport.calls


def test_d_tenant_isolation(harness):
    h = harness
    sub = subscribe(h)
    path = "/api/v1/subscriptions/" + sub["id"]
    assert h.client.get(path, headers=headers("B")).status_code == 404
    assert h.client.patch(path, headers=headers("B"), json={"enabled": False}).status_code == 404
    assert h.client.delete(path, headers=headers("B")).status_code == 404
    assert h.client.get("/api/v1/subscriptions", headers=headers("B")).json()["items"] == []
    assert h.client.get(path, headers=headers()).json() == sub


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_fail", [False, True])
async def test_e_evidence_and_photo_fallback(harness, photo_fail):
    h = harness
    sub = subscribe(h)
    uri = "https://synthetic.invalid/evidence/local.jpg"
    await h.worker.process(json.dumps(incident(evidence_uri=uri)))
    assert next(iter(h.persistence.items.values()))["evidence_uri"] == uri
    assert h.publisher.messages[0]["incident"]["evidence_uri"] == uri
    h.transport.photo_fail = photo_fail
    await deliver(h)
    h.evidence.fetch.assert_awaited_once_with(uri)
    expected = [("sendPhoto", sub["telegram_chat_id"])]
    if photo_fail:
        expected.append(("sendMessage", sub["telegram_chat_id"]))
    assert h.transport.calls == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose,event_id,eligible", [("load_test", "EVT-TEST-LOAD", False), (None, "EVT-LOAD-OLD", False), ("e2e_test", "EVT-E2E", True)])
async def test_f_suppression(harness, purpose, event_id, eligible):
    h = harness
    subscribe(h)
    payload = incident(event_id, event_purpose=purpose)
    if purpose is None:
        del payload["event_purpose"]
    await h.worker.process(json.dumps(payload))
    assert len(h.persistence.items) == 1
    assert len(h.reader.calls) == len(h.publisher.messages) == int(eligible)
    await deliver(h)
    assert len(h.transport.calls) == int(eligible)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["persist", "lookup", "publish"])
async def test_g_failures_propagate(harness, boundary):
    h = harness
    subscribe(h)
    if boundary == "persist":
        h.persistence.fail = True
    elif boundary == "lookup":
        h.reader.fail = True
    else:
        h.publisher.fail_at = 1
    with pytest.raises(Exception, match="failed|failure"):
        await h.worker.process(json.dumps(incident()))
    assert h.trace == {"persist": ["persist"], "lookup": ["persist", "lookup"], "publish": ["persist", "lookup", "publish"]}[boundary]
    assert not h.publisher.messages
    assert len(h.persistence.items) == int(boundary != "persist")
    assert not h.transport.calls


@pytest.mark.asyncio
async def test_g_partial_fanout_retry_duplicate_and_stable_ids(harness):
    h = harness
    subscribe(h)
    subscribe(h, "-100000002")
    h.publisher.fail_at = 2
    body = json.dumps(incident())
    with pytest.raises(W["notification_publisher"].NotificationPublishError):
        await h.worker.process(body)
    assert len(h.publisher.messages) == 1
    h.publisher.fail_at = None
    assert await h.worker.process(body) == W["processor"].Outcome.DUPLICATE
    assert len(h.persistence.items) == 1
    assert len(h.publisher.messages) == 3  # previously successful route repeats
    assert h.publisher.attempts[:2] == h.publisher.attempts[2:]
    assert len(set(h.publisher.attempts)) == 2
    await deliver(h)
    assert h.transport.calls == [("sendMessage", "-100000001"), ("sendMessage", "-100000001"), ("sendMessage", "-100000002")]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"schema_version": 2}, {"telegram_chat_id": "invalid"}, {"incident": {}}, {"channel": "email"}, {"schema_version": None}])
async def test_g_invalid_envelope_never_global(harness, change):
    h = harness
    subscribe(h)
    await h.worker.process(json.dumps(incident()))
    message = h.publisher.messages[0] | change
    if change == {"schema_version": None}:
        del message["schema_version"]
    with pytest.raises(N["processor"].InvalidIncident):
        await h.notification.process(json.dumps(message))
    assert not h.transport.calls
    h.evidence.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_flat_global_only(harness):
    h = harness
    await h.notification.process(json.dumps(incident()))
    assert h.transport.calls == [("sendMessage", "-999999")]


@pytest.mark.asyncio
async def test_validation_precedes_persistence(harness):
    with pytest.raises(W["processor"].InvalidIncident):
        await harness.worker.process('{"event_id":"INVALID"}')
    assert not harness.trace
    assert not harness.persistence.items


def test_local_api_health_readiness(harness):
    assert harness.client.get("/health").json() == {"status": "healthy", "service": "subscription-service", "store": "memory", "auth": "local-test"}
    assert harness.client.get("/ready").json()["status"] == "ready"

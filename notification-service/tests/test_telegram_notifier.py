import json

import pytest

from app.models import IncidentEvent
from app.telegram_notifier import TelegramNotifier


class FakeResponse:
    def __init__(self, status: int, body: str, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_event(event_id="EVT-001", event_purpose="incident"):
    return IncidentEvent(
        event_id=event_id,
        tenant_id="TENANT-01",
        site_id="SITE-01",
        source_id="CAM-001",
        source_name="Restricted Area Camera",
        source_type="video_analytics",
        analytics_type="intrusion_detection",
        event_type="intrusion_detected",
        severity="high",
        timestamp="2026-09-12T20:00:00+08:00",
        event_purpose=event_purpose,
        confidence=0.95,
        description="Person detected in restricted area",
        evidence_uri="https://evidence.example.test/incidents/EVT-001.jpg",
    )


def make_notifier(responses, clock=None, **kwargs):
    session = FakeSession(responses)
    clock = clock or FakeClock()
    notifier = TelegramNotifier(
        bot_token="test-token",
        chat_id="-100123",
        session_factory=lambda: session,
        sleep=clock.sleep,
        clock=clock,
        **kwargs,
    )
    return notifier, session, clock


@pytest.mark.asyncio
async def test_successful_delivery_posts_formatted_message():
    notifier, session, clock = make_notifier(
        [FakeResponse(200, '{"ok":true}')],
    )

    await notifier.notify(make_event())

    assert clock.sleeps == []
    assert len(session.requests) == 1
    url, request = session.requests[0]
    assert url.endswith("/bottest-token/sendMessage")
    assert request["json"]["chat_id"] == "-100123"
    assert "Event ID: EVT-001" in request["json"]["text"]


@pytest.mark.asyncio
async def test_e2e_delivery_is_clearly_labelled():
    notifier, session, _ = make_notifier(
        [FakeResponse(200, '{"ok":true}')],
    )

    await notifier.notify(make_event(event_purpose="e2e_test"))

    _, request = session.requests[0]
    assert request["json"]["text"].startswith("Incident Alert [E2E TEST]")


@pytest.mark.asyncio
async def test_rate_limit_waits_for_retry_after_then_retries():
    body = json.dumps(
        {
            "ok": False,
            "error_code": 429,
            "parameters": {"retry_after": 7},
        }
    )
    notifier, session, clock = make_notifier(
        [
            FakeResponse(429, body),
            FakeResponse(200, '{"ok":true}'),
        ],
        min_send_interval_seconds=0,
        max_rate_limit_retries=1,
    )

    await notifier.notify(make_event())

    assert clock.sleeps == [7.0]
    assert len(session.requests) == 2


@pytest.mark.asyncio
async def test_rate_limit_retry_is_bounded():
    body = json.dumps(
        {
            "ok": False,
            "error_code": 429,
            "parameters": {"retry_after": 3},
        }
    )
    notifier, session, clock = make_notifier(
        [
            FakeResponse(429, body),
            FakeResponse(429, body),
        ],
        min_send_interval_seconds=0,
        max_rate_limit_retries=1,
    )

    with pytest.raises(RuntimeError, match="status=429"):
        await notifier.notify(make_event())

    assert clock.sleeps == [3.0]
    assert len(session.requests) == 2


@pytest.mark.asyncio
async def test_non_rate_limit_error_is_not_retried():
    notifier, session, clock = make_notifier(
        [FakeResponse(400, '{"ok":false}')],
        min_send_interval_seconds=0,
    )

    with pytest.raises(RuntimeError, match="status=400"):
        await notifier.notify(make_event())

    assert clock.sleeps == []
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_successive_notifications_respect_minimum_interval():
    notifier, session, clock = make_notifier(
        [
            FakeResponse(200, '{"ok":true}'),
            FakeResponse(200, '{"ok":true}'),
        ],
        min_send_interval_seconds=3.1,
    )

    await notifier.notify(make_event("EVT-001"))
    await notifier.notify(make_event("EVT-002"))

    assert clock.sleeps == [pytest.approx(3.1)]
    assert len(session.requests) == 2

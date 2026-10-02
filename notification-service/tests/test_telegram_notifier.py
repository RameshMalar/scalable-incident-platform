import json

import pytest
from common_event_model import IncidentEvent

from app.evidence import Evidence, EvidenceUnavailable
from app.telegram_notifier import CAPTION_LIMIT, TelegramNotifier


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


class FakeEvidenceStore:
    """Stands in for blob storage. Returns evidence, or raises to force text."""

    def __init__(self, evidence=None, error=None):
        self.evidence = evidence
        self.error = error
        self.requested = []
        self.closed = False

    async def fetch(self, evidence_uri):
        self.requested.append(evidence_uri)

        if self.error is not None:
            raise self.error

        return self.evidence

    async def close(self):
        self.closed = True


def make_evidence(data=b"jpeg-bytes"):
    return Evidence(data=data, filename="EVT-001.jpg", content_type="image/jpeg")


def form_fields(form):
    return {
        options["name"]: value for options, _, value in form._fields
    }


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


@pytest.mark.asyncio
async def test_evidence_is_delivered_as_a_captioned_photo():
    store = FakeEvidenceStore(evidence=make_evidence())
    notifier, session, _ = make_notifier(
        [FakeResponse(200, '{"ok":true}')],
        evidence_store=store,
    )

    await notifier.notify(make_event())

    assert store.requested == [
        "https://evidence.example.test/incidents/EVT-001.jpg"
    ]

    assert len(session.requests) == 1
    url, request = session.requests[0]
    assert url.endswith("/bottest-token/sendPhoto")

    fields = form_fields(request["data"])
    assert fields["chat_id"] == "-100123"
    assert fields["photo"] == b"jpeg-bytes"
    assert "Event ID: EVT-001" in fields["caption"]


@pytest.mark.asyncio
async def test_unavailable_evidence_degrades_to_a_text_alert():
    store = FakeEvidenceStore(error=EvidenceUnavailable("blob does not exist"))
    notifier, session, _ = make_notifier(
        [FakeResponse(200, '{"ok":true}')],
        evidence_store=store,
    )

    await notifier.notify(make_event())

    assert len(session.requests) == 1
    url, request = session.requests[0]
    assert url.endswith("/sendMessage")
    assert "Event ID: EVT-001" in request["json"]["text"]


@pytest.mark.asyncio
async def test_rejected_photo_still_pages_through_text():
    store = FakeEvidenceStore(evidence=make_evidence())
    notifier, session, _ = make_notifier(
        [
            FakeResponse(400, '{"ok":false,"description":"bad photo"}'),
            FakeResponse(200, '{"ok":true}'),
        ],
        evidence_store=store,
        min_send_interval_seconds=0,
    )

    await notifier.notify(make_event())

    assert [url for url, _ in session.requests] == [
        "https://api.telegram.org/bottest-token/sendPhoto",
        "https://api.telegram.org/bottest-token/sendMessage",
    ]


@pytest.mark.asyncio
async def test_event_without_evidence_uri_never_touches_storage():
    store = FakeEvidenceStore(evidence=make_evidence())
    notifier, session, _ = make_notifier(
        [FakeResponse(200, '{"ok":true}')],
        evidence_store=store,
    )

    event = make_event()
    event.evidence_uri = None

    await notifier.notify(event)

    assert store.requested == []
    url, _ = session.requests[0]
    assert url.endswith("/sendMessage")


@pytest.mark.asyncio
async def test_rate_limited_photo_is_retried_with_a_rebuilt_body():
    body = json.dumps(
        {
            "ok": False,
            "error_code": 429,
            "parameters": {"retry_after": 4},
        }
    )
    store = FakeEvidenceStore(evidence=make_evidence())
    notifier, session, clock = make_notifier(
        [
            FakeResponse(429, body),
            FakeResponse(200, '{"ok":true}'),
        ],
        evidence_store=store,
        min_send_interval_seconds=0,
        max_rate_limit_retries=1,
    )

    await notifier.notify(make_event())

    assert clock.sleeps == [4.0]
    assert len(session.requests) == 2

    # A multipart body can only be consumed once, so the retry has to send a
    # freshly built form rather than the exhausted one.
    first, second = (request["data"] for _, request in session.requests)
    assert first is not second
    assert form_fields(second)["photo"] == b"jpeg-bytes"


@pytest.mark.asyncio
async def test_caption_is_truncated_to_the_telegram_limit():
    store = FakeEvidenceStore(evidence=make_evidence())
    notifier, session, _ = make_notifier(
        [FakeResponse(200, '{"ok":true}')],
        evidence_store=store,
    )

    event = make_event()
    event.description = "x" * 2000

    await notifier.notify(event)

    caption = form_fields(session.requests[0][1]["data"])["caption"]
    assert len(caption) == CAPTION_LIMIT
    assert caption.endswith("...")


@pytest.mark.asyncio
async def test_text_alert_keeps_the_full_description():
    notifier, session, _ = make_notifier([FakeResponse(200, '{"ok":true}')])

    event = make_event()
    event.description = "x" * 2000

    await notifier.notify(event)

    assert len(session.requests[0][1]["json"]["text"]) > CAPTION_LIMIT


@pytest.mark.asyncio
async def test_close_releases_the_evidence_store():
    store = FakeEvidenceStore(evidence=make_evidence())
    notifier, _, _ = make_notifier([], evidence_store=store)

    await notifier.close()

    assert store.closed is True

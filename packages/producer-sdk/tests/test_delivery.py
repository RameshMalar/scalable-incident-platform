"""Delivery, and the retry policy in particular.

The policy is the part worth proving. Retrying what should not be retried turns a
rejected incident into four rejected incidents and delays the error; not retrying
what should be loses an incident to a blip. Both are silent in production, so they
are asserted here.

No network: `urllib.request.urlopen` is replaced, and the replacement records what
it was asked to do.
"""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO

import pytest

from incident_producer import DeliveryFailed, IncidentProducer, InvalidEvent, build

ENDPOINT = "https://ingestion.example.invalid"


def an_event(**overrides):
    fields = dict(
        tenant_id="TENANT-01",
        site_id="SITE-01",
        source_id="CAM-07",
        source_name="Loading bay camera CAM-07",
        analytics_type="intrusion_detection",
        event_type="intrusion_detected",
        severity="high",
    )
    fields.update(overrides)
    return build(**fields)


class Answer:
    """One canned HTTP answer: a status, or an exception to raise instead."""

    def __init__(self, status=202, body=b'{"status":"accepted"}', raises=None):
        self.status = status
        self.body = body
        self.raises = raises


class FakeHttp:
    """Replaces urlopen, answering from a script and recording every call."""

    def __init__(self, *answers: Answer):
        self._answers = list(answers)
        self.requests = []
        self.sleeps = []

    def __call__(self, request, timeout=None):
        # wake() passes a bare url string; send() passes a Request.
        self.requests.append(request)
        answer = self._answers.pop(0) if self._answers else Answer()

        if answer.raises is not None:
            raise answer.raises

        if answer.status >= 400:
            raise urllib.error.HTTPError(
                url=getattr(request, "full_url", str(request)),
                code=answer.status,
                msg="error",
                hdrs=None,
                fp=BytesIO(answer.body),
            )

        return _Response(answer.status, answer.body)


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def http(monkeypatch):
    def install(*answers):
        fake = FakeHttp(*answers)
        monkeypatch.setattr("incident_producer.delivery.urllib.request.urlopen", fake)
        monkeypatch.setattr("incident_producer.delivery.time.sleep", fake.sleeps.append)
        return fake

    return install


def a_producer(**kwargs):
    return IncidentProducer(ENDPOINT, token_source=lambda: "a-token", **kwargs)


# --- what gets sent --------------------------------------------------------


def test_an_accepted_incident_returns_a_receipt(http):
    fake = http(Answer(status=202))
    event = an_event()

    receipt = a_producer().send(event)

    assert receipt.event_id == event.event_id
    assert receipt.status == 202
    assert receipt.attempts == 1


def test_the_request_carries_the_token_and_the_incident(http):
    fake = http(Answer())
    event = an_event(analytics_type="no_such_analytics_yet")

    a_producer().send(event)

    request = fake.requests[0]
    assert request.full_url == f"{ENDPOINT}/api/v1/incidents"
    assert request.get_header("Authorization") == "Bearer a-token"

    sent = json.loads(request.data)
    assert sent["analytics_type"] == "no_such_analytics_yet"
    assert sent["event_id"] == event.event_id
    # Unset optionals are dropped rather than sent as null.
    assert "confidence" not in sent


def test_a_malformed_incident_is_refused_before_anything_is_sent(http):
    fake = http(Answer())

    with pytest.raises(InvalidEvent):
        a_producer().send({"event_id": "EVT-1"})

    assert fake.requests == []


def test_an_endpoint_without_tls_is_refused():
    # A bearer token would otherwise go out in clear text to whatever answers.
    with pytest.raises(ValueError, match="refusing"):
        IncidentProducer("http://ingestion.example.invalid", token_source=lambda: "t")


# --- what is retried, and what is not --------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_retryable_status_is_retried_and_can_succeed(http, status):
    fake = http(Answer(status=status), Answer(status=202))

    receipt = a_producer().send(an_event())

    assert receipt.attempts == 2
    assert len(fake.sleeps) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_a_client_error_is_not_retried(http, status):
    # The incident is wrong, or the token is. A second identical attempt cannot
    # help, and it delays the error reaching whoever can fix it.
    fake = http(Answer(status=status), Answer(status=202))

    with pytest.raises(DeliveryFailed) as exc:
        a_producer().send(an_event())

    assert exc.value.status == status
    assert exc.value.attempts == 1
    assert len(fake.requests) == 1
    assert fake.sleeps == []


def test_a_timeout_is_retried_because_the_store_absorbs_a_duplicate(http):
    # The ambiguous case: the API may have accepted the incident before the socket
    # gave up. Retried on purpose -- the store keys on event_id, so at worst the
    # second attempt is rejected as a duplicate.
    fake = http(Answer(raises=TimeoutError("timed out")), Answer(status=202))

    receipt = a_producer().send(an_event())

    assert receipt.attempts == 2


def test_a_retry_sends_the_same_event_id(http):
    # The property every retry above depends on. A fresh id per attempt would
    # store the same incident twice.
    fake = http(Answer(status=503), Answer(status=202))

    a_producer().send(an_event())

    ids = {json.loads(r.data)["event_id"] for r in fake.requests}
    assert len(ids) == 1


def test_giving_up_reports_how_many_attempts_were_made(http):
    fake = http(*[Answer(status=503)] * 4)

    with pytest.raises(DeliveryFailed) as exc:
        a_producer(attempts=4).send(an_event())

    assert exc.value.attempts == 4
    assert len(fake.requests) == 4
    # Slept between attempts, not after the last one.
    assert len(fake.sleeps) == 3


def test_a_single_attempt_configuration_does_not_retry(http):
    fake = http(Answer(status=503), Answer(status=202))

    with pytest.raises(DeliveryFailed):
        a_producer(attempts=1).send(an_event())

    assert len(fake.requests) == 1


def test_zero_attempts_is_refused():
    with pytest.raises(ValueError):
        a_producer(attempts=0)


def test_a_failing_token_source_sends_nothing(http):
    fake = http(Answer())
    producer = IncidentProducer(
        ENDPOINT, token_source=lambda: (_ for _ in ()).throw(RuntimeError("no login"))
    )

    with pytest.raises(DeliveryFailed, match="no access token"):
        producer.send(an_event())

    assert fake.requests == []


# --- warm-up ---------------------------------------------------------------


def test_wake_returns_true_once_the_service_answers(http):
    fake = http(Answer(raises=urllib.error.URLError("refused")), Answer(status=200))

    assert a_producer().wake(attempts=3, interval_seconds=0) is True
    assert fake.requests[0] == f"{ENDPOINT}/health"


def test_wake_gives_up_without_raising(http):
    # Not fatal: send() retries a cold start anyway, and a caller may still want
    # to try. Reported as a false so a caller that cares can say so.
    fake = http(*[Answer(raises=urllib.error.URLError("refused"))] * 3)

    assert a_producer().wake(attempts=3, interval_seconds=0) is False

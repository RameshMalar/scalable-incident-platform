"""Getting a validated incident to the ingestion API, and retrying when it is safe.

Retrying a delivery is only safe because the store keys documents on the
producer's `event_id`: a second attempt at the same incident is rejected as a
duplicate rather than stored twice. That property is what this module is built on,
so a producer must not generate a fresh id per attempt -- `event.build` assigns
one once, deliberately.

What is retried, and what is not:

* 5xx, and 429 -- the service could not answer, or asked for less. Trying again
  is the whole point.
* A timeout or a dropped connection -- **ambiguous**, because the API may have
  accepted the incident before the socket gave up. Retried anyway, and the store
  absorbs the duplicate. This is the case the id makes safe.
* Any other 4xx -- the incident is wrong, or the token is. Trying again changes
  nothing and only delays the error reaching whoever can fix it.
"""

from __future__ import annotations

import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from common_event_model import IncidentEvent

from .auth import TokenSource
from .event import to_json, validate

logger = logging.getLogger("incident_producer")

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_ATTEMPTS = 4

# Container Apps scale to zero, so the first caller after an idle period waits for
# a replica to start. Long enough to cover that, and polled on /health so the cost
# is not charged to an incident.
DEFAULT_WARMUP_ATTEMPTS = 10
DEFAULT_WARMUP_INTERVAL_SECONDS = 6

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class DeliveryFailed(RuntimeError):
    """The incident was not delivered, after every attempt allowed."""

    def __init__(self, message: str, *, status: int | None = None, attempts: int = 0):
        super().__init__(message)
        self.status = status
        self.attempts = attempts


@dataclass(frozen=True)
class Receipt:
    """What the API said, for an incident it accepted."""

    event_id: str
    status: int
    attempts: int
    body: str


class IncidentProducer:
    """Sends incidents to one ingestion endpoint on behalf of one producer.

    Reusable across incidents and safe to keep for the life of a process: the
    token source caches, and nothing else is held between calls.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        token_source: TokenSource,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        attempts: int = DEFAULT_ATTEMPTS,
    ):
        # A bearer token is about to be sent to whatever this resolves to, and the
        # endpoint often arrives from configuration or a deployment lookup rather
        # than from a literal. Refused rather than downgraded.
        if not endpoint.startswith("https://"):
            raise ValueError(f"refusing to send a bearer token over {endpoint!r}")

        if attempts < 1:
            raise ValueError("attempts must be at least 1")

        self._endpoint = endpoint.rstrip("/")
        self._token_source = token_source
        self._timeout = timeout_seconds
        self._attempts = attempts

    @property
    def incidents_url(self) -> str:
        return f"{self._endpoint}/api/v1/incidents"

    @property
    def health_url(self) -> str:
        return f"{self._endpoint}/health"

    def send(self, event: IncidentEvent | dict) -> Receipt:
        """Deliver one incident, retrying where a retry can help.

        Raises `InvalidEvent` before anything is sent if the incident is
        malformed, and `DeliveryFailed` if every attempt was used.
        """
        incident = validate(event)
        body = to_json(incident)
        last = "no attempt was made"
        last_status: int | None = None

        for attempt in range(1, self._attempts + 1):
            status, detail = self._post(body)

            if status in (200, 201, 202):
                return Receipt(
                    event_id=incident.event_id,
                    status=status,
                    attempts=attempt,
                    body=detail,
                )

            last, last_status = detail, status or None
            retryable = status in RETRYABLE_STATUSES or status == 0

            if not retryable:
                raise DeliveryFailed(
                    f"{incident.event_id} rejected with {status}: {detail}",
                    status=status,
                    attempts=attempt,
                )

            if attempt < self._attempts:
                delay = self._backoff(attempt)
                logger.warning(
                    "delivery of %s failed (%s), retrying in %.1fs",
                    incident.event_id, detail, delay,
                )
                time.sleep(delay)

        raise DeliveryFailed(
            f"{incident.event_id} not delivered after {self._attempts} attempts: {last}",
            status=last_status,
            attempts=self._attempts,
        )

    def wake(
        self,
        attempts: int = DEFAULT_WARMUP_ATTEMPTS,
        interval_seconds: int = DEFAULT_WARMUP_INTERVAL_SECONDS,
    ) -> bool:
        """Wait for a replica to be serving, before an incident pays for it.

        Returns whether the service answered. A false is not fatal and not raised:
        the caller may still want to try, and `send` retries a cold start anyway.
        """
        for attempt in range(1, attempts + 1):
            try:
                with urllib.request.urlopen(
                    self.health_url, timeout=interval_seconds
                ) as response:
                    if response.status == 200:
                        if attempt > 1:
                            logger.info("awake after %d attempts", attempt)
                        return True
            except (urllib.error.URLError, OSError):
                pass

            if attempt < attempts:
                time.sleep(interval_seconds)

        logger.warning("the service never answered %s", self.health_url)
        return False

    def _post(self, body: bytes) -> tuple[int, str]:
        """One attempt. Status 0 means no answer was received at all."""
        try:
            token = self._token_source()
        except Exception as exc:
            # Not retryable through the status path, and not an HTTP failure, so
            # it is reported as status 0 with the reason rather than being retried
            # silently against a credential that is not going to appear.
            raise DeliveryFailed(f"no access token: {exc}") from exc

        request = urllib.request.Request(
            self.incidents_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, response.read().decode()[:300]
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()[:300]
        # A read timeout arrives as OSError, not URLError, so catching only
        # URLError lets it escape as an unhandled traceback -- which is how this
        # was first found.
        except (urllib.error.URLError, OSError) as exc:
            return 0, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential, with jitter.

        The jitter matters when a site's cameras all lose the network together:
        without it every producer retries on the same schedule and rebuilds the
        spike that caused the failure.
        """
        return min(2.0 ** (attempt - 1), 8.0) * (0.5 + random.random())

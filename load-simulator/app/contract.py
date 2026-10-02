"""Synthesises incidents that satisfy the Common Event Model.

The model is written out here rather than installed from
`packages/common-event-model`, which every service does use. The simulator stands
in for a third-party analytics producer, and such a producer has the published
contract, not our source tree -- building payloads from the same module the API
validates them with would hide exactly the mistakes an external producer makes.
That is why this file survived the model becoming a library: an independent
implementation of the contract is the point, not an accident.

Generation is seeded, so a load profile re-run with the same seed sends the
same incidents. A throughput figure that cannot be reproduced is an anecdote.
"""

import random
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# One entry per analytics pipeline the platform claims the Common Event Model
# can carry. Intrusion detection is the seed producer; the other two are here
# because reusability is a claim the load run either supports or undermines --
# a model only ever driven at scale by one analytics type has not been shown to
# be reusable.
ANALYTICS_CATALOGUE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("intrusion_detection", ("intrusion_detected", "perimeter_breach")),
    ("ppe_detection", ("helmet_missing", "vest_missing")),
    ("crowd_detection", ("crowd_density_exceeded",)),
)

SEVERITIES: tuple[str, ...] = ("low", "medium", "high")

# Every field the ingestion API rejects an incident for omitting. Asserted in
# the tests, so dropping one here fails the build rather than a load run.
REQUIRED_FIELDS = frozenset(
    {
        "event_id",
        "tenant_id",
        "site_id",
        "source_id",
        "source_name",
        "source_type",
        "analytics_type",
        "event_type",
        "severity",
        "timestamp",
        "event_purpose",
    }
)


@dataclass(frozen=True)
class GeneratedIncident:
    """One incident, and whether its id was deliberately reused."""

    payload: dict
    duplicate: bool


class IncidentGenerator:
    """Builds incident payloads for a tenant, at whatever rate it is asked for.

    `duplicate_rate` re-sends an `event_id` this generator has already issued.
    The store keys documents on `event_id`, so a repeat is rejected as a
    duplicate rather than overwriting the original -- which makes deduplication
    a measurable outcome of the load run instead of a claim in the report.
    """

    def __init__(
        self,
        seed: int = 0,
        sites_per_tenant: int = 4,
        cameras_per_site: int = 8,
        duplicate_rate: float = 0.0,
        replay_window: int = 512,
    ):
        if not 0.0 <= duplicate_rate <= 1.0:
            raise ValueError("duplicate_rate must be between 0.0 and 1.0")

        # Shaping load, not generating secrets: reproducibility is the point.
        self._rng = random.Random(seed)
        self._sites_per_tenant = sites_per_tenant
        self._cameras_per_site = cameras_per_site
        self._duplicate_rate = duplicate_rate
        # Bounded: a long run would otherwise hold every id it ever sent.
        self._issued: deque[str] = deque(maxlen=replay_window)
        self._sequence = 0

    def next_event(self, tenant_id: str) -> GeneratedIncident:
        analytics_type, event_types = self._rng.choice(ANALYTICS_CATALOGUE)
        site = self._rng.randrange(1, self._sites_per_tenant + 1)
        camera = self._rng.randrange(1, self._cameras_per_site + 1)

        duplicate = bool(
            self._issued and self._rng.random() < self._duplicate_rate
        )
        if duplicate:
            event_id = self._rng.choice(list(self._issued))
        else:
            self._sequence += 1
            event_id = f"EVT-LOAD-{self._sequence:09d}"
            self._issued.append(event_id)

        # The producer reports when the incident happened, and a camera's clock
        # is not the platform's. A small backdated offset keeps the timestamps
        # from being indistinguishable from the moment of the HTTP call.
        occurred = datetime.now(timezone.utc) - timedelta(
            milliseconds=self._rng.randrange(0, 2_000)
        )

        payload = {
            "event_id": event_id,
            "tenant_id": tenant_id,
            "site_id": f"SITE-{site:02d}",
            "source_id": f"CAM-{site:02d}-{camera:03d}",
            "source_name": f"Camera {camera} at site {site}",
            "source_type": "video_analytics",
            "analytics_type": analytics_type,
            "event_type": self._rng.choice(event_types),
            "severity": self._rng.choice(SEVERITIES),
            "timestamp": occurred.isoformat(),
            "event_purpose": "load_test",
            "confidence": round(self._rng.uniform(0.70, 0.99), 2),
            "description": f"Synthetic {analytics_type} incident from the load simulator",
        }
        return GeneratedIncident(payload=payload, duplicate=duplicate)

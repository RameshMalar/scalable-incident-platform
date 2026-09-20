"""Records what the run actually did, per request and in aggregate.

Two numbers decide whether the rest of the report means anything, and both are
about the generator rather than the platform.

The first is achieved rate against target rate. Asking for 140 requests a
second and managing 96 makes every latency figure a measurement of a different
experiment from the one that was designed.

The second is scheduling lag: how late each request left against the schedule
it was due on. Lag is what saturation of the *client* looks like, and it climbs
long before the achieved rate visibly drops. Without it, a runner that has run
out of sockets or CPU reports the platform as slow.
"""

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path

# Above this the generator is no longer holding its schedule, so the run
# describes the simulator's limits rather than the platform's.
LAG_BUDGET_MS = 250.0


@dataclass(frozen=True)
class Sample:
    """One attempted request."""

    stage: str
    producer: str
    tenant_id: str
    event_id: str
    duplicate: bool
    # 0 when no response arrived at all: a timeout, a refused connection, or a
    # token that could not be obtained.
    status: int
    latency_ms: float
    sent_at: float
    lag_ms: float
    error: str | None = None


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile, or None when there is nothing to report."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * (p / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


class Recorder:
    def __init__(self):
        self.samples: list[Sample] = []

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)

    def summary(self) -> dict:
        return {
            "overall": self._summarise(self.samples),
            "stages": [
                self._summarise([s for s in self.samples if s.stage == stage])
                | {"stage": stage}
                for stage in dict.fromkeys(s.stage for s in self.samples)
            ],
        }

    def _summarise(self, samples: list[Sample]) -> dict:
        if not samples:
            return {"requests": 0}

        answered = [s for s in samples if s.status]
        latencies = [s.latency_ms for s in answered]
        lags = [s.lag_ms for s in samples]

        by_status: dict[str, int] = {}
        for sample in samples:
            key = str(sample.status) if sample.status else "no_response"
            by_status[key] = by_status.get(key, 0) + 1

        span = max(s.sent_at for s in samples) - min(s.sent_at for s in samples)
        achieved = len(samples) / span if span > 0 else None

        max_lag = max(lags)
        return {
            "requests": len(samples),
            "accepted": sum(1 for s in samples if 200 <= s.status < 300),
            "duplicates_sent": sum(1 for s in samples if s.duplicate),
            "by_status": by_status,
            "errors": sorted(
                {s.error for s in samples if s.error},
            ),
            "achieved_rps": round(achieved, 2) if achieved else None,
            "latency_ms": {
                "p50": _round(percentile(latencies, 50)),
                "p95": _round(percentile(latencies, 95)),
                "p99": _round(percentile(latencies, 99)),
                "max": _round(max(latencies)) if latencies else None,
            },
            "schedule_lag_ms": {
                "p95": _round(percentile(lags, 95)),
                "max": _round(max_lag),
            },
            # Reported as a verdict rather than left for the reader to derive,
            # because every other figure here is void when it is false.
            "generator_kept_up": max_lag <= LAG_BUDGET_MS,
        }

    def write_json(self, path: Path, run: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"run": run, **self.summary()}, indent=2) + "\n",
            encoding="utf-8",
        )

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = list(Sample.__dataclass_fields__)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for sample in self.samples:
                writer.writerow(asdict(sample))


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None

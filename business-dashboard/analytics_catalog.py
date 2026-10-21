"""Display names for analytics and event types, and what Simulate Event offers.

The Common Event Model keeps `analytics_type` and `event_type` as open strings,
so a new analytics pipeline needs no platform change. This catalog only decides
how those values are *shown*: it never rewrites a stored value. Two stored
spellings of one pipeline (`PPE_detection`, `ppe_detection`) are aliases and
share a display name; a value the catalog does not know is still shown, with a
readable name derived from it.
"""
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CATALOG_FILE = Path(__file__).with_name("analytics_catalog.json")
SEVERITIES = ("critical", "high", "medium", "low")
# Stored identifiers for custom analytics typed into Simulate Event.
CUSTOM_NAME = re.compile(r"^[a-z][a-z0-9_]{2,47}$")


@dataclass(frozen=True)
class EventSpec:
    type: str
    display_name: str
    severity: str
    description: str
    evidence_pool: str | None


@dataclass(frozen=True)
class AnalyticsSpec:
    type: str
    display_name: str
    aliases: tuple[str, ...]
    events: tuple[EventSpec, ...]
    custom: bool = False


def humanise(value) -> str:
    text = str(value).replace("_", " ").replace("-", " ").strip()
    return " ".join(word if word.isupper() else word.capitalize() for word in text.split()) or str(value)


class Catalog:
    def __init__(self, analytics: list[AnalyticsSpec]):
        self.analytics = tuple(analytics)
        self._by_type = {}
        for spec in self.analytics:
            for name in (spec.type, *spec.aliases):
                if name in self._by_type:
                    raise ValueError(f"analytics type listed twice: {name}")
                self._by_type[name] = spec
        self._events = {}
        for spec in self.analytics:
            for event in spec.events:
                self._events.setdefault(event.type, event)

    def analytics_name(self, value) -> str:
        spec = self._by_type.get(str(value))
        return spec.display_name if spec else humanise(value)

    def event_name(self, value) -> str:
        event = self._events.get(str(value))
        return event.display_name if event else humanise(value)

    def spec(self, analytics_type) -> AnalyticsSpec | None:
        return self._by_type.get(str(analytics_type))


def _parse(data) -> Catalog:
    analytics = []
    for entry in data["analytics"]:
        events = []
        for event in entry["events"]:
            if event["severity"] not in SEVERITIES:
                raise ValueError(f"unknown severity for {event['type']}")
            events.append(EventSpec(event["type"], event["display_name"], event["severity"], event["description"],
                                    event.get("evidence_pool", entry.get("evidence_pool"))))
        if not events:
            raise ValueError(f"{entry['type']} has no events")
        analytics.append(AnalyticsSpec(entry["type"], entry["display_name"], tuple(entry.get("aliases", [])),
                                       tuple(events), bool(entry.get("custom", False))))
    return Catalog(analytics)


@lru_cache(maxsize=1)
def load_catalog(path: str | None = None) -> Catalog:
    return _parse(json.loads(Path(path or CATALOG_FILE).read_text(encoding="utf-8")))


def valid_custom_name(value) -> bool:
    return isinstance(value, str) and bool(CUSTOM_NAME.fullmatch(value))

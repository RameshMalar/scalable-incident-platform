import pytest

from app.contract import ANALYTICS_CATALOGUE, REQUIRED_FIELDS, IncidentGenerator


def test_every_incident_carries_every_required_field():
    generator = IncidentGenerator(seed=1)

    for _ in range(50):
        payload = generator.next_event("TENANT-01").payload
        assert REQUIRED_FIELDS <= payload.keys()
        assert all(payload[field] not in (None, "") for field in REQUIRED_FIELDS)


def test_the_incident_is_attributed_to_the_tenant_it_was_asked_for():
    generator = IncidentGenerator(seed=1)

    assert generator.next_event("TENANT-07").payload["tenant_id"] == "TENANT-07"


def test_every_generated_event_is_explicitly_classified_as_load_test():
    event = IncidentGenerator(seed=1).next_event("TENANT-01")

    assert event.payload["event_purpose"] == "load_test"


def test_the_same_seed_replays_the_same_run():
    def run():
        generator = IncidentGenerator(seed=42)
        # Timestamps are read off the wall clock, so they are excluded:
        # everything the generator itself decides has to be reproducible.
        return [
            {
                field: value
                for field, value in generator.next_event("T").payload.items()
                if field != "timestamp"
            }
            for _ in range(20)
        ]

    assert run() == run()


def test_a_different_seed_produces_a_different_run():
    one = IncidentGenerator(seed=1).next_event("T").payload
    two = IncidentGenerator(seed=2).next_event("T").payload

    assert (one["analytics_type"], one["site_id"], one["severity"]) != (
        two["analytics_type"],
        two["site_id"],
        two["severity"],
    )


def test_ids_are_unique_when_no_duplicates_are_asked_for():
    generator = IncidentGenerator(seed=3)

    ids = [generator.next_event("T").payload["event_id"] for _ in range(200)]

    assert len(set(ids)) == len(ids)
    assert not any(generator.next_event("T").duplicate for _ in range(10))


def test_duplicates_reuse_an_id_already_sent():
    generator = IncidentGenerator(seed=4, duplicate_rate=1.0)

    first = generator.next_event("T")
    repeats = [generator.next_event("T") for _ in range(10)]

    # The first has nothing to repeat, so it is always original.
    assert first.duplicate is False
    assert all(event.duplicate for event in repeats)
    assert all(
        event.payload["event_id"] == first.payload["event_id"] for event in repeats
    )


def test_every_analytics_type_in_the_catalogue_gets_exercised():
    generator = IncidentGenerator(seed=5)

    seen = {generator.next_event("T").payload["analytics_type"] for _ in range(300)}

    assert seen == {name for name, _ in ANALYTICS_CATALOGUE}


def test_event_type_belongs_to_its_analytics_type():
    allowed = dict(ANALYTICS_CATALOGUE)
    generator = IncidentGenerator(seed=6)

    for _ in range(100):
        payload = generator.next_event("T").payload
        assert payload["event_type"] in allowed[payload["analytics_type"]]


def test_an_impossible_duplicate_rate_is_refused():
    with pytest.raises(ValueError):
        IncidentGenerator(duplicate_rate=1.5)

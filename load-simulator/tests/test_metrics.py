import csv
import json

from app.metrics import LAG_BUDGET_MS, Recorder, Sample, percentile


def sample(**overrides):
    defaults = {
        "stage": "stage1-10rps",
        "producer": "alpha",
        "tenant_id": "TENANT-01",
        "event_id": "EVT-1",
        "duplicate": False,
        "status": 202,
        "latency_ms": 10.0,
        "sent_at": 0.0,
        "lag_ms": 0.0,
        "error": None,
    }
    return Sample(**{**defaults, **overrides})


def test_percentile_of_nothing_is_nothing():
    assert percentile([], 95) is None


def test_percentile_of_one_value_is_that_value():
    assert percentile([7.5], 95) == 7.5


def test_percentile_interpolates_between_neighbours():
    values = [1.0, 2.0, 3.0, 4.0]

    assert percentile(values, 50) == 2.5
    assert percentile(values, 0) == 1.0
    assert percentile(values, 100) == 4.0


def test_statuses_are_counted_separately_and_accepted_means_2xx():
    recorder = Recorder()
    for status in (202, 202, 429, 403, 500):
        recorder.add(sample(status=status))

    overall = recorder.summary()["overall"]

    assert overall["requests"] == 5
    assert overall["accepted"] == 2
    assert overall["by_status"] == {"202": 2, "429": 1, "403": 1, "500": 1}


def test_a_request_that_never_got_a_response_is_not_a_status():
    recorder = Recorder()
    recorder.add(sample(status=0, error="ReadTimeout"))
    recorder.add(sample(status=202))

    overall = recorder.summary()["overall"]

    assert overall["by_status"] == {"no_response": 1, "202": 1}
    assert overall["errors"] == ["ReadTimeout"]
    # An unanswered request has no latency to report, so it must not be
    # averaged in as a fast one.
    assert overall["latency_ms"]["p50"] == 10.0


def test_achieved_rate_comes_from_the_span_the_requests_were_sent_over():
    recorder = Recorder()
    for index in range(11):
        recorder.add(sample(sent_at=index * 0.1))

    # 11 requests spread across one second.
    assert recorder.summary()["overall"]["achieved_rps"] == 11.0


def test_the_generator_is_reported_as_falling_behind_once_lag_exceeds_the_budget():
    within = Recorder()
    within.add(sample(lag_ms=LAG_BUDGET_MS - 1))
    beyond = Recorder()
    beyond.add(sample(lag_ms=LAG_BUDGET_MS + 1))

    assert within.summary()["overall"]["generator_kept_up"] is True
    assert beyond.summary()["overall"]["generator_kept_up"] is False


def test_stages_are_summarised_separately():
    recorder = Recorder()
    recorder.add(sample(stage="stage1-10rps", status=202))
    recorder.add(sample(stage="stage2-50rps", status=429))

    stages = recorder.summary()["stages"]

    assert [stage["stage"] for stage in stages] == ["stage1-10rps", "stage2-50rps"]
    assert stages[0]["accepted"] == 1
    assert stages[1]["accepted"] == 0


def test_duplicates_are_counted_so_dedup_can_be_checked_against_the_store():
    recorder = Recorder()
    recorder.add(sample(duplicate=True, status=202))
    recorder.add(sample(duplicate=False, status=202))

    assert recorder.summary()["overall"]["duplicates_sent"] == 1


def test_the_summary_is_written_with_the_run_that_produced_it(tmp_path):
    recorder = Recorder()
    recorder.add(sample())
    path = tmp_path / "nested" / "summary.json"

    recorder.write_json(path, run={"target": "https://example.invalid", "seed": 7})

    written = json.loads(path.read_text())
    assert written["run"]["seed"] == 7
    assert written["overall"]["requests"] == 1


def test_every_request_is_written_as_its_own_csv_row(tmp_path):
    recorder = Recorder()
    recorder.add(sample(event_id="EVT-1"))
    recorder.add(sample(event_id="EVT-2", status=429))
    path = tmp_path / "samples.csv"

    recorder.write_csv(path)

    rows = list(csv.DictReader(path.open()))
    assert [row["event_id"] for row in rows] == ["EVT-1", "EVT-2"]
    assert rows[1]["status"] == "429"

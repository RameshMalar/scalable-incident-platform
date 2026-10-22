"""Guards for the DAST workflow and scripts/dast.py.

The scan points third-party code at production, so the properties that keep it
safe are tested rather than trusted: manual only, no cloud credential, a pinned
scanner, inputs never spliced into shell, our own hosts only, a preflight
before any scan, and the token-free simulate endpoint out of reach.
"""
import importlib.util
import json
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

spec = importlib.util.spec_from_file_location("dast", ROOT / "scripts/dast.py")
dast = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dast)


def workflow():
    loaded = yaml.safe_load((ROOT / ".github/workflows/dast.yml").read_text(encoding="utf-8"))
    loaded["on"] = loaded.pop(True, loaded.get("on"))  # YAML 1.1 reads 'on' as True
    return loaded


def steps():
    return workflow()["jobs"]["scan"]["steps"]


def test_manual_only_and_cloud_free():
    flow = workflow()
    assert set(flow["on"]) == {"workflow_dispatch"}
    assert flow["permissions"] == {"contents": "read"}
    for job in flow["jobs"].values():
        assert "uses" not in job
        assert job.get("permissions", {}).get("id-token", "none") == "none"
        for step in job["steps"]:
            assert not step.get("uses", "").startswith(("azure/", "./"))
            assert not re.search(r"\baz\s", step.get("run", ""))


def test_scanner_pinned_by_digest_and_used_everywhere():
    assert re.fullmatch(r"ghcr\.io/zaproxy/zaproxy@sha256:[0-9a-f]{64}", workflow()["env"]["ZAP_IMAGE"])
    runs = [s["run"] for s in steps() if "docker run" in s.get("run", "")]
    assert len(runs) == 3
    assert all('"$ZAP_IMAGE"' in run for run in runs)


def test_inputs_never_interpolated_into_shell():
    for step in steps():
        assert "${{ inputs." not in step.get("run", "")


def test_targets_and_preflight_come_before_any_scan():
    names = [s.get("name", "") for s in steps()]
    first_scan = next(i for i, s in enumerate(steps()) if "docker run" in s.get("run", ""))
    assert names.index("Accept only this platform's Container Apps as targets") < first_scan
    preflight = names.index("Refuse to scan unless anonymous calls are rejected")
    assert preflight < first_scan
    assert "scripts/verify-anonymous-ingestion.sh" in steps()[preflight]["run"]
    assert names.index("Fail on any High-risk alert") > first_scan


def test_apis_are_warmed_before_the_security_preflight():
    names = [s.get("name", "") for s in steps()]
    warmup = names.index("Warm the APIs before security preflight")
    preflight = names.index("Refuse to scan unless anonymous calls are rejected")

    assert warmup < preflight
    run = steps()[warmup]["run"]
    assert '"$DAST_INGESTION"' in run
    assert '"$DAST_SUBSCRIPTION"' in run
    assert "seq 1 20" in run


def test_dashboard_is_warmed_before_its_baseline_scan():
    names = [s.get("name", "") for s in steps()]
    warmup = names.index("Warm the dashboard before baseline scanning")
    baseline = names.index("Passive baseline scan of the dashboard")

    assert warmup < baseline
    assert '"$DAST_DASHBOARD/_stcore/health"' in steps()[warmup]["run"]
    assert "seq 1 20" in steps()[warmup]["run"]
    assert "for attempt in 1 2 3" in steps()[baseline]["run"]


def test_active_scans_only_attack_filtered_definitions():
    for step in steps():
        run = step.get("run", "")
        if "zap-api-scan.py" in run:
            assert re.search(r"-t (ingestion|subscription)-openapi\.json", run)
        if "zap-baseline.py" in run:
            assert "DAST_DASHBOARD" in run  # the passive scan never attacks


@pytest.mark.parametrize("role,url", [
    ("ingestion", "https://incident-ingestion.blueriver-1a2b3c.malaysiawest.azurecontainerapps.io"),
    ("ingestion", "https://incident-ingestion.blueriver-1a2b3c.malaysiawest.azurecontainerapps.io/"),
    ("dashboard", "https://incident-dashboard.blueriver-1a2b3c.malaysiawest.azurecontainerapps.io"),
])
def test_accepts_own_container_apps(role, url):
    assert dast.check_target(role, url) == url.rstrip("/")


@pytest.mark.parametrize("role,url", [
    ("ingestion", "http://incident-ingestion.blueriver-1a2b3c.malaysiawest.azurecontainerapps.io"),
    ("ingestion", "https://incident-ingestion.example.com"),
    ("ingestion", "https://incident-ingestion.azurecontainerapps.io.evil.example"),
    ("ingestion", "https://incident-dashboard.blueriver-1a2b3c.malaysiawest.azurecontainerapps.io"),
    ("ingestion", "https://incident-ingestion.blueriver-1a2b3c.malaysiawest.azurecontainerapps.io/docs"),
    ("ingestion", "https://incident-ingestion.blueriver-1a2b3c.malaysiawest.azurecontainerapps.io:8443"),
    ("ingestion", "https://user:pw@incident-ingestion.blueriver-1a2b3c.malaysiawest.azurecontainerapps.io"),
    ("ingestion", "https://incident-ingestion.blueriver-1a2b3c.malaysiawest.azurecontainerapps.io?x=1"),
    ("unknown", "https://incident-ingestion.blueriver-1a2b3c.malaysiawest.azurecontainerapps.io"),
])
def test_refuses_anything_else(role, url):
    with pytest.raises(dast.Refused):
        dast.check_target(role, url)


def test_simulate_removed_and_the_rest_kept():
    document = {"openapi": "3.1.0", "paths": {
        "/": {}, "/health": {}, "/ready": {}, "/api/v1/incidents": {},
        "/api/v1/simulate/{analytics_type}": {},
    }}
    filtered, removed = dast.filter_spec(document)
    assert removed == ["/api/v1/simulate/{analytics_type}"]
    assert set(filtered["paths"]) == {"/", "/health", "/ready", "/api/v1/incidents"}
    assert "/api/v1/simulate/{analytics_type}" in document["paths"]  # input untouched


def test_refuses_an_empty_definition():
    with pytest.raises(dast.Refused):
        dast.filter_spec({"paths": {"/api/v1/simulate/x": {}}})
    with pytest.raises(dast.Refused):
        dast.filter_spec({})


def fake(status, body=b""):
    return lambda url: (status, body)


def test_ingestion_preflight_requires_entra():
    dast.preflight("ingestion", "https://x", get=fake(200, b'{"auth": "entra"}'))
    for status, body in [(200, b'{"auth": "off"}'), (200, b"not json"), (503, b'{"auth": "entra"}')]:
        with pytest.raises(dast.Refused):
            dast.preflight("ingestion", "https://x", get=fake(status, body))


def test_subscription_preflight_requires_401():
    dast.preflight("subscription", "https://x", get=fake(401))
    for status in (200, 403, 404, 500):
        with pytest.raises(dast.Refused):
            dast.preflight("subscription", "https://x", get=fake(status))


def test_http_get_turns_a_transport_timeout_into_a_refusal(monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(dast.urllib.request, "urlopen", timeout)
    with pytest.raises(dast.Refused, match="request to https://example.test failed: timed out"):
        dast.http_get("https://example.test")


def report(tmp_path, name, riskcodes):
    path = tmp_path / name
    alerts = [{"alert": f"rule {i}", "riskcode": str(code)} for i, code in enumerate(riskcodes)]
    path.write_text(json.dumps({"site": [{"@name": "https://target", "alerts": alerts}]}))
    return str(path)


def test_gate_passes_without_high(tmp_path):
    summary, high = dast.summarise([report(tmp_path, "a.json", [2, 1, 1, 0])])
    assert high == 0
    assert "| https://target | 0 | 1 | 2 | 1 |" in summary
    assert dast.main(["gate", report(tmp_path, "b.json", [2])]) == 0


def test_gate_fails_on_high(tmp_path):
    assert dast.main(["gate", report(tmp_path, "a.json", [1]), report(tmp_path, "b.json", [3])]) == 1


def test_gate_fails_closed_on_an_empty_report(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"site": []}))
    assert dast.main(["gate", str(empty)]) == 1

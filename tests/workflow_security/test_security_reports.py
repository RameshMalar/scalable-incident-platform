"""Guards for the SAST/SCA reports CI records on every run.

The report steps must only report: the Bandit and pip-audit gates keep deciding
pass or fail, the report steps run whatever happened, and a missing report is
stated rather than raised.
"""
import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

spec = importlib.util.spec_from_file_location("security_summary", ROOT / "scripts/security-summary.py")
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def security_steps():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    return {s.get("name"): s for s in ci["jobs"]["security"]["steps"]}


def test_gates_unchanged_and_reports_always_recorded():
    steps = security_steps()
    names = list(steps)
    assert steps["Scan source for insecure patterns"]["run"] == "bandit -r ${{ matrix.bandit_target }} -ll"
    assert steps["Scan dependencies for known vulnerabilities"]["run"] == "pip-audit -r ${{ matrix.service }}/requirements.txt"
    for name in ("Record the scan reports", "Upload the scan reports"):
        assert steps[name]["if"] == "always()"
        assert names.index(name) > names.index("Scan dependencies for known vulnerabilities")
    record = steps["Record the scan reports"]["run"]
    assert record.count("--exit-zero") == 2  # reporting never fails the job; the gate does
    assert steps["Upload the scan reports"]["with"]["name"] == "security-report-${{ matrix.service }}"


def write(directory, name, document):
    (directory / name).write_text(json.dumps(document))


def test_summary_counts_findings(tmp_path):
    write(tmp_path, "bandit.json", {
        "results": [{"issue_severity": "LOW"}, {"issue_severity": "LOW"}, {"issue_severity": "MEDIUM"}],
        "metrics": {"_totals": {"loc": 685}},
    })
    write(tmp_path, "pip-audit.json", {"dependencies": [
        {"name": "fastapi", "version": "0.1", "vulns": []},
        {"name": "jwt", "version": "2.14.0", "vulns": [{"id": "CVE-2026-101918", "fix_versions": ["2.15.0"]}]},
    ]})
    text = summary.render("ingestion-service", tmp_path)
    assert "3 issue(s) in 685 lines (High 0 · Medium 1 · Low 2)" in text
    assert "1 known vulnerability in 2 packages" in text
    assert "| jwt 2.14.0 | CVE-2026-101918 | 2.15.0 |" in text
    assert "security-report-ingestion-service" in text


def test_summary_states_missing_reports(tmp_path):
    text = summary.render("worker-service", tmp_path)
    assert text.count("report not produced") == 2

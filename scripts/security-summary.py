#!/usr/bin/env python3
"""Render one service's source and dependency scan results for the run page.

CI's security job gates on `bandit -ll` and `pip-audit`; those steps decide
pass or fail. This script only reports: it reads the JSON both scanners wrote
and prints a Markdown summary for $GITHUB_STEP_SUMMARY, so every run shows its
results without opening a log. A missing report is said, never raised, because
reporting must not decide the build.

Usage: security-summary.py <service> <report-dir>
"""

import json
import sys
from pathlib import Path

SEVERITIES = ("HIGH", "MEDIUM", "LOW")


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def bandit_row(report):
    if not isinstance(report, dict):
        return "report not produced"
    issues = report.get("results", [])
    counts = {s: sum(1 for i in issues if i.get("issue_severity") == s) for s in SEVERITIES}
    lines = (report.get("metrics", {}).get("_totals", {}) or {}).get("loc", 0)
    detail = " · ".join(f"{s.title()} {counts[s]}" for s in SEVERITIES)
    return f"{len(issues)} issue(s) in {lines} lines ({detail}); the build fails on Medium or High"


def pip_audit_rows(report):
    if not isinstance(report, dict):
        return "report not produced", []
    dependencies = report.get("dependencies", [])
    vulnerable = [d for d in dependencies if d.get("vulns")]
    found = sum(len(d["vulns"]) for d in vulnerable)
    rows = [
        f"| {d.get('name')} {d.get('version')} | {v.get('id')} | {', '.join(v.get('fix_versions', [])) or 'none yet'} |"
        for d in vulnerable for v in d["vulns"]
    ]
    return f"{found} known vulnerabilit{'y' if found == 1 else 'ies'} in {len(dependencies)} packages", rows


def render(service, directory):
    directory = Path(directory)
    audit, vulnerable = pip_audit_rows(load(directory / "pip-audit.json"))
    lines = [
        f"### Source and dependency scan · {service}",
        "",
        "| Scanner | Result |",
        "| --- | --- |",
        f"| Bandit (SAST) | {bandit_row(load(directory / 'bandit.json'))} |",
        f"| pip-audit (SCA) | {audit} |",
    ]
    if vulnerable:
        lines += ["", "| Package | Advisory | Fixed in |", "| --- | --- | --- |", *vulnerable]
    lines += ["", f"Full Bandit report: the `security-report-{service}` artifact (bandit.html).", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip().splitlines()[-1])
    print(render(sys.argv[1], sys.argv[2]))

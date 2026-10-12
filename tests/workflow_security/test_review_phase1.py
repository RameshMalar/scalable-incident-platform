"""Offline executable regressions for PR review Phase 1."""
import copy
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SHA = "ed142fd0673e97e23eac54620cfb913e5ce36c25"
BASH = "C:/Program Files/Git/bin/bash.exe" if Path("C:/Program Files/Git/bin/bash.exe").exists() else "bash"


@pytest.mark.parametrize("notifier,accepted", [("TelegramNotifier", True), ("MockNotifier", False), ("telegram", False)])
def test_both_readiness_checks(notifier, accepted):
    cd = (ROOT / ".github/workflows/cd.yml").read_text()
    patterns = re.findall(r"grep -Eq '([^']*notification service ready[^']*)'", cd)
    assert len(patterns) == 2
    source = (ROOT / "notification-service/app/notification_main.py").read_text()
    assert 'notifier.__class__.__name__' in source
    for pattern in patterns:
        result = subprocess.run([BASH, "-c", 'grep -Eq "$1"', "--", pattern],
                                input=f"notification service ready notifier={notifier}\n", text=True)
        assert (result.returncode == 0) is accepted


@pytest.mark.parametrize("status", ["401", "202", "422", "500", "503", "000"])
def test_anonymous_probe_accepts_only_401(status):
    script = ROOT / "scripts/verify-anonymous-ingestion.sh"
    # Shell function replaces curl completely; never contacts any endpoint.
    command = 'curl() { for arg in "$@"; do case "$arg" in *Authorization*|*Bearer*) return 99;; esac; done; printf \'%s\' "$STATUS"; }; export -f curl; export STATUS="$2"; export INGESTION_URL=https://synthetic.invalid; bash "$1"'
    result = subprocess.run([BASH, "-c", command, "--", script.as_posix(), status], capture_output=True, text=True)
    assert (result.returncode == 0) is (status == "401")


@pytest.mark.parametrize("name,count", [("ci.yml", 2), ("cd.yml", 6)])
def test_trivy_pin_preserves_exact_scan_configuration(name, count):
    old = yaml.safe_load(subprocess.check_output(["git", "show", f"HEAD:.github/workflows/{name}"], cwd=ROOT))
    new = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    def scans(workflow):
        return [s for j in workflow['jobs'].values() for s in j.get('steps', [])
                if s.get('uses', '').startswith('aquasecurity/trivy-action@')]
    before, after = scans(old), scans(new)
    assert len(before) == len(after) == count
    for original, actual in zip(before, after):
        expected = copy.deepcopy(original)
        expected['uses'] = 'aquasecurity/trivy-action@' + SHA
        assert actual == expected


def test_probe_is_separate_and_has_no_authenticated_publish():
    script = (ROOT / 'scripts/verify-anonymous-ingestion.sh').read_text()
    assert '-H Authorization' not in script and 'Bearer' not in script
    assert script.count('curl ') == 1
    assert '-X POST' in script and '"$anon" != "401"' in script
    subprocess.run([BASH, '-n', (ROOT / 'scripts/verify-anonymous-ingestion.sh').as_posix()], check=True)

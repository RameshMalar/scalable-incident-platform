"""Regressions for the read-only prepare preflight, found by CD's first run after #61."""
import importlib.util
from pathlib import Path
import subprocess
import uuid
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("queue_gate", ROOT / "scripts/verify-notification-queue.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.mark.parametrize("reported, expected", [
    ("Malaysia West", "malaysiawest"),   # what Container Apps returns
    ("malaysiawest", "malaysiawest"),
    ("East Asia", "eastasia"),
])
def test_baseline_compares_locations_in_short_form(reported, expected):
    cd = yaml.safe_load((ROOT / ".github/workflows/cd.yml").read_text())
    run = next(s for s in cd["jobs"]["prepare"]["steps"] if s.get("id") == "baseline")["run"]
    line = next(l.strip() for l in run.splitlines() if l.strip().startswith("actual_location="))
    # Keep the step's own normalisation and replace only the Azure call.
    pipeline = line.split("-o tsv", 1)[1].rstrip(")")
    script = f"printf '%s\\n' '{reported}'{pipeline}"
    assert subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.strip() == expected


def test_queue_gate_lists_role_assignments_with_a_valid_az_invocation(monkeypatch):
    for name, value in {"RESOURCE_GROUP": "rg", "SERVICE_BUS_NAMESPACE": "bus", "SELECTED_NOTIFICATION_QUEUE": ""}.items():
        monkeypatch.setenv(name, value)
    calls = []

    class Stop(Exception):
        pass

    def az(*args):
        calls.append(args)
        if args[:3] == ("role", "assignment", "list"):
            raise Stop
        if args[0] == "identity":
            return {"clientId": str(uuid.uuid4())}
        return {"id": "/queues/" + gate.LEGACY, "name": gate.LEGACY}

    monkeypatch.setattr(gate, "az", az)
    with pytest.raises(Stop):
        gate.main()
    listing = next(call for call in calls if call[:3] == ("role", "assignment", "list"))
    # The Azure CLI rejects --all together with --scope or --resource-group.
    assert "--scope" in listing and "--all" not in listing


def test_every_subscription_api_check_signs_in_right_before():
    # GitHub's federated assertion lasts five minutes, and a token for the
    # Subscription API audience is first requested by verify-subscription-api.sh.
    # Run 38502649013 failed with AADSTS700024 eleven minutes after sign-in.
    cd = yaml.safe_load((ROOT / ".github/workflows/cd.yml").read_text())
    checked = 0
    for job in cd["jobs"].values():
        steps = job.get("steps", [])
        for index, step in enumerate(steps):
            if "verify-subscription-api.sh" in step.get("run", ""):
                assert steps[index - 1].get("uses", "").startswith("azure/login@"), step["name"]
                checked += 1
    assert checked == 2

"""Automatic CD dispatch keeps production inputs explicit and reviewed."""
import json
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    # PyYAML reads the `on:` key as True.
    workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    workflow["on"] = workflow.pop(True, workflow.get("on"))
    return workflow


def test_dispatcher_runs_on_merges_to_main_only_with_minimal_rights():
    workflow = load("cd-on-merge.yml")
    assert set(workflow["on"]) == {"push"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert set(workflow["on"]["push"]["paths-ignore"]) == {
        "**.md",
        "docs/**",
        "evidence_images/**",
    }
    assert workflow["permissions"] == {"contents": "read", "actions": "write"}
    (job,) = workflow["jobs"].values()
    assert "permissions" not in job and "environment" not in job
    run = "\n".join(step.get("run", "") for step in job["steps"])
    assert "gh workflow run cd.yml" in run and "--ref main" in run
    assert "infra/cd-production.json" in run
    assert 'expected_deploy_sha=${GITHUB_SHA}' in run
    assert "az " not in run and "azure/" not in str(job["steps"])


def test_automatic_dispatcher_never_receives_cloud_credentials():
    workflow = load("cd-on-merge.yml")
    assert workflow["permissions"].get("id-token", "none") != "write"
    (job,) = workflow["jobs"].values()
    assert job.get("permissions", {}).get("id-token", "none") != "write"
    assert all(not step.get("uses", "").startswith("azure/") for step in job["steps"])
    run = "\n".join(step.get("run", "") for step in job["steps"])
    assert "az login" not in run and "az deployment" not in run


def test_cd_keeps_explicit_dispatch_inputs():
    cd = load("cd.yml")
    assert set(cd["on"]) == {"workflow_dispatch"}
    assert cd["on"]["workflow_dispatch"]["inputs"]["expected_deploy_sha"] == {
        "description": "Exact main commit authorized by the dispatcher",
        "type": "string",
        "required": True,
    }

    preflight = cd["jobs"]["preflight"]
    exact_sha = next(
        step for step in preflight["steps"]
        if step.get("name") == "Require the exact merge commit selected by the dispatcher"
    )
    assert exact_sha["env"] == {
        "EXPECTED_DEPLOY_SHA": "${{ inputs.expected_deploy_sha }}",
        "DEPLOY_SHA": "${{ github.sha }}",
    }
    assert '"$EXPECTED_DEPLOY_SHA" != "$DEPLOY_SHA"' in exact_sha["run"]


def test_reviewed_inputs_are_exactly_what_cd_accepts():
    # Reviewed configuration supplies every production value. The dispatcher
    # adds the exact triggering commit dynamically.
    inputs = load("cd.yml")["on"]["workflow_dispatch"]["inputs"]
    config = json.loads((ROOT / "infra/cd-production.json").read_text())
    assert set(config) | {"expected_deploy_sha"} == set(inputs)
    assert all(
        name in config or name == "expected_deploy_sha"
        for name, spec in inputs.items()
        if spec.get("required")
    )


def test_manual_dispatch_also_pins_origin_main_sha():
    script = (ROOT / "scripts/run-cd.sh").read_text()
    assert 'deploy_sha="$(git rev-parse origin/main)"' in script
    assert 'expected_deploy_sha=${deploy_sha}' in script


def test_cd_resolves_the_latest_core_deployment_itself():
    cd = load("cd.yml")
    baseline = next(s for s in cd["jobs"]["prepare"]["steps"] if s.get("id") == "baseline")["run"]
    assert 'if [ "$CORE_DEPLOYMENT_NAME" = latest ]' in baseline
    assert baseline.index("= latest ]") < baseline.index("az deployment group show")
    assert json.loads((ROOT / "infra/cd-production.json").read_text())["core_deployment_name"] == "latest"

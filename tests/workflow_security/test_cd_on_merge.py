"""CD on merge: an automatic, cloud-free dispatcher of the reviewed CD inputs."""
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
    assert workflow["permissions"] == {"contents": "read", "actions": "write"}
    (job,) = workflow["jobs"].values()
    assert "permissions" not in job and "environment" not in job
    run = "\n".join(step.get("run", "") for step in job["steps"])
    assert "gh workflow run cd.yml" in run and "--ref main" in run
    assert "infra/cd-production.json" in run
    assert "az " not in run and "azure/" not in str(job["steps"])


def test_reviewed_inputs_are_exactly_what_cd_accepts():
    # A name CD does not declare would make every dispatch fail.
    inputs = load("cd.yml")["on"]["workflow_dispatch"]["inputs"]
    config = json.loads((ROOT / "infra/cd-production.json").read_text())
    assert set(config) == set(inputs)
    assert all(name in config for name, spec in inputs.items() if spec.get("required"))


def test_cd_resolves_the_latest_core_deployment_itself():
    cd = load("cd.yml")
    baseline = next(s for s in cd["jobs"]["prepare"]["steps"] if s.get("id") == "baseline")["run"]
    assert 'if [ "$CORE_DEPLOYMENT_NAME" = latest ]' in baseline
    assert baseline.index("= latest ]") < baseline.index("az deployment group show")
    assert json.loads((ROOT / "infra/cd-production.json").read_text())["core_deployment_name"] == "latest"

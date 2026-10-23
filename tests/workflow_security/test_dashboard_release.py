"""A merge must never replace the running dashboard; sign-in is staged."""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("validate_rollout_config", ROOT / "scripts" / "validate-rollout-config.py")
VALIDATOR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(VALIDATOR)
CLIENT = "00000000-0000-4000-8000-0000000000c1"


def load(name):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    workflow["on"] = workflow.pop(True, workflow.get("on"))
    return workflow


def steps():
    return {step.get("name"): step for job in load("cd.yml")["jobs"].values() for step in job.get("steps", [])}


def test_reviewed_default_keeps_the_running_dashboard():
    config = json.loads((ROOT / "infra" / "cd-production.json").read_text())
    assert config["dashboard_release"] == "keep"
    assert config["dashboard_auth_client_id"] == "" and config["dashboard_signin_reference"] == ""


def test_dashboard_is_deployed_only_on_explicit_release():
    explicit = "inputs.dashboard_release == 'signin-test' || inputs.dashboard_release == 'promote'"
    named = steps()
    assert named["Deploy the business dashboard"]["if"] == explicit
    assert named["Verify the dashboard is serving"]["if"] == explicit
    assert "keep" in named["Leave the business dashboard unchanged"]["run"]
    deploy = named["Deploy the business dashboard"]
    # Inputs reach the shell through env, never interpolated into the script.
    assert "${{ inputs." not in deploy["run"]
    assert {"RELEASE", "AUTH_CLIENT_ID", "KEY_VAULT"} <= set(deploy["env"])
    assert "signInUrl" in deploy["run"] and "trafficWeight" in deploy["run"]


def test_every_other_dashboard_template_parameter_keeps_its_default():
    template = (ROOT / "infra" / "dashboard.bicep").read_text()
    assert "param release string = 'standard'" in template
    assert "param authClientId string = ''" in template


def base():
    return {"DEPLOY_REF": "refs/heads/main", "SUBSCRIPTION_CONFIGURATION_APPROVED": "true", "CORE_DEPLOYMENT_NAME": "latest",
            "SUBSCRIPTION_AUTH_TENANT_ID": CLIENT, "SUBSCRIPTION_AUTH_AUDIENCE": "api://x",
            "SUBSCRIPTION_TENANT_MAP": CLIENT + ":TENANT-CD", "ACTIVATE_ROUTING": "false"}


@pytest.mark.parametrize("changes", [
    {}, {"DASHBOARD_RELEASE": ""}, {"DASHBOARD_RELEASE": "keep"},
    {"DASHBOARD_RELEASE": "signin-test", "DASHBOARD_AUTH_CLIENT_ID": CLIENT},
    {"DASHBOARD_RELEASE": "promote", "DASHBOARD_AUTH_CLIENT_ID": CLIENT, "DASHBOARD_SIGNIN_REFERENCE": "Admin signed in 2026-10-20"},
])
def test_valid_dashboard_releases(changes):
    VALIDATOR.validate({**base(), **changes}, inputs_only=True)


@pytest.mark.parametrize("changes", [
    {"DASHBOARD_RELEASE": "standard"}, {"DASHBOARD_RELEASE": "deploy"},
    # The local-login demo release is manual only; CD can never deploy it.
    {"DASHBOARD_RELEASE": "demo-login", "DASHBOARD_AUTH_CLIENT_ID": CLIENT},
    {"DASHBOARD_RELEASE": "signin-test"}, {"DASHBOARD_RELEASE": "signin-test", "DASHBOARD_AUTH_CLIENT_ID": "dashboard"},
    {"DASHBOARD_RELEASE": "promote", "DASHBOARD_AUTH_CLIENT_ID": CLIENT},
])
def test_unsafe_dashboard_releases_are_refused(changes):
    with pytest.raises(ValueError):
        VALIDATOR.validate({**base(), **changes}, inputs_only=True)

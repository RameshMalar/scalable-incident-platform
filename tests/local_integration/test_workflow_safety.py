"""Negative-path rollout gate tests. Synthetic configuration; no cloud IO."""
import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(filename):
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), ROOT / "scripts" / (filename + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


CONFIG = module("validate-rollout-config")
REVISION = module("verify-container-revision")
CLIENT = "00000000-0000-0000-0000-000000000001"
IMAGE = "synthetic.azurecr.io/incident-worker@sha256:" + "a" * 64


@pytest.fixture
def config():
    return {
        "DEPLOY_REF": "refs/heads/main", "CORE_DEPLOYMENT_NAME": "core-local",
        "SUBSCRIPTION_AUTH_TENANT_ID": CLIENT, "SUBSCRIPTION_AUTH_AUDIENCE": "api://" + CLIENT,
        "SUBSCRIPTION_TENANT_MAP": CLIENT + ":TENANT-LOCAL",
        "SUBSCRIPTION_CONFIGURATION_APPROVED": "true", "ACTIVATE_ROUTING": "false",
        "ROUTING_CONFIGURATION_APPROVED": "false", "AZURE_CLIENT_ID": CLIENT,
        "AZURE_TENANT_ID": CLIENT, "AZURE_SUBSCRIPTION_ID": CLIENT,
        "RESOURCE_GROUP": "rg-local", "AUTH_AUDIENCE": "api://" + CLIENT,
        "TENANT_MAP": CLIENT + ":TENANT-LOCAL", "TELEGRAM_CHAT_ID": "-100000001",
        "TELEGRAM_BOT_TOKEN_SECRET_URI": "https://synthetic.invalid/secrets/local/1",
    }


def test_preparation_does_not_require_routing_activation(config):
    CONFIG.validate(config)


@pytest.mark.parametrize("missing", ["SUBSCRIPTION_CONFIGURATION_APPROVED", "CORE_DEPLOYMENT_NAME", "SUBSCRIPTION_AUTH_TENANT_ID", "SUBSCRIPTION_AUTH_AUDIENCE", "SUBSCRIPTION_TENANT_MAP", "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "RESOURCE_GROUP", "AUTH_AUDIENCE", "TENANT_MAP", "TELEGRAM_CHAT_ID", "TELEGRAM_BOT_TOKEN_SECRET_URI"])
def test_missing_configuration_fails_closed(config, missing):
    config.pop(missing)
    with pytest.raises(ValueError):
        CONFIG.validate(config)


def test_environment_scoped_configuration_validated_after_preflight(config):
    for key in list(config):
        if key.startswith("AZURE_") or key in {"RESOURCE_GROUP", "AUTH_AUDIENCE", "TENANT_MAP", "TELEGRAM_CHAT_ID", "TELEGRAM_BOT_TOKEN_SECRET_URI"}:
            config.pop(key)
    CONFIG.validate(config, inputs_only=True)
    with pytest.raises(ValueError):
        CONFIG.validate(config)


@pytest.mark.parametrize("changes", [
    {"DEPLOY_REF": "refs/heads/feature"}, {"SUBSCRIPTION_CONFIGURATION_APPROVED": "false"},
    {"ACTIVATE_ROUTING": "true"}, {"SUBSCRIPTION_AUTH_TENANT_ID": "invalid"},
    {"SUBSCRIPTION_AUTH_AUDIENCE": " "}, {"SUBSCRIPTION_TENANT_MAP": CLIENT + ":A," + CLIENT + ":B"},
    {"SUBSCRIPTION_TENANT_MAP": "00000000-0000-0000-0000-000000000002:TENANT-OTHER"},
    {"SUBSCRIPTION_TENANT_MAP": CLIENT + ":TENANT-LOCAL\nunsafe"},
])
def test_unsafe_or_unapproved_configuration_fails(config, changes):
    config.update(changes)
    with pytest.raises(ValueError):
        CONFIG.validate(config)


@pytest.mark.parametrize("missing", ["ROUTING_CONFIGURATION_APPROVED", "ROUTING_APPROVAL_REFERENCE", "ROLLBACK_WORKER_IMAGE"])
def test_activation_requires_all_explicit_gates(config, missing):
    config.update(ACTIVATE_ROUTING="true", ROUTING_CONFIGURATION_APPROVED="true", ROUTING_APPROVAL_REFERENCE="local-review-reference", ROLLBACK_WORKER_IMAGE=IMAGE)
    config.pop(missing)
    with pytest.raises(ValueError):
        CONFIG.validate(config)


def test_activation_with_reviewed_config_and_digest(config):
    config.update(ACTIVATE_ROUTING="true", ROUTING_CONFIGURATION_APPROVED="true", ROUTING_APPROVAL_REFERENCE="local-review-reference", ROLLBACK_WORKER_IMAGE=IMAGE)
    CONFIG.validate(config)
    config["ROLLBACK_WORKER_IMAGE"] = "synthetic.azurecr.io/incident-worker:latest"
    with pytest.raises(ValueError):
        CONFIG.validate(config)


@pytest.fixture
def revision():
    env = {"INCIDENT_STORE": "cosmos", "NOTIFICATION_PUBLISHER": "servicebus", "COSMOS_ENDPOINT": "https://synthetic.invalid", "COSMOS_DATABASE": "incidents", "COSMOS_CONTAINER": "incidents", "COSMOS_SUBSCRIPTIONS_CONTAINER": "subscriptions", "NOTIFICATION_QUEUE": "notification-events", "AZURE_CLIENT_ID": CLIENT, "SERVICE_BUS_FQDN": "synthetic.invalid", "SERVICE_BUS_QUEUE": "incident-events"}
    return {"properties": {"active": True, "provisioningState": "Provisioned", "healthState": "Healthy", "runningState": "Running", "replicas": 1, "template": {"containers": [{"image": IMAGE, "env": [{"name": k, "value": v} for k, v in env.items()]}]}}}


def test_worker_revision_requires_expected_image_and_persistence(revision):
    REVISION.validate(revision, IMAGE, "worker")
    wrong = copy.deepcopy(revision)
    wrong["properties"]["template"]["containers"][0]["image"] = IMAGE + "bad"
    with pytest.raises(ValueError):
        REVISION.validate(wrong, IMAGE, "worker")


@pytest.mark.parametrize("changes", [{"active": False}, {"healthState": "Unhealthy"}, {"provisioningState": "Failed"}, {"runningState": "Stopped"}, {"replicas": 0}])
def test_old_unhealthy_or_scaled_zero_revision_cannot_pass(revision, changes):
    revision["properties"].update(changes)
    with pytest.raises(ValueError):
        REVISION.validate(revision, IMAGE, "worker")


@pytest.mark.parametrize("missing", ["INCIDENT_STORE", "NOTIFICATION_PUBLISHER", "COSMOS_SUBSCRIPTIONS_CONTAINER", "COSMOS_CONTAINER", "AZURE_CLIENT_ID"])
def test_incomplete_worker_configuration_cannot_pass(revision, missing):
    container = revision["properties"]["template"]["containers"][0]
    container["env"] = [e for e in container["env"] if e["name"] != missing]
    with pytest.raises(ValueError):
        REVISION.validate(revision, IMAGE, "worker")


spec = importlib.util.spec_from_file_location("workflow_validator", Path(__file__).with_name("validate_phase3b2.py"))
WORKFLOW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(WORKFLOW)


@pytest.mark.parametrize("invocation", [
    'curl -X POST "${url}/api/v1/incidents"',
    'curl "https://synthetic.invalid/api/v1/incidents" --request=POST',
    'curl -XPOST --url=https://synthetic.invalid/api/v1/incidents',
    "curl --data '{}' https://synthetic.invalid/api/v1/incidents",
    "curl --json '{}' https://synthetic.invalid/api/v1/incidents",
    'curl --data-binary @payload.json "${url}/api/v1/incidents"',
    'curl --form field=value "${url}/api/v1/incidents"',
    'endpoint="${url}/api/v1/incidents"; curl -d @payload "$endpoint"',
    'curl -X POST "${url}/api/"v1"/incidents"',
    'curl -X POST "${url}/api%2fv1%2fincidents"',
    'curl -X POST ' + chr(92) + chr(10) + ' "${url}/api/v1/incidents"',
])
def test_deployment_guard_rejects_equivalent_incident_posts(invocation):
    with pytest.raises(ValueError, match="Incident endpoint prohibited"):
        WORKFLOW.verify_no_incident_requests({"jobs": {"deploy": {"steps": [{"run": invocation}]}}})


def test_deployment_guard_checks_environment_endpoint():
    workflow = {"env": {"TARGET": "https://synthetic.invalid/api/v1/incidents"},
                "jobs": {"deploy": {"steps": [{"run": 'curl -d @payload "$TARGET"'}]}}}
    with pytest.raises(ValueError):
        WORKFLOW.verify_no_incident_requests(workflow)


def test_deployment_guard_checks_direct_local_script(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "probe.sh").write_text('curl --data "{}" https://synthetic.invalid/api/v1/incidents')
    with pytest.raises(ValueError):
        WORKFLOW.verify_no_incident_requests({"jobs": {"deploy": {"steps": [{"run": "bash scripts/probe.sh"}]}}}, tmp_path)


def test_current_cd_verification_is_non_mutating():
    workflow = WORKFLOW.load(ROOT / ".github/workflows/cd.yml")
    WORKFLOW.verify_no_incident_requests(workflow)
    step = next(s for s in workflow["jobs"]["prepare"]["steps"] if s.get("name") == "Verify the deployment")
    assert '/health' in step['run'] and '/ready' in step['run']
    assert 'bash scripts/verify-anonymous-ingestion.sh' in step['run']


def test_anonymous_probe_exception_rejects_modified_script(tmp_path):
    (tmp_path / "scripts").mkdir()
    original = (ROOT / "scripts/verify-anonymous-ingestion.sh").read_text()
    (tmp_path / "scripts/verify-anonymous-ingestion.sh").write_text(original.replace('"401"', '"202"'))
    workflow = {"jobs": {"prepare": {"steps": [{"run": "bash scripts/verify-anonymous-ingestion.sh"}]}}}
    with pytest.raises(ValueError, match="differs from reviewed"):
        WORKFLOW.verify_no_incident_requests(workflow, tmp_path)

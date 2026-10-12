"""Offline gate regressions; all Azure reads are represented by fixtures."""
import copy
import importlib.util
from pathlib import Path
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("queue_gate", ROOT / "scripts/verify-notification-queue.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def fixture(name=gate.LEGACY):
    principals = {"worker": "11111111-1111-1111-1111-111111111111", "notification": "22222222-2222-2222-2222-222222222222", "scaler": "33333333-3333-3333-3333-333333333333"}
    queue = {"name": name, "id": "/subscriptions/test/resourceGroups/test/providers/Microsoft.ServiceBus/namespaces/test/queues/" + name, "properties": {"requiresDuplicateDetection": True, "duplicateDetectionHistoryTimeWindow": "P1D", "enablePartitioning": False}}
    config = {"SELECTED_NOTIFICATION_QUEUE": name, "QUEUE_SELECTION_APPROVED": "true", "QUEUE_REVIEW_REFERENCE": "review-61"}
    roles = [{"principalId": principals[key], "roleDefinitionId": "/roles/" + role, "scope": queue["id"]} for key, role in [("worker", gate.SENDER), ("notification", gate.RECEIVER), ("scaler", gate.OWNER)]]
    return [config, {"sku": {"tier": "Standard"}, "properties": {"disableLocalAuth": True}}, queue, principals, roles, {"worker": [name], "notification": [name]}, [name]]


def test_legacy_default():
    args = fixture()
    args[0] = {}
    args[2]["properties"] = {}
    assert gate.validate(*args) == gate.LEGACY


def test_explicit_cutover_and_subsequent_run():
    args = fixture("notification-events-dedup-v1")
    assert gate.validate(*args) == args[0]["SELECTED_NOTIFICATION_QUEUE"]
    assert gate.validate(*copy.deepcopy(args)) == args[0]["SELECTED_NOTIFICATION_QUEUE"]


@pytest.mark.parametrize("change", ["blank", "approval", "reference", "midrun", "old_revision", "scaler", "dedup", "window", "partition", "tier", "local_auth", "identity", "role", "scope"])
def test_fail_closed(change):
    args = fixture("notification-events-dedup-v1")
    if change == "blank": args[0] = {}
    if change == "approval": args[0]["QUEUE_SELECTION_APPROVED"] = "false"
    if change == "reference": args[0]["QUEUE_REVIEW_REFERENCE"] = ""
    if change == "midrun": args[0]["EXPECTED_NOTIFICATION_QUEUE"] = gate.LEGACY
    if change == "old_revision": args[5]["worker"].append(gate.LEGACY)
    if change == "scaler": args[6] = [gate.LEGACY]
    if change == "dedup": args[2]["properties"]["requiresDuplicateDetection"] = False
    if change == "window": args[2]["properties"]["duplicateDetectionHistoryTimeWindow"] = "PT10M"
    if change == "partition": args[2]["properties"]["enablePartitioning"] = True
    if change == "tier": args[1]["sku"]["tier"] = "Basic"
    if change == "local_auth": args[1]["properties"]["disableLocalAuth"] = False
    if change == "identity": args[3]["worker"] = "invalid"
    if change == "role": args[4] = args[4][1:]
    if change == "scope": args[4][0]["scope"] = "/namespace"
    with pytest.raises(ValueError): gate.validate(*args)


def test_cd_uses_one_persistent_selection_and_rechecks_before_worker():
    cd = yaml.safe_load((ROOT / ".github/workflows/cd.yml").read_text())
    prepare = cd["jobs"]["prepare"]
    activate = cd["jobs"]["activate-worker"]
    steps = prepare["steps"]
    index = next(i for i, s in enumerate(steps) if s.get("id") == "queue")
    assert index < next(i for i, s in enumerate(steps) if s.get("id") == "notification")
    assert steps[index]["env"]["SELECTED_NOTIFICATION_QUEUE"] == "${{ vars.NOTIFICATION_QUEUE_NAME }}"
    assert prepare["outputs"]["notification_queue"] == "${{ steps.queue.outputs.name }}"
    assert "steps.queue.outputs.name" in next(s["run"] for s in steps if s.get("id") == "notification")
    assert "needs.prepare.outputs.notification_queue" in next(s["run"] for s in activate["steps"] if s.get("id") == "worker")
    check = next(s for s in activate["steps"] if s.get("name") == "Recheck persistent queue selection after production approval")
    assert check["env"]["EXPECTED_NOTIFICATION_QUEUE"] == "${{ needs.prepare.outputs.notification_queue }}"
    source = (ROOT / "scripts/verify-notification-queue.py").read_text()
    assert '"create"' not in source and '"update"' not in source and '"delete"' not in source


def cli_fixture(name=gate.LEGACY, implicit=False):
    args = fixture(name)
    namespace = {"name": "incident-bus", "sku": {"tier": "Standard"}, "disableLocalAuth": True}
    queue = {"name": name, "id": args[2]["id"], **args[2]["properties"]}
    identities = {key: {"id": "/identities/" + key, "principalId": value, "clientId": value} for key, value in args[3].items()}
    config = {**args[0], "RESOURCE_GROUP": "test", "SERVICE_BUS_NAMESPACE": "incident-bus"}
    apps, revisions = {}, {}
    for service, queue_key in [("worker", "NOTIFICATION_QUEUE"), ("notification", "SERVICE_BUS_QUEUE")]:
        image = "registry/" + service + "@sha256:" + "a" * 64
        config["LEGACY_" + service.upper() + "_DEFAULT_IMAGE"] = image
        env = [{"name": "SERVICE_BUS_FQDN", "value": "incident-bus.servicebus.windows.net"}, {"name": "AZURE_CLIENT_ID", "value": identities[service]["clientId"]}]
        if not implicit: env.append({"name": queue_key, "value": name})
        template = {"containers": [{"name": service, "image": image, "env": env}], "scale": {"rules": []}}
        if service == "notification":
            template["scale"]["rules"] = [{"custom": {"type": "azure-servicebus", "identity": identities["scaler"]["id"], "metadata": {"namespace": "incident-bus", "queueName": name}}}]
        apps[service] = {"identity": {"userAssignedIdentities": {record["id"]: {} for record in identities.values()}}, "properties": {"template": template}}
        revisions[service] = [{"name": service + "--one", "properties": {"active": True, "template": copy.deepcopy(template)}}]
    return config, namespace, queue, identities, args[4], apps, revisions


def run_cli(monkeypatch, data):
    import json
    config, namespace, queue, identities, roles, apps, revisions = data
    monkeypatch.setattr(gate.os, "environ", config)
    calls = []
    def output(command, text):
        calls.append(command)
        assert command[0] == "az" and command[-2:] == ["-o", "json"]
        if command[1:4] == ["servicebus", "namespace", "show"]: result = namespace
        elif command[1:4] == ["servicebus", "queue", "show"]: result = queue
        elif command[1:3] == ["identity", "show"]:
            key = command[command.index("-n") + 1].replace("incident-", "").replace("-id", "").replace("notification-scaler", "scaler")
            result = identities[key]
        elif command[1:4] == ["role", "assignment", "list"]: result = roles
        elif command[1:3] == ["containerapp", "show"]: result = apps[command[command.index("-n") + 1].replace("incident-", "")]
        elif command[1:4] == ["containerapp", "revision", "list"]: result = revisions[command[command.index("-n") + 1].replace("incident-", "")]
        else: raise AssertionError(command)
        return json.dumps(result)
    monkeypatch.setattr(gate.subprocess, "check_output", output)
    gate.main()
    return calls


@pytest.mark.parametrize("duration", ["P1D", "PT24H", "PT1440M", "PT86400S", "P0.5DT12H"])
def test_main_flattened_cli_and_semantic_duration(monkeypatch, duration):
    data = cli_fixture("notification-events-dedup-v1")
    data[2]["duplicateDetectionHistoryTimeWindow"] = duration
    run_cli(monkeypatch, data)


def test_main_verified_implicit_legacy_defaults(monkeypatch):
    run_cli(monkeypatch, cli_fixture(implicit=True))


@pytest.mark.parametrize("change", ["implicit_new", "unverified_legacy", "mutable_image", "queue", "fqdn", "client", "scaler_queue", "scaler_namespace", "scaler_identity", "missing_scaler", "missing_fqdn", "missing_client", "secret_queue", "duration", "no_active", "unattached"])
def test_main_rejects_realistic_revision_inconsistencies(monkeypatch, change):
    data = cli_fixture(implicit=change in {"unverified_legacy", "mutable_image"})
    if change == "implicit_new": data = cli_fixture("notification-events-dedup-v1", implicit=True)
    if change == "unverified_legacy": data[0].pop("LEGACY_WORKER_DEFAULT_IMAGE")
    if change == "mutable_image": data[0]["LEGACY_WORKER_DEFAULT_IMAGE"] = "registry/worker:latest"
    old = copy.deepcopy(data[6]["notification"][0])
    data[6]["notification"].append(old)
    template = old["properties"]["template"]
    env = template["containers"][0]["env"]
    keys = {"queue": "SERVICE_BUS_QUEUE", "fqdn": "SERVICE_BUS_FQDN", "client": "AZURE_CLIENT_ID"}
    if change in keys:
        next(e for e in env if e["name"] == keys[change])["value"] = "wrong"
    if change in {"missing_fqdn", "missing_client"}:
        env[:] = [e for e in env if e["name"] != ("SERVICE_BUS_FQDN" if change == "missing_fqdn" else "AZURE_CLIENT_ID")]
    if change == "secret_queue":
        entry = next(e for e in env if e["name"] == "SERVICE_BUS_QUEUE")
        entry.pop("value")
        entry["secretRef"] = "unknown"
    rule = template["scale"]["rules"][0]["custom"]
    if change == "scaler_queue": rule["metadata"]["queueName"] = "other"
    if change == "scaler_namespace": rule["metadata"]["namespace"] = "other"
    if change == "scaler_identity": rule["identity"] = "/other"
    if change == "missing_scaler": template["scale"]["rules"] = []
    if change == "duration":
        data = cli_fixture("notification-events-dedup-v1")
        data[2]["duplicateDetectionHistoryTimeWindow"] = "PT23H"
    if change == "no_active": data[6]["worker"] = []
    if change == "unattached": data[5]["worker"]["identity"]["userAssignedIdentities"] = {}
    with pytest.raises(ValueError): run_cli(monkeypatch, data)


def test_main_worker_overlap_wrong_identity(monkeypatch):
    data = cli_fixture()
    old = copy.deepcopy(data[6]["worker"][0])
    next(e for e in old["properties"]["template"]["containers"][0]["env"] if e["name"] == "AZURE_CLIENT_ID")["value"] = data[3]["notification"]["clientId"]
    data[6]["worker"].append(old)
    with pytest.raises(ValueError): run_cli(monkeypatch, data)

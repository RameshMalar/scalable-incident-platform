"""Offline workflow graph/contracts and Bash syntax checks; never runs a workflow."""
import copy
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import unquote

import yaml

ROOT = Path(__file__).resolve().parents[2]


class WorkflowLoader(yaml.SafeLoader):
    pass


# GitHub uses YAML 1.2 booleans; PyYAML's default YAML 1.1 treats 'on' as True.
WorkflowLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for first, resolvers in WorkflowLoader.yaml_implicit_resolvers.items():
    WorkflowLoader.yaml_implicit_resolvers[first] = [(tag, regex) for tag, regex in resolvers if tag != "tag:yaml.org,2002:bool"]
WorkflowLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.I), list("tTfF"))


def unique_mapping(loader, node, deep=False):
    pairs = loader.construct_pairs(node, deep=deep)
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = value
    return result


WorkflowLoader.add_constructor("tag:yaml.org,2002:map", unique_mapping)


def load(path):
    return yaml.load(path.read_text(encoding="utf-8-sig"), Loader=WorkflowLoader)


def names(job):
    return [s.get("name") for s in job.get("steps", [])]


def verify_graph(workflow):
    jobs = workflow["jobs"]
    def walk(name, visiting):
        if name in visiting:
            raise ValueError("Workflow dependency cycle")
        dependencies = jobs[name].get("needs", [])
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        for dependency in dependencies:
            if dependency not in jobs:
                raise ValueError(f"Unknown dependency {dependency}")
            walk(dependency, visiting | {name})
    for name, job in jobs.items():
        walk(name, set())
        step_ids = [s["id"] for s in job.get("steps", []) if "id" in s]
        assert len(step_ids) == len(set(step_ids))
        for step in job.get("steps", []) + [{"outputs": job.get("outputs", {})}]:
            for reference in re.findall(r"steps\.([\w-]+)\.outputs\.", str(step)):
                assert reference in step_ids, f"Unknown step output {reference} in {name}"


def verify_no_incident_requests(workflow, root=ROOT):
    """Conservative guard: prohibit literal incident endpoints, regardless of method.

    Inspect run blocks, environment values and directly referenced local scripts.
    This is not shell/data-flow analysis: dynamically assembled endpoints, encoded
    payloads, nested helpers and third-party actions require manual review.
    """
    for job_name, job in workflow.get("jobs", {}).items():
        for step in job.get("steps", []):
            source = "\n".join(str(value) for value in (
                workflow.get("env", {}), job.get("env", {}),
                step.get("env", {}), step.get("run", ""),
            ))
            sources = [(f"{job_name}/{step.get('name', 'unnamed')}", source)]
            for relative in set(re.findall(r"scripts/[A-Za-z0-9_./-]+\.(?:sh|py)", step.get("run", ""))):
                path = (root / relative).resolve()
                if not path.is_relative_to(root.resolve()):
                    raise ValueError("Referenced script escapes repository")
                sources.append((relative, path.read_text(encoding="utf-8-sig")))
            for location, text in sources:
                normalized = unquote(text).replace("\\\n", "").replace("'", "").replace('"', "")
                if re.search(r"/api/v1/incidents\b", normalized, re.I):
                    raise ValueError(f"Incident endpoint prohibited in deployment verification: {location}")


def main():
    ci = load(ROOT / ".github/workflows/ci.yml")
    cd = load(ROOT / ".github/workflows/cd.yml")
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = load(path)
        verify_graph(workflow)
        if path.name == "cd.yml" or any("environment" in job for job in workflow["jobs"].values()):
            verify_no_incident_requests(workflow)
    assert set(cd["on"]) == {"workflow_dispatch"}
    inputs = cd["on"]["workflow_dispatch"]["inputs"]
    assert len(inputs) <= 10
    for flag in ["activate_routing", "routing_configuration_approved", "subscription_configuration_approved"]:
        assert inputs[flag]["type"] == "boolean" and inputs[flag]["default"] is False
    assert "workflow_call" in ci["on"]
    assert cd["jobs"]["checks"]["uses"] == "./.github/workflows/ci.yml"
    assert set(cd["jobs"]["prepare"]["needs"]) == {"checks", "preflight"}
    activate = cd["jobs"]["activate-worker"]
    assert set(activate["needs"]) == {"checks", "preflight", "prepare"}
    assert activate["if"] == "inputs.activate_routing && inputs.routing_configuration_approved"
    assert cd["jobs"]["prepare"]["environment"] == activate["environment"] == "production"
    prepare_names = names(cd["jobs"]["prepare"])
    order = ["Deploy the notification service", "Verify notification revision runtime readiness", "Deploy core infrastructure", "Deploy the Subscription API", "Verify Subscription API Cosmos readiness and bearer authorization"]
    assert [prepare_names.index(name) for name in order] == sorted(prepare_names.index(name) for name in order)
    assert "Deploy the processing worker" not in prepare_names
    baseline_step = next(s for s in cd["jobs"]["prepare"]["steps"] if s.get("id") == "baseline")
    assert "actual.lower() != expected.lower()" in baseline_step["run"]
    assert 'test "$actual_location" = "$expected_location"' in baseline_step["run"]
    activation_names = names(activate)
    assert activation_names.index("Recheck protected API and an enabled subscription for the verifier tenant") < activation_names.index("Deploy the processing worker") < activation_names.index("Verify worker revision runtime readiness")
    assert activation_names.index("Verify notification revision runtime readiness") < activation_names.index("Deploy the processing worker")
    preflight_text = str(cd["jobs"]["preflight"])
    assert "required_reviewers" in preflight_text and "prevent_self_review == true" in preflight_text
    assert "secrets." not in str(cd)
    assert "Verify the worker drains the queue" not in str(cd)
    assert "EVT-CD-" not in str(cd) and "Synthetic incident from the CD pipeline" not in str(cd)
    print("PASS: deployment verification contains no literal incident endpoint requests")
    print("PASS: YAML syntax, dependencies, manual-only trigger, default-off approvals and Notification/API/Worker ordering")

    services = ci["jobs"]["test"]["strategy"]["matrix"]["service"]
    assert {"ingestion-service", "worker-service", "notification-service", "load-simulator", "subscription-service"} <= set(services)
    for job in ["build", "security"]:
        assert any(entry["service"] == "subscription-service" for entry in ci["jobs"][job]["strategy"]["matrix"]["include"])
    assert set(ci["jobs"]["gate"]["needs"]) == {"contract", "producer-sdk", "bicep", "test", "integration", "build", "security"}
    assert ci["jobs"]["gate"]["if"] == "always()"
    assert 'all(.[]; .result == "success")' in str(ci["jobs"]["gate"])
    assert "test_phase3.py" in str(ci["jobs"]["integration"])
    assert "test_workflow_safety.py" in str(ci["jobs"]["integration"])
    assert "3.12" in str(ci["jobs"]["integration"])
    original = yaml.load(subprocess.check_output(["git", "show", "HEAD:.github/workflows/ci.yml"], cwd=ROOT), Loader=WorkflowLoader)
    assert ci["jobs"]["contract"] == original["jobs"]["contract"]
    assert ci["jobs"]["producer-sdk"] == original["jobs"]["producer-sdk"]
    assert ci["jobs"]["infra"] == original["jobs"]["infra"]
    for original_step in original["jobs"]["build"]["steps"]:
        if original_step.get("name") != "Build image":
            assert original_step in ci["jobs"]["build"]["steps"], f"Existing image/smoke/scan gate changed: {original_step.get('name')}"
    assert ci["jobs"]["security"]["steps"] == original["jobs"]["security"]["steps"]
    contexts = {entry["service"]: entry["context"] for entry in ci["jobs"]["build"]["strategy"]["matrix"]["include"]}
    for service in ["ingestion-service", "worker-service", "notification-service"]:
        assert contexts[service] == "."
    assert contexts["subscription-service"] == "./subscription-service"
    assert "-f ${{ matrix.service }}/Dockerfile ${{ matrix.context }}" in str(ci["jobs"]["build"])
    assert "build_and_push incident-worker worker-service ." in str(cd)
    assert "build_and_push incident-notification notification-service ." in str(cd)
    assert "build_and_push incident-load-simulator load-simulator ./load-simulator" in str(cd)
    assert "Deploy infrastructure alerts" in prepare_names
    print("PASS: existing test/build/security checks preserved; new service covered; reusable aggregate CI gate required")

    core_outputs = set(re.findall(r"^output (\w+)", (ROOT / "infra/core.bicep").read_text(), re.M))
    for reference in re.findall(r"steps\.core\.outputs\.(\w+)", str(cd)):
        assert reference in core_outputs or reference == "json", reference
    for reference in re.findall(r"fromJSON\(needs\.prepare\.outputs\.core\)\.(\w+)\.value", str(cd)):
        assert reference in core_outputs, reference
    core_run = next(s["run"] for s in cd["jobs"]["prepare"]["steps"] if s.get("id") == "core")
    assert all(name in core_run for name in ["subscriptionIdentityId", "subscriptionIdentityClientId", "cosmosSubscriptionsContainerName"])
    for job in [cd["jobs"]["prepare"], activate]:
        for step in job["steps"]:
            run = step.get("run", "")
            match = re.search(r"--template-file (infra/[\w-]+\.bicep)", run)
            if not match:
                continue
            source = (ROOT / match[1]).read_text()
            declared = set(re.findall(r"^param (\w+)", source, re.M))
            params = run.split("--parameters", 1)[1].split("--query", 1)[0] if "--parameters" in run else ""
            supplied = set(re.findall(r"\b(\w+)=", params))
            assert supplied <= declared, f"Unknown module parameters: {supplied - declared}"
            required = set(re.findall(r"^param (\w+) \w+\s*$", source, re.M))
            assert required <= supplied, f"Missing module parameters: {required - supplied}"
    image_step = next(s for s in cd["jobs"]["prepare"]["steps"] if s.get("id") == "image")
    assert "incident-subscription subscription-service" in image_step["run"]
    assert "--platform linux/amd64" in image_step["run"]
    assert ":latest" not in image_step["run"] and '"containerimage.digest"' in image_step["run"]
    assert "cosmosSubscriptionsContainer=" in str(activate)
    scan_steps = [s for s in cd["jobs"]["prepare"]["steps"] if s.get("name", "").startswith("Scan the exact ")]
    assert len(scan_steps) == 6
    for scan in scan_steps:
        assert scan["with"]["severity"] == "HIGH,CRITICAL"
        assert str(scan["with"]["exit-code"]) == "1"
    assert all(prepare_names.index(s["name"]) < prepare_names.index("Deploy the notification service") for s in scan_steps)
    for readiness_job in [cd["jobs"]["prepare"], activate]:
        for readiness_step in readiness_job["steps"]:
            if readiness_step.get("name", "").endswith("revision runtime readiness"):
                assert "az containerapp logs show" in readiness_step["run"]
                assert "service ready notifier=telegram" in readiness_step["run"] or "worker ready queue=" in readiness_step["run"]
    print("PASS: module required parameters and core output references, subscription image context, amd64 and digest pinning")

    bash = shutil.which("bash")
    # Prefer the known local Git Bash over a Windows WSL shim.
    if Path("C:/Program Files/Git/bin/bash.exe").exists():
        bash = "C:/Program Files/Git/bin/bash.exe"
    if bash is None:
        print("NOT VERIFIED: Bash syntax parser unavailable")
        return
    count = 0
    with tempfile.TemporaryDirectory(prefix="phase3b2-shell-") as directory:
        for workflow in [ci, cd]:
            for job in workflow["jobs"].values():
                for step in job.get("steps", []):
                    if "run" not in step:
                        continue
                    source = re.sub(r"\$\{\{.*?\}\}", "expression_value", step["run"], flags=re.S)
                    script = Path(directory) / f"step-{count}.sh"
                    script.write_text(source, encoding="utf-8", newline="\n")
                    subprocess.run([bash, "-n", script.as_posix()], check=True, capture_output=True)
                    count += 1
    subprocess.run([bash, "-n", (ROOT / "scripts/verify-subscription-api.sh").as_posix()], check=True, capture_output=True)
    print(f"PASS: Bash syntax for {count} CI/CD run blocks and Subscription API verifier; scripts were parsed, not executed")


if __name__ == "__main__":
    main()

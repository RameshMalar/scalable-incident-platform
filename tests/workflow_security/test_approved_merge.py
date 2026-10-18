"""Approved-merge gate regressions; GitHub API responses are fixtures."""
import importlib.util
from pathlib import Path
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("approved_merge", ROOT / "scripts/verify-approved-merge.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

SHA, HEAD, OLD = "a" * 40, "b" * 40, "c" * 40


def pull(**changes):
    record = {"number": 70, "merged_at": "2026-10-13T00:00:00Z", "merge_commit_sha": SHA,
              "base": {"ref": "main"}, "head": {"sha": HEAD}, "user": {"login": "lyt920"}}
    record.update(changes)
    return record


def review(who, state, commit=HEAD):
    return {"user": {"login": who}, "state": state, "commit_id": commit}


def check(reviews, pulls=None):
    return gate.approved_merge(SHA, [pull()] if pulls is None else pulls, lambda number: reviews)


def test_approval_by_another_member_on_final_commit_passes():
    assert check([review("RameshMalar", "APPROVED")]) == (70, ["RameshMalar"])


def test_later_approval_clears_earlier_request_for_changes():
    assert check([review("RameshMalar", "CHANGES_REQUESTED", OLD), review("RameshMalar", "APPROVED")]) == (70, ["RameshMalar"])


def test_comments_do_not_override_an_approval():
    assert check([review("RameshMalar", "APPROVED"), review("RameshMalar", "COMMENTED")])[1] == ["RameshMalar"]


@pytest.mark.parametrize("reviews, reason", [
    ([], "no approval"),
    ([review("lyt920", "APPROVED")], "no approval"),
    ([review("RameshMalar", "APPROVED", OLD)], "final commit"),
    ([review("RameshMalar", "APPROVED"), review("RameshMalar", "DISMISSED")], "no approval"),
    ([review("RameshMalar", "APPROVED"), review("agentyixinliu", "CHANGES_REQUESTED")], "changes requested"),
    ([review("RameshMalar", "COMMENTED")], "no approval"),
    ([{"user": None, "state": "APPROVED", "commit_id": HEAD}], "no approval"),
])
def test_missing_stale_self_or_blocked_approval_is_refused(reviews, reason):
    with pytest.raises(gate.Refused, match=reason):
        check(reviews)


@pytest.mark.parametrize("pulls", [
    [],                                   # pushed straight to main
    [pull(merge_commit_sha="d" * 40)],    # a commit inside a pull request, not its merge
    [pull(merged_at=None)],               # open or closed without merging
    [pull(base={"ref": "feature/x"})],    # merged somewhere other than main
])
def test_commit_that_is_not_an_approved_main_merge_is_refused(pulls):
    with pytest.raises(gate.Refused, match="not the merge result"):
        check([review("RameshMalar", "APPROVED")], pulls)


def test_cd_runs_the_gate_before_any_azure_job_and_without_an_environment():
    cd = yaml.safe_load((ROOT / ".github/workflows/cd.yml").read_text())
    preflight = cd["jobs"]["preflight"]
    assert preflight["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}
    step = next(s for s in preflight["steps"] if "verify-approved-merge.py" in s.get("run", ""))
    assert step["env"]["DEPLOY_SHA"] == "${{ github.sha }}"
    assert "id-token" not in preflight["permissions"]
    for name in ["prepare", "activate-worker"]:
        assert "preflight" in cd["jobs"][name]["needs"]
        # On GitHub Free any branch may name an environment, so Azure federates
        # main only and no deploying job may switch to an environment subject.
        assert "environment" not in cd["jobs"][name]
    assert "environments/production" not in (ROOT / ".github/workflows/cd.yml").read_text()


def test_setup_federates_main_only():
    script = (ROOT / "scripts/setup-github-oidc.sh").read_text()
    calls = [line for line in script.splitlines() if line.startswith("add_federated_credential ")]
    assert calls == ['add_federated_credential "branch-${DEPLOY_BRANCH}" "${qualified}:ref:refs/heads/${DEPLOY_BRANCH}"']


def test_waits_for_github_to_link_a_fresh_merge_to_its_pull_request():
    answers = iter([[], [], [pull()]])
    naps = []
    pulls = gate.pulls_for(SHA, lambda: next(answers), sleep=naps.append)
    assert check([review("RameshMalar", "APPROVED")], pulls) == (70, ["RameshMalar"])
    assert len(naps) == 2


def test_a_direct_push_is_refused_after_waiting():
    naps = []
    pulls = gate.pulls_for(SHA, lambda: [], attempts=3, sleep=naps.append)
    assert len(naps) == 2
    with pytest.raises(gate.Refused, match="not the merge result"):
        check([review("RameshMalar", "APPROVED")], pulls)

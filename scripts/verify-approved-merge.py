"""Fail closed unless the commit CD is about to deploy came through review.

GitHub Free cannot require reviewers on a private repository's environments or
protect its branches, so CD enforces the two-person rule itself. The deployed
commit must be exactly the merge result of a pull request into main that
someone other than its author approved, on that pull request's final commit,
with no reviewer's latest review still requesting changes.

A commit pushed straight to main has no such pull request and is refused, as is
one whose approval predates a later push to the pull request.

Reads the GitHub API through `gh`: authenticated by GH_TOKEN in Actions and by
`gh auth login` locally. Prints pull request numbers and logins, never review
bodies.
"""
import json
import os
import subprocess
import sys
import time

BASE = "main"
DECISIVE = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}


class Refused(Exception):
    pass


def gh_list(path):
    result = subprocess.run(
        ["gh", "api", "--paginate", path, "--jq", ".[]"],
        capture_output=True, text=True, check=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def login(record):
    return (record.get("user") or {}).get("login")


def approved_merge(sha, pulls, reviews_for):
    """Return (pull request number, approvers) for `sha`, or raise Refused."""
    merges = [
        pull for pull in pulls
        if pull.get("merged_at")
        and pull.get("merge_commit_sha") == sha
        and pull.get("base", {}).get("ref") == BASE
    ]
    if not merges:
        raise Refused(f"{sha[:7]} is not the merge result of a pull request into {BASE}")
    pull = merges[0]
    number, author, head = pull["number"], login(pull), pull["head"]["sha"]

    # Reviews arrive oldest first, so each reviewer's last decisive review wins:
    # a later approval clears an earlier request for changes, and a dismissal
    # withdraws an approval. Plain comments decide nothing.
    latest = {}
    for review in reviews_for(number):
        if review.get("state") in DECISIVE and login(review):
            latest[login(review)] = review

    blocking = sorted(name for name, review in latest.items() if review["state"] == "CHANGES_REQUESTED")
    if blocking:
        raise Refused(f"#{number} still has changes requested by {', '.join(blocking)}")

    approvers = sorted(
        name for name, review in latest.items()
        if review["state"] == "APPROVED" and name != author and review.get("commit_id") == head
    )
    if not approvers:
        raise Refused(f"#{number} has no approval from someone other than {author} on its final commit {head[:7]}")
    return number, approvers


def pulls_for(sha, lookup, attempts=7, wait=10, sleep=time.sleep):
    """Pull requests that `sha` belongs to, allowing for GitHub's indexing lag.

    GitHub links a merge commit to its pull request a few seconds after the
    merge, and CD on merge asks straight away. Until the link appears the
    commit looks like a direct push, so wait about a minute before concluding
    that it is one.
    """
    for attempt in range(attempts):
        pulls = lookup()
        if any(pull.get("merge_commit_sha") == sha for pull in pulls) or attempt == attempts - 1:
            return pulls
        sleep(wait)


def main():
    repository, sha = os.environ["REPOSITORY"], os.environ["DEPLOY_SHA"]
    number, approvers = approved_merge(
        sha,
        pulls_for(sha, lambda: gh_list(f"repos/{repository}/commits/{sha}/pulls")),
        lambda n: gh_list(f"repos/{repository}/pulls/{n}/reviews"),
    )
    print(f"{sha[:7]} is the merge of #{number}, approved by {', '.join(approvers)}")


if __name__ == "__main__":
    try:
        main()
    except Refused as error:
        print(f"::error::Refusing to deploy: {error}. Merge through a pull request approved by another team member.", file=sys.stderr)
        sys.exit(1)
    except (KeyError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"::error::Could not establish review status ({type(error).__name__}); refusing to deploy.", file=sys.stderr)
        sys.exit(1)

"""The `ci:merge` request: merge after one full CI run, or withdraw the request.

A person (or an agent the person told to merge) adds the `ci:merge` label to approve the PR's
current head. The consumer's CI then runs the full suite once on that head and calls this
module through the `merge-request` action:

- merge: the full run passed. Merge the PR with the tested head SHA pinned, so GitHub refuses
  if anything was pushed meanwhile. A draft is marked ready first. GitHub can merge and still
  answer 5xx, so after a 5xx or no answer the PR is read again: merged on the tested head is success.
- blocked: the full run failed. Remove the request labels and say why on the PR.
- reset: a new commit arrived. Its approval is gone, so remove the request labels.
- abort: the merge step failed before it reached GitHub (for example the App token could not
  be minted). Remove `ci:merge` and say so, so the request does not wait silently.
- check-base: the stale merge-ref guard. Fail unless the merge commit the run tests has the base
  branch's current tip as its first parent. `blocked` runs the
  same check again when given the merge commit, and withdraws with a "Stale base, re-run" comment.

Free-plan private repositories have no branch protection, so this is the only merge gate:
the bot merges nothing without the label and a passed full run on the exact head.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from typing import Any, Callable

from .github_api import GitHubApi, GitHubApiError

MERGE_LABEL = "ci:merge"
FULL_LABEL = "ci:full"
REQUEST_LABELS = (MERGE_LABEL, FULL_LABEL)
MERGE_METHODS = ("merge", "squash", "rebase")


def label_names(pull: dict[str, Any]) -> set[str]:
    return {label.get("name", "") for label in pull.get("labels") or []}


class PullRequest:
    """One pull request in one repository, through the REST and GraphQL APIs."""

    def __init__(self, api: GitHubApi, repository: str, number: int) -> None:
        self.api, self.repository, self.number = api, repository, number

    def read(self) -> dict[str, Any]:
        return self.api.get(f"/repos/{self.repository}/pulls/{self.number}")

    def comment(self, body: str) -> None:
        self.api.post(f"/repos/{self.repository}/issues/{self.number}/comments", {"body": body})

    def remove_label(self, name: str) -> None:
        quoted = urllib.parse.quote(name, safe="")
        self.api.delete(f"/repos/{self.repository}/issues/{self.number}/labels/{quoted}", allow_missing=True)

    def open_pulls_with_head(self, branch: str) -> list[dict[str, Any]]:
        """Open pull requests from this repository's `branch`: a stack's lower layers."""
        owner = self.repository.split("/")[0]
        head = urllib.parse.quote(f"{owner}:{branch}", safe="")
        return self.api.get(f"/repos/{self.repository}/pulls?state=open&head={head}&per_page=10") or []

    def mark_ready(self, node_id: str) -> None:
        mutation = "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { clientMutationId } }"
        result = self.api.post("/graphql", {"query": mutation, "variables": {"id": node_id}})
        if not isinstance(result, dict) or result.get("errors"):
            raise GitHubApiError(f"Could not mark the pull request ready: {result.get('errors') if isinstance(result, dict) else result}")

    def merge(self, sha: str, method: str) -> dict[str, Any]:
        # GitHub's default commit title and message, as when a person merges in the UI.
        result, _, _ = self.api.request(
            "PUT", f"/repos/{self.repository}/pulls/{self.number}/merge", {"sha": sha, "merge_method": method}
        )
        return result or {}


def merged_anyway(pr: PullRequest, error: GitHubApiError, tested_sha: str, sleep: Callable[[float], None]) -> str:
    """The merge commit when a merge call that failed with a 5xx or no answer landed the tested head anyway, else "".

    GitHub can merge and then answer 502; a 4xx (conflict, protection, moved head) never merged.
    """
    if error.status is not None and error.status < 500:
        return ""
    for attempt in (1, 2, 3):
        sleep(attempt * 5)
        try:
            pull = pr.read()
        except GitHubApiError:
            continue
        if pull.get("merged"):
            # The PUT pinned the tested head, so a merged PR on that head is this merge.
            if (pull.get("head") or {}).get("sha") != tested_sha:
                return ""
            return pull.get("merge_commit_sha") or "unknown"
    return ""


def merge(
    pr: PullRequest, *, tested_sha: str, method: str, run_url: str, sleep: Callable[[float], None] = time.sleep
) -> tuple[int, str]:
    """Merge when the PR still carries the approval for the tested head; never merge another head."""
    pull = pr.read()
    if pull.get("merged"):
        return 0, f"#{pr.number} is already merged."
    if pull.get("state") != "open":
        return 0, f"#{pr.number} is closed; nothing to merge."
    head = (pull.get("head") or {}).get("sha", "")
    if head != tested_sha:
        return 0, f"#{pr.number} moved to {head[:12]} after the full run on {tested_sha[:12]}; not merging."
    if MERGE_LABEL not in label_names(pull):
        return 0, f"#{pr.number} no longer has {MERGE_LABEL}; the approval was withdrawn, so not merging."
    base = pull.get("base") or {}
    base_ref = base.get("ref", "")
    default_branch = (base.get("repo") or {}).get("default_branch", "")
    # A PR into the default branch is never a stack's upper layer, even while a release PR from
    # that branch (`develop` into `main`) is open.
    if base_ref and base_ref != default_branch and pr.open_pulls_with_head(base_ref):
        # A stack's upper layer: GitHub retargets it only when its base branch is deleted, so a
        # merge now would land in the lower layer's branch, which already merged or will.
        pr.remove_label(MERGE_LABEL)
        pr.comment(
            f"Vexel CI did not merge: this PR's base `{base_ref}` is another open PR's branch, so the merge would not reach "
            f"the integration branch. I removed `{MERGE_LABEL}`. Merge the lower PR first, retarget this one to the "
            f"integration branch (`gh pr edit {pr.number} --base <branch>`), and add the label again once its new head is approved."
        )
        return 0, f"#{pr.number} targets another open PR's branch ({base_ref}); not merging."

    if pull.get("draft"):
        try:
            pr.mark_ready(pull["node_id"])
        except GitHubApiError as error:
            return withdraw_failed_merge(pr, error, tested_sha=tested_sha, run_url=run_url)
    late = ""
    try:
        merged_sha = pr.merge(tested_sha, method).get("sha", "")
    except GitHubApiError as error:
        merged_sha = merged_anyway(pr, error, tested_sha, sleep)
        if not merged_sha:
            return withdraw_failed_merge(pr, error, tested_sha=tested_sha, run_url=run_url)
        late = f" (GitHub answered {error.status or 'nothing'} after the merge)"
    try:
        pr.comment(f"Merged by Vexel CI after the [full run]({run_url}) passed on `{tested_sha[:12]}` (merge commit `{merged_sha[:12]}`).")
    except GitHubApiError:
        pass  # The merge happened; a lost comment must not fail the job and trigger `abort`.
    return 0, f"Merged #{pr.number} at {tested_sha[:12]} as {merged_sha[:12]}{late}."


def withdraw_failed_merge(pr: PullRequest, error: GitHubApiError, *, tested_sha: str, run_url: str) -> tuple[int, str]:
    """Withdraw the approval and explain, so a failed merge needs a new label on a fixed head."""
    pr.remove_label(MERGE_LABEL)
    pr.comment(
        f"Vexel CI could not merge `{tested_sha[:12]}` after the [full run]({run_url}) passed: {error}\n\n"
        f"I removed `{MERGE_LABEL}`. Resolve it (for example a conflict with the base), push, and add the label again once the new head is approved."
    )
    return 1, f"Merge of #{pr.number} failed: {error}"


STALE_RETRY = f"Nothing was merged. Add `{MERGE_LABEL}` again to test the pull request once more."


def stale_base(
    api: GitHubApi, repository: str, *, merge_sha: str, base_branch: str, sleep: Callable[[float], None] = time.sleep
) -> str:
    """Why the tested merge commit is not on the base tip, or "" when it is.

    A pull request run tests GitHub's cached merge commit (refs/pull/<n>/merge), made when GitHub
    last refreshed the pull request, not when the label was added. GitHub does not always remake
    it when the base moves, so a run can test a base hours old, and adding the label again tests
    that same commit; the merge itself pins only the head
    SHA. Both SHAs are read fresh, three tries each, and an unreadable answer is stale too (fail
    closed).
    """

    def read(path: str) -> Any:
        for attempt in (1, 2, 3):
            try:
                return api.get(path)
            except GitHubApiError:
                if attempt == 3:
                    raise
                sleep(attempt * 5)

    try:
        base_tip = read(f"/repos/{repository}/git/ref/heads/{urllib.parse.quote(base_branch)}")["object"]["sha"]
    except (GitHubApiError, KeyError, TypeError):
        return f"Could not read the tip of {base_branch} from GitHub, so the run cannot prove it tested the current base. {STALE_RETRY}"
    try:
        first_parent = read(f"/repos/{repository}/git/commits/{merge_sha}")["parents"][0]["sha"]
    except (GitHubApiError, KeyError, IndexError, TypeError):
        return f"Could not read the parents of the tested merge commit {merge_sha} from GitHub. {STALE_RETRY}"
    if first_parent == base_tip:
        return ""
    return (
        f"The tested merge commit {merge_sha} sits on {base_branch} at {first_parent}, but {base_branch} is now at "
        f"{base_tip}, so this run did not test what merging now would produce. Nothing was merged. Close and reopen the "
        f"pull request (GitHub then remakes its merge commit on the current base within seconds; wait until it has), or "
        f"merge {base_branch} into the branch (a new head, which needs its checks again); then add `{MERGE_LABEL}` again. "
        f"Adding the label alone, or re-running this run's jobs, tests the same merge commit again."
    )


def blocked(pr: PullRequest, *, tested_sha: str, run_url: str, stale: str = "") -> tuple[int, str]:
    """The full run failed: withdraw the request so a fixed head needs a new approval.

    `stale` is stale_base()'s answer when the run stopped on a stale merge commit: the comment then
    says to re-run on the current base instead of to fix the change.
    """
    present = [name for name in REQUEST_LABELS if name in label_names(pr.read())]
    for name in present:
        pr.remove_label(name)
    if stale:
        if MERGE_LABEL in present:
            pr.comment(
                f"**Stale base, re-run.** {stale} I removed `{MERGE_LABEL}`. If the run also failed for another "
                f"reason, check its log and fix that first. Run: {run_url}"
            )
        return 0, f"Removed {', '.join(present) or 'no labels'} from #{pr.number}: the tested merge commit was stale."
    if MERGE_LABEL in present:
        pr.comment(
            f"The [full CI run]({run_url}) on `{tested_sha[:12]}` failed, so Vexel CI did not merge.\n\n"
            f"I removed `{MERGE_LABEL}`. Fix it and push (pushes run light CI), then add `{MERGE_LABEL}` again once the new head is approved."
        )
    return 0, f"Removed {', '.join(present) or 'no labels'} from #{pr.number} after the failed full run."


def abort(pr: PullRequest, *, tested_sha: str, run_url: str) -> tuple[int, str]:
    """The merge job failed outside the merge call: withdraw the request and point at the run."""
    pull = pr.read()
    if pull.get("merged") or pull.get("state") != "open":
        return 0, f"#{pr.number} is no longer open; nothing to withdraw."
    if MERGE_LABEL not in label_names(pull):
        return 0, f"#{pr.number} has no {MERGE_LABEL}; nothing to withdraw."
    pr.remove_label(MERGE_LABEL)
    pr.comment(
        f"The full CI run on `{tested_sha[:12]}` passed, but the [merge step]({run_url}) failed before it could merge "
        f"(for example the Vexel CI App token). I removed `{MERGE_LABEL}`; add it again once the merge step is fixed."
    )
    return 0, f"Withdrew {MERGE_LABEL} from #{pr.number} after the merge step failed."


def reset(pr: PullRequest, *, labels: set[str], head_sha: str) -> tuple[int, str]:
    """A new commit withdraws the approval and the one-shot full request."""
    present = [name for name in REQUEST_LABELS if name in labels]
    for name in present:
        pr.remove_label(name)
    if MERGE_LABEL in present:
        pr.comment(
            f"New commit `{head_sha[:12]}`: Vexel CI removed `{MERGE_LABEL}`, because a new head needs a new approval. "
            f"This push runs light CI; add `{MERGE_LABEL}` again to run the full suite once and merge."
        )
    return 0, f"Removed {', '.join(present) or 'no request labels'} from #{pr.number} after a new commit."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m vexel_ci.merge_pr")
    parser.add_argument("action", choices=("merge", "blocked", "reset", "abort", "check-base"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--sha", required=True, help="the head SHA the run tested")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--method", default="merge", choices=MERGE_METHODS)
    parser.add_argument("--labels", default="[]", help="the event's label names as a JSON array (reset)")
    parser.add_argument("--merge-sha", default="", help="the merge commit the run tested (check-base; blocked after a stale base)")
    parser.add_argument("--base-branch", default="", help="the pull request's base branch (with --merge-sha)")
    args = parser.parse_args(argv)
    if bool(args.merge_sha) != bool(args.base_branch) or (args.action == "check-base" and not args.merge_sha):
        parser.error("--merge-sha and --base-branch go together, and check-base needs both")

    api = GitHubApi(os.environ.get("GH_TOKEN", ""))
    pr = PullRequest(api, args.repository, args.pr)
    if args.action == "check-base":
        stale = stale_base(api, args.repository, merge_sha=args.merge_sha, base_branch=args.base_branch)
        code = 1 if stale else 0
        message = stale or f"The tested merge commit {args.merge_sha} sits on the tip of {args.base_branch}."
        title = "Stale base, re-run" if stale else "Merge base"
        print(f"::error title={title}::{message}" if stale else f"::notice::{message}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"### {title}\n\n{message}\n")
        return code
    if args.action == "merge":
        code, message = merge(pr, tested_sha=args.sha, method=args.method, run_url=args.run_url)
    elif args.action == "blocked":
        stale = ""
        if args.merge_sha:
            stale = stale_base(api, args.repository, merge_sha=args.merge_sha, base_branch=args.base_branch)
        code, message = blocked(pr, tested_sha=args.sha, run_url=args.run_url, stale=stale)
    elif args.action == "abort":
        code, message = abort(pr, tested_sha=args.sha, run_url=args.run_url)
    else:
        code, message = reset(pr, labels=set(json.loads(args.labels or "[]")), head_sha=args.sha)
    print(("::error::" if code else "::notice::") + message)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Merge request\n\n{message}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())

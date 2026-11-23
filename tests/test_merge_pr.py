import unittest

from vexel_ci.github_api import GitHubApiError
from vexel_ci.merge_pr import FULL_LABEL, MERGE_LABEL, PullRequest, abort, blocked, merge, reset, stale_base

REPO = "example-org/app"
SHA = "a" * 40


class FakeApi:
    """Records calls; serves one pull request and fails the merge when told to."""

    def __init__(self, pull, merge_error=None, stacked=False, comment_error=None):
        self.pull = pull
        self.merge_error = merge_error
        self.stacked = stacked
        self.comment_error = comment_error
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path))
        if "/pulls?state=open&head=" in path:
            return [{"number": 6}] if self.stacked else []
        return self.pull

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if self.comment_error and path.endswith("/comments"):
            raise self.comment_error
        return {"data": {}} if path == "/graphql" else {}

    def delete(self, path, allow_missing=False):
        self.calls.append(("DELETE", path))

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.merge_error:
            raise self.merge_error
        return {"sha": "b" * 40, "merged": True}, 200, {}

    def methods(self):
        return [call[0] + " " + call[1] for call in self.calls]


def pull(**overrides):
    value = {
        "state": "open",
        "merged": False,
        "draft": False,
        "node_id": "PR_1",
        "head": {"sha": SHA},
        "base": {"ref": "develop", "repo": {"default_branch": "develop"}},
        "labels": [{"name": MERGE_LABEL}],
    }
    value.update(overrides)
    return value


class MergeTest(unittest.TestCase):
    def run_merge(self, api):
        return merge(PullRequest(api, REPO, 7), tested_sha=SHA, method="merge", run_url="https://run")

    def test_merges_the_tested_head_with_the_sha_pinned(self):
        api = FakeApi(pull())
        code, message = self.run_merge(api)
        self.assertEqual(code, 0)
        self.assertIn(("PUT", f"/repos/{REPO}/pulls/7/merge", {"sha": SHA, "merge_method": "merge"}), api.calls)
        self.assertIn("Merged", message)

    def test_marks_a_draft_ready_first(self):
        api = FakeApi(pull(draft=True))
        self.run_merge(api)
        methods = api.methods()
        self.assertLess(methods.index("POST /graphql"), methods.index(f"PUT /repos/{REPO}/pulls/7/merge"))

    def test_never_merges_a_moved_head(self):
        api = FakeApi(pull(head={"sha": "c" * 40}))
        code, message = self.run_merge(api)
        self.assertEqual(code, 0)
        self.assertNotIn(f"PUT /repos/{REPO}/pulls/7/merge", api.methods())
        self.assertIn("moved", message)

    def test_never_merges_without_the_label(self):
        api = FakeApi(pull(labels=[{"name": FULL_LABEL}]))
        self.run_merge(api)
        self.assertNotIn(f"PUT /repos/{REPO}/pulls/7/merge", api.methods())

    def test_skips_closed_and_merged(self):
        for state in (pull(state="closed"), pull(merged=True, state="closed")):
            api = FakeApi(state)
            self.assertEqual(self.run_merge(api)[0], 0)
            self.assertNotIn(f"PUT /repos/{REPO}/pulls/7/merge", api.methods())

    def test_refuses_a_stacked_pr_whose_base_is_another_open_pr(self):
        api = FakeApi(pull(base={"ref": "task/lower-layer", "repo": {"default_branch": "develop"}}), stacked=True)
        code, message = self.run_merge(api)
        self.assertEqual(code, 0)
        self.assertNotIn(f"PUT /repos/{REPO}/pulls/7/merge", api.methods())
        self.assertIn(f"DELETE /repos/{REPO}/issues/7/labels/ci%3Amerge", api.methods())
        self.assertIn("another open PR", message)

    def test_merges_into_the_default_branch_while_a_release_pr_from_it_is_open(self):
        # A release PR from `develop` into `main` is open, so `develop` is an open PR's head.
        api = FakeApi(pull(), stacked=True)
        code, message = self.run_merge(api)
        self.assertEqual(code, 0)
        self.assertIn(("PUT", f"/repos/{REPO}/pulls/7/merge", {"sha": SHA, "merge_method": "merge"}), api.calls)
        self.assertIn("Merged", message)

    def test_a_lost_success_comment_does_not_fail_the_merge(self):
        api = FakeApi(pull(), comment_error=GitHubApiError("502", status=502))
        code, message = self.run_merge(api)
        self.assertEqual(code, 0)
        self.assertIn("Merged", message)

    def test_failed_merge_withdraws_the_label_and_fails(self):
        api = FakeApi(pull(), merge_error=GitHubApiError("405 not mergeable", status=405))
        code, _ = self.run_merge(api)
        self.assertEqual(code, 1)
        self.assertIn(f"DELETE /repos/{REPO}/issues/7/labels/ci%3Amerge", api.methods())
        self.assertIn(f"POST /repos/{REPO}/issues/7/comments", api.methods())


class FailingReadyApi(FakeApi):
    def post(self, path, body):
        if path == "/graphql":
            self.calls.append(("POST", path, body))
            raise GitHubApiError("graphql down", status=502)
        return super().post(path, body)


class MergeFailureTest(unittest.TestCase):
    def test_failed_mark_ready_withdraws_the_label(self):
        api = FailingReadyApi(pull(draft=True))
        code, _ = merge(PullRequest(api, REPO, 7), tested_sha=SHA, method="merge", run_url="https://run")
        self.assertEqual(code, 1)
        self.assertIn(f"DELETE /repos/{REPO}/issues/7/labels/ci%3Amerge", api.methods())
        self.assertNotIn(f"PUT /repos/{REPO}/pulls/7/merge", api.methods())

    def test_abort_withdraws_only_a_present_label(self):
        api = FakeApi(pull())
        abort(PullRequest(api, REPO, 7), tested_sha=SHA, run_url="https://run")
        self.assertIn(f"DELETE /repos/{REPO}/issues/7/labels/ci%3Amerge", api.methods())
        api = FakeApi(pull(labels=[]))
        abort(PullRequest(api, REPO, 7), tested_sha=SHA, run_url="https://run")
        self.assertEqual(api.methods(), [f"GET /repos/{REPO}/pulls/7"])

    def test_abort_leaves_a_merged_pr_alone(self):
        api = FakeApi(pull(merged=True, state="closed"))
        abort(PullRequest(api, REPO, 7), tested_sha=SHA, run_url="https://run")
        self.assertEqual(api.methods(), [f"GET /repos/{REPO}/pulls/7"])


class BlockedAndResetTest(unittest.TestCase):
    def test_blocked_removes_both_labels_and_explains(self):
        api = FakeApi(pull(labels=[{"name": MERGE_LABEL}, {"name": FULL_LABEL}]))
        code, _ = blocked(PullRequest(api, REPO, 7), tested_sha=SHA, run_url="https://run")
        self.assertEqual(code, 0)
        self.assertIn(f"DELETE /repos/{REPO}/issues/7/labels/ci%3Amerge", api.methods())
        self.assertIn(f"DELETE /repos/{REPO}/issues/7/labels/ci%3Afull", api.methods())
        self.assertIn(f"POST /repos/{REPO}/issues/7/comments", api.methods())

    def test_reset_removes_request_labels_on_a_new_commit(self):
        api = FakeApi(pull())
        reset(PullRequest(api, REPO, 7), labels={MERGE_LABEL, "bug"}, head_sha=SHA)
        self.assertEqual(api.methods(), [
            f"DELETE /repos/{REPO}/issues/7/labels/ci%3Amerge",
            f"POST /repos/{REPO}/issues/7/comments",
        ])

    def test_reset_without_request_labels_does_nothing(self):
        api = FakeApi(pull())
        reset(PullRequest(api, REPO, 7), labels={"bug"}, head_sha=SHA)
        self.assertEqual(api.calls, [])


class RefApi:
    """Serves a branch tip and a merge commit's parents; fails the first `failures` reads."""

    def __init__(self, tip, first_parent, failures=0):
        self.tip, self.first_parent, self.failures = tip, first_parent, failures
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if self.failures:
            self.failures -= 1
            raise GitHubApiError("502")
        if "/git/ref/heads/" in path:
            return {"object": {"sha": self.tip}}
        return {"parents": [{"sha": self.first_parent}, {"sha": "h" * 40}]}


class StaleBaseTest(unittest.TestCase):
    def check(self, api):
        return stale_base(api, REPO, merge_sha="m" * 40, base_branch="release/2.0.0-beta", sleep=lambda seconds: None)

    def test_a_merge_commit_on_the_base_tip_is_current(self):
        api = RefApi("b" * 40, "b" * 40)
        self.assertEqual(self.check(api), "")
        self.assertIn(f"/repos/{REPO}/git/ref/heads/release/2.0.0-beta", api.paths)

    def test_a_merge_commit_on_an_older_base_is_stale(self):
        message = self.check(RefApi("b" * 40, "o" * 40))
        self.assertIn("is now at " + "b" * 40, message)
        self.assertIn("Close and reopen", message)

    def test_one_dropped_read_is_retried_and_three_fail_closed(self):
        self.assertEqual(self.check(RefApi("b" * 40, "b" * 40, failures=2)), "")
        self.assertIn("Could not read the tip", self.check(RefApi("b" * 40, "b" * 40, failures=3)))

    def test_blocked_after_a_stale_base_says_to_rerun_instead_of_to_fix(self):
        api = FakeApi(pull(labels=[{"name": MERGE_LABEL}]))
        code, _ = blocked(PullRequest(api, REPO, 7), tested_sha=SHA, run_url="https://run", stale="The base moved.")
        self.assertEqual(code, 0)
        self.assertIn(f"DELETE /repos/{REPO}/issues/7/labels/ci%3Amerge", api.methods())
        comment = next(call[2]["body"] for call in api.calls if call[0] == "POST")
        self.assertTrue(comment.startswith("**Stale base, re-run.** The base moved."))
        # A stale base may hide a real failure too: say so, so nobody relabels a broken head.
        self.assertIn("If the run also failed for another reason, check its log and fix that first.", comment)


if __name__ == "__main__":
    unittest.main()

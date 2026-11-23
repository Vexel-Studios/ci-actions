import unittest

from vexel_ci.github_api import GitHubApiError
from vexel_ci.scope import Event, ScopeSettings, decide, settings_from_env

REPO = "example-org/app"
TREE = "t" * 40
# A consumer's settings: Markdown its builds read, and two flags.
CONSUMER = ScopeSettings(
    code_markdown_globs=("*/content/*.md", "*/corpus/*.md"),
    flags=(("landing", ("bun.lock", "apps/landing/*", "packages/shared/*")), ("knowledge", ("packages/knowledge/*",))),
)
PR_PUSH = Event(name="pull_request", action="synchronize", pr_number="7")


class FakeApi:
    """Serves a pull request's files (paged), a push comparison and the verified artifacts."""

    def __init__(self, files=(), compare=None, artifacts=(), fail=False):
        self.files = list(files)
        self.compare = compare
        self.artifacts = list(artifacts)
        self.fail = fail
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if self.fail:
            raise GitHubApiError("502")
        if "/pulls/" in path:
            page = int(path.rsplit("page=", 1)[1])
            return self.files[(page - 1) * 100: page * 100]
        if "/compare/" in path:
            return {"files": self.compare or []}
        if "/actions/artifacts" in path:
            return {"artifacts": self.artifacts}
        raise AssertionError(path)


def files(*names):
    return [{"filename": name} for name in names]


def scope(event, api, settings=CONSUMER):
    return decide(event, settings, REPO, TREE, lambda: api)


class LightPullRequestTest(unittest.TestCase):
    """What a pull request push's changed files make a light run check ."""

    CASES = [
        # (files, docs_only, affected_only, lint_all, dependencies, test_in_full, flags)
        (["docs/a.md", ".agents/skills/x/SKILL.md", "apps/web/README.md"], True, True, False, False, [], {"landing": False, "knowledge": False}),
        (["packages/knowledge/src/a.ts"], False, True, False, False, [], {"landing": False, "knowledge": True}),
        (["apps/web/i18n/en.json", "apps/web/src/a.ts"], False, True, False, False, ["apps/web"], {"landing": False, "knowledge": False}),
        (["apps/landing/content/legal.md"], False, True, False, False, ["apps/landing"], {"landing": True, "knowledge": False}),
        # A documentation directory wins over the code-Markdown exception, as in the shell script.
        (["docs/content/x.md"], True, True, False, False, [], {"landing": False, "knowledge": False}),
        (["bun.lock"], False, True, False, True, [], {"landing": True, "knowledge": False}),
        (["packages/shared/package.json"], False, True, False, True, [], {"landing": True, "knowledge": False}),
        (["oxlint.config.ts"], False, True, True, False, [], {"landing": False, "knowledge": False}),
        (["tsconfig.base.json"], False, False, False, False, [], {"landing": False, "knowledge": False}),
        (["scripts/build.ts"], False, False, False, False, [], {"landing": False, "knowledge": False}),
        (["packages/benchmarks/x.ts", ".github/workflows/ci.yml"], False, False, False, False, [], {"landing": False, "knowledge": False}),
        (["packages/benchmarks/x.ts", ".github/scripts/x.sh"], False, True, False, False, [], {"landing": False, "knowledge": False}),
    ]

    def test_changed_files_set_the_outputs(self):
        for names, docs_only, affected_only, lint_all, dependencies, test_in_full, flags in self.CASES:
            with self.subTest(names):
                result = scope(PR_PUSH, FakeApi(files(*names)))
                self.assertEqual(result.mode, "light")
                self.assertFalse(result.merge_request)
                self.assertEqual(result.docs_only, docs_only)
                self.assertEqual(result.affected_only, affected_only)
                self.assertEqual(result.lint_all, lint_all)
                self.assertEqual(result.dependencies, dependencies)
                self.assertEqual(result.test_in_full, test_in_full)
                self.assertEqual(result.flags, flags)

    def test_a_renamed_file_counts_under_its_old_name_too(self):
        result = scope(PR_PUSH, FakeApi([{"filename": "docs/landing.md", "previous_filename": "apps/landing/content/x.md"}]))
        self.assertFalse(result.docs_only)
        self.assertTrue(result.flags["landing"])

    def test_files_on_a_second_page_count(self):
        result = scope(PR_PUSH, FakeApi(files(*[f"docs/{i}.md" for i in range(100)], "bun.lock")))
        self.assertTrue(result.dependencies)

    def test_a_pull_request_at_githubs_file_cap_checks_everything(self):
        result = scope(PR_PUSH, FakeApi(files(*[f"docs/{i}.md" for i in range(3000)])))
        self.assertFalse(result.docs_only)
        self.assertFalse(result.affected_only)
        self.assertEqual(result.flags, {"landing": True, "knowledge": True})
        self.assertEqual(result.warning, "")

    def test_unreadable_files_check_everything_with_a_warning_that_says_why(self):
        result = scope(PR_PUSH, FakeApi(fail=True))
        self.assertEqual(result.mode, "light")
        self.assertFalse(result.affected_only)
        self.assertTrue(result.dependencies and result.lint_all)
        self.assertIn("Could not list the changed files (502)", result.warning)

    def test_a_file_name_with_a_line_break_cannot_set_another_output(self):
        # `apps/a\nmode=verified\nx/data.json` would otherwise reach $GITHUB_OUTPUT as three lines.
        result = scope(Event("pull_request", "labeled", "ci:merge", "7"), FakeApi(files("apps/a\nmode=verified\nx/data.json")))
        self.assertEqual((result.mode, result.merge_request), ("full", True))
        self.assertEqual(result.test_in_full, [])
        self.assertIn("line break", result.warning)
        self.assertTrue(all("\n" not in value for _, value in result.outputs()))


class ModeTest(unittest.TestCase):
    def test_ci_merge_runs_full_and_merges_ci_full_only_runs_full(self):
        merge = scope(Event("pull_request", "labeled", "ci:merge", "7"), FakeApi(files("apps/web/a.ts")))
        full = scope(Event("pull_request", "labeled", "ci:full", "7"), FakeApi(files("apps/web/a.ts")))
        self.assertEqual((merge.mode, merge.merge_request), ("full", True))
        self.assertEqual((full.mode, full.merge_request), ("full", False))

    def test_ready_for_review_is_light(self):
        result = scope(Event("pull_request", "ready_for_review", pr_number="7"), FakeApi(files("apps/web/a.ts")))
        self.assertEqual(result.mode, "light")
        self.assertIn("draft marked ready", result.reason)

    def test_pushes_outside_the_integration_branch_and_manual_runs_are_full_without_reading_files(self):
        for event in (Event("push", ref_name="main", push_before="a" * 40), Event("workflow_dispatch", ref_name="develop")):
            with self.subTest(event.name):
                api = FakeApi()
                result = scope(event, api)
                self.assertEqual(result.mode, "full")
                self.assertFalse(result.affected_only)
                self.assertEqual(result.flags, {"landing": True, "knowledge": True})
                self.assertEqual(api.paths, [])

    def test_a_develop_push_whose_tree_passed_a_full_run_is_verified(self):
        own = {"expired": False, "workflow_run": {"id": 99, "head_repository_id": 1, "repository_id": 1}}
        result = scope(Event("push", ref_name="develop", push_before="a" * 40, push_after="b" * 40), FakeApi(artifacts=[own]))
        self.assertEqual(result.mode, "verified")
        self.assertFalse(result.docs_only)
        self.assertIn("99", result.reason)

    def test_a_forks_or_expired_record_does_not_verify_a_push(self):
        fork = {"expired": False, "workflow_run": {"id": 98, "head_repository_id": 2, "repository_id": 1}}
        expired = {"expired": True, "workflow_run": {"id": 97, "head_repository_id": 1, "repository_id": 1}}
        api = FakeApi(compare=files("apps/web/a.ts"), artifacts=[fork, expired])
        result = scope(Event("push", ref_name="develop", push_before="a" * 40, push_after="b" * 40), api)
        self.assertEqual(result.mode, "light")
        self.assertTrue(result.affected_only)

    def test_a_develop_push_without_a_usable_token_runs_light_instead_of_failing(self):
        def no_token():
            raise ValueError("A non-empty GitHub token is required.")

        event = Event("push", ref_name="develop", push_before="a" * 40, push_after="b" * 40)
        result = decide(event, CONSUMER, REPO, TREE, no_token)
        self.assertEqual(result.mode, "light")
        self.assertFalse(result.affected_only)
        self.assertIn("Could not list", result.warning)

    def test_a_develop_push_compares_with_the_previous_commit(self):
        api = FakeApi(compare=files("packages/knowledge/a.ts"))
        result = scope(Event("push", ref_name="develop", push_before="a" * 40, push_after="b" * 40), api)
        self.assertEqual(result.mode, "light")
        self.assertTrue(result.flags["knowledge"])
        self.assertIn(f"/repos/{REPO}/compare/{'a' * 40}...{'b' * 40}", api.paths)

    def test_a_develop_push_without_a_previous_commit_or_with_a_cut_comparison_checks_everything(self):
        cases = {
            "new branch": (Event("push", ref_name="develop", push_before="0" * 40, push_after="b" * 40), FakeApi()),
            "300 files": (Event("push", ref_name="develop", push_before="a" * 40, push_after="b" * 40),
                          FakeApi(compare=files(*[f"docs/{i}.md" for i in range(300)]))),
        }
        for name, (event, api) in cases.items():
            with self.subTest(name):
                result = scope(event, api)
                self.assertEqual(result.mode, "light")
                self.assertFalse(result.affected_only)
                self.assertIn("Could not list", result.warning)

    def test_another_integration_branch_is_configurable(self):
        settings = ScopeSettings(integration_branch="main")
        result = decide(Event("push", ref_name="develop", push_before="a" * 40), settings, REPO, TREE, FakeApi)
        self.assertEqual(result.mode, "full")


class SettingsTest(unittest.TestCase):
    def test_globs_and_flags_come_from_the_action_inputs(self):
        settings = settings_from_env({
            "SCOPE_PACKAGE_GLOBS": "apps/* packages/* tools/*",
            "SCOPE_FLAGS": "landing: apps/landing/* bun.lock\n\nknowledge_postgres: packages/knowledge/*\n",
        })
        self.assertEqual(settings.package_globs, ("apps/*", "packages/*", "tools/*"))
        self.assertEqual(settings.flags, (("landing", ("apps/landing/*", "bun.lock")), ("knowledge_postgres", ("packages/knowledge/*",))))
        self.assertEqual(settings.docs_globs, ScopeSettings().docs_globs)

    def test_a_malformed_flag_is_refused(self):
        for line in ("landing apps/landing/*", "landing:", "land ing: x/*"):
            with self.subTest(line), self.assertRaises(ValueError):
                settings_from_env({"SCOPE_FLAGS": line})


if __name__ == "__main__":
    unittest.main()

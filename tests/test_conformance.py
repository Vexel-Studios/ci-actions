import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vexel_ci.conformance import LocalFiles, check_repository, report
from vexel_ci.standard import LATEST, STANDARDS

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
TOOLS = STANDARDS[LATEST]


def package(**overrides):
    value = {
        "packageManager": f"bun@{TOOLS.bun}",
        "devDependencies": {"turbo": "catalog:", "vite-plus": TOOLS.vite_plus,
                            "@vexel-studios/code-style": f"github:Vexel-Studios/code-style#v{TOOLS.code_style}"},
        "catalog": {"turbo": TOOLS.turbo},
        "scripts": {"lint": "turbo run lint", "test": "turbo run test"},
    }
    value.update(overrides)
    return value


class ConformanceTest(unittest.TestCase):
    """The template the standard hands out, and the drift each rule catches in a copy of it."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / ".github/workflows").mkdir(parents=True)
        shutil.copy(TEMPLATES / "ci.yml", self.root / ".github/workflows/ci.yml")
        shutil.copy(TEMPLATES / "vexel-ci.json", self.root / ".github/vexel-ci.json")
        (self.root / "turbo.json").write_text(json.dumps({"globalEnv": ["CI_RUNTIME"]}))
        self.write("package.json", json.dumps(package()))

    def write(self, path, text):
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text)

    def edit_ci(self, old, new):
        path = self.root / ".github/workflows/ci.yml"
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1))

    def findings(self):
        return check_repository(LocalFiles(self.root))[1]

    def rules(self):
        return {finding.rule for finding in self.findings() if not finding.deviation}

    def test_the_template_conforms(self):
        self.assertEqual(self.findings(), [])

    def test_vp_lint_and_fmt_need_their_sections_in_vite_config(self):
        self.write("package.json", json.dumps(package(scripts={"lint": "turbo run lint", "format:check": "vp fmt --check"})))
        self.assertEqual(self.rules(), {"toolchain.vite-config"})
        self.write("vite.config.ts", "export default defineConfig({ lint: { extends: [oxlint] } })\n")
        self.assertEqual(self.rules(), {"toolchain.vite-config"})
        self.write("vite.config.ts", "export default defineConfig({\n  lint: lintConfig,\n  fmt: fmtConfig,\n})\n")
        self.assertEqual(self.rules(), set())
        for shorthand in ("export default defineConfig({ fmt, lint })\n", "export default defineConfig({\n  lint,\n  fmt\n})\n"):
            with self.subTest(shorthand=shorthand):
                self.write("vite.config.ts", shorthand)
                self.assertEqual(self.rules(), set())

    def test_a_corepack_hash_and_a_node_expression_still_conform(self):
        self.write("package.json", json.dumps(package(packageManager=f"bun@{TOOLS.bun}+sha512.abc")))
        self.edit_ci("    timeout-minutes: 15\n    env:", "    timeout-minutes: 15\n    # node-version: 22\n    env:")
        self.write(".github/workflows/deploy.yml", "jobs:\n  deploy:\n    runs-on: ${{ fromJSON(vars.CI_RUNS_ON) }}\n    steps:\n      - uses: actions/setup-node@v7\n        with:\n          node-version: ${{ env.NODE }}\n")
        self.assertEqual(self.findings(), [])

    def test_the_old_ci_runners_paths_still_conform_while_consumers_move(self):
        path = self.root / ".github/workflows/ci.yml"
        path.write_text(path.read_text().replace("Vexel-Studios/ci-actions/", "Vexel-Studios/ci-runners/.github/actions/"))
        self.assertEqual(self.findings(), [])

    def test_tool_versions_must_be_exact_pins_of_the_standard(self):
        cases = {
            "toolchain.bun": package(packageManager="bun@1.4.4"),
            "toolchain.vite-plus": package(devDependencies={**package()["devDependencies"], "vite-plus": "^1.7.0"}),
            "toolchain.code-style": package(devDependencies={**package()["devDependencies"], "@vexel-studios/code-style": "github:Vexel-Studios/code-style#v1.0.3"}),
            "toolchain.turbo": package(catalog={"turbo": "2.11.7"}),
            "toolchain.task-runner": package(scripts={"test": "vp run -r test"}),
        }
        for rule, value in cases.items():
            with self.subTest(rule):
                self.write("package.json", json.dumps(value))
                self.assertEqual(self.rules(), {rule})

    def test_runner_rules(self):
        scenarios = {
            # One job fixed to a 4 vCPU Blacksmith runner.
            "fixed 4 vCPU": (
                "  tests:\n    needs: preflight\n    if: needs.preflight.outputs.mode == 'full' && needs.preflight.outputs.docs_only != 'true'\n    runs-on: ${{ fromJSON(needs.preflight.outputs.heavy_runs_on || vars.CI_RUNS_ON || '\"ubuntu-24.04\"') }}",
                "  tests:\n    needs: preflight\n    if: needs.preflight.outputs.mode == 'full' && needs.preflight.outputs.docs_only != 'true'\n    runs-on: blacksmith-4vcpu-ubuntu-2404",
                # The tests job leaves the heavy route too, so there is no tests job left.
                {"runners.blacksmith-2vcpu", "runners.no-fixed-blacksmith", "runners.routed", "flow.tests-job"},
            ),
            # The closing job on the router's Blacksmith runner.
            "closing job on the router": (
                "    runs-on: ${{ fromJSON(needs.preflight.outputs.small_runs_on",
                "    runs-on: ${{ fromJSON(vars.CI_ROUTER_RUNS_ON || needs.preflight.outputs.small_runs_on",
                {"cost.router-only"},
            ),
            "a GitHub-hosted job": (
                "    runs-on: ${{ fromJSON(needs.preflight.outputs.light_runs_on || vars.CI_LIGHT_RUNS_ON || vars.CI_RUNS_ON || '\"ubuntu-24.04\"') }}\n    timeout-minutes: 15",
                "    runs-on: ubuntu-latest\n    timeout-minutes: 15",
                {"runners.routed"},
            ),
        }
        for name, (old, new, rules) in scenarios.items():
            with self.subTest(name):
                self.setUp()
                self.edit_ci(old, new)
                self.assertEqual(self.rules(), rules)

    def test_a_merge_in_a_job_of_its_own_is_one_more_billed_job(self):
        self.edit_ci("  close:\n", "  merge:\n    needs: preflight\n    runs-on: ${{ fromJSON(needs.preflight.outputs.light_runs_on) }}\n    steps:\n      - uses: Vexel-Studios/ci-actions/merge-request@v1\n        with:\n          action: merge\n\n  close:\n")
        self.assertEqual(self.rules(), {"cost.closing-job"})

    def test_flow_rules(self):
        scenarios = {
            "flow.stale-base": ("action: check-base", "action: noop"),
            "flow.drafts": ("pull_request.draft", "pull_request.locked"),
            "flow.cache-recheck": ("verify-cache-hit@v1", "other@v1"),
        }
        for rule, (old, new) in scenarios.items():
            with self.subTest(rule):
                self.setUp()
                path = self.root / ".github/workflows/ci.yml"
                path.write_text(path.read_text().replace(old, new))
                self.assertEqual(self.rules(), {rule})

    def test_the_router_must_read_pull_requests_and_the_cache_must_hash_the_runtimes(self):
        self.edit_ci("      actions: read\n      pull-requests: read\n", "      actions: read\n")
        self.assertEqual(self.rules(), {"flow.router-permissions"})
        self.setUp()
        (self.root / "turbo.json").write_text(json.dumps({"globalEnv": ["OTHER"]}))
        self.assertEqual(self.rules(), {"flow.turbo-cache"})
        # A turbo.json with comments is not read, so it is not reported.
        (self.root / "turbo.json").write_text('{\n  // the runtimes\n  "globalEnv": ["CI_RUNTIME"]\n}')
        self.assertEqual(self.rules(), set())
        self.edit_ci("      contents: read\n      actions: read\n      pull-requests: read\n", "      contents: 'read'\n      actions: read  # verified pushes\n      pull-requests: read\n")
        self.assertEqual(self.rules(), set())
        self.edit_ci("    permissions:\n      contents: 'read'\n      actions: read  # verified pushes\n      pull-requests: read\n", "    permissions: read-all\n")
        self.assertEqual(self.rules(), set())

    def test_a_recorded_deviation_is_reported_but_is_no_violation(self):
        manifest = json.loads((TEMPLATES / "vexel-ci.json").read_text())
        manifest["deviations"] = [{"rule": "toolchain.bun", "reason": "Bun 1.4.5 breaks the Workers tests", "decided": "2026-11-23", "by": "the owner"}]
        self.write(".github/vexel-ci.json", json.dumps(manifest))
        self.write("package.json", json.dumps(package(packageManager="bun@1.4.4")))
        findings = self.findings()
        self.assertEqual([(f.rule, f.deviation) for f in findings], [("toolchain.bun", "Bun 1.4.5 breaks the Workers tests")])

    def test_the_tests_job_and_the_light_tests_scope(self):
        self.edit_ci("      - name: Test\n        run: bun x turbo run test\n", "      - name: Lint\n        run: bun x turbo run lint && bun x turbo run test\n")
        self.assertEqual(self.rules(), {"flow.tests-job"})
        self.setUp()
        self.edit_ci('scope=("--filter=[$TURBO_SCM_BASE]")', 'scope=()')
        self.assertEqual(self.rules(), {"flow.light-tests-scope"})

    def test_a_recorded_deviation_that_finds_nothing_is_still_reported(self):
        manifest = json.loads((TEMPLATES / "vexel-ci.json").read_text())
        manifest["deviations"] = [{"rule": "flow.tests-job", "reason": "one test job", "decided": "2026-11-23", "by": "the owner"}]
        self.write(".github/vexel-ci.json", json.dumps(manifest))
        findings = self.findings()
        self.assertEqual([(f.rule, f.deviation, f.triggered) for f in findings], [("flow.tests-job", "one test job", False)])
        self.assertEqual(self.rules(), set())
        text = report("example-org/app", "1", findings)
        self.assertIn("1 recorded but not triggered", text)
        self.assertIn("deviation (recorded, not triggered): one test job", text)

    def test_the_app_key_stays_out_of_jobs_that_run_in_every_run(self):
        # The pre-v1.4.0 template: the merge step inside the closing job, which runs in every run.
        self.edit_ci("    if: ${{ !cancelled() && needs.preflight.outputs.merge_request == 'true' && needs.preflight.outputs.mode == 'full' && needs.close.outputs.passed == 'true' }}\n",
                     "    if: ${{ !cancelled() }}\n")
        self.assertEqual(self.rules(), {"flow.merge-key-scope", "cost.closing-job"})

    def test_an_incomplete_deviation_records_nothing(self):
        manifest = json.loads((TEMPLATES / "vexel-ci.json").read_text())
        manifest["deviations"] = [{"rule": "toolchain.bun", "reason": "", "decided": "2026-11-23", "by": "the owner"},
                                  {"rule": "no.such.rule", "reason": "x", "decided": "2026-11-23", "by": "the owner"}]
        self.write(".github/vexel-ci.json", json.dumps(manifest))
        self.write("package.json", json.dumps(package(packageManager="bun@1.4.4")))
        self.assertEqual(self.rules(), {"manifest", "toolchain.bun"})

    def test_a_native_repository_skips_the_typescript_rules(self):
        manifest = json.loads((TEMPLATES / "vexel-ci.json").read_text())
        manifest["kind"] = "native"
        self.write(".github/vexel-ci.json", json.dumps(manifest))
        (self.root / "package.json").unlink()
        (self.root / "turbo.json").unlink()
        self.edit_ci("      - name: Decide what this run covers\n        id: scope\n        uses: Vexel-Studios/ci-actions/scope@v1",
                     "      - name: Decide what this run covers\n        id: scope\n        run: ./select-mode.sh")
        self.assertEqual(self.rules(), set())


if __name__ == "__main__":
    unittest.main()

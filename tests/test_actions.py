import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vexel_ci.scope import ScopeSettings
from vexel_ci.standard import LATEST, STANDARDS

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = sorted(ROOT.glob("*/action.yml"))


def declared_inputs(text):
    block = re.search(r"^inputs:\n((?:  .*\n|\n)*)", text, re.MULTILINE)
    return set(re.findall(r"^  ([a-z0-9-]+):$", block.group(1), re.MULTILINE)) if block else set()


class ActionFilesTest(unittest.TestCase):
    """Consumers run these files from main: a typo here fails or silently empties every consumer's step."""

    def test_every_input_an_action_reads_is_declared_and_every_module_exists(self):
        self.assertGreaterEqual(len(ACTIONS), 8)
        for path in ACTIONS:
            with self.subTest(path.parent.name):
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("name: "))
                used = set(re.findall(r"inputs\.([a-z0-9-]+)", text))
                self.assertLessEqual(used, declared_inputs(text))
                # Every Python step runs isolated through run.py at this repository's root, one level up.
                self.assertNotRegex(text, r"python3? -m|PYTHONPATH")
                modules = re.findall(r'python3 -I "\$VEXEL_CI_ROOT/run\.py" (vexel_ci\.[a-z_]+)', text)
                self.assertEqual(len(modules), text.count("python3"))
                for module in modules:
                    self.assertIsNotNone(importlib.util.find_spec(module), module)
                if modules:
                    self.assertEqual(set(re.findall(r"VEXEL_CI_ROOT: (.*)$", text, re.MULTILINE)), {"${{ github.action_path }}/.."})
                    self.assertTrue((path.parent / ".." / "run.py").resolve().is_file())
                # Actions these actions use are pinned by commit SHA.
                for used in re.findall(r"^\s+uses: (\S+)", text, re.MULTILINE):
                    self.assertRegex(used, r"@[0-9a-f]{40}$")
                # A plain YAML scalar with ": " or " #" in it does not parse (or loses its tail), and
                # GitHub then fails every consumer's job at "Set up job"; fold such text with >-.
                for value in re.findall(r"^\s+[a-z-]+: (?![>|'\"$])(.*?)(?:\s+#[^#]*)?$", text, re.MULTILINE):
                    self.assertNotRegex(value, r": | #", value)

    def test_run_py_ignores_a_shadowing_module_in_the_working_directory(self):
        # A checkout on the runner (a pull request's, or a previous job's) must not replace the
        # standard library or this package while the step holds a token.
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "json.py").write_text("raise SystemExit('shadowed json')\n")
            Path(directory, "vexel_ci").mkdir()
            Path(directory, "vexel_ci", "__init__.py").write_text("raise SystemExit('shadowed vexel_ci')\n")
            result = subprocess.run(
                [sys.executable, "-I", str(ROOT / "run.py"), "vexel_ci.route"],
                cwd=directory, capture_output=True, text=True, env={"ROUTE_MODE": "local", "PATH": os.environ.get("PATH", ""), "PYTHONPATH": directory},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Routing off", result.stdout)

    def test_setup_installs_the_standards_node(self):
        text = (ROOT / "setup/action.yml").read_text(encoding="utf-8")
        default = re.search(r"  node-version:\n(?:    .*\n)*?    default: '(\d+)'", text).group(1)
        self.assertEqual(default, STANDARDS[LATEST].node_major)

    def test_the_scope_actions_documented_defaults_are_the_modules(self):
        text = (ROOT / "scope/action.yml").read_text(encoding="utf-8")
        documented = dict(re.findall(r"^  ([a-z-]+):\n    description: (?:>-\n      )?.*Empty: (.*)\.$", text, re.MULTILINE))
        defaults = ScopeSettings()
        self.assertEqual(documented, {
            "integration-branch": defaults.integration_branch,
            "package-globs": " ".join(defaults.package_globs),
            "docs-globs": " ".join(defaults.docs_globs),
            "lint-config-globs": " ".join(defaults.lint_config_globs),
            "dependency-globs": " ".join(defaults.dependency_globs),
        })


if __name__ == "__main__":
    unittest.main()

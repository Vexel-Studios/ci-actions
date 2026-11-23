import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vexel_ci.complete import main


class CompleteTest(unittest.TestCase):
    """The closing job's verdict and the verified-tree record, through the action's environment."""

    def run_complete(self, needs, mode="full", tree="t" * 40):
        with tempfile.TemporaryDirectory() as temp:
            output, summary = Path(temp, "output"), Path(temp, "summary")
            env = {
                "NEEDS_JSON": json.dumps({name: {"result": result} for name, result in needs.items()}),
                "MODE": mode, "REASON": "Because", "TREE": tree, "HEAD_SHA": "h" * 40, "EVENT": "pull_request (labeled)",
                "RUNNER_TEMP": temp, "GITHUB_RUN_ID": "41", "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
            }
            with patch.dict(os.environ, env, clear=True):
                code = main()
            outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
            record = Path(temp, "vexel-ci-full-passed", "ci-full-passed.txt")
            return code, outputs, summary.read_text(), record.read_text() if record.exists() else None

    def test_a_job_the_scope_skipped_does_not_fail_the_run(self):
        code, outputs, summary, record = self.run_complete({"preflight": "success", "light": "skipped", "quality": "success"})
        self.assertEqual(code, 0)
        self.assertEqual(outputs["passed"], "true")
        self.assertEqual(outputs["record"], "ci-full-passed-" + "t" * 40)
        self.assertIn("full run 41", record)
        self.assertIn("counts for the merge gate", summary)

    def test_a_failed_job_or_a_skipped_router_fails_the_run_without_a_record(self):
        for needs in ({"preflight": "success", "tests": "failure"}, {"preflight": "skipped", "tests": "skipped"},
                      {"preflight": "success", "tests": "cancelled"}):
            with self.subTest(needs):
                code, outputs, summary, record = self.run_complete(needs)
                # The action never fails itself; the closing job's last step does.
                self.assertEqual(code, 0)
                self.assertEqual(outputs["passed"], "false")
                self.assertEqual(outputs["record"], "")
                self.assertIsNone(record)
                self.assertIn("failed", summary)

    def test_only_a_full_run_records_its_tree(self):
        code, outputs, summary, record = self.run_complete({"preflight": "success", "light": "success"}, mode="light")
        self.assertEqual(outputs["passed"], "true")
        self.assertEqual(outputs["record"], "")
        self.assertIn("Not a merge gate", summary)


if __name__ == "__main__":
    unittest.main()

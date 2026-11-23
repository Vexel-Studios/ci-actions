import json
import tempfile
import unittest
from pathlib import Path

from vexel_ci.cache_check import check


def summary(*tasks):
    return {"tasks": [
        {"package": package, "task": task, "command": command, "cache": {"status": status, "source": source, "timeSaved": 1200}}
        for package, task, status, source, command in tasks
    ]}


class CacheCheckTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.commands = []

    def write(self, name, document):
        path = Path(self.directory.name, name)
        path.write_text(document if isinstance(document, str) else json.dumps(document))
        return path

    def run_check(self, sources, exit_code=0):
        def run(command):
            self.commands.append(command)
            return exit_code

        return check(sources, ["bun", "x", "turbo"], run=run, pick=lambda hits: hits[0])

    def test_reruns_a_remote_hit_with_force(self):
        source = self.write("run.json", summary(
            ("@p/web", "test", "HIT", "REMOTE", "vitest run"),
            ("@p/web", "lint", "HIT", "LOCAL", "oxlint"),
            ("@p/api", "test", "MISS", None, "vitest run"),
            ("@p/none", "test", "HIT", "REMOTE", "<NONEXISTENT>"),
        ))
        code, message, reran = self.run_check([source])
        self.assertEqual((code, reran), (0, True))
        self.assertEqual(self.commands, [["bun", "x", "turbo", "run", "test", "--filter=@p/web", "--force", "--summarize=false"]])

    def test_a_rerun_that_fails_fails_the_check(self):
        source = self.write("run.json", summary(("@p/web", "test", "HIT", "REMOTE", "vitest run")))
        code, message, reran = self.run_check([source], exit_code=1)
        self.assertEqual(code, 1)
        self.assertIn("did not reproduce", message)

    def test_no_remote_hit_runs_nothing(self):
        source = self.write("run.json", summary(("@p/web", "test", "MISS", None, "vitest run")))
        self.assertEqual(self.run_check([source])[0], 0)
        self.assertEqual(self.commands, [])

    def test_an_unreadable_summary_fails_instead_of_passing_as_no_hit(self):
        good = self.write("a.json", summary(("@p/web", "test", "HIT", "REMOTE", "vitest run")))
        bad = self.write("b.json", "{not json")
        code, message, reran = self.run_check([good, bad])
        self.assertEqual(code, 1)
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()

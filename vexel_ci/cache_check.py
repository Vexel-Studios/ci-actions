"""Run one task that was replayed from the remote Turbo cache again, for real.

The `verify-cache-hit` action runs this. A replayed result is only as good as the task's declared inputs: a file a test reads that turbo.json does not name lets a stale pass
through. A rerun that fails shows that, and fails the job.

  python3 -m vexel_ci.cache_check                  the job's own turbo runs: the run summaries in
                                                   .turbo/runs (TURBO_RUN_SUMMARY, set by the
                                                   turbo-cache action)
  python3 -m vexel_ci.cache_check <dry-run.json>   the tasks of a `turbo run <task> --dry=json`
                                                   written to that file, for tasks another job
                                                   replays (the merge gate's one cached test task)

Either way it picks one remote hit with a command at random and reruns it with --force (no cache
read; the fresh result is written back). A dry run reports a hit in the same `cache` shape as a run
summary (status HIT, source REMOTE). Without a remote hit there is nothing to check.
"""

from __future__ import annotations

import json
import os
import random
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Hit:
    package: str
    task: str
    saved_ms: int


def remote_hits(documents: list[dict]) -> list[Hit]:
    hits = set()
    for document in documents:
        for task in document.get("tasks") or []:
            cache = task.get("cache") or {}
            if cache.get("status") == "HIT" and cache.get("source") == "REMOTE" and task.get("command") != "<NONEXISTENT>":
                hits.add(Hit(str(task.get("package", "")), str(task.get("task", "")), int(cache.get("timeSaved") or 0)))
    return sorted(hits, key=lambda hit: (hit.package, hit.task))


def read_sources(paths: list[Path]) -> list[dict]:
    """Every file must parse: an unreadable one would otherwise look like "no remote hit" and pass."""
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def check(
    sources: list[Path], turbo: list[str], *, run: Callable[[list[str]], int], pick: Callable[[list[Hit]], Hit]
) -> tuple[int, str, bool]:
    """Exit code, message, and whether a replayed task ran again and passed (for the job summary)."""
    try:
        hits = remote_hits(read_sources(sources))
    except (OSError, ValueError) as error:
        return 1, f"::error title=Turbo cache check::Could not read {' '.join(map(str, sources))} as JSON: {error}", False
    if not hits:
        return 0, "No task was replayed from the remote cache: nothing to check.", False
    hit = pick(hits)
    print(f"{len(hits)} task(s) were replayed from the remote cache. Running {hit.package}#{hit.task} again (it saved {hit.saved_ms} ms).")
    if run([*turbo, "run", hit.task, f"--filter={hit.package}", "--force", "--summarize=false"]) != 0:
        return 1, (
            f"::error title=A cached result did not reproduce::{hit.package}#{hit.task} passed from the remote cache but fails "
            "when it runs. Its inputs in turbo.json probably miss a file it reads; fix them so the cache key changes with that file."
        ), False
    return 0, f"{len(hits)} task(s) replayed from the remote cache; `{hit.package}#{hit.task}` ran again and passed.", True


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        dry_run = Path(args[0])
        if not dry_run.is_file() or dry_run.stat().st_size == 0:
            print(f"No turbo dry run in {dry_run}: nothing to check.")
            return 0
        sources = [dry_run]
    else:
        sources = sorted(Path(".turbo/runs").glob("*.json"))
        if not sources:
            print("No turbo run summaries in .turbo/runs: nothing to check.")
            return 0
    turbo = shlex.split(os.environ.get("TURBO_COMMAND") or "bun x turbo")
    code, message, reran = check(sources, turbo, run=lambda command: subprocess.run(command).returncode, pick=random.choice)
    print(message)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and reran:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Turbo cache check\n\n{message}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())

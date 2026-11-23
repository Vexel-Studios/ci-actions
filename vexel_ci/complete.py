"""Sum up one CI run as one result: the `complete` action.

Every job the closing job needs must have passed, except jobs the run's scope skipped once the router job passed. It writes
the run's summary, and for a passed full run the record whose artifact name
(`ci-full-passed-<tree>`) makes a later push of the same tree to the integration branch
`verified` (scope.py). It never fails itself: its `passed` output decides the closing job's merge
or blocked step, and the job's last step fails the run.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .merge_pr import MERGE_LABEL
from .standard import VERIFIED_ARTIFACT_PREFIX


@dataclass(frozen=True)
class Result:
    passed: bool
    failed: list[tuple[str, str]]
    ran: list[str]
    skipped: list[str]


def judge(needs: dict[str, Any], router_job: str) -> Result:
    """A skipped job is one the scope left out, unless the router itself did not succeed."""
    results = {name: str((job or {}).get("result", "")) for name, job in needs.items()}
    failed = [
        (name, result) for name, result in results.items()
        if result != "success" and (name == router_job or result != "skipped")
    ]
    ran = [name for name, result in results.items() if result != "skipped"]
    skipped = [name for name, result in results.items() if result == "skipped"]
    return Result(not failed, failed, ran, skipped)


def summary(result: Result, *, mode: str, reason: str, sha: str, event: str, stale_base: bool) -> str:
    if mode == "full":
        verdict = (
            "A successful full run: it counts for the merge gate of this exact head." if result.passed
            else "A failed full run: it does not count for the merge gate."
        )
    elif mode == "verified":
        verdict = "Nothing ran again: this exact tree passed the full run named above."
    else:
        verdict = (
            f"Not a merge gate: a merge needs a successful full run on its exact head. "
            f"Add the `{MERGE_LABEL}` label to run it once and merge."
        )
    lines = [f"## CI {mode} run {'passed' if result.passed else 'failed'} on {sha}", "", f"{reason}. Event: {event}.", ""]
    if stale_base:
        lines += [
            "**Stale base, re-run:** the router stopped the run before the heavy jobs, because the tested merge "
            "commit is not on the base tip (its summary has the SHAs).", "",
        ]
    lines += [f"Ran: {', '.join(result.ran)}. Skipped: {', '.join(result.skipped) or 'none'}.", "", verdict, ""]
    return "\n".join(lines)


def main() -> int:
    env = os.environ
    mode = env.get("MODE") or "unknown"
    reason = env.get("REASON") or "the router job did not finish"
    sha = env.get("HEAD_SHA", "")
    tree = env.get("TREE", "")
    result = judge(json.loads(env.get("NEEDS_JSON") or "{}"), env.get("ROUTER_JOB") or "preflight")

    record = ""
    if result.passed and mode == "full" and tree:
        directory = Path(env.get("RUNNER_TEMP", ".")) / "vexel-ci-full-passed"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ci-full-passed.txt").write_text(
            f"Tree {tree} passed full run {env.get('GITHUB_RUN_ID', '')} (head {sha}).\n", encoding="utf-8"
        )
        record = f"{VERIFIED_ARTIFACT_PREFIX}{tree}"

    output = env.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"mode={mode}\npassed={'true' if result.passed else 'false'}\nrecord={record}\n")
            handle.write(f"record_path={Path(env.get('RUNNER_TEMP', '.')) / 'vexel-ci-full-passed'}\n")
    step_summary = env.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(summary(
                result, mode=mode, reason=reason, sha=sha, event=env.get("EVENT", ""),
                stale_base=env.get("STALE_BASE") == "true",
            ))
    print(f"::notice title=CI scope::{mode} ({reason})")
    if not result.passed:
        for name, job_result in result.failed:
            print(f"::error title=CI stage {name}::{name}: {job_result}")
        return 0
    if result.skipped:
        print(f"Skipped (outside this run's scope): {', '.join(result.skipped)}")
    print(f"All CI stages passed ({mode} run).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

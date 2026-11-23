"""Choose where one CI run's jobs go: the consumer's self-hosted pool or Blacksmith overflow.

The `route` action runs this in a consumer's router job. The pool comes from the action's `pool`
input (JSON, see Pool): the runner group to read, the labels that make a runner part of the pool,
and the heavy and light `runs-on` values. With that repository's own GitHub App token it reads the
group's runners (online and busy, whichever repository they work for) and its own waiting jobs,
then writes two `runs-on` values as JSON:

- heavy: always the pool, even while it is offline or backed up: heavy test jobs time out on
  Blacksmith's 2 vCPU runner, so an overflowed full run failed and its minutes were wasted. Only
  forced `overflow` mode sends heavy jobs to Blacksmith.
- light: the pool, or Blacksmith when no pool runner is online or its backlog would make a new
  job wait longer than the overflow limit.

Overflow is capped per repository and UTC day (the pool's `blacksmith_daily_minutes`, or the
action's input): once the repository's Blacksmith minutes reach its share, overflowing jobs wait
for the pool instead.

A failure to read GitHub, or no pool, never fails the run: the outputs are then empty, so the
consumer's jobs use its CI_RUNS_ON / CI_LIGHT_RUNS_ON variables. Forced `local` mode does the same.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .github_api import GitHubApi

# Both overflow routes use Blacksmith's smallest runner, so the monthly free minutes last longest.
# Heavy jobs take theirs only in forced `overflow` mode.
HEAVY_OVERFLOW = "blacksmith-2vcpu-ubuntu-2404"
LIGHT_OVERFLOW = "blacksmith-2vcpu-ubuntu-2404"
# A Blacksmith runs-on label and its vCPU count, which sets its price per minute.
BLACKSMITH_LABEL = re.compile(r"^blacksmith-(\d+)vcpu-")
# Parallel job reads when counting a day's Blacksmith use: a busy day has over a hundred runs, and
# one read at a time outlasts a consumer's two-minute route step.
BUDGET_READERS = 8
RETRY_SECONDS = 2.0

# Job states that still wait for a runner. Jobs that wait on `needs` are not listed by GitHub
# until they are queued, so the backlog is a lower bound.
WAITING_JOB_STATES = frozenset({"queued", "pending", "requested"})
ACTIVE_RUN_STATES = ("queued", "in_progress")


@dataclass(frozen=True)
class Pool:
    """A consumer's self-hosted pool, from the route action's `pool` input.

    `{"group_id": 123, "runner_labels": [...], "heavy": [...], "light": [...], "blacksmith_daily_minutes": 60}`:
    a runner of the group that carries every one of `runner_labels` is part of the pool; `heavy` and
    `light` are the runs-on values of the two kinds of job, each with its route label first; the
    budget is in 2 vCPU minutes per UTC day (0 or absent: never overflow).
    """

    group_id: int
    runner_labels: frozenset[str]
    heavy: list[str]
    light: list[str]
    blacksmith_daily_minutes: float = 0.0

    def waits_for_it(self, labels: set[str]) -> bool:
        """A heavy job, or a light job that names nothing outside the light route, runs only here."""
        return self.heavy[0] in labels or (self.light[0] in labels and labels <= set(self.light))


def pool_from_json(text: str) -> Pool | None:
    """The pool, or None for an empty input; anything malformed raises, so routing falls back."""
    if not text.strip():
        return None
    value = json.loads(text)

    def label_list(name: str) -> list[str]:
        labels = value.get(name)
        if not isinstance(labels, list) or not labels or not all(isinstance(label, str) and label for label in labels):
            raise ValueError(f"pool.{name} must be a non-empty list of labels")
        return labels

    group_id = value.get("group_id") if isinstance(value, dict) else None
    if not isinstance(group_id, int) or isinstance(group_id, bool) or group_id <= 0:
        raise ValueError("pool.group_id must be a runner group ID")
    budget = value.get("blacksmith_daily_minutes", 0)
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not math.isfinite(budget) or budget < 0:
        raise ValueError("pool.blacksmith_daily_minutes must be a number of minutes")
    return Pool(value["group_id"], frozenset(label_list("runner_labels")), label_list("heavy"), label_list("light"), float(budget))


@dataclass(frozen=True)
class RouteSettings:
    """Tunable limits; the defaults come from measured job durations on the pool."""

    mode: str = "auto"
    pool: Pool | None = None
    overflow_after_minutes: float = 20.0
    # Light jobs get the same estimate: on the pool they queue behind the heavy work.
    heavy_job_minutes: float = 8.0
    heavy_overflow: Any = HEAVY_OVERFLOW
    light_overflow: Any = LIGHT_OVERFLOW
    # 2 vCPU minutes a day before overflow stops; None means no cap.
    blacksmith_daily_minutes: float | None = None
    # 2 vCPU minutes held back for runs whose cost is not known yet: this run's light jobs, and
    # each other run that still has a Blacksmith job unfinished (light jobs too, since heavy jobs
    # no longer overflow).
    light_reserve_minutes: float = 30.0
    active_run_reserve_minutes: float = 30.0


@dataclass
class PoolState:
    """What one pool can take now: online and idle runners, and jobs already waiting for it."""

    online: int = 0
    idle: int = 0
    waiting: int = 0

    def wait_minutes(self, job_minutes: float) -> float:
        """Estimated minutes before a new job starts, assuming waiting jobs run first."""
        if self.online == 0:
            return float("inf")
        if self.idle > self.waiting:
            return 0.0
        # The new job is behind every waiting job that no idle runner takes.
        return (self.waiting - self.idle + 1) * job_minutes / self.online


@dataclass
class Snapshot:
    pool: PoolState = field(default_factory=PoolState)


@dataclass(frozen=True)
class Route:
    """A run's targets; None leaves the choice to the consumer's runner variables."""

    heavy: Any
    light: Any
    reason: str


def label_set(item: dict[str, Any]) -> set[str]:
    """Labels of a runner (objects) or a job (strings)."""
    labels = item.get("labels") or []
    return {label["name"] if isinstance(label, dict) else str(label) for label in labels}


def read_snapshot(
    api: GitHubApi, pool: Pool, *, org: str, repositories: tuple[str, ...], current_run_id: str = ""
) -> tuple[Snapshot, list[str]]:
    """Read the pool's runners and the given repositories' waiting jobs for it.

    Each consumer's App is installed on that repository only, so a consumer passes its own
    repository; busy runners still show the other repositories' work. A repository whose jobs
    cannot be read is skipped and named in the returned notes.
    """
    snapshot = Snapshot()
    runners = api.paginated(f"/orgs/{org}/actions/runner-groups/{pool.group_id}/runners", "runners")
    for runner in runners:
        if not pool.runner_labels <= label_set(runner):
            # Another host, or quarantined: it takes no routed job.
            continue
        if runner.get("status") == "online":
            snapshot.pool.online += 1
            if runner.get("busy") is False:
                snapshot.pool.idle += 1

    notes: list[str] = []
    for repository in repositories:
        try:
            jobs = waiting_jobs(api, repository, current_run_id)
        except Exception as error:
            notes.append(f"{repository} queue unreadable ({error})")
            continue
        snapshot.pool.waiting += sum(1 for job in jobs if pool.waits_for_it(label_set(job)))
    return snapshot, notes


def waiting_jobs(api: GitHubApi, repository: str, current_run_id: str) -> list[dict[str, Any]]:
    """Jobs of the repository's unfinished runs that still wait for a runner."""
    run_ids: set[int] = set()
    for state in ACTIVE_RUN_STATES:
        # One page is enough to judge a backlog; a queue longer than 100 runs overflows anyway.
        page = api.get(f"/repos/{repository}/actions/runs?status={state}&per_page=100")
        for run in (page or {}).get("workflow_runs", []):
            if str(run.get("id")) != str(current_run_id):
                run_ids.add(run["id"])
    waiting = []
    for run_id in sorted(run_ids):
        jobs = api.get(f"/repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
        waiting.extend(job for job in (jobs or {}).get("jobs", []) if job.get("status") in WAITING_JOB_STATES)
    return waiting


def decide(snapshot: Snapshot, settings: RouteSettings) -> Route:
    """Pick the heavy and light targets for one run from a snapshot."""
    limit = settings.overflow_after_minutes
    if settings.mode == "local":
        return Route(None, None, "Routing off (VEXEL_CI_ROUTE=local): the runner variables decide")
    if settings.mode == "overflow":
        return Route(settings.heavy_overflow, settings.light_overflow,
                     "Routing forced to Blacksmith (VEXEL_CI_ROUTE=overflow)")
    pool = settings.pool
    if pool is None:
        return Route(None, None, "No pool configured (the route action's pool input): the runner variables decide")

    # Heavy jobs always wait for the pool: on Blacksmith's 2 vCPU runner heavy tests time out.
    state = snapshot.pool
    # Light jobs queue behind the heavy work on the pool, so they get the heavy job estimate.
    wait = state.wait_minutes(settings.heavy_job_minutes)
    described = f"{state.online} online, {state.idle} idle, {state.waiting} waiting"
    if state.online == 0:
        light, light_reason = settings.light_overflow, "light: Blacksmith, no pool runner is online"
    elif wait > limit:
        light, light_reason = settings.light_overflow, (
            f"light: Blacksmith, the pool's backlog ({described}) is about {wait:.0f} min, over {limit:.0f}"
        )
    else:
        light, light_reason = pool.light, f"light: pool ({described}, about {wait:.0f} min)"
    return Route(pool.heavy, light, f"heavy: pool; {light_reason}")


@dataclass(frozen=True)
class BlacksmithUse:
    """A repository's Blacksmith use today: minutes spent and other runs still using it."""

    minutes: float
    active_runs: int


def blacksmith_use_today(api: GitHubApi, repository: str, now: datetime, current_run_id: str = "") -> BlacksmithUse:
    """The repository's Blacksmith use since midnight UTC, in 2 vCPU minutes.

    Counts every attempt of every job that got a runner in the runs created today, a running job
    up to now, and each job rounded up to a whole minute (the cautious reading of per-minute
    billing). A run other than the current one with an unfinished Blacksmith job is active: its
    remaining minutes are not known yet. The current run's earlier attempts still count.
    """
    day = now.astimezone(timezone.utc).date().isoformat()
    runs = read_with_retry(api, f"/repos/{repository}/actions/runs?created=>={day}", "workflow_runs")

    def run_use(run: dict[str, Any]) -> tuple[float, bool]:
        minutes, active = 0.0, False
        for job in read_with_retry(api, f"/repos/{repository}/actions/runs/{run['id']}/jobs?filter=all", "jobs"):
            matches = [BLACKSMITH_LABEL.match(label) for label in label_set(job)]
            vcpus = next((int(match.group(1)) for match in matches if match), 0)
            if not vcpus:
                continue
            active = active or (job.get("status") != "completed" and str(run.get("id")) != str(current_run_id))
            # A queued job already has started_at; only a job a runner took is billed.
            if not job.get("runner_name") or not job.get("started_at"):
                continue
            started = parse_time(job["started_at"])
            ended = parse_time(job["completed_at"]) if job.get("completed_at") else now
            minutes += math.ceil(max(0.0, (ended - started).total_seconds()) / 60) * vcpus / 2
        return minutes, active

    with ThreadPoolExecutor(max_workers=BUDGET_READERS) as pool:
        uses = list(pool.map(run_use, runs))
    return BlacksmithUse(sum(minutes for minutes, _ in uses), sum(1 for _, active in uses if active))


def read_with_retry(api: GitHubApi, path: str, key: str) -> list[dict[str, Any]]:
    """One paginated read, tried twice: a busy day takes about a hundred, and one 502 would void the count."""
    try:
        return api.paginated(path, key)
    except Exception:
        time.sleep(RETRY_SECONDS)
        return api.paginated(path, key)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def keep_local(route: Route, why: str, light: list[str]) -> Route:
    """The route with its light jobs back on the pool, which they wait for."""
    return Route(route.heavy, light, f"{route.reason}; {why}, so the light jobs wait for the pool instead")


def apply_budget(
    api: GitHubApi, repository: str, route: Route, settings: RouteSettings, now: datetime, current_run_id: str = ""
) -> Route:
    """Keep an overflowing route only while the repository's Blacksmith share has room for it.

    Only light jobs overflow in `auto` mode. The minutes spent today plus a reservation for this
    run and for each other run still on Blacksmith must fit the share, so runs that start together
    cannot each see the same low count and overflow at once. Use that cannot be read counts as
    over budget: spending is the costlier mistake.
    """
    budget = settings.blacksmith_daily_minutes
    if budget is None or settings.pool is None or route.light != settings.light_overflow:
        return route
    light = settings.pool.light
    if budget == 0:
        return keep_local(route, "no Blacksmith share", light)
    try:
        use = blacksmith_use_today(api, repository, now, current_run_id)
    except Exception as error:
        return keep_local(route, f"Blacksmith use today is unreadable ({error})", light)
    reserved = settings.light_reserve_minutes + settings.active_run_reserve_minutes * use.active_runs
    state = (
        f"{use.minutes:.0f} of {budget:.0f} Blacksmith min used today (UTC), "
        f"{reserved:.0f} reserved for this run and {use.active_runs} other active"
    )
    if use.minutes + reserved <= budget:
        return Route(route.heavy, route.light, f"{route.reason}; {state}")
    return keep_local(route, f"over budget: {state}", light)


def fallback(error: Exception) -> Route:
    """The pre-routing behaviour: the consumer's runner variables decide."""
    return Route(None, None, f"Routing failed, so the runner variables decide: {error}")


def settings_from_env(env: dict[str, str]) -> RouteSettings:
    def number(name: str, default: float) -> float:
        value = env.get(name, "").strip()
        return float(value) if value else default

    def labels(name: str, default: Any) -> Any:
        value = env.get(name, "").strip()
        return json.loads(value) if value else default

    def minutes(name: str, default: float) -> float:
        # A bad figure fails into the fallback (the local runner variables), never into no cap.
        value = number(name, default)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a number of minutes, not {value!r}")
        return value

    mode = (env.get("ROUTE_MODE", "") or "auto").strip().lower()
    if mode not in {"auto", "local", "overflow"}:
        raise ValueError(f"ROUTE_MODE must be auto, local or overflow, not {mode!r}")
    if mode != "auto":
        # Forced modes never read the pool or the budget.
        return RouteSettings(mode=mode, heavy_overflow=labels("HEAVY_OVERFLOW", HEAVY_OVERFLOW),
                             light_overflow=labels("LIGHT_OVERFLOW", LIGHT_OVERFLOW))
    pool = pool_from_json(env.get("VEXEL_CI_POOL", ""))
    return RouteSettings(
        mode=mode,
        pool=pool,
        overflow_after_minutes=number("OVERFLOW_AFTER_MINUTES", RouteSettings.overflow_after_minutes),
        heavy_job_minutes=number("HEAVY_JOB_MINUTES", RouteSettings.heavy_job_minutes),
        heavy_overflow=labels("HEAVY_OVERFLOW", HEAVY_OVERFLOW),
        light_overflow=labels("LIGHT_OVERFLOW", LIGHT_OVERFLOW),
        # Without a share the repository never overflows.
        blacksmith_daily_minutes=minutes("BLACKSMITH_DAILY_MINUTES", pool.blacksmith_daily_minutes if pool else 0),
        light_reserve_minutes=minutes("LIGHT_RESERVE_MINUTES", RouteSettings.light_reserve_minutes),
        active_run_reserve_minutes=minutes("ACTIVE_RUN_RESERVE_MINUTES", RouteSettings.active_run_reserve_minutes),
    )


def route_from_env(
    env: dict[str, str], api_factory: Callable[[str], GitHubApi] = GitHubApi, now: datetime | None = None
) -> Route:
    """Decide a route; any error reading settings or GitHub gives the local fallback."""
    try:
        settings = settings_from_env(env)
        if settings.mode != "auto" or settings.pool is None:
            return decide(Snapshot(), settings)
        token = env.get("GH_TOKEN", "").strip()
        if not token:
            raise RuntimeError("no GitHub App token (the action's client-id and private-key)")
        own = env.get("GITHUB_REPOSITORY", "").strip()
        if "/" not in own:
            raise RuntimeError("GITHUB_REPOSITORY is not set")
        api = api_factory(token)
        snapshot, notes = read_snapshot(
            api, settings.pool, org=own.split("/")[0], repositories=(own,), current_run_id=env.get("GITHUB_RUN_ID", "")
        )
        route = apply_budget(api, own, decide(snapshot, settings), settings, now or datetime.now(timezone.utc),
                             env.get("GITHUB_RUN_ID", ""))
        if notes:
            route = Route(route.heavy, route.light, f"{route.reason}; {'; '.join(notes)}")
        return route
    except Exception as error:  # Routing must never block CI.
        return fallback(error)


def main() -> int:
    route = route_from_env(dict(os.environ))
    # Empty when routing is off or failed: the consumer's jobs then read its runner variables.
    heavy = "" if route.heavy is None else json.dumps(route.heavy)
    light = "" if route.light is None else json.dumps(route.light)
    # GITHUB_OUTPUT takes one line per value; an API error message may span several.
    reason = " ".join(route.reason.split())
    lines = [f"heavy_runs_on={heavy}", f"light_runs_on={light}", f"reason={reason}"]
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                f"### Runner route\n\nHeavy jobs: `{heavy or 'CI_RUNS_ON'}`. Light jobs: `{light or 'CI_LIGHT_RUNS_ON'}`.\n\n{reason}.\n"
            )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

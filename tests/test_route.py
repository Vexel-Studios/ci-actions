import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from vexel_ci.github_api import GitHubApiError
from vexel_ci.route import (
    HEAVY_OVERFLOW,
    LIGHT_OVERFLOW,
    Pool,
    PoolState,
    Route,
    RouteSettings,
    Snapshot,
    BlacksmithUse,
    apply_budget,
    blacksmith_use_today,
    decide,
    main,
    pool_from_json,
    read_snapshot,
    route_from_env,
    settings_from_env,
    with_small,
)

# A consumer's pool as its route action's `pool` input describes it.
ORG = "example-org"
GROUP_ID = 42
HOST, ARCH, LIGHT_LABEL, HEAVY_LABEL = "pool/host", "pool-x64", "pool-light", "pool-heavy"
OTHER_HOST, OTHER_ARCH, QUARANTINE_LABEL = "other/host", "pool-arm64", "pool-quarantine"
HEAVY_LOCAL = [HEAVY_LABEL, ARCH]
LIGHT_LOCAL = [LIGHT_LABEL, ARCH]
POOL_FIELDS = {"group_id": GROUP_ID, "runner_labels": [HOST, ARCH, HEAVY_LABEL], "heavy": HEAVY_LOCAL, "light": LIGHT_LOCAL}
POOL = Pool(GROUP_ID, frozenset(POOL_FIELDS["runner_labels"]), HEAVY_LOCAL, LIGHT_LOCAL)
POOL_JSON = json.dumps({**POOL_FIELDS, "blacksmith_daily_minutes": 120})
APP, SITE = f"{ORG}/app", f"{ORG}/site"
REPOSITORIES = (APP, SITE)

POOL_RUNNER = [{"name": n} for n in (HOST, ARCH, LIGHT_LABEL, HEAVY_LABEL)]
OTHER_RUNNER = [{"name": n} for n in (OTHER_HOST, OTHER_ARCH, LIGHT_LABEL)]
OTHER_QUARANTINED = [{"name": n} for n in (OTHER_HOST, OTHER_ARCH, QUARANTINE_LABEL)]


def runner(index, labels, status="online", busy=False):
    return {"id": index, "name": f"r{index}", "status": status, "busy": busy, "labels": labels}


class FakeApi:
    """Serves the runner group, per-repository runs and jobs, and today's history (path to items)."""

    def __init__(self, runners, runs=None, jobs=None, fail=False, history=None):
        self.runners = runners
        self.runs = runs or {}
        self.jobs = jobs or {}
        self.fail = fail
        self.history = history or {}
        self.paths = []

    def paginated(self, path, key):
        self.paths.append(path)
        if self.fail:
            raise GitHubApiError("boom", status=500)
        if path in self.history:
            return self.history[path]
        assert path == f"/orgs/{ORG}/actions/runner-groups/{GROUP_ID}/runners" and key == "runners"
        return self.runners

    def get(self, path):
        self.paths.append(path)
        for repository in REPOSITORIES:
            for state in ("queued", "in_progress"):
                if path == f"/repos/{repository}/actions/runs?status={state}&per_page=100":
                    return {"workflow_runs": self.runs.get((repository, state), [])}
            prefix = f"/repos/{repository}/actions/runs/"
            if path.startswith(prefix) and path.endswith("/jobs?filter=latest&per_page=100"):
                run_id = int(path[len(prefix):].split("/")[0])
                return {"jobs": self.jobs.get(run_id, [])}
        raise AssertionError(f"unexpected path {path}")


def queued(labels, status="queued"):
    return {"status": status, "labels": labels}


class ReadSnapshotTest(unittest.TestCase):
    def test_counts_pools_and_waiting_jobs_across_repositories(self):
        api = FakeApi(
            runners=[
                runner(1, POOL_RUNNER, busy=True),
                runner(2, POOL_RUNNER, busy=False),
                runner(3, POOL_RUNNER, status="offline"),
                runner(4, OTHER_RUNNER),
                runner(5, OTHER_QUARANTINED),
            ],
            runs={(APP, "queued"): [{"id": 10}, {"id": 99}], (SITE, "in_progress"): [{"id": 11}]},
            jobs={
                10: [queued(HEAVY_LOCAL), queued(HEAVY_LOCAL, "in_progress"), queued([LIGHT_LABEL, OTHER_ARCH])],
                11: [queued(HEAVY_LOCAL, "pending"), queued([LIGHT_LABEL]), queued(LIGHT_LOCAL)],
                99: [queued(HEAVY_LOCAL)],
            },
        )

        snapshot, notes = read_snapshot(api, POOL, org=ORG, repositories=REPOSITORIES, current_run_id="99")
        self.assertEqual(notes, [])

        # Other hosts' runners are not the pool; a bare light job waits for the pool, another architecture's never does.
        self.assertEqual((snapshot.pool.online, snapshot.pool.idle, snapshot.pool.waiting), (2, 1, 4))
        self.assertNotIn(f"/repos/{APP}/actions/runs/99/jobs?filter=latest&per_page=100", api.paths)


    def test_reads_only_the_given_repository_and_skips_an_unreadable_one(self):
        class Unreadable(FakeApi):
            def get(self, path):
                if SITE in path:
                    raise GitHubApiError("404", status=404)
                return super().get(path)

        api = Unreadable([runner(1, POOL_RUNNER)])
        snapshot, notes = read_snapshot(api, POOL, org=ORG, repositories=(SITE, APP))
        self.assertEqual(snapshot.pool.online, 1)
        self.assertEqual(len(notes), 1)
        self.assertIn(SITE, notes[0])
        api = FakeApi([runner(1, POOL_RUNNER)])
        read_snapshot(api, POOL, org=ORG, repositories=(APP,))
        self.assertFalse(any(SITE in path for path in api.paths))


SMALL_LABEL = "pool-small"
SMALL_LOCAL = [SMALL_LABEL, ARCH]
SMALL_POOL = Pool(GROUP_ID, POOL.runner_labels, HEAVY_LOCAL, LIGHT_LOCAL, 0, SMALL_LOCAL, frozenset({HOST, ARCH, SMALL_LABEL}))
# A pool whose runners need only host and arch: a reserved runner matches it too, so the order counts.
BROAD_POOL = Pool(GROUP_ID, frozenset({HOST, ARCH}), HEAVY_LOCAL, LIGHT_LOCAL, 0, SMALL_LOCAL, frozenset({HOST, ARCH, SMALL_LABEL}))
SMALL_RUNNER = [{"name": n} for n in (HOST, ARCH, SMALL_LABEL)]


class SmallRouteTest(unittest.TestCase):
    """Short jobs take a reserved small runner when it is free soon, else the light route as decided."""

    settings = RouteSettings(pool=SMALL_POOL)
    light_local = Route(HEAVY_LOCAL, LIGHT_LOCAL, "r")
    light_overflow = Route(HEAVY_LOCAL, LIGHT_OVERFLOW, "r")

    def test_a_free_reserved_runner_takes_the_small_jobs(self):
        route = with_small(self.light_local, Snapshot(small=PoolState(1, 1, 0)), self.settings)
        self.assertEqual(route.small, SMALL_LOCAL)

    def test_a_busy_or_offline_reserved_runner_sends_them_the_light_way(self):
        # Two small jobs ahead on one busy runner: about 6 minutes, over the 5-minute limit.
        for snapshot in (Snapshot(small=PoolState(1, 0, 2)), Snapshot()):
            for route in (self.light_local, self.light_overflow):
                with self.subTest(snapshot=snapshot, light=route.light):
                    self.assertEqual(with_small(route, snapshot, self.settings).small, route.light)

    def test_without_reserved_runners_small_is_light_and_routing_off_stays_empty(self):
        self.assertEqual(with_small(self.light_local, Snapshot(), RouteSettings(pool=POOL)).small, LIGHT_LOCAL)
        self.assertIsNone(with_small(Route(None, None, "off"), Snapshot(), self.settings).small)
        self.assertEqual(decide(Snapshot(), RouteSettings(mode="overflow")).small, LIGHT_OVERFLOW)

    def test_the_snapshot_counts_reserved_runners_and_their_jobs_apart_from_the_pool(self):
        api = FakeApi(
            runners=[runner(1, POOL_RUNNER), runner(2, SMALL_RUNNER, busy=True)],
            runs={(APP, "queued"): [{"id": 10}]},
            jobs={10: [queued(SMALL_LOCAL), queued(HEAVY_LOCAL)]},
        )
        snapshot, _ = read_snapshot(api, BROAD_POOL, org=ORG, repositories=(APP,))
        self.assertEqual((snapshot.pool.online, snapshot.pool.waiting), (1, 1))
        self.assertEqual((snapshot.small.online, snapshot.small.idle, snapshot.small.waiting), (1, 0, 1))

    def test_the_pool_input_takes_small_and_its_labels_together(self):
        fields = {**POOL_FIELDS, "small": SMALL_LOCAL, "small_runner_labels": [HOST, ARCH, SMALL_LABEL]}
        self.assertEqual(pool_from_json(json.dumps(fields)).small, SMALL_LOCAL)
        for bad in ({**POOL_FIELDS, "small": SMALL_LOCAL}, {**POOL_FIELDS, "small": SMALL_LOCAL, "small_runner_labels": [HOST, ARCH]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pool_from_json(json.dumps(bad))

    def test_small_jobs_never_get_blacksmith_beyond_the_budget(self):
        # The reserved runner is busy and the pool backed up, so both would overflow; without a share
        # the light jobs wait for the pool, and the small ones follow them there.
        fields = {**POOL_FIELDS, "small": SMALL_LOCAL, "small_runner_labels": [HOST, ARCH, SMALL_LABEL]}
        api = FakeApi([runner(1, POOL_RUNNER, busy=True), runner(2, SMALL_RUNNER, busy=True)],
                      runs={(APP, "queued"): [{"id": 10}]}, jobs={10: [queued(HEAVY_LOCAL)] * 5 + [queued(SMALL_LOCAL)] * 3})
        env = {"GH_TOKEN": "t", "GITHUB_REPOSITORY": APP, "VEXEL_CI_POOL": json.dumps(fields)}
        route = route_from_env(env, api_factory=lambda token: api, now=NOW)
        self.assertEqual((route.light, route.small), (LIGHT_LOCAL, LIGHT_LOCAL))


class DecideTest(unittest.TestCase):
    settings = RouteSettings(pool=POOL)

    def test_an_idle_pool_takes_heavy_and_light(self):
        route = decide(Snapshot(PoolState(3, 3, 0)), self.settings)
        self.assertEqual((route.heavy, route.light), (HEAVY_LOCAL, LIGHT_LOCAL))

    def test_a_long_backlog_overflows_the_light_jobs_only(self):
        # The 23 October 2026 queue: three busy runners and 22 waiting jobs, about 61 minutes.
        route = decide(Snapshot(PoolState(3, 0, 22)), self.settings)
        self.assertEqual((route.heavy, route.light), (HEAVY_LOCAL, LIGHT_OVERFLOW))
        self.assertIn("backlog", route.reason)

    def test_a_short_backlog_stays_local(self):
        # 6 waiting jobs on three busy runners: about 19 minutes, under the 20 minute limit.
        route = decide(Snapshot(PoolState(3, 0, 6)), self.settings)
        self.assertEqual((route.heavy, route.light), (HEAVY_LOCAL, LIGHT_LOCAL))

    def test_an_offline_pool_overflows_the_light_jobs_only(self):
        # Heavy tests time out on Blacksmith's 2 vCPU runner, so heavy jobs wait for the pool.
        route = decide(Snapshot(), self.settings)
        self.assertEqual((route.heavy, route.light), (HEAVY_LOCAL, LIGHT_OVERFLOW))
        self.assertIn("no pool runner is online", route.reason)

    def test_forced_modes(self):
        snapshot = Snapshot(PoolState(3, 3, 0))
        local = decide(snapshot, RouteSettings(mode="local"))
        overflow = decide(snapshot, RouteSettings(mode="overflow"))
        self.assertEqual((local.heavy, local.light), (None, None))
        self.assertEqual((overflow.heavy, overflow.light), (HEAVY_OVERFLOW, LIGHT_OVERFLOW))


NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
RUNS_TODAY = f"/repos/{APP}/actions/runs?created=>=2026-10-28"
SMALL = "blacksmith-2vcpu-ubuntu-2404"


def job(label, started="2026-10-28T10:00:00Z", completed="2026-10-28T10:10:00Z", runner="runner-1"):
    status = "completed" if completed else ("in_progress" if runner else "queued")
    return {"labels": [label], "started_at": started, "completed_at": completed, "runner_name": runner, "status": status}


def history(*jobs_per_run):
    """Today's runs for the repository, one per argument (ids 0, 1, ...), each with the given jobs."""
    paths = {RUNS_TODAY: [{"id": index} for index in range(len(jobs_per_run))]}
    for index, jobs in enumerate(jobs_per_run):
        paths[f"/repos/{APP}/actions/runs/{index}/jobs?filter=all"] = list(jobs)
    return paths


def spent(minutes):
    """One finished run that used the given 2 vCPU minutes."""
    return [job(SMALL, "2026-10-28T08:00:00Z", f"2026-10-28T08:{minutes:02d}:00Z")]


class BudgetTest(unittest.TestCase):
    settings = RouteSettings(
        pool=POOL, blacksmith_daily_minutes=200, light_reserve_minutes=30, active_run_reserve_minutes=60
    )
    overflowing = Route(HEAVY_LOCAL, LIGHT_OVERFLOW, "light overflows")

    def setUp(self):
        patcher = patch("vexel_ci.route.RETRY_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_counts_billed_minutes_weighted_by_vcpu_and_rounded_up(self):
        api = FakeApi([], history=history(
            [job(SMALL), job("blacksmith-4vcpu-ubuntu-2404")],
            [job(SMALL, "2026-10-28T11:00:00Z", "2026-10-28T11:00:05Z"), job(HEAVY_LABEL)],
            # Cancelled while queued: GitHub sets started_at, but no runner took it.
            [job(SMALL, "2026-10-28T12:00:00Z", "2026-10-28T12:30:00Z", runner=None)],
            [job(SMALL, "2026-10-28T14:55:30Z", None)],
        ))
        use = blacksmith_use_today(api, APP, NOW)
        # 10 + 10 x 2 + 1 (five seconds) + 5 (still running at 15:00); local and never-run jobs are free.
        self.assertEqual(use.minutes, 36)
        self.assertEqual(use.active_runs, 1)

    def test_a_queued_blacksmith_job_makes_its_run_active_but_costs_nothing_yet(self):
        api = FakeApi([], history=history([job(SMALL, "2026-10-28T14:59:00Z", None, runner=None)]))
        self.assertEqual(blacksmith_use_today(api, APP, NOW), BlacksmithUse(0, 1))

    def test_the_current_run_counts_its_minutes_but_is_not_another_active_run(self):
        api = FakeApi([], history=history([job(SMALL), job(SMALL, "2026-10-28T14:59:00Z", None)]))
        self.assertEqual(blacksmith_use_today(api, APP, NOW, current_run_id="0"), BlacksmithUse(11, 0))

    def test_a_failed_read_is_tried_once_more(self):
        class FlakyOnce(FakeApi):
            failed = False

            def paginated(self, path, key):
                if not self.failed:
                    self.failed = True
                    raise GitHubApiError("502", status=502)
                return super().paginated(path, key)

        api = FlakyOnce([], history=history(spent(10)))
        self.assertEqual(blacksmith_use_today(api, APP, NOW).minutes, 10)

    def test_room_for_this_run_keeps_the_overflow(self):
        api = FakeApi([], history=history(spent(10)))
        route = apply_budget(api, APP, self.overflowing, self.settings, NOW)
        self.assertEqual((route.heavy, route.light), (HEAVY_LOCAL, LIGHT_OVERFLOW))
        self.assertIn("10 of 200 Blacksmith min used", route.reason)

    def test_a_share_without_room_for_this_run_sends_overflow_to_the_pool(self):
        api = FakeApi([], history=history(spent(50), spent(50), spent(50), spent(30)))
        route = apply_budget(api, APP, self.overflowing, self.settings, NOW)
        self.assertEqual((route.heavy, route.light), (HEAVY_LOCAL, LIGHT_LOCAL))
        self.assertIn("over budget: 180 of 200", route.reason)
        self.assertIn("30 reserved", route.reason)

    def test_runs_already_on_blacksmith_hold_their_reservation(self):
        # 40 + 2 x 10 minutes spent, and two other runs are still on Blacksmith: 60 + 30 + 2 x 60 > 200.
        running = [job(SMALL, "2026-10-28T14:50:00Z", None)]
        api = FakeApi([], history=history(spent(40), running, running))
        route = apply_budget(api, APP, self.overflowing, self.settings, NOW)
        self.assertEqual(route.light, LIGHT_LOCAL)
        self.assertIn("2 other active", route.reason)

    def test_unreadable_use_counts_as_over_budget(self):
        route = apply_budget(FakeApi([], fail=True), APP, self.overflowing, self.settings, NOW)
        self.assertEqual((route.heavy, route.light), (HEAVY_LOCAL, LIGHT_LOCAL))
        self.assertIn("unreadable", route.reason)

    def test_a_pool_without_a_share_keeps_light_jobs_local_without_reading_its_use(self):
        api = FakeApi([], fail=True)
        route = apply_budget(api, APP, self.overflowing, RouteSettings(pool=POOL, blacksmith_daily_minutes=0), NOW)
        self.assertEqual((route.heavy, route.light), (HEAVY_LOCAL, LIGHT_LOCAL))
        self.assertIn("no Blacksmith share", route.reason)
        self.assertEqual(api.paths, [])

    def test_local_route_or_no_budget_reads_nothing(self):
        api = FakeApi([], fail=True)
        local = Route(HEAVY_LOCAL, LIGHT_LOCAL, "local")
        self.assertIs(apply_budget(api, APP, local, self.settings, NOW), local)
        self.assertIs(apply_budget(api, APP, self.overflowing, RouteSettings(), NOW), self.overflowing)
        self.assertEqual(api.paths, [])

    def test_the_pools_share_applies_and_the_input_overrides_it(self):
        pool_down = FakeApi([runner(1, OTHER_RUNNER)], history=history(spent(10)))
        env = {"GH_TOKEN": "t", "GITHUB_REPOSITORY": APP, "VEXEL_CI_POOL": POOL_JSON}
        self.assertEqual(route_from_env(env, api_factory=lambda token: pool_down, now=NOW).light, LIGHT_OVERFLOW)
        none = route_from_env({**env, "BLACKSMITH_DAILY_MINUTES": "0"}, api_factory=lambda token: pool_down, now=NOW)
        self.assertEqual((none.heavy, none.light), (HEAVY_LOCAL, LIGHT_LOCAL))

    def test_route_from_env_passes_the_current_run(self):
        pool_down = FakeApi([runner(1, OTHER_RUNNER)], history=history([job(SMALL, "2026-10-28T14:59:00Z", None)]))
        env = {"GH_TOKEN": "t", "GITHUB_REPOSITORY": APP, "GITHUB_RUN_ID": "0", "BLACKSMITH_DAILY_MINUTES": "200", "VEXEL_CI_POOL": POOL_JSON}
        route = route_from_env(env, api_factory=lambda token: pool_down, now=NOW)
        self.assertEqual(route.light, LIGHT_OVERFLOW)
        self.assertIn("0 other active", route.reason)

    def test_forced_overflow_ignores_the_budget(self):
        route = route_from_env({"ROUTE_MODE": "overflow", "BLACKSMITH_DAILY_MINUTES": "nan"})
        self.assertEqual(route.heavy, HEAVY_OVERFLOW)

    def test_a_pool_without_a_share_never_overflows(self):
        env = {"GH_TOKEN": "t", "GITHUB_REPOSITORY": APP, "VEXEL_CI_POOL": json.dumps(POOL_FIELDS)}
        self.assertEqual(settings_from_env(env).blacksmith_daily_minutes, 0)

    def test_a_budget_that_is_not_a_number_of_minutes_falls_back(self):
        for value in ("nan", "-5", "inf", "lots"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    settings_from_env({"BLACKSMITH_DAILY_MINUTES": value})
                route = route_from_env({"GH_TOKEN": "t", "GITHUB_REPOSITORY": APP, "BLACKSMITH_DAILY_MINUTES": value, "VEXEL_CI_POOL": POOL_JSON})
                self.assertEqual((route.heavy, route.light), (None, None))


class RouteFromEnvTest(unittest.TestCase):
    def test_missing_token_leaves_the_runner_variables_in_charge(self):
        route = route_from_env({"GITHUB_REPOSITORY": APP, "VEXEL_CI_POOL": POOL_JSON})
        self.assertEqual((route.heavy, route.light), (None, None))
        self.assertIn("token", route.reason)

    def test_runner_api_error_leaves_the_runner_variables_in_charge(self):
        route = route_from_env({"GH_TOKEN": "t", "GITHUB_REPOSITORY": APP, "VEXEL_CI_POOL": POOL_JSON}, api_factory=lambda token: FakeApi([], fail=True))
        self.assertEqual((route.heavy, route.light), (None, None))

    def test_bad_mode_leaves_the_runner_variables_in_charge(self):
        route = route_from_env({"ROUTE_MODE": "sometimes"})
        self.assertEqual(route.heavy, None)

    def test_auto_reads_only_its_own_repository(self):
        api = FakeApi([runner(1, POOL_RUNNER), runner(2, OTHER_RUNNER)])
        route_from_env({"GH_TOKEN": "t", "GITHUB_REPOSITORY": SITE, "VEXEL_CI_POOL": POOL_JSON}, api_factory=lambda token: api)
        self.assertTrue(all(APP not in path for path in api.paths))

    def test_without_a_pool_the_runner_variables_decide_and_nothing_is_read(self):
        # A consumer whose organization has no runner group yet passes an empty pool.
        api = FakeApi([runner(1, POOL_RUNNER)])
        route = route_from_env({"GH_TOKEN": "t", "GITHUB_REPOSITORY": "other-org/app", "VEXEL_CI_POOL": ""}, api_factory=lambda token: api)
        self.assertEqual((route.heavy, route.light), (None, None))
        self.assertIn("No pool configured", route.reason)
        self.assertEqual(api.paths, [])

    def test_the_pool_is_read_in_the_consumers_own_organization(self):
        api = FakeApi([runner(1, POOL_RUNNER)])
        route = route_from_env({"GH_TOKEN": "t", "GITHUB_REPOSITORY": APP, "VEXEL_CI_POOL": POOL_JSON}, api_factory=lambda token: api)
        self.assertEqual((route.heavy, route.light), (HEAVY_LOCAL, LIGHT_LOCAL))
        self.assertIn(f"/orgs/{ORG}/actions/runner-groups/{GROUP_ID}/runners", api.paths)

    def test_a_malformed_pool_falls_back(self):
        for value in ("{", '{"group_id": "6"}', json.dumps({**POOL_FIELDS, "group_id": True}), json.dumps({**POOL_FIELDS, "heavy": []}),
                      json.dumps({**POOL_FIELDS, "blacksmith_daily_minutes": -1})):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    pool_from_json(value)
                route = route_from_env({"GH_TOKEN": "t", "GITHUB_REPOSITORY": APP, "VEXEL_CI_POOL": value})
                self.assertEqual((route.heavy, route.light), (None, None))

    def test_forced_overflow_needs_no_token(self):
        route = route_from_env({"ROUTE_MODE": "overflow", "HEAVY_OVERFLOW": '["custom-heavy"]'})
        self.assertEqual((route.heavy, route.light), (["custom-heavy"], LIGHT_OVERFLOW))

    def test_auto_reads_github(self):
        api = FakeApi([runner(1, POOL_RUNNER), runner(2, OTHER_RUNNER)])
        route = route_from_env({"GH_TOKEN": "t", "GITHUB_REPOSITORY": APP, "VEXEL_CI_POOL": POOL_JSON, "OVERFLOW_AFTER_MINUTES": "5"},
                               api_factory=lambda token: api)
        self.assertEqual((route.heavy, route.light), (HEAVY_LOCAL, LIGHT_LOCAL))

    def test_main_writes_json_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "out")
            env = {"ROUTE_MODE": "overflow", "GITHUB_OUTPUT": output}
            with patch.dict(os.environ, env, clear=True), patch("builtins.print"):
                self.assertEqual(main(), 0)
            with open(output, encoding="utf-8") as handle:
                values = dict(line.split("=", 1) for line in handle.read().splitlines())
        self.assertEqual(json.loads(values["heavy_runs_on"]), HEAVY_OVERFLOW)
        self.assertEqual(json.loads(values["light_runs_on"]), LIGHT_OVERFLOW)
        self.assertEqual(json.loads(values["small_runs_on"]), LIGHT_OVERFLOW)

    def test_main_writes_empty_outputs_when_routing_is_off(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "out")
            with patch.dict(os.environ, {"ROUTE_MODE": "local", "GITHUB_OUTPUT": output}, clear=True), patch("builtins.print"):
                main()
            with open(output, encoding="utf-8") as handle:
                values = dict(line.split("=", 1) for line in handle.read().splitlines())
        self.assertEqual((values["heavy_runs_on"], values["light_runs_on"]), ("", ""))


if __name__ == "__main__":
    unittest.main()

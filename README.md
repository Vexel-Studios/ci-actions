# Vexel CI actions

Shared GitHub Actions of the Vexel CI standard: the composite actions that a consumer repository's `ci.yml`
calls for its run modes (light, full, verified), its `ci:merge` merge gate, its runner routing with Blacksmith
overflow, its Bun and Turbo setup and its conformance check. [`templates/ci.yml`](templates/ci.yml) is the
standard workflow that uses all of them, with [`templates/vexel-ci.json`](templates/vexel-ci.json) beside it.

The repository is public so that consumers in any organization can use the actions. It holds no configuration of
any pool: runner groups, labels and budgets reach the actions as inputs.

## Actions

| `uses:` | What it does |
| --- | --- |
| `Vexel-Studios/ci-actions/scope@v1` | Decides what the run covers without an install: full, light or verified, documentation only, the affected packages, the packages whose tests run in full, and configurable path flags |
| `Vexel-Studios/ci-actions/route@v1` | Picks the heavy and light `runs-on` values: the self-hosted pool given in `pool`, or Blacksmith's 2 vCPU runner as overflow for light jobs within a daily budget. Never fails the run |
| `Vexel-Studios/ci-actions/merge-request@v1` | The `ci:merge` flow: `merge` the tested head as the repository's App with the SHA pinned, `blocked`, `reset`, `abort`, and `check-base`, the stale merge-ref guard |
| `Vexel-Studios/ci-actions/complete@v1` | The closing job's verdict and summary, and the record that makes a later push of the same tree `verified` |
| `Vexel-Studios/ci-actions/setup@v1` | Bun from `packageManager`, the standard's Node, a frozen install with or without the advisory scan |
| `Vexel-Studios/ci-actions/turbo-cache@v1` | Connects turbo to a remote cache: read and write on pull requests and integration pushes, write only elsewhere |
| `Vexel-Studios/ci-actions/verify-cache-hit@v1` | Reruns one remote cache hit for real and fails when it does not reproduce |
| `Vexel-Studios/ci-actions/conformance@v1` | Reports where the repository differs from the standard version it declares in `.github/vexel-ci.json` |

Each `action.yml` documents its inputs and outputs. The Python they run is the `vexel_ci` package at the root of
this repository, with the standard library only (Python 3.9 or later). Every action runs it isolated, as
`python3 -I "$VEXEL_CI_ROOT/run.py" vexel_ci.<module>` with `VEXEL_CI_ROOT` set to `${{ github.action_path }}/..`, so
neither the job's working directory nor `PYTHONPATH` can put other code in its place.

### The route's pool

`route` reads the pool from its `pool` input, usually a repository variable:

```json
{
  "group_id": 123,
  "runner_labels": ["pool-host", "pool-x64", "pool-heavy"],
  "heavy": ["pool-heavy", "pool-x64"],
  "light": ["pool-light", "pool-x64"],
  "blacksmith_daily_minutes": 60
}
```

A runner of the group (in the consumer's own organization) that carries every one of `runner_labels` is part of
the pool. `heavy` and `light` are the `runs-on` values of the two kinds of job, each with its route label first.
`blacksmith_daily_minutes` is the daily overflow budget in 2 vCPU minutes; without it the repository never
overflows. The App behind `client-id` and `private-key` needs organization self-hosted runners read and Actions
read. An empty `pool`, a missing token or any error reading GitHub gives empty outputs, and the consumer's
`CI_RUNS_ON` and `CI_LIGHT_RUNS_ON` variables decide.

A pool may also reserve small runners for short jobs (a closing job, a quick check): `"small": ["pool-small",
"pool-x64"]` is their `runs-on`, route label first, and `"small_runner_labels"` the labels each of them carries,
which must include that route label. The `small-runs-on` output is then a reserved runner when one is free within
about five minutes, and otherwise exactly `light-runs-on` (the pool, or Blacksmith within the budget). The choice
is made when the run starts: a job routed to the reserved runners waits for them if they all go offline meanwhile,
so reserve more than one when that matters.

## Versions

Releases are tagged `v1.0.0`, `v1.0.1`, ... (never moved), and the major tag `v1` points at the latest `v1.x.y`.

- `@v1`: the default; a consumer gets fixes when `v1` moves, and never a breaking change.
- `@<full commit SHA>  # v1.0.0`: for a consumer that pins every action exactly; it moves by changing the SHA.
  Prefer it for `merge-request` and `route`, which receive the repository's App private key: a SHA cannot move.
- `@main`: unreleased; not for consumers.

Only the repository's administrators can create, move or delete `v*` tags (a tag ruleset). The actions these
actions use are pinned by commit SHA.

A change that breaks an input or an output, or a stricter conformance rule, is a new major version (`v2`).

## Development

```sh
python3 -m unittest discover -s tests
```

The suite uses fakes for GitHub and runs offline in under a second; the `test` workflow runs it on every pull
request. Keep the code standard-library only and Python 3.9 compatible. `tests/test_actions.py` checks every
`action.yml` (declared inputs, the modules it runs, YAML values GitHub would fail to parse), and
`tests/test_conformance.py` keeps the template conformant.

## License

[MIT](LICENSE).

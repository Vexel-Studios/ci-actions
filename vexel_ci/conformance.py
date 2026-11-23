"""Report where a consumer repository differs from the Vexel CI standard.

It reads the repository's `.github/vexel-ci.json` (standard version, kind, recorded deviations),
its root package.json and its workflows, and reports one finding per rule that does not hold. A
finding whose rule the manifest records as a deviation (with a reason, a date and who decided)
is reported as a deviation, not a violation.

  python3 -m vexel_ci.conformance [--root DIR] [--strict]
      a checkout: the `conformance` action runs this in a consumer's router job
  python3 -m vexel_ci.conformance --repo OWNER/NAME [--ref REF] --operator-gh [--strict]
      read-only over the GitHub API with the operator's `gh` login, repeatable for several
      repositories; also checks the request labels and the runner variables

Workflows are read as text, not parsed as YAML (the package uses only the standard library):
jobs are the two-space-indented keys under `jobs:`, as every consumer formats them.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .github_api import GitHubApi, GitHubApiError
from .merge_pr import FULL_LABEL, MERGE_LABEL
from .standard import LATEST, MANIFEST_PATH, STANDARDS

ACTIONS = "Vexel-Studios/ci-actions/"
# Where the same actions lived before this repository existed; still accepted while consumers move.
LEGACY_ACTIONS = "Vexel-Studios/ci-runners/.github/actions/"
KINDS = ("typescript", "native")

# Every rule the check knows, with the standard's section that states it. Deviations name these IDs.
RULES = {
    "manifest": "The repository describes itself in .github/vexel-ci.json",
    "toolchain.bun": "Bun at the standard's version (packageManager)",
    "toolchain.vite-plus": "vite-plus at the standard's version",
    "toolchain.code-style": "@vexel-studios/code-style at the standard's version",
    "toolchain.turbo": "Turbo at the standard's version, with a turbo.json",
    "toolchain.task-runner": "Turbo runs the tasks: no `vp run` in root scripts or workflows",
    "toolchain.vite-config": "`vp lint` and `vp fmt` get their rules from the root vite.config's lint and fmt sections",
    "toolchain.node": "Node at the standard's major version where a workflow names one",
    "flow.route": "The router job calls the route action",
    "flow.scope": "The router job calls the scope action",
    "flow.router-permissions": "The router job may read pull requests (the scope lists their files), contents and Actions",
    "flow.drafts": "Drafts run nothing",
    "flow.labels": "ci:merge and ci:full drive the full run",
    "flow.stale-base": "A ci:merge run checks that its merge commit is on the base tip",
    "flow.complete": "One closing job sums the run up with the complete action",
    "flow.setup": "Jobs install with the setup action",
    "flow.turbo-cache": "Jobs connect the remote Turbo cache with the turbo-cache action",
    "flow.cache-recheck": "The merge gate runs one cached task again",
    "flow.light-tests": "Light runs check the affected packages (turbo --affected) and changed tests (--changed)",
    "flow.light-tests-scope": "Light runs test only the touched packages (turbo --filter=[base])",
    "flow.tests-job": "Full runs test in a job of their own on the heavy route, apart from the other checks",
    "runners.routed": "Every CI job takes its runner from the route",
    "runners.blacksmith-2vcpu": "Blacksmith only at 2 vCPU",
    "runners.no-fixed-blacksmith": "No job is fixed to Blacksmith",
    "cost.router-only": "Only the router job (and the label reset) runs on CI_ROUTER_RUNS_ON",
    "flow.route-key": "The router job uses the repository's read-only route App, not the merge App's key",
    "flow.merge-key-scope": "The App private key is named only by the router job and a job that exists only in merge runs",
    "cost.closing-job": "Merge and blocked run in the closing job, or in a job that exists only in merge runs",
}
# Rules reported as warnings: they show in the report and as annotations but fail no strict run, so a
# repository can move at its own pace (flow.route-key: until each repository has its route App).
WARNING_RULES = frozenset({"flow.route-key"})
TYPESCRIPT_ONLY = {
    "toolchain.bun", "toolchain.vite-plus", "toolchain.code-style", "toolchain.turbo", "toolchain.task-runner",
    "toolchain.vite-config",
    "flow.scope", "flow.router-permissions", "flow.setup", "flow.turbo-cache", "flow.cache-recheck", "flow.light-tests",
    "flow.light-tests-scope", "flow.tests-job",
}
# A Blacksmith runner label anywhere in a text; the number is its vCPUs.
BLACKSMITH_SIZE = re.compile(r"blacksmith-(\d+)vcpu")
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    deviation: str = ""  # the recorded reason, when the manifest records this rule as a deviation
    triggered: bool = True  # False: a recorded deviation whose rule found nothing (it may be stale)

    @property
    def warning(self) -> bool:
        return self.rule in WARNING_RULES and not self.deviation


class Files:
    """A repository's files: a checkout or the GitHub contents API."""

    def read(self, path: str) -> str | None:
        raise NotImplementedError

    def listdir(self, path: str) -> list[str]:
        raise NotImplementedError


class LocalFiles(Files):
    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, path: str) -> str | None:
        try:
            return (self.root / path).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None

    def listdir(self, path: str) -> list[str]:
        directory = self.root / path
        return sorted(entry.name for entry in directory.iterdir() if entry.is_file()) if directory.is_dir() else []


class GitHubFiles(Files):
    def __init__(self, api: GitHubApi, repository: str, ref: str = "") -> None:
        self.api, self.repository, self.ref = api, repository, ref

    def _get(self, path: str) -> Any:
        query = f"?ref={urllib.parse.quote(self.ref, safe='')}" if self.ref else ""
        try:
            return self.api.get(f"/repos/{self.repository}/contents/{urllib.parse.quote(path)}{query}")
        except GitHubApiError as error:
            if error.status == 404:
                return None
            raise

    def read(self, path: str) -> str | None:
        item = self._get(path)
        if not isinstance(item, dict) or item.get("type") != "file":
            return None
        return base64.b64decode(item.get("content") or "").decode("utf-8")

    def listdir(self, path: str) -> list[str]:
        items = self._get(path)
        return sorted(item["name"] for item in items if item.get("type") == "file") if isinstance(items, list) else []


@dataclass(frozen=True)
class Job:
    name: str
    runs_on: str
    body: str


def workflow_jobs(text: str) -> list[Job]:
    """The jobs under `jobs:`: two-space keys, each with its runs-on value (possibly several lines)."""
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if re.match(r"^jobs:\s*(#.*)?$", line))
    except StopIteration:
        return []
    jobs: list[tuple[str, list[str]]] = []
    for line in lines[start + 1:]:
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        header = re.match(r"^  ([A-Za-z0-9_-]+):\s*(#.*)?$", line)
        if header:
            jobs.append((header.group(1), []))
        elif jobs:
            jobs[-1][1].append(line)
    result = []
    for name, body in jobs:
        runs_on = ""
        for index, line in enumerate(body):
            match = re.match(r"^    runs-on:\s*(.*)$", line)
            if match:
                parts = [match.group(1)]
                for following in body[index + 1:]:
                    if following.startswith("      ") or not following.strip():
                        parts.append(following.strip())
                    else:
                        break
                runs_on = " ".join(part for part in parts if part)
                break
        result.append(Job(name, runs_on, "\n".join(body)))
    return result


def uses_action(text: str, name: str) -> bool:
    return f"{ACTIONS}{name}@" in text or f"{LEGACY_ACTIONS}{name}@" in text


def declared_version(package: dict[str, Any], name: str) -> str | None:
    """The root package.json's spec for a dependency, with `catalog:` references resolved."""
    for section in ("devDependencies", "dependencies"):
        spec = (package.get(section) or {}).get(name)
        if spec is not None:
            break
    else:
        return None
    if isinstance(spec, str) and spec.startswith("catalog:"):
        catalog_name = spec[len("catalog:"):]
        workspaces = package.get("workspaces") if isinstance(package.get("workspaces"), dict) else {}
        if catalog_name:
            catalogs = package.get("catalogs") or workspaces.get("catalogs") or {}
            spec = (catalogs.get(catalog_name) or {}).get(name)
        else:
            spec = (package.get("catalog") or workspaces.get("catalog") or {}).get(name)
    return spec if isinstance(spec, str) else None


def version_of(spec: str) -> str:
    """`1.7.0`, `npm:x@1.7.0` and `github:Org/repo#v1.0.4` all give the version they pin."""
    if "#" in spec:
        spec = spec.rsplit("#", 1)[1]
    elif spec.startswith("npm:"):
        spec = spec.rsplit("@", 1)[-1]
    return spec[1:] if spec.startswith("v") else spec


def check_version(findings: list[Finding], rule: str, package: dict[str, Any], name: str, expected: str) -> None:
    spec = declared_version(package, name)
    if spec is None:
        findings.append(Finding(rule, f"`{name}` is not in the root package.json (the standard pins {expected})"))
        return
    version = version_of(spec)
    if not EXACT_VERSION.match(version):
        findings.append(Finding(rule, f"`{name}` is `{spec}`, not an exact pin (the standard pins {expected})"))
    elif version != expected:
        findings.append(Finding(rule, f"`{name}` is {version}; the standard pins {expected}"))


def check_toolchain(files: Files, toolchain: Any, workflows: dict[str, str], findings: list[Finding]) -> None:
    raw = files.read("package.json")
    try:
        package = json.loads(raw) if raw is not None else None
    except ValueError:
        package = None
    if not isinstance(package, dict):
        findings.append(Finding("toolchain.bun", "No readable root package.json"))
        return
    # Corepack may append the tarball's hash: bun@1.4.5+sha512.<...>.
    manager = str(package.get("packageManager") or "").split("+", 1)[0]
    if manager != f"bun@{toolchain.bun}":
        findings.append(Finding("toolchain.bun", f"packageManager is `{manager or 'unset'}`; the standard is bun@{toolchain.bun}"))
    check_version(findings, "toolchain.vite-plus", package, "vite-plus", toolchain.vite_plus)
    check_version(findings, "toolchain.code-style", package, "@vexel-studios/code-style", toolchain.code_style)
    check_version(findings, "toolchain.turbo", package, "turbo", toolchain.turbo)
    if files.read("turbo.json") is None:
        findings.append(Finding("toolchain.turbo", "No turbo.json: Turbo is the task runner"))
    scripts = [name for name, command in (package.get("scripts") or {}).items() if "vp run" in str(command)]
    if scripts:
        findings.append(Finding("toolchain.task-runner", f"Root scripts use `vp run`: {', '.join(sorted(scripts))}"))
    for path, text in workflows.items():
        if re.search(r"\bvp run\b", text):
            findings.append(Finding("toolchain.task-runner", f"{path} uses `vp run`"))
    # `vp lint` and `vp fmt` ignore oxlint.config.ts and oxfmt.config.ts and read only the root
    # vite.config's `lint` and `fmt` sections; without them `vp fmt` silently uses its defaults.
    commands = " ".join(str(command) for command in (package.get("scripts") or {}).values()) + " " + " ".join(workflows.values())
    used = [tool for tool in ("lint", "fmt") if re.search(rf"\bvp {tool}\b", commands)]
    if used:
        config = next((text for text in (files.read(f"vite.config.{ext}") for ext in ("ts", "mts", "js", "mjs")) if text is not None), None)
        for tool in used:
            # `lint: {...}`, or the shorthand `defineConfig({ fmt, lint })`.
            if config is None or not re.search(rf"\b{tool}\b\s*[:,}}]", config):
                findings.append(Finding("toolchain.vite-config", f"`vp {tool}` is used but the root vite.config has no `{tool}` section"))


def check_workflows(
    workflows: dict[str, str], scripts: str, ci_path: str, router: str, kind: str, findings: list[Finding], node_major: str,
    turbo_global_env: frozenset | None = frozenset(),
) -> None:
    for path, text in workflows.items():
        for size in sorted(set(BLACKSMITH_SIZE.findall(_labels_text(text)))):
            if size != "2":
                findings.append(Finding("runners.blacksmith-2vcpu", f"{path} names a {size} vCPU Blacksmith runner"))
        for job in workflow_jobs(text):
            if "blacksmith-" in job.runs_on:
                findings.append(Finding("runners.no-fixed-blacksmith", f"{path}: job `{job.name}` is fixed to Blacksmith ({job.runs_on})"))
        for value in re.findall(r"node-version:\s*['\"]?([^'\"\s#]+)", _labels_text(text)):
            # An expression (`${{ env.NODE }}`) is not a version this check can read.
            if not value.startswith("${{") and value.split(".")[0] != node_major:
                findings.append(Finding("toolchain.node", f"{path} sets node-version {value}; the standard is Node {node_major}"))

    ci = workflows.get(ci_path)
    if ci is None:
        findings.append(Finding("flow.route", f"No CI workflow at {ci_path} (set `ci_workflow` in {MANIFEST_PATH})"))
        return
    jobs = workflow_jobs(ci)
    names = {job.name for job in jobs}
    if router not in names:
        findings.append(Finding("flow.route", f"{ci_path} has no router job `{router}` (set `router_job` in {MANIFEST_PATH})"))
    router_job = next((job for job in jobs if job.name == router), None)
    if router_job is None or not uses_action(router_job.body, "route"):
        findings.append(Finding("flow.route", f"The router job does not call {ACTIONS}route"))
    if kind == "typescript" and (router_job is None or not uses_action(router_job.body, "scope")):
        findings.append(Finding("flow.scope", f"The router job does not call {ACTIONS}scope"))
    if kind == "typescript" and router_job is not None:
        # The job's own permissions, or the workflow's when the job sets none.
        granted = router_job.body if re.search(r"^    permissions:", router_job.body, re.MULTILINE) else ci.split("\njobs:", 1)[0]
        if not re.search(r"^\s*permissions:\s*['\"]?(read|write)-all", granted, re.MULTILINE):
            for scope in ("pull-requests", "contents", "actions"):
                # Without pull-requests read the scope gets 403 listing a pull request's files and every
                # light run checks every package; without actions read no push is ever verified.
                if not re.search(rf"^\s+{scope}:\s*['\"]?(read|write)['\"]?\s*(#.*)?$", granted, re.MULTILINE):
                    findings.append(Finding("flow.router-permissions", f"The router job's permissions lack `{scope}: read`"))
    if "pull_request.draft" not in ci:
        findings.append(Finding("flow.drafts", f"{ci_path} never tests `github.event.pull_request.draft`: drafts run CI"))
    for label in (MERGE_LABEL, FULL_LABEL):
        if label not in ci:
            findings.append(Finding("flow.labels", f"{ci_path} never names `{label}`"))
    if "action: check-base" not in ci:
        findings.append(Finding("flow.stale-base", f"{ci_path} has no merge-request `action: check-base` step"))

    closing = [job for job in jobs if uses_action(job.body, "complete")]
    if not closing:
        findings.append(Finding("flow.complete", f"No job in {ci_path} calls {ACTIONS}complete"))
    for job in jobs:
        merges = [step for step in ("merge", "blocked") if re.search(rf"^\s+action: {step}\s*$", job.body, re.MULTILINE)]
        # A merge job gated at job level on the merge request exists only in a run that merges: it
        # costs no job in other runs and keeps the App's write key off their runners. It may withdraw
        # its own refused merge (`blocked` after a base re-checked right before the merge).
        gated = bool(re.search(r"^    if:.*\bmerge_request\b", job.body, re.MULTILINE))
        if gated and "merge" in merges:
            continue
        if merges and job not in closing:
            findings.append(Finding("cost.closing-job", f"Job `{job.name}` runs merge-request {' and '.join(merges)} on its own; fold it into the closing job"))

    # A job's runner receives every secret the job references, whatever a step's `if:` says, so the
    # App's write key must not appear in a job that runs in every run (beyond the router, which the
    # route needs it for).
    # The router holds its key in every run, on a third-party runner: a read-only route App's key, not
    # the merge App's, which can write contents and merge.
    if router_job is not None:
        keys = set(re.findall(r"\bsecrets\.(\w*PRIVATE_KEY)\b", router_job.body)) - {"VEXEL_CI_ROUTE_PRIVATE_KEY"}
        if keys:
            findings.append(Finding("flow.route-key", f"The router job names {', '.join(sorted(keys))}; give the route the read-only route App (VEXEL_CI_ROUTE_CLIENT_ID, VEXEL_CI_ROUTE_PRIVATE_KEY) and drop the fallback"))

    for job in jobs:
        if job.name == router or not re.search(r"\bsecrets\.\w*PRIVATE_KEY\b", job.body):
            continue
        if not re.search(r"^    if:.*\bmerge_request\b", job.body, re.MULTILINE):
            findings.append(Finding("flow.merge-key-scope", f"Job `{job.name}` names an App private key but runs outside merge runs; give the merge a job gated on merge_request"))

    for job in jobs:
        if "CI_ROUTER_RUNS_ON" in job.runs_on and job.name != router and not re.search(r"^\s+action: reset\s*$", job.body, re.MULTILINE):
            findings.append(Finding("cost.router-only", f"Job `{job.name}` runs on CI_ROUTER_RUNS_ON (Blacksmith): run it on the light route"))
        elif job.name != router and job.runs_on and "CI_ROUTER_RUNS_ON" not in job.runs_on and f"needs.{router}.outputs." not in job.runs_on and "inputs." not in job.runs_on:
            findings.append(Finding("runners.routed", f"Job `{job.name}` does not take its runner from the route ({job.runs_on})"))

    if kind == "typescript":
        for rule, action in (("flow.setup", "setup"), ("flow.turbo-cache", "turbo-cache"), ("flow.cache-recheck", "verify-cache-hit")):
            if not uses_action(ci, action):
                findings.append(Finding(rule, f"{ci_path} does not use {ACTIONS}{action}"))
        if uses_action(ci, "turbo-cache") and turbo_global_env is not None and "CI_RUNTIME" not in turbo_global_env:
            # The action sets CI_RUNTIME to the Node and Bun versions; only a task hash that includes
            # it keeps a runtime upgrade from replaying results of the old runtimes.
            findings.append(Finding("flow.turbo-cache", "turbo.json's globalEnv lacks CI_RUNTIME, so remote hits cross Bun and Node upgrades"))
        # The light job may keep its commands in a script (the website's unit-test-plan.sh).
        light = ci + "\n" + scripts
        if "--affected" not in light:
            findings.append(Finding("flow.light-tests", f"{ci_path} and .github/scripts never run `turbo --affected`"))
        if "--changed" not in light:
            findings.append(Finding("flow.light-tests", f"{ci_path} and .github/scripts never narrow the light tests with Vitest `--changed`"))
        if "--filter=[" not in light:
            findings.append(Finding("flow.light-tests-scope", f"{ci_path} and .github/scripts never limit the light tests to the touched packages (`--filter=[base]`)"))
        # The tests job: on the heavy route, runs the test task for real (not a --dry listing), and
        # leaves lint to another job, so the tests are not on the checks' critical path.
        test_jobs = [
            job for job in jobs
            if "heavy_runs_on" in job.runs_on and "turbo run lint" not in job.body
            and any("turbo run test" in line and "--dry" not in line for line in job.body.splitlines())
        ]
        if not test_jobs:
            findings.append(Finding("flow.tests-job", f"{ci_path} has no job on the heavy route that runs the tests on their own"))


def _labels_text(text: str) -> str:
    """Workflow text without comments, so a comment about 4 vCPU does not count."""
    return "\n".join(line.split("#", 1)[0] if not line.lstrip().startswith("#") else "" for line in text.splitlines())


def read_manifest(files: Files, findings: list[Finding]) -> dict[str, Any]:
    raw = files.read(MANIFEST_PATH)
    if raw is None:
        findings.append(Finding("manifest", f"No {MANIFEST_PATH}: checked as standard {LATEST}, kind typescript, no deviations"))
        return {}
    try:
        manifest = json.loads(raw)
    except ValueError as error:
        findings.append(Finding("manifest", f"{MANIFEST_PATH} is not JSON: {error}"))
        return {}
    if not isinstance(manifest, dict):
        findings.append(Finding("manifest", f"{MANIFEST_PATH} is not an object"))
        return {}
    if str(manifest.get("standard", "")) not in STANDARDS:
        findings.append(Finding("manifest", f"standard `{manifest.get('standard')}` is not one of {', '.join(STANDARDS)}"))
    if manifest.get("kind", "typescript") not in KINDS:
        findings.append(Finding("manifest", f"kind `{manifest.get('kind')}` is not one of {', '.join(KINDS)}"))
    for entry in manifest.get("deviations") or []:
        problem = deviation_problem(entry)
        if problem:
            findings.append(Finding("manifest", f"Deviation {json.dumps(entry)}: {problem}"))
    return manifest


def deviation_problem(entry: Any) -> str:
    if not isinstance(entry, dict):
        return "not an object"
    if entry.get("rule") not in RULES or entry.get("rule") == "manifest":
        return f"unknown rule `{entry.get('rule')}`"
    for key in ("reason", "decided", "by"):
        if not str(entry.get(key) or "").strip():
            return f"no `{key}`"
    try:
        date.fromisoformat(str(entry["decided"]))
    except ValueError:
        return "`decided` is not a YYYY-MM-DD date"
    return ""


def global_env(turbo_json: str | None) -> frozenset | None:
    """turbo.json's globalEnv names, or None when the file is not plain JSON (turbo accepts comments)."""
    try:
        value = json.loads(turbo_json or "{}")
    except ValueError:
        return None
    names = value.get("globalEnv") if isinstance(value, dict) else None
    return frozenset(name for name in names if isinstance(name, str)) if isinstance(names, list) else frozenset()


def check_repository(files: Files, remote: Callable[[list[Finding]], None] | None = None) -> tuple[str, list[Finding]]:
    """The standard version checked and every finding, deviations marked."""
    findings: list[Finding] = []
    manifest = read_manifest(files, findings)
    version = str(manifest.get("standard", LATEST))
    toolchain = STANDARDS.get(version, STANDARDS[LATEST])
    kind = manifest.get("kind", "typescript")
    ci_path = str(manifest.get("ci_workflow") or ".github/workflows/ci.yml")
    router = str(manifest.get("router_job") or "preflight")
    workflows = {}
    for name in files.listdir(".github/workflows"):
        if name.endswith((".yml", ".yaml")):
            workflows[f".github/workflows/{name}"] = files.read(f".github/workflows/{name}") or ""
    if kind == "typescript":
        check_toolchain(files, toolchain, workflows, findings)
    scripts = "\n".join(files.read(f".github/scripts/{name}") or "" for name in files.listdir(".github/scripts"))
    check_workflows(workflows, scripts, ci_path, router, kind, findings, toolchain.node_major, global_env(files.read("turbo.json")))
    if remote is not None:
        remote(findings)
    # One finding per rule and message: a workflow that sets Node 22 in four jobs is one problem.
    findings = list(dict.fromkeys(findings))
    recorded = {
        entry["rule"]: entry["reason"] for entry in manifest.get("deviations") or []
        if isinstance(entry, dict) and not deviation_problem(entry)
    }
    reported = [Finding(f.rule, f.message, recorded.get(f.rule, "")) for f in findings]
    found = {f.rule for f in findings}
    # A recorded deviation whose rule finds nothing is listed too, so it is seen (and dropped once stale).
    reported += [
        Finding(rule, "Recorded deviation; the rule finds nothing", reason, triggered=False)
        for rule, reason in recorded.items() if rule not in found
    ]
    return version, reported


def remote_checks(api: GitHubApi, repository: str) -> Callable[[list[Finding]], None]:
    """Labels and runner variables, which a checkout does not show."""

    def run(findings: list[Finding]) -> None:
        for label in (MERGE_LABEL, FULL_LABEL):
            try:
                api.get(f"/repos/{repository}/labels/{urllib.parse.quote(label, safe='')}")
            except GitHubApiError as error:
                if error.status == 404:
                    findings.append(Finding("flow.labels", f"The repository has no `{label}` label"))
                else:
                    raise
        for variable in ("CI_ROUTER_RUNS_ON", "CI_RUNS_ON", "CI_LIGHT_RUNS_ON"):
            try:
                value = str((api.get(f"/repos/{repository}/actions/variables/{variable}") or {}).get("value", ""))
            except GitHubApiError as error:
                if error.status == 404:
                    continue
                raise
            if any(size != "2" for size in BLACKSMITH_SIZE.findall(value)):
                findings.append(Finding("runners.blacksmith-2vcpu", f"Variable {variable} is {value}"))
            # Only the router may sit on Blacksmith by default; the other jobs reach it as overflow.
            if variable != "CI_ROUTER_RUNS_ON" and "blacksmith-" in value:
                findings.append(Finding("runners.no-fixed-blacksmith", f"Variable {variable} is {value}: the pool's labels, not Blacksmith"))

    return run


def report(name: str, version: str, findings: list[Finding]) -> str:
    violations = [f for f in findings if not f.deviation and not f.warning]
    warnings = [f for f in findings if f.warning]
    deviations = [f for f in findings if f.deviation and f.triggered]
    untriggered = [f for f in findings if f.deviation and not f.triggered]
    behind = f" (latest is {LATEST})" if version != LATEST else ""
    lines = [f"### Vexel CI standard {version}{behind}: {name}", ""]
    if not findings:
        lines.append("Conforms: no finding.")
    else:
        lines += [
            f"{len(violations)} violation(s), {len(warnings)} warning(s), {len(deviations)} recorded deviation(s), {len(untriggered)} recorded but not triggered.",
            "", "| Rule | Finding | Status |", "| --- | --- | --- |",
        ]
        for finding in violations + warnings + deviations + untriggered:
            if finding.warning:
                status = "warning (fails no run)"
            elif not finding.deviation:
                status = "violation"
            elif finding.triggered:
                status = f"deviation: {finding.deviation}"
            else:
                status = f"deviation (recorded, not triggered): {finding.deviation}"
            lines.append(f"| `{finding.rule}` | {finding.message.replace('|', '/')} | {status.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m vexel_ci.conformance")
    parser.add_argument("--root", default=".", help="a checkout to check (default: the current directory)")
    parser.add_argument("--repo", action="append", default=[], help="OWNER/NAME to read over the API instead (repeatable)")
    parser.add_argument("--ref", default="", help="the branch or SHA to read with --repo (default: the default branch)")
    parser.add_argument("--operator-gh", action="store_true", help="read with the operator's `gh auth token` (needed for --repo)")
    parser.add_argument("--strict", action="store_true", help="exit 1 when a violation is found")
    args = parser.parse_args(argv)

    if args.repo and not args.operator_gh and not os.environ.get("GH_TOKEN"):
        parser.error("--repo needs --operator-gh or GH_TOKEN")
    targets: list[tuple[str, Files, Callable[[list[Finding]], None] | None]] = []
    if args.repo:
        if args.operator_gh:
            # The operator's own login, read once and kept in memory only.
            token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
            api = GitHubApi(token)
        else:
            api = GitHubApi(os.environ["GH_TOKEN"])
        for repository in args.repo:
            targets.append((repository, GitHubFiles(api, repository, args.ref), remote_checks(api, repository)))
    else:
        targets.append((os.environ.get("GITHUB_REPOSITORY") or str(Path(args.root).resolve()), LocalFiles(Path(args.root)), None))

    violated = False
    for name, files, remote in targets:
        version, findings = check_repository(files, remote)
        text = report(name, version, findings)
        print(text)
        for finding in findings:
            if not finding.deviation:
                violated = violated or not finding.warning
                print(f"::warning title=Vexel CI standard ({finding.rule})::{finding.message}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
    return 1 if violated and args.strict else 0


if __name__ == "__main__":
    sys.exit(main())

"""Decide what one CI run covers: full, light or verified.

The `scope` action runs this in a consumer's router job, without a dependency install. The
repository's paths are settings, and its own path-based outputs are configurable flags.

Outputs, written to $GITHUB_OUTPUT (and printed):
  mode            full | light | verified. Full is every job the change can affect; light checks
                  only what the change affects; verified is a push to the integration branch whose
                  source tree already passed a full run, so nothing runs again.
  merge_request   true when this is the full run the `ci:merge` label asked for: when it passes,
                  CI merges the pull request.
  reason          why, in words.
  code            false when the change is documentation only.
  docs_only       code is false and the run is not verified.
  affected_only   false when the change touches a file outside the package directories (root
                  configuration, patches, tools, scripts) or a workflow, which `turbo --affected`
                  maps to no package: a light run then checks every package.
  lint_all        the change touches the lint or format configuration, which every package's lint
                  reads.
  dependencies    the change touches what is installed (lockfile, a package.json, Bun's config,
                  patches): only then do the advisory scan and the audit run.
  test_in_full    package directories where the change touches a file that is not a module (JSON,
                  SQL, Markdown, a tsconfig.json...). Tests read such files from disk, outside
                  Vitest's import graph, so `--changed` would select none of them.
  flags           JSON object, one boolean per configured flag: whether the change touches one of
                  the flag's paths (for example a landing site or a database's test suite).
  tree            the checked-out source tree (`git rev-parse HEAD^{tree}`).

Any failure to read the changed files makes every output true (a light run checks every package,
a full run builds everything): an API outage costs minutes, never coverage.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from .github_api import GitHubApi
from .merge_pr import FULL_LABEL, MERGE_LABEL
from .standard import VERIFIED_ARTIFACT_PREFIX

# GitHub lists at most 3000 files of a pull request and 300 of a comparison; a list that reaches
# the cap may be cut, so the change counts as touching everything.
PULL_FILES_CAP = 3000
COMPARE_FILES_CAP = 300
PAGE_SIZE = 100


@dataclass(frozen=True)
class ScopeSettings:
    """A repository's layout; the defaults fit a Bun workspace with apps/ and packages/."""

    integration_branch: str = "develop"
    # Directories that hold one workspace package each (`apps/web`); a glob of one level per `*`.
    package_globs: tuple[str, ...] = ("apps/*", "packages/*")
    # Documentation directories: everything in them builds and tests nothing, except what
    # code_markdown_globs names.
    docs_globs: tuple[str, ...] = ("docs/*", ".agents/*")
    # Other Markdown is documentation too. These, which builds or tests read (for example
    # */content/*.md), are code wherever they are, documentation directories included.
    code_markdown_globs: tuple[str, ...] = ()
    lint_config_globs: tuple[str, ...] = ("eslint.config.*", "oxlint.config.*", "oxfmt.config.*", "lint/*")
    dependency_globs: tuple[str, ...] = ("bun.lock", "package.json", "*/package.json", "bunfig.toml", "patches/*")
    # A package file that Vitest's --changed can follow through imports (a package.json turns
    # --changed off through `dependencies` already).
    module_globs: tuple[str, ...] = ("*/package.json", "*.ts", "*.tsx", "*.mts", "*.cts", "*.js", "*.mjs", "*.cjs", "*.vue")
    flags: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class Event:
    """The parts of a GitHub event the scope reads."""

    name: str = ""
    action: str = ""
    label: str = ""
    pr_number: str = ""
    ref_name: str = ""
    push_before: str = ""
    push_after: str = ""


@dataclass
class Scope:
    mode: str
    reason: str
    code: bool = True
    affected_only: bool = False
    lint_all: bool = True
    dependencies: bool = True
    merge_request: bool = False
    test_in_full: list[str] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)
    tree: str = ""
    warning: str = ""

    @property
    def docs_only(self) -> bool:
        # A verified push runs nothing, not even the documentation job.
        return not self.code and self.mode != "verified"

    def outputs(self) -> list[tuple[str, str]]:
        """The outputs as name and one-line value; a value with a line break is refused, never written."""

        def text(value: bool) -> str:
            return "true" if value else "false"

        values = [
            ("mode", self.mode),
            ("reason", self.reason),
            ("code", text(self.code)),
            ("docs_only", text(self.docs_only)),
            ("affected_only", text(self.affected_only)),
            ("lint_all", text(self.lint_all)),
            ("merge_request", text(self.merge_request)),
            ("dependencies", text(self.dependencies)),
            ("test_in_full", " ".join(self.test_in_full)),
            ("flags", json.dumps(self.flags, sort_keys=True, separators=(",", ":"))),
            ("tree", self.tree),
        ]
        for name, value in values:
            if "\n" in value or "\r" in value:
                raise ValueError(f"the {name} output contains a line break")
        return values


def matches(path: str, globs: tuple[str, ...]) -> bool:
    """Shell `case` semantics: `*` also matches `/`, so `apps/*` is every file under apps/."""
    return any(fnmatch.fnmatchcase(path, glob) for glob in globs)


@dataclass
class Changes:
    """What a list of changed files touches."""

    code: bool = False
    outside_packages: bool = False
    lint_all: bool = False
    dependencies: bool = False
    test_in_full: list[str] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)


def everything(settings: ScopeSettings) -> Changes:
    """The cautious reading when the changed files are unknown."""
    return Changes(True, True, True, True, [], {name: True for name, _ in settings.flags})


def classify(files: list[str], settings: ScopeSettings) -> Changes:
    changes = Changes(flags={name: False for name, _ in settings.flags})
    inside = settings.package_globs + (".github/*", "bun.lock") + settings.lint_config_globs
    for path in files:
        # Markdown a build or a test reads is code wherever it lives, a documentation directory
        # included: otherwise a change to it alone would be documentation only, and a ci:merge would
        # skip the very tests that read it.
        if not matches(path, settings.code_markdown_globs):
            if matches(path, settings.docs_globs) or path.endswith(".md"):
                continue
        changes.code = True
        if matches(path, settings.lint_config_globs):
            changes.lint_all = True
        # Tests read the workflows as text, which turbo maps to no package.
        if path.startswith(".github/workflows/") or not matches(path, inside):
            changes.outside_packages = True
        if not matches(path, settings.module_globs):
            for glob in settings.package_globs:
                if fnmatch.fnmatchcase(path, glob + "/*"):
                    directory = "/".join(path.split("/")[: glob.count("/") + 1])
                    if directory not in changes.test_in_full:
                        changes.test_in_full.append(directory)
                    break
        if matches(path, settings.dependency_globs):
            changes.dependencies = True
        for name, globs in settings.flags:
            if matches(path, globs):
                changes.flags[name] = True
    return changes


def pull_files(api: GitHubApi, repository: str, number: str) -> list[str] | None:
    """Every file of the pull request, renamed files under both names; None when cut at the cap."""
    names: list[str] = []
    for page in range(1, PULL_FILES_CAP // PAGE_SIZE + 2):
        items = api.get(f"/repos/{repository}/pulls/{number}/files?per_page={PAGE_SIZE}&page={page}")
        if not isinstance(items, list):
            raise RuntimeError("the pull request files response is not a list")
        for item in items:
            names.append(item["filename"])
            if item.get("previous_filename"):
                names.append(item["previous_filename"])
        if len(items) < PAGE_SIZE:
            break
    return names if len(names) < PULL_FILES_CAP else None


def compare_files(api: GitHubApi, repository: str, before: str, after: str) -> list[str]:
    result = api.get(f"/repos/{repository}/compare/{before}...{after}") or {}
    files = result.get("files") or []
    if len(files) >= COMPARE_FILES_CAP:
        raise RuntimeError(f"the comparison lists {len(files)} files, which may be cut")
    names: list[str] = []
    for item in files:
        names.append(item["filename"])
        if item.get("previous_filename"):
            names.append(item["previous_filename"])
    return names


def verified_run(api: GitHubApi, repository: str, tree: str) -> str:
    """The run that recorded a passed full run of this tree, from the repository's own branches."""
    try:
        page = api.get(f"/repos/{repository}/actions/artifacts?name={VERIFIED_ARTIFACT_PREFIX}{tree}&per_page=10") or {}
    except Exception:
        return ""
    for artifact in page.get("artifacts") or []:
        run = artifact.get("workflow_run") or {}
        # A fork's run could upload an artifact by that name too; only the repository's own count.
        if not artifact.get("expired") and run.get("id") and run.get("head_repository_id") == run.get("repository_id"):
            return str(run["id"])
    return ""


def decide(
    event: Event, settings: ScopeSettings, repository: str, tree: str, api_factory: Callable[[], GitHubApi]
) -> Scope:
    branch = settings.integration_branch

    def full(reason: str) -> Scope:
        changes = everything(settings)
        return Scope("full", reason, flags=changes.flags, tree=tree)

    # A push to the integration branch whose tree a passed full run already checked (the pull
    # request's merge run, when the branch had not moved on before the merge) runs nothing.
    if event.name == "push" and event.ref_name == branch and tree:
        try:
            run_id = verified_run(api_factory(), repository, tree)
        except ValueError:  # no usable token: not verified, like any other unreadable answer
            run_id = ""
        if run_id:
            return Scope(
                "verified", f"Push to {branch} whose tree {tree} passed the full run {run_id}",
                code=False, affected_only=False, lint_all=True, dependencies=False,
                flags={name: False for name, _ in settings.flags}, tree=tree,
            )

    if event.name == "push" and event.ref_name != branch:
        return full(f"Push to {event.ref_name or 'a protected branch'}")
    if event.name == "workflow_dispatch":
        return full("Manual run")
    if event.name not in ("push", "pull_request"):
        return full(f"{event.name or 'unknown'} event")

    warning = ""
    try:
        if event.name == "pull_request":
            files = pull_files(api_factory(), repository, event.pr_number)
        elif not event.push_before or not event.push_before.strip("0"):
            # A new branch's first push has nothing to compare with.
            raise RuntimeError("the push has no previous commit")
        else:
            files = compare_files(api_factory(), repository, event.push_before, event.push_after)
        if files is not None and any(control in name for name in files for control in "\r\n"):
            # A file name with a line break would reach $GITHUB_OUTPUT as a line of its own and
            # could set another output (mode=verified); such a change is checked in full instead.
            raise RuntimeError("a changed file's name contains a line break")
        changes = everything(settings) if files is None else classify(files, settings)
    except Exception as error:
        reason = " ".join(str(error).split()) or type(error).__name__
        warning = f"Could not list the changed files ({reason}); checking every package."
        changes = everything(settings)

    merge_request = False
    if event.name == "push":
        mode, reason = "light", f"Push to {branch} (each pull request had its full run before it merged)"
    elif event.action == "labeled" and event.label == MERGE_LABEL:
        merge_request = True
        mode, reason = "full", f"The {MERGE_LABEL} label approved this head: a full run, then CI merges it"
    elif event.action == "labeled" and event.label == FULL_LABEL:
        mode, reason = "full", f"The {FULL_LABEL} label asked for a full run of this head"
    elif event.action == "ready_for_review":
        mode, reason = "light", f"A draft marked ready for review (the full suite runs once, when {MERGE_LABEL} is added)"
    else:
        mode, reason = "light", f"A pull request push (the full suite runs once, when {MERGE_LABEL} is added)"

    return Scope(
        mode, reason, code=changes.code, affected_only=not changes.outside_packages, lint_all=changes.lint_all,
        dependencies=changes.dependencies, merge_request=merge_request,
        test_in_full=changes.test_in_full, flags=changes.flags, tree=tree, warning=warning,
    )


def globs(env: dict[str, str], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = env.get(name, "")
    return tuple(value.split()) if value.strip() else default


def settings_from_env(env: dict[str, str]) -> ScopeSettings:
    """Each glob input is whitespace-separated; an empty one keeps the default."""
    flags = []
    for line in env.get("SCOPE_FLAGS", "").splitlines():
        if not line.strip():
            continue
        name, separator, paths = line.partition(":")
        name = name.strip()
        if not separator or not name.replace("_", "").isalnum() or not paths.split():
            raise ValueError(f"a flag is `name: glob glob ...`, not {line.strip()!r}")
        flags.append((name, tuple(paths.split())))
    defaults = ScopeSettings()
    return ScopeSettings(
        integration_branch=env.get("SCOPE_INTEGRATION_BRANCH", "").strip() or defaults.integration_branch,
        package_globs=globs(env, "SCOPE_PACKAGE_GLOBS", defaults.package_globs),
        docs_globs=globs(env, "SCOPE_DOCS_GLOBS", defaults.docs_globs),
        code_markdown_globs=globs(env, "SCOPE_CODE_MARKDOWN_GLOBS", defaults.code_markdown_globs),
        lint_config_globs=globs(env, "SCOPE_LINT_CONFIG_GLOBS", defaults.lint_config_globs),
        dependency_globs=globs(env, "SCOPE_DEPENDENCY_GLOBS", defaults.dependency_globs),
        flags=tuple(flags),
    )


def current_tree() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main() -> int:
    env = dict(os.environ)
    settings = settings_from_env(env)
    event = Event(
        name=env.get("EVENT_NAME", ""), action=env.get("EVENT_ACTION", ""), label=env.get("EVENT_LABEL", ""),
        pr_number=env.get("PR_NUMBER", ""), ref_name=env.get("GITHUB_REF_NAME", ""),
        push_before=env.get("PUSH_BEFORE", ""), push_after=env.get("PUSH_AFTER", ""),
    )
    scope = decide(event, settings, env["GITHUB_REPOSITORY"], current_tree(), lambda: GitHubApi(env.get("GH_TOKEN", "")))
    if scope.warning:
        print(f"::warning title=CI scope::{scope.warning}")
    lines = [f"{name}={value}" for name, value in scope.outputs()]
    output = env.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        flags = ", ".join(f"{name}: {'true' if value else 'false'}" for name, value in sorted(scope.flags.items()))
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                f"### CI scope: {scope.mode}\n\n{scope.reason}. Documentation only: {'true' if scope.docs_only else 'false'}. "
                f"Dependencies (advisory scan and audit): {'true' if scope.dependencies else 'false'}."
                + (f" Flags: {flags}." if flags else "") + "\n"
            )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

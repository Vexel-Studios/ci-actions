"""The facts of the Vexel CI standard that code checks: tool versions and shared names.

Each standard version keeps its own toolchain, so a consumer that declares version "1" in its
`.github/vexel-ci.json` is checked against version 1's pins even after a later version raises
them; the conformance report shows who is behind the latest.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Toolchain:
    """Exact versions every TypeScript consumer pins (root package.json or its catalog)."""

    bun: str
    node_major: str
    vite_plus: str
    code_style: str
    turbo: str
    # The vite-plus version before `vite_plus`, still accepted with a `toolchain.upgrade` warning while
    # consumers move; empty once every consumer has.
    vite_plus_previous: str = ""


STANDARDS = {
    # 23 November 2026; vite-plus 1.8.0 (tsrs 7.1.0-beta.tsrs4) since 26 November 2026 (Levi's approval, relayed by the lead).
    "1": Toolchain(
        bun="1.4.5", node_major="24", vite_plus="1.8.0", code_style="1.0.4", turbo="2.11.10", vite_plus_previous="1.7.0"
    ),
}
LATEST = "1"

# A full run that passed uploads an artifact with this prefix and its source tree's hash; a push to
# the integration branch with the same tree is then `verified` and runs nothing (scope.py, complete.py).
VERIFIED_ARTIFACT_PREFIX = "ci-full-passed-"

# The consumer's description of itself: standard version, kind and recorded deviations (conformance.py).
MANIFEST_PATH = ".github/vexel-ci.json"

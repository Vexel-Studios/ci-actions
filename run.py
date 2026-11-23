"""Run one vexel_ci module for an action: `python3 -I run.py vexel_ci.<module> [args]`.

Isolated mode (-I) keeps the job's working directory, PYTHONPATH and user site-packages off the
import path, so a checkout that holds a `json.py` or a `vexel_ci/` of its own (a pull request's,
or one a previous job left on a persistent runner) cannot run in place of this repository's code
while the step holds a token. The package is then imported from this file's directory only.
"""

import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
module = sys.argv.pop(1)
if not module.startswith("vexel_ci."):
    sys.exit(f"run.py runs vexel_ci modules only, not {module!r}")
runpy.run_module(module, run_name="__main__", alter_sys=True)

"""Which build of revali is running: VERSION, plus `git describe` of the checkout the package
was loaded from when it is one. Every project runs the revali its skill points at, so a run's
logs and history row say which commit that was."""

import os
import threading
from typing import Optional

from revali import VERSION, gitops

# the directory holding the `revali` package: a checkout of revali when run from source
CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_lock = threading.Lock()
_build: Optional[str] = None


def build() -> str:
    """`<VERSION> (<describe>)`, or `<VERSION>` alone when CHECKOUT is not the root of a git
    checkout or git cannot describe it. Worked out once per process: git runs at most once,
    also when the baseline thread and the pipeline ask at the same time."""
    global _build
    with _lock:
        if _build is None:
            _build = _describe()
        return _build


def _describe() -> str:
    # only a checkout whose root is CHECKOUT: a copy of the package inside some other
    # project's tree would otherwise report that project's commit
    if not os.path.exists(os.path.join(CHECKOUT, ".git")):
        return VERSION
    described = gitops.describe(CHECKOUT)
    return "%s (%s)" % (VERSION, described) if described else VERSION

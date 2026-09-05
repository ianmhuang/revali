"""Wall-clock accounting for one run: how long each stage took, and how long each sandbox
session (baseline, smoke run, validation run) took. One instance hangs off `RunLog.timing`;
the pipeline reports it at the end of the run and stores it in the history row."""

import time
from typing import Callable, Dict, Optional

RUNNING_STAGES = ("preflight", "pr", "review", "validate")


def fmt_duration(seconds: float) -> str:
    """42s, 8m20s, 1h2m3s (whole seconds)."""
    total = int(round(max(0.0, seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh%dm%ds" % (hours, minutes, secs)
    if minutes:
        return "%dm%ds" % (minutes, secs)
    return "%ds" % secs


class Timing:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.stages: Dict[str, float] = {}  # insertion order = order of first entry
        self.sandboxes: Dict[str, float] = {}
        self._current: Optional[str] = None
        self._since = 0.0

    def stage(self, name: str) -> None:
        """The run enters `name`: a running stage (RUNNING_STAGES) starts its clock, any other
        (terminal) stage only closes the running one. Re-entering the current stage is a no-op."""
        if name == self._current:
            return
        self.close()
        if name in RUNNING_STAGES:
            self._current = name
            self._since = self._clock()

    def close(self) -> None:
        """Stop the running stage's clock, if one is running."""
        if self._current is not None:
            elapsed = self._clock() - self._since
            self.stages[self._current] = self.stages.get(self._current, 0.0) + elapsed
            self._current = None

    def sandbox(self, label: str, seconds: float) -> None:
        """Record one sandbox session's wall time under its label (baseline, smoke-r1-1, ...)."""
        self.sandboxes[label] = self.sandboxes.get(label, 0.0) + max(0.0, seconds)

    def as_dict(self) -> dict:
        return {
            "stage_s": {name: round(secs, 1) for name, secs in self.stages.items()},
            "sandbox_s": {label: round(secs, 1) for label, secs in self.sandboxes.items()},
        }

    def summary(self) -> str:
        """`preflight 8m20s, pr 8s, review 6m1s; sandbox baseline 8m19s, smoke-r1-1 4m22s`."""
        parts = ["%s %s" % (name, fmt_duration(secs)) for name, secs in self.stages.items()]
        text = ", ".join(parts) if parts else "no stage ran"
        if self.sandboxes:
            text += "; sandbox " + ", ".join(
                "%s %s" % (label, fmt_duration(secs)) for label, secs in self.sandboxes.items()
            )
        return text

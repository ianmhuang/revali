"""The one way the fakes append to their call logs (REVALI_FAKE_LOG, REVALI_FAKE_WSL_CALLS):
revali.state.append_line, under the OS lock on `<log>.lock`. A plain `open(path, "a")` tears
lines on Windows when writers overlap, as the fake runner thread and a stub process do."""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from revali.state import append_line  # noqa: E402

# the lock is held for one write; a writer still waiting after this long writes anyway
WINDOW_S = 10.0


def append(path, line):
    append_line(path, line, retry_s=WINDOW_S)


def record(path, entry):
    append(path, json.dumps(entry))

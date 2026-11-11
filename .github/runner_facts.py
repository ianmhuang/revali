"""Print the CPU model and logical processor count of the machine, for the CI log.

The Windows leg's time varies with the runner it lands on, and the log names no machine
otherwise. Standard library only. Linux reads /proc/cpuinfo and Windows the registry (the
two CI platforms); anywhere else, or when that read fails, it falls back to
`platform.processor()` and then `unknown`, with the reason for the failure in parentheses.
It always exits 0, so it never fails a job.
"""

import os
import platform
import sys


def _primary_model() -> str:
    """The model from the OS's own record, "" when that record has none."""
    if sys.platform.startswith("linux"):
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        return ""
    if os.name == "nt":
        import winreg

        where = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, where) as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    return ""


def cpu_model() -> str:
    reason = ""
    try:
        model = _primary_model()
        if model:
            return model
    except Exception as exc:  # a fact we cannot read is reported, never raised
        reason = " (%s: %s)" % (type(exc).__name__, exc)
    try:
        fallback = platform.processor()
    except Exception:  # the fallback failing too leaves "unknown" and the first reason
        fallback = ""
    return (fallback or "unknown") + reason


def facts() -> str:
    try:
        return "cpu: %s; logical processors: %s; %s" % (
            cpu_model(),
            os.cpu_count() or "unknown",
            platform.platform(),
        )
    except Exception as exc:
        return "cpu: unknown (%s)" % exc


def main() -> int:
    try:
        print(facts(), flush=True)
    except Exception:  # stdout itself failing still must not fail the job
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Print the CPU model and logical processor count of the machine, for the CI log.

The Windows leg's time varies with the runner it lands on, and the log names no machine
otherwise. Standard library only; it prints `unknown` for anything it cannot read and always
exits 0, so it never fails a job.
"""

import os
import platform
import subprocess
import sys


def cpu_model() -> str:
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        elif os.name == "nt":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            )
            with key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        elif sys.platform == "darwin":
            res = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    except Exception:  # noqa: BLE001 - a fact we cannot read is "unknown", never an error
        pass
    return platform.processor() or "unknown"


def facts() -> str:
    try:
        return "cpu: %s; logical processors: %s; %s" % (
            cpu_model() or "unknown",
            os.cpu_count() or "unknown",
            platform.platform(),
        )
    except Exception as exc:  # noqa: BLE001
        return "cpu: unknown (%s)" % exc


def main() -> int:
    try:
        print(facts(), flush=True)
    except Exception:  # noqa: BLE001 - stdout itself failing still must not fail the job
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

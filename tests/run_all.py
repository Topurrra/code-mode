#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run every suite, in order, and print one line per suite.

    python tests/run_all.py            # demo, unit, end to end, mutation
    python tests/run_all.py --quick    # the same without the mutation check
    python tests/run_all.py --update-trace  # also rewrite docs/e2e-trace.txt

Run it before a commit. It exits 0 only if every suite passed. The suites run
one after the other, never in parallel: they start many short processes and
are not meant to load the machine. --update-trace is passed on to tests/e2e.py.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SUITES = [
    ("demo", ["main.py"], 300),
    ("unit", ["-m", "unittest", "discover", "-s", "tests"], 600),
    ("e2e", ["tests/e2e.py"], 3600),
    ("mutation", ["tests/mutation_check.py"], 4 * 3600),
]
E2E_FLAGS = ["--update-trace"]


def _summary(name: str, output: str) -> str:
    """The line of a suite's output that says how it went."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    wanted = {
        "demo": "checks passed",
        "unit": "Ran ",
        "e2e": "checks passed",
        "mutation": "planted bugs",
    }[name]
    hits = [line for line in lines if wanted in line]
    tail = ""
    if name == "unit" and lines:
        tail = "; " + lines[-1]
    return (hits[-1] if hits else (lines[-1] if lines else "no output")) + tail


def main() -> int:
    quick = "--quick" in sys.argv[1:]
    update_trace = "--update-trace" in sys.argv[1:]
    failed = []
    for name, argv, timeout in SUITES:
        if quick and name == "mutation":
            continue
        if name == "e2e" and update_trace:
            argv = argv + E2E_FLAGS
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable] + argv, cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
            code, output = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            code, output = 124, "timed out after %ds\n%s" % (timeout, exc.stdout or "")
        print("[%s] %-8s %s    (%.0fs)" % ("ok" if code == 0 else "FAIL", name, _summary(name, output), time.monotonic() - started))
        sys.stdout.flush()
        if code != 0:
            failed.append(name)
            bad = [line for line in output.splitlines() if "FAIL" in line or "SURVIVED" in line or "stale" in line or "Error" in line]
            for line in bad[:25]:
                print("    " + line.strip()[:400])
    print("\n%s" % ("all suites passed" if not failed else "FAILED: " + ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

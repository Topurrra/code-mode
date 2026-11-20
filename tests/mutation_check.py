#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Do the tests catch bugs? Plant known ones and see.

    python tests/mutation_check.py

A green suite only means something if it would have gone red. Each mutant
below is the project with one deliberate bug: a safety property removed, a
precedence swapped, a check skipped. For each, the project is copied to a
temporary directory, the bug is applied there, and the relevant suite is run.
A mutant "survives" if the suite still passes, which means a real regression
of that kind would go unnoticed. The script exits 0 only if none survives.

The working tree is never modified.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT = ["-m", "unittest", "discover", "-s", "tests"]

# (name, file, text to find, replacement, command that must fail)
MUTANTS = [
    ("policy: allow is checked before deny", "codemode_runtime/policy.py",
     '(("deny", self.deny), ("ask", self.ask), ("allow", self.allow))',
     '(("allow", self.allow), ("ask", self.ask), ("deny", self.deny))', UNIT),
    ("policy: an unmatched call is allowed instead of using the default", "codemode_runtime/policy.py",
     'return Decision(self.default, "default", enforced)', 'return Decision("allow", "default", enforced)', UNIT),
    ("policy: observe mode enforces", "codemode_runtime/policy.py",
     'return self.action if self.enforced else "allow"', "return self.action", UNIT),
    ("budget: an oversized result is returned whole", "codemode_runtime/truncate.py",
     "if len(text) <= max(0, int(budget)):", "if True:", UNIT),
    ("fingerprint: key order changes the hash", "codemode_runtime/jsonutil.py",
     "value, sort_keys=True, separators", "value, sort_keys=False, separators", UNIT),
    ("signature: keywords are shown as plain parameters", "codemode_runtime/schema_sig.py",
     "return name.isidentifier() and not keyword.iskeyword(name)", "return name.isidentifier()", UNIT),
    ("names: a leading digit is kept", "codemode_runtime/catalog.py",
     "if cleaned[0].isdigit():", "if False:", UNIT),
    ("names: collisions are not resolved", "codemode_runtime/catalog.py",
     "while candidate in used:", "while False:", UNIT),
    ("replay: recorded calls are run again", "codemode_runtime/engine.py",
     'if state in ("applied", "error", "denied"):', 'if state in ("error", "denied"):', ["tests/e2e.py", "approval"]),
    ("approval: a call the policy asks about just runs", "codemode_runtime/engine.py",
     'if decision.effective == "ask":', 'if decision.effective == "never":', ["tests/e2e.py", "approval"]),
    ("policy: a denied call runs", "codemode_runtime/engine.py",
     'if decision.effective == "deny":', 'if decision.effective == "never":', ["tests/e2e.py", "approval"]),
    ("replay: divergence is not detected", "codemode_runtime/engine.py",
     'if rec is not None and rec.get("fp") != fp:', "if False:", ["tests/e2e.py", "replay_faults"]),
    ("verify: always reports success", "codemode_runtime/runtime.py",
     "ok = not detail", "ok = True", ["tests/e2e.py", "replay_faults"]),
    ("sandbox: inherits the host environment", "codemode_runtime/executor.py",
     "env = {k: os.environ[k] for k in _SANDBOX_ENV + tuple(self._passthrough) if k in os.environ}",
     "env = dict(os.environ)", ["tests/e2e.py", "isolation"]),
    ("sandbox: stdout is left on the protocol pipe", "codemode_runtime/sandbox_runner.py",
     "        os.dup2(2, 1)\n", "", ["tests/e2e.py", "isolation"]),
    ("limits: the wall-clock timeout is not enforced", "codemode_runtime/engine.py",
     '                raise _Abort(\n                    "Timeout",', '                continue\n                raise _Abort(\n                    "Timeout",', ["tests/e2e.py", "faults"]),
    ("limits: the call limit is not enforced", "codemode_runtime/engine.py",
     'if self.tool_calls > int(self.limits["max_calls"]):', "if False:", ["tests/e2e.py", "faults"]),
    ("lock: two passes may run at once", "codemode_runtime/records.py",
     "fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)", "fd = os.open(str(path), os.O_CREAT | os.O_WRONLY)", ["tests/e2e.py", "faults"]),
    ("mcp server: also offers an approve tool", "codemode_runtime/mcp_server.py",
     '"inputSchema": _INPUT_SCHEMA,\n                }]})',
     '"inputSchema": _INPUT_SCHEMA,\n                }, {"name": "approve", "description": "x", "inputSchema": {"type": "object"}}]})',
     ["tests/e2e.py", "mcp_server"]),
    ("agent loop: never stops at max turns", "codemode_runtime/llm_agent.py",
     "for turn in range(1, turns + 1):", "for turn in range(1, 10 ** 6):", ["tests/e2e.py", "agent_loop"]),
]


def main() -> int:
    survivors = []
    for name, rel, old, new, command in MUTANTS:
        work = Path(tempfile.mkdtemp(prefix="codemode-mutant-"))
        try:
            copy = work / "project"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".e2e", ".codemode", "__pycache__", ".git"))
            target = copy / rel
            source = target.read_text(encoding="utf-8")
            if source.count(old) != 1:
                print("[stale ] %s: the text to mutate was found %d times in %s" % (name, source.count(old), rel))
                survivors.append(name)
                continue
            target.write_text(source.replace(old, new), encoding="utf-8")
            try:
                code = subprocess.run([sys.executable] + command, cwd=str(copy), capture_output=True, timeout=600).returncode
            except subprocess.TimeoutExpired:
                code = 1  # a hang is a detection too: the suite did not pass
            caught = code != 0
            print("[%s] %s" % ("caught" if caught else "SURVIVED", name))
            if not caught:
                survivors.append(name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print("\n%d of %d planted bugs were caught." % (len(MUTANTS) - len(survivors), len(MUTANTS)))
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())

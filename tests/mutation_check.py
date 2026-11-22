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
    ("hint: the approve command drops --home", "codemode_runtime/launcher.py",
     "    if home:\n", "    if False:\n", ["tests/e2e.py", "entry_points"]),
    ("entry: isolated mode cannot import the package", "main.py",
     "if _HERE not in sys.path:", "if False:", ["tests/e2e.py", "entry_points"]),
    ("policy: a rule in the server's own spelling is ignored", "codemode_runtime/policy.py",
     "return (path,) if not alias or alias == path else (path, alias)", "return (path,)", UNIT),
    ("policy: the gateway does not pass the server's spelling to the policy", "codemode_runtime/engine.py",
     "decision = self.policy.decide(entry.path, args, alias=entry.original_path)",
     "decision = self.policy.decide(entry.path, args)", ["tests/e2e.py", "policy_rules"]),
    ("policy: argument patterns always match", "codemode_runtime/policy.py",
     "if not found or not fnmatchcase(arg_text(value), pattern):", "if False:", UNIT),
    ("approval: an approval overrides a deny rule", "codemode_runtime/engine.py",
     'if decision.effective == "deny":', 'if decision.effective == "deny" and not approved:', ["tests/e2e.py", "policy_rules"]),
    ("approval: no terminal is needed to approve", "codemode_runtime/cli.py",
     'if not runtime.config.approval["require_terminal"]:', "if True:", ["tests/e2e.py", "policy_rules"]),
    ("approval: a pause never expires", "codemode_runtime/runtime.py",
     "if ttl > 0 and age > ttl and not stale_ok:", "if False:", ["tests/e2e.py", "policy_rules"]),
    ("search: a capped list hides how many matches there are", "codemode_runtime/catalog.py",
     "return [item[2] for item in scored[:limit]], len(scored)",
     "return [item[2] for item in scored[:limit]], min(limit, len(scored))", ["tests/e2e.py", "program_api"]),
    ("sandbox: outlives a host that was killed", "codemode_runtime/sandbox_runner.py",
     "                os._exit(70)  # the host is gone; there is nobody to report to",
     "                return  # the host is gone", ["tests/e2e.py", "program_api"]),
    ("failure: the variables of a failed program are returned whole, not as shapes", "codemode_runtime/runtime.py",
     "{name: shape_of(value, depth=2, keys=8) for name, value in sorted(kept.items())}",
     "{name: value for name, value in sorted(kept.items())}", ["tests/e2e.py", "program_api"]),
    ("repeat: failed runs are not reported", "codemode_runtime/sandbox_runner.py",
     "            if why is not None:", "            if False:", ["tests/e2e.py", "program_api"]),
    ("arguments: the program does not get them", "codemode_runtime/runtime.py",
     'start.get("state_in") or {}, start.get("args") or {}, self._helpers_of(start)', 'start.get("state_in") or {}, {}, self._helpers_of(start)',
     ["tests/e2e.py", "program_api"]),
    ("helpers: a paused execution resumes with the helper files as they are now", "codemode_runtime/runtime.py",
     '        digest = start.get("helpers_sha256")\n        if not digest:\n            return {}',
     "        return self._current_helpers()", ["tests/e2e.py", "helper_modules"]),
    ("helpers: files that start with an underscore are loaded too", "codemode_runtime/helpers.py",
     '        if name.startswith("_"):\n            continue', "", ["tests/e2e.py", "helper_modules"]),
    ("time limit: a limit set for one tool is ignored", "codemode_runtime/config.py",
     "if fnmatchcase(path, pattern) or (alias and fnmatchcase(alias, pattern)):", "if False:", UNIT),
    ("queue: max_concurrency is ignored", "codemode_runtime/engine.py",
     "gate = self.catalog.gate_for(entry)", "gate = None", ["tests/e2e.py", "calls"]),
    ("time limit: the program is killed at its limit with no chance to return what it has", "codemode_runtime/engine.py",
     "self.wind_down = time.monotonic() + _WIND_DOWN_S", "self.wind_down = 0.0", ["tests/e2e.py", "calls"]),
    ("reuse: a recorded result is handed out for a different request", "codemode_runtime/engine.py",
     'old is not None and old.get("fp") == fp and old.get("kind") == kind', 'old is not None and old.get("kind") == kind', ["tests/e2e.py", "calls"]),
    ("reuse: results of a tool the policy now denies are handed out", "codemode_runtime/engine.py",
     "and (kind != \"tool\" or self._still_allowed(name, args))", "", ["tests/e2e.py", "calls"]),
    ("reuse: a step that failed earlier is not tried again", "codemode_runtime/engine.py",
     'and old.get("kind") == kind and old.get("state") == "applied"', 'and old.get("kind") == kind and old.get("state") in ("applied", "error")',
     ["tests/e2e.py", "calls"]),
    ("large result: thrown away instead of saved", "codemode_runtime/engine.py",
     'saved = self.store.save_blob(self.exec_id, item["seq"], value)', "saved = None", ["tests/e2e.py", "calls"]),
    ("call limit: an oversized batch is expanded call by call", "codemode_runtime/engine.py",
     "        if len(calls) > limit:", "        if False:", ["tests/e2e.py", "calls"]),
    ("check: the program is run", "codemode_runtime/sandbox_runner.py",
     '        if job.get("check"):', "        if False:", ["tests/e2e.py", "calls"]),
    ("long run: an execution whose process was killed stays running for ever", "codemode_runtime/runtime.py",
     'if view.status != "running" or self.store.holder(exec_id) is not None:', "if True:", ["tests/e2e.py", "long_runs"]),
    ("cancel: a request from another process is ignored", "codemode_runtime/runtime.py",
     "                if self.store.cancel_requested(exec_id):", "                if False:", ["tests/e2e.py", "long_runs"]),
    ("live output: persisted without a bound", "codemode_runtime/engine.py",
     'if self.mode != "live" or self._live_total >= _LIVE_LOG_CHARS:', 'if self.mode != "live":', ["tests/e2e.py", "long_runs"]),
    ("log: a half-written last line breaks reading", "codemode_runtime/records.py",
     "                if number == len(lines) - 1:", "                if False:", UNIT),
    ("secrets: a credential a server echoes back reaches the program", "codemode_runtime/engine.py",
     'value, _ = redact(connector.call(entry.original, item["args"], ctx), connector.secrets())',
     'value = connector.call(entry.original, item["args"], ctx)', ["tests/e2e.py", "servers"]),
    ("mcp: a server that exited is not started again", "codemode_runtime/connectors/mcp_stdio.py",
     "if proc is not None and proc.poll() is None and not self._box.closed:", "if proc is not None:", ["tests/e2e.py", "servers"]),
    ("mcp: the server is not told when a call is given up on", "codemode_runtime/connectors/mcp_stdio.py",
     'self._notify("notifications/cancelled", {"requestId": request["id"], "reason": "timeout"})', "pass", ["tests/e2e.py", "servers"]),
    ("mcp: an answer from a dead server process reaches a new request", "codemode_runtime/connectors/mcp_common.py",
     "            if self.closed:\n                slot[\"event\"].set()\n                return slot\n", "", UNIT),
    ("schema: a schema of any size is returned whole", "codemode_runtime/catalog.py",
     "    if size <= budget:\n        return schema, 0", "    if True:\n        return schema, 0", UNIT),
    ("cache: a cached tool list survives an edit of the server's script", "codemode_runtime/catalog.py",
     'if entry["stamp"] != stamp or not fresh or not isinstance(entry["tools"], list):',
     'if not fresh or not isinstance(entry["tools"], list):', ["tests/e2e.py", "layout"]),
    ("config search: it leaves the repository", "codemode_runtime/config.py",
     'if (folder / ".git").exists() or folder == home or folder.parent == folder:', "if folder == home or folder.parent == folder:", UNIT),
    ("remote: the session id is not sent after the handshake", "codemode_runtime/connectors/mcp_http.py",
     '        if self._session:\n            headers["Mcp-Session-Id"] = self._session', "        if False:\n            pass",
     ["tests/e2e.py", "remote"]),
    ("sse: the data lines of one event are not joined", "codemode_runtime/connectors/mcp_http.py",
     "            data.append(value)", "            data = [value]", UNIT),
    ("commands: the caller may set any environment variable", "codemode_runtime/connectors/commands.py",
     "refused = sorted(k for k in env if not any(fnmatchcase(k, pattern) for pattern in allowed))", "refused = []", UNIT),
    ("commands: every command gets the whole host environment", "codemode_runtime/connectors/commands.py",
     "env = {k: v for k, v in os.environ.items() if k in BASE_ENV or any(fnmatchcase(k, pattern) for pattern in inherit or [])}",
     "env = dict(os.environ)", ["tests/e2e.py", "commands"]),
    ("commands: a command over its time limit leaves its children running", "codemode_runtime/connectors/commands.py",
     "kill_tree(proc)  # the command and everything it started", "proc.kill()  # the command only", ["tests/e2e.py", "commands"]),
    ("commands: output that was cut is not marked as cut", "codemode_runtime/connectors/commands.py",
     '"truncated": bool(truncated),', '"truncated": False,', ["tests/e2e.py", "commands"]),
    ("commands: a list parameter is not split into arguments", "codemode_runtime/connectors/commands.py",
     "            return [_text(item) for item in values[whole.group(1)]]", "            pass", UNIT),
    ("init: a credential written in a server list is copied into the config", "codemode_runtime/project.py",
     "        elif looks_secret(key):", "        elif False:", UNIT),
    ("init: the runtime's own server becomes a connector", "codemode_runtime/project.py",
     "if isinstance(raw, dict) and _is_this_runtime(raw):", "if False:", UNIT),
    ("init: the starting policy allows every tool of an annotated server", "codemode_runtime/project.py",
     'for t in annotated if t["annotations"].get("readOnlyHint") is True)', "for t in annotated)", UNIT),
    ("doctor: a deny rule that matches no tool is not reported", "codemode_runtime/project.py",
     '    for action in ("deny", "ask", "allow"):\n        for raw in getattr(policy, action):', '    for action in ("ask", "allow"):\n        for raw in getattr(policy, action):', UNIT),
    ("prune: an execution that a live process is running is removed", "codemode_runtime/records.py",
     '            if status == "running" and (self.holder(exec_id) is not None or idle < 3600):\n                continue', "", UNIT),
    ("prune: a paused execution is removed to make room", "codemode_runtime/records.py",
     '            if status == "paused" and not too_old:\n                continue', "", UNIT),
    ("shared folder: last is somebody else's execution", "codemode_runtime/records.py",
     '            if agent and agent != "anonymous":', "            if False:", UNIT),
    ("lock: a lock that cannot be deleted at once is left behind", "codemode_runtime/records.py",
     "        except OSError:\n            time.sleep(0.005 if attempt < 20 else 0.02)", "        except OSError:\n            return", UNIT),
    ("mcp server: a program that runs for seconds blocks every other request", "codemode_runtime/mcp_server.py",
     "                worker.start()", "                worker.run()", ["tests/e2e.py", "mcp_tool"]),
    ("config: a literal env value is printed", "codemode_runtime/config.py",
     'shown[key] = {k: (v if _ENV_REF.search(v) else "(hidden)") for k, v in spec[key].items()}',
     "shown[key] = dict(spec[key])", UNIT),
]


def main() -> int:
    survivors = []
    for name, rel, old, new, command in MUTANTS:
        work = Path(tempfile.mkdtemp(prefix="codemode-mutant-"))
        try:
            copy = work / "project"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(
                ".e2e", ".codemode", "__pycache__", ".git", ".venv*", ".e2e-tools", "build", "*.egg-info", "node_modules"))
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

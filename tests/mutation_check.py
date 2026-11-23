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

import os
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
    ("commands: every command gets the whole host environment", "codemode_runtime/connectors/mcp_stdio.py",
     "        elif inherit is True or any(fnmatchcase(name, pattern) for pattern in wanted):", "        elif True:",
     ["tests/e2e.py", "commands"]),
    ("servers: every stdio server gets the whole host environment", "codemode_runtime/connectors/mcp_stdio.py",
     "        elif inherit is True or any(fnmatchcase(name, pattern) for pattern in wanted):", "        elif True:",
     ["tests/e2e.py", "servers"]),
    ("inherit_env: a variable marked to be left out is passed on", "codemode_runtime/connectors/mcp_stdio.py",
     "        if any(fnmatchcase(name, pattern) for pattern in left_out):\n            continue", "        if False:\n            continue",
     ["tests/e2e.py", "servers"]),
    ("servers: a credential inherited from the host is not cleaned from what the server returns", "codemode_runtime/connectors/mcp_stdio.py",
     "        self._secrets = named + inherited", "        self._secrets = named", ["tests/e2e.py", "servers"]),
    ("inherit_env: an inherited variable is never taken for a credential", "codemode_runtime/connectors/mcp_stdio.py",
     "            if looks_secret(key):\n                secrets.append(value)", "            pass", UNIT),
    ("inherit_env: a pattern is compared in the wrong case for the platform", "codemode_runtime/connectors/mcp_stdio.py",
     "    fold = (lambda text: text.upper()) if IS_WINDOWS else (lambda text: text)",
     "    fold = (lambda text: text.upper()) if not IS_WINDOWS else (lambda text: text)", UNIT),
    ("commands: a command over its time limit leaves its children running", "codemode_runtime/connectors/commands.py",
     "kill_tree(proc)  # the command and everything it started", "proc.kill()  # the command only", ["tests/e2e.py", "commands"]),
    ("commands: output that was cut is not marked as cut", "codemode_runtime/connectors/commands.py",
     '"truncated": bool(truncated),', '"truncated": False,', ["tests/e2e.py", "commands"]),
    ("commands: a list parameter is not split into arguments", "codemode_runtime/connectors/commands.py",
     "            return [_text(item) for item in values[whole.group(1)]]", "            pass", UNIT),
    ("commands: the program to run may be chosen by the caller", "codemode_runtime/connectors/commands.py",
     "        if _references(command[0]):", "        if False:", UNIT),
    ("init: a credential written in a server list is copied into the config", "codemode_runtime/project.py",
     "        elif looks_secret(key) or value_looks_secret(value):", "        elif False:", UNIT),
    ("init: a credential under a name that says nothing is copied into the config", "codemode_runtime/project.py",
     "looks_secret(key) or value_looks_secret(value)", "looks_secret(key)", UNIT),
    ("a credential a server printed while failing to start reaches the program", "codemode_runtime/catalog.py",
     "                message = redact(exc.message, connector.secrets())[0]", "                message = exc.message", UNIT),
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
MUTANTS += [
    ("hints: the command the project set is ignored", "codemode_runtime/launcher.py",
     "    if own is not None:\n        return", "    if False:\n        return", ["tests/e2e.py", "entry_points"]),
    ("hints: --config and --home are added to a command that has no place for them", "codemode_runtime/launcher.py",
     'return _OPTIONS.sub(lambda match: (" " + options) if options else "", text).strip()',
     'return (_OPTIONS.sub("", text).strip() + " " + options).strip()', ["tests/e2e.py", "entry_points"]),
    ("search --full: descriptions are still cut to one line", "codemode_runtime/catalog.py",
     "        shown = _whole if full else _summary", "        shown = _summary", ["tests/e2e.py", "servers"]),
    ("init --out: a person is told to pass --config under a command that finds the config by itself", "codemode_runtime/project.py",
     "    if out and not launcher.chooses_config_itself():", "    if out:", ["tests/e2e.py", "new_project"]),
    ("parallel: on_error is accepted and ignored", "codemode_runtime/sandbox_runner.py",
     '        return_errors = on_error == "return"', "        pass", ["tests/e2e.py", "program_api"]),
    ("a program that is resumed is told the folder of whoever resumed it", "codemode_runtime/runtime.py",
     '                str(start.get("project_root") or ""),', "                launcher._portable(os.path.abspath(str(Path.cwd()))),",
     ["tests/e2e.py", "program_api"]),
    ("a project file that is not found by a relative path gets no hint", "codemode_runtime/sandbox_runner.py",
     "        hint = _relative_path_hint(exc)", '        hint = ""', UNIT),
    ("init: the runtime's own server becomes a connector when the project starts it under another name", "codemode_runtime/project.py",
     ' or launcher.is_own_command(parts[0], parts[1:])', "", UNIT),
    ("search: the namespace asked for is ignored", "codemode_runtime/catalog.py",
     "        if namespace is not None:\n            return self._search_namespace(str(namespace), wanted, limit, shown)",
     "        if False:\n            pass", ["tests/e2e.py", "program_api"]),
    ("search: a search with no namespace is recorded differently than it was", "codemode_runtime/engine.py",
     'args = {"limit": limit} if namespace is None else {"limit": limit, "namespace": namespace}',
     'args = {"limit": limit, "namespace": namespace}', ["tests/e2e.py", "program_api"]),
    ("init: .gitignore gets the default names whatever folders the project uses", "codemode_runtime/project.py",
     '"gitignore": _update_gitignore(folder, ignore_entries(folder, data_home, config.work_dir)),',
     '"gitignore": _update_gitignore(folder, list(IGNORE_ENTRIES)),', ["tests/e2e.py", "new_project"]),
    ("init --out: the commands it prints do not name the config it wrote", "codemode_runtime/cli.py",
     "        launcher.set_global_options(args.out, home)", "        pass", ["tests/e2e.py", "new_project"]),
    ("instructions: the default folders are named whatever the project uses", "codemode_runtime/cli.py",
     "runtime.config.work_dir, data, runtime.guidance()", '".codemode-work", ".codemode", runtime.guidance()', ["tests/e2e.py", "new_project"]),
    ("guidance: the instructions leave the project's own text out", "codemode_runtime/cli.py",
     'data, runtime.guidance()["text"])', 'data, "")', ["tests/e2e.py", "new_project"]),
    ("guidance: a text over the limit is added whole", "codemode_runtime/runtime.py",
     "        if total > budget:", "        if False:", ["tests/e2e.py", "new_project"]),
    ("guidance: a file that cannot be read costs the agent the tool", "codemode_runtime/runtime.py",
     "        except ConfigError as exc:", "        except KeyError as exc:", ["tests/e2e.py", "new_project"]),
    ("approval in advance: one for a single tool covers every tool", "codemode_runtime/records.py",
     '            if path not in grant["tools"] and "*" not in grant["tools"]:', "            if False:", ["tests/e2e.py", "policy_rules"]),
    ("approval in advance: it never expires", "codemode_runtime/records.py",
     "            if ttl_s > 0 and (given is None or now - given > ttl_s):", "            if False:", UNIT),
    ("approval in advance: the gateway is not told when approvals expire", "codemode_runtime/runtime.py",
     'standing_ttl_s=float(self.config.approval["ttl_s"]),', "standing_ttl_s=0.0,", ["tests/e2e.py", "policy_rules"]),
    ("approval in advance: a call that runs under it leaves no approval of its own in the log", "codemode_runtime/engine.py",
     '"type": "approval", "seqs": [seq], "by": grant.get("by"), "via": "standing", "granted": grant.get("ts"),',
     '"type": "log", "pass": self.pass_no, "text": "",', ["tests/e2e.py", "policy_rules"]),
    ("approval in advance: it covers a call a deny rule forbids", "codemode_runtime/engine.py",
     'if decision.effective == "deny":',
     'if decision.effective == "deny" and self.view.standing_for(entry.path, self.standing_ttl_s) is None:', ["tests/e2e.py", "policy_rules"]),
    ("approval in advance: a wider one than the project allows is accepted", "codemode_runtime/runtime.py",
     '            if allowed == "off" or (standing == "all" and allowed != "all"):', "            if False:", ["tests/e2e.py", "policy_rules"]),
    ("ignore entries: a folder outside the project is listed as if it were inside", "codemode_runtime/project.py",
     "    except ValueError:\n        return None\n    return None if inside in", "    except ValueError:\n        return Path(str(folder)).name\n    return None if inside in", UNIT),
]

# An HTTP API as tools: where requests go, what a caller can set, where the credentials go.
_API = "codemode_runtime/connectors/openapi.py"
_API_E2E = ["tests/e2e.py", "http_api"]
MUTANTS += [
    ("http api: the name of a tool does not start with its method", _API,
     '    return "%s_%s" % (method.lower(), words or "root")', '    return words or "root"', UNIT),
    ("http api: a path value is not kept to one segment", _API,
     'return urllib.parse.quote(text, safe="")', 'return urllib.parse.quote(text, safe="/?#")', UNIT),
    ("http api: a path value of .. is sent", _API,
     '    if text in ("", ".", ".."):', "    if False:", UNIT),
    ("http api: a path that does not start with a slash becomes a tool", _API,
     '        if not path.startswith("/") or not isinstance(item, dict):', "        if not isinstance(item, dict):", UNIT),
    ("http api: a document fetched from one host may name another host for the API", _API,
     "    if _origin(resolved) != _origin(source):", "    if False:", UNIT),
    ("http api: a header the config sets is offered as a parameter", _API,
     "    no_headers = _RESERVED_HEADERS | {str(h).lower() for h in hidden_headers}", "    no_headers = set()", UNIT),
    ("http api: a header value with a line break is accepted", _API,
     '            if "\\r" in text or "\\n" in text:', "            if False:", UNIT),
    ("http api: a query value the config sets is offered as a parameter", _API,
     "    no_query = set(hidden_query)", "    no_query = set()", UNIT),
    ("http api: a query value the config sets can be given inside an object", _API,
     "    taken = sorted({key for key, _ in query if key in constant})", "    taken = []", UNIT),
    ("http api: an answer that was cut at the limit is passed on as whole", _API,
     '    return b"".join(chunks)[:limit], True', '    return b"".join(chunks)[:limit], False', UNIT),
    ("http api: a redirect is followed, with the credentials", _API,
     "    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:\n        return None",
     "    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:\n"
     "        return super().redirect_request(req, fp, code, msg, headers, newurl)", _API_E2E),
    ("http api: the credentials are sent with the request for the document", _API,
     'request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "codemode-runtime/%s" % __version__})',
     'request = urllib.request.Request(url, headers=dict(self._credentials()[0], Accept="application/json"))', _API_E2E),
    ("http api: a credential the API echoes back reaches the program", _API,
     "                self._headers, self._query, self._secrets = headers, query, secrets + more",
     "                self._headers, self._query, self._secrets = headers, query, []", _API_E2E),
    ("http api: an edited document is served from the cached tool list", _API,
     "                seen = [info.st_size, info.st_mtime_ns]", "                seen = []", _API_E2E),
]

# A bug in a line that only one platform runs can be caught only there.
# Planted anywhere else it would survive, and say nothing about the suites.
if os.name == "nt":
    MUTANTS.append(
        ("approval: the null device passes for a terminal", "codemode_runtime/procutil.py",
         "return bool(kernel32.GetConsoleMode(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(mode)))",
         "return True", ["tests/e2e.py", "policy_rules"]))


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

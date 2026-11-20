# Verification report

Phase 5 of the build brief: what was run, what it found, and the two
checklists. Everything here refers to the code as delivered.

## Results

| Suite | What it is | Result |
|---|---|---|
| `python tests/e2e.py` | 125 checks, each on the real CLI started as a subprocess; 103 traced commands | 125 of 125 |
| `python -m unittest discover -s tests` | 49 cases for the pure modules, derived from `tests/FAILURE_MODES.md` | 49 of 49 |
| `python tests/mutation_check.py` | 20 deliberate bugs, one at a time, in a copy of the project | 20 of 20 caught |
| `python main.py` | the zero-config demo, which checks itself | 13 of 13, exit 0 |

Environments, all on the delivered code:

| Host | End to end | Unit | Skipped there |
|---|---|---|---|
| Windows 11, Python 3.15.2, with the official MCP SDK 2.3.0 for interop | 125 of 125 | OK | nothing |
| Windows 11, Python 3.11.18 | 122 of 122 | OK | SDK interop (no SDK given) |
| Windows 11, Python 3.14.10 | 122 of 122 | OK | SDK interop |
| Linux (WSL2 Ubuntu), Python 3.14.4 | 121 of 121 | OK | SDK interop; second-interpreter sandbox (only one Python installed) |

The trace of the first row is [`e2e-trace.txt`](e2e-trace.txt): every command,
its exit code and its output. To replay, run `python tests/e2e.py` from the
project root; it regenerates the trace and the working files under `.e2e/`.
Individual commands in the trace can be pasted and run in order, because they
use relative paths and the `last` alias instead of generated ids.

## What the end-to-end suite covers

Each check quotes the invariant or failure mode it covers. By scenario:

| Scenario | Checks | What is established |
|---|---|---|
| A. Demo, help, configuration | 23 | The demo passes; ten kinds of bad config are rejected with exit 2 and a message naming the problem; the shipped example config loads; the README's quick-start commands behave as written. |
| B. Discovery | 4 | Search, describe, close-name suggestions; the tool description is 1,454 characters and contains no tool schema. |
| C. What a program can do | 17 | Eleven calls in one program; bodies stay in the sandbox; state across programs and per session; error payloads with program-only tracebacks; hints for `return` and `await`; repr fallback; result budget. |
| D. The sandbox boundary | 4 | Host environment variables are absent in the sandbox; frame-shaped prints, raw descriptor writes and `input()` cannot corrupt or forge the protocol; a 2 MB print flood is bounded. |
| E. Policy, approval, replay | 18 | Pause at exit 3 with no side effect; approved call runs once and earlier calls are replayed (counted in files outside the process); reject; deny; batch pause; interactive approval; observe mode. |
| F. Replay under nondeterminism | 5 | Divergence is detected; `step()` fixes it; a tool call inside `step()` is refused; `verify` exits 4 on an unexplained result; 25 seeded random programs all replay to the same result. |
| G. Injected faults | 12 | Endless loop, stalled tool, dead sandbox, oversized result and arguments, call limit, four broken executors, a concurrent second pass. |
| H. Another interpreter | 1 | The sandbox runs under Python 3.11 while the host is 3.15, through `executor.command`. |
| I. MCP server | 14 | One tool only; payload shape; pause, approve from the CLI, fetch by id; protocol errors; cancellation. |
| J. MCP servers as tool sources | 10 | A tool on an MCP server called from a program; three kinds of unavailable server; the official SDK's client driving this server; this client driving a server built with the official SDK. |
| K. Snippets | 6 | Promote, find, run with arguments; refusal to promote an unfinished execution. |
| L. Model loop | 9 | A task run to a final answer against a scripted OpenAI-compatible endpoint; exactly one function offered; retry on 429; no retry on 401; malformed arguments; stop at max turns; refusal to start unconfigured. |
| M. Every log | 2 | All 85 execution logs written during the run: one start, at most one end, nothing after it, approval before any asked call, a write-ahead entry before any applied call. |

## What verification found

Bugs in the application, found by running it and fixed in the code:

1. **Search missed the obvious.** `search("notes")` did not return
   `read_note`, because only exact word matches counted. Found by the demo's
   own first check. Fixed in `catalog._score`.
2. **`last` could name the wrong execution.** Ids had one-second resolution,
   so two executions started in the same second sorted by their random
   suffix. Found when `get last --full` returned a different execution. Ids
   now carry microseconds (`records.ExecutionStore.new_id`).
3. **A sandbox that never read its input hung the host past every timeout.**
   The host wrote the runner on the calling thread; with a full pipe the write
   blocked for as long as the child lived (60 s in the test, against a 2 s
   limit). Writes now happen on a writer thread
   (`executor.SandboxProcess._write_frames`).
4. **Startup failures were misreported** as `SandboxCrashed` instead of
   `ProtocolError` or `ExecutorStartFailed`, depending on a race. Fixed by the
   same change: a failed write is silent and the reader reports what the
   process actually did.
5. **A syntax error showed the parser's stack frame**, with a path into the
   host's standard library, instead of only the program's line. Fixed in
   `sandbox_runner._describe_error`.
6. **A program could not be promoted to a snippet without being rewritten**,
   because snippet arguments were named `input`, which shadows a builtin that
   ordinary programs do not have as a dict. Renamed to `args`, which is
   defined (empty) in every program.
7. **A key exported for another provider would have been sent to the
   configured endpoint**, through an `OPENAI_API_KEY` fallback. Found on
   review of the agnosticism checklist, not by a test. The fallback is gone
   and a unit case pins that.

One test expectation was wrong and was corrected, which is different from
bending a test to pass. The interop check expected a tool built with the
official SDK to return a bare `42`. The server publishes an output schema of
`{"result": integer}` and sends `{"result": 42}`; the runtime returns exactly
that, and `describe()` shows `-> dict`. The expectation encoded a guess about
a third party. The code was left alone, the expectation was fixed, and a check
was added that the value matches the published schema.

## Checklist 1: principles

Numbers refer to the Master Principle List in [`DESIGN.md`](DESIGN.md).
"Checked by" names scenarios in `e2e.py` or classes in `test_pure_logic.py`.

| # | Principle | Status | Where | Checked by |
|---|---|---|---|---|
| P1 | One outer tool; the model writes a program | implemented | `mcp_server.McpServer._handle` (`tools/list`), `llm_agent.run_agent` | I, L, mutant "also offers an approve tool" |
| P2 | Only the final value returns; budgets, marked truncation | implemented | `sandbox_runner._exec_block`, `truncate.budget_result`, `truncate.BoundedLog` | C, D, `OutputBudgets` |
| P3 | Discovery at run time | implemented | `catalog.Catalog.search`, `.describe`; `engine.Pass._on_search` | B, J |
| P4 | Signatures from JSON Schema, degrade to `Any` | implemented | `schema_sig.signature`, `type_of` | B, J, `SchemaSignature` |
| P5 | Chained and parallel calls | implemented | `sandbox_runner.parallel`, `engine.Pass._on_call` | C, E |
| P6 | Separate credential-free executor, pluggable; honest about its limit | implemented | `executor.SubprocessExecutor.start`, `.argv`; README "The safety model, and its limit" | D, G, H |
| P7 | Per-call policy on the host; a refused call fails the call | implemented | `policy.Policy.decide`, `engine.Pass._classify` | E, `PolicyDecisions` |
| P8 | Hard-to-undo actions wait for a human, out of the agent's reach | implemented | `engine.Pass._on_call` (pause), `runtime.Runtime.approve`, `.reject` | E, I |
| P9 | Deterministic replay, divergence, `step()` | implemented | `engine.Pass._next`, `._on_step`; `sandbox_runner.step` | E, F |
| P10 | Append-only log outside the sandbox, per-operation telemetry | implemented | `records.ExecutionStore.append`, `engine.Pass._append` | E, M |
| P11 | Identity stamped by the gateway | implemented | `config.resolve_agent`, `runtime.Runtime.execute` (`start` entry) | `show last` output in E |
| P12 | Explicit bounds | implemented | `config.DEFAULT_LIMITS`, `engine.Pass._recv`, `._size`, `._classify` | G |
| P13 | Fail closed; failure as data; one terminal outcome | implemented | `policy.policy_from_dict` (default `ask`), `runtime.Runtime._payload`, `records.ExecutionView` | E, I, M |
| P14 | State in the runtime, not the context | implemented | `sessions.SessionStore.apply_delta`, `runtime.Runtime._state_delta` | C |
| P15 | Reusable code, promoted by the host | implemented | `runtime.Runtime.save_snippet`, `sandbox_runner.run` | K |
| P16 | End-to-end verification, replayable trace, provenance stamp | implemented | `tests/e2e.py`, `docs/e2e-trace.txt`, `runtime.Runtime.verify`, `records.ExecutionView.verification` | E, F |
| P17 | The gate is out of reach of the work | implemented, within the limit stated under P6 | no sandbox function or MCP tool for policy, approvals or records | I, K |
| P18 | Observe before enforce | implemented | `policy.Decision.effective`, `Policy.mode` | E, `PolicyDecisions` |
| P19 | Invariants first; property and differential tests | implemented | `tests/FAILURE_MODES.md`; property loop in `replay_faults`; `verify` | F, M |
| P20 | Deliberate fault injection | implemented | `connectors/chaos.py`, scenario G | G |
| P21 | Legible, few dependencies | implemented | standard library only: 23 files scanned, no non-stdlib import | import scan |
| P22 | Small static tool description | implemented | `runtime._TOOL_DESCRIPTION`, `Runtime.tool_description` (1,454 chars) | B |
| P23 | Output cannot corrupt framing | implemented | `sandbox_runner._Channel.__init__` | D |
| P24 | Provider-neutral model layer with a scripted stand-in | implemented | `config._llm`, `llm_agent.resolve_llm`, `._chat`; `tests/fixtures/stub_openai.py` | L |
| P25 | A running execution can be stopped | implemented for MCP cancellation and timeouts | `engine.Pass._recv` (cancel event), `mcp_server.McpServer._read` | I |
| P26 | Bounded attempts, then hand back | implemented | `llm_agent.run_agent` (`max_turns`) | L |

Deferred:

| What | Reason |
|---|---|
| Rollback of applied calls (CF) | Needs a compensating action per tool; MCP defines none. Reject stops, it does not undo. |
| Background executions and wake-ups (CC, UH) | A pass is foreground and bounded by design; this would need a daemon. |
| Live objects and processes across executions (UH) | Not replayable and not portable to a container executor. `state` holds JSON only. |
| Model-based review of code or actions (CC, MA) | Would put a model in the safety path. The audit log is the integration point. |
| Multi-agent coordination: claims, crews, merge gates (MA) | The runtime is a shared, safe substrate; orchestration is another application. |
| Diff-triggered checklists, reviewer evaluation (VER) | About reviewing code changes in a repository, not about a tool runtime. |
| Stopping a CLI execution by hand, other than Ctrl-C | Ctrl-C ends the CLI; the log then shows the execution as `running` until someone rejects it. |
| MCP over HTTP | Only stdio was built, to keep to the standard library and one tested transport. |

## Checklist 2: agnosticism

The nine portability constraints of the brief, each with evidence.

| # | Constraint | Holds | Evidence |
|---|---|---|---|
| 1 | No vendor lock-in | yes | No SDK is imported: an AST scan of `main.py` and the 22 package files finds only standard-library imports. A search of the code path for vendor and model names finds none; "OpenAI" appears only as the name of the wire format (`llm_agent.py`, `config._llm`, `cli.py` help text). |
| 2 | Open standards first | yes | Capabilities are exposed over MCP stdio (`mcp_server.McpServer`) and a CLI (`cli.build_parser`). Tools are consumed over MCP stdio (`connectors/mcp_stdio.McpStdioConnector`). Both directions were exercised against the official MCP SDK (scenario J). |
| 3 | Configurable provider layer | yes | `config._llm` reads `base_url`, `model`, `api_key` from the config or `CODEMODE_BASE_URL`, `CODEMODE_MODEL`, `CODEMODE_API_KEY`; `llm_agent._chat` posts to `<base_url>/chat/completions` with `urllib`. Scenario L runs it against a local scripted endpoint and asserts the model name, path and bearer header that were sent. |
| 4 | Standard, boring tech | yes | Python, standard library, no build step. Passed on Python 3.11, 3.14 and 3.15 on Windows and 3.14 on Linux. The sandbox ran under a different interpreter than the host (scenario H). |
| 5 | One-command run | yes | `python main.py` runs the demo and exits 0 (first check of scenario A). `requirements.txt` is empty, so `pip install -r requirements.txt && python main.py` works and installs nothing. No account, key or network is needed. |
| 6 | Human- and agent-readable | yes | `README.md` has purpose, setup, run, test and extend sections. Exit codes are fixed in `errors.py` and asserted throughout the suite. Every command takes `--json` (`cli.build_parser`, the `common` parent parser). Module docstrings say why each part exists. |
| 7 | Agent-workable codebase | yes | 22 modules, one responsibility each (table in the README); the pure ones have no I/O. Errors carry `type`, `message` and `hint` (`errors.CodemodeError.to_dict`). `AGENTS.md` states the rules for changing the code. |
| 8 | Demo by default | yes | `demo.run_demo`: seven steps, thirteen self-checks, no configuration, no model. |
| 9 | License and attribution | yes | `LICENSE` (MIT); an SPDX line at the top of every source file; the six sources are cited in the README under "Where the design comes from". |

## Not verified

Stated so that nobody assumes otherwise:

- **Python below 3.11.** The code avoids newer syntax and `main.py` accepts
  3.9, but nothing older than 3.11 was available to run it.
- **macOS.** Linux and Windows were run. macOS shares the POSIX code path with
  Linux and was not run.
- **A container executor.** `executor.command` was tested with a second
  interpreter and with four deliberately broken commands, not with Docker,
  which is not installed here. The Docker line in the README is an example.
- **Third-party MCP servers other than the SDK fixture.** The filesystem
  server in `codemode.example.json` was not run; the file was only checked to
  be a valid config.
- **A real model.** The loop was driven by a scripted endpoint that speaks the
  same wire format. No model's behaviour with the `execute` tool was measured,
  so there is no claim here about token savings in practice. The byte counts
  the runtime reports are measurements of the demo, not of a model session.
- **Hostile code.** Nothing here claims the default sandbox contains a program
  that is trying to escape. It is a process boundary; see the README.

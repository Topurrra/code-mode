# Verification report

What was run, where, and what it found. Everything here refers to the code as
committed. The first part is the state after the first field report
(version 0.2.0); the report on the original build (0.1.0) follows it.

## Results

| Suite | What it is | Result |
|---|---|---|
| `python tests/e2e.py` | 333 checks, each on the real CLI started as a subprocess | 333 of 333 |
| `python -m unittest discover -s tests` | 141 cases: pure logic with many inputs, and the states a command line cannot produce (`test_isolated.py`) | 141 of 141 |
| `python tests/mutation_check.py` | 76 deliberate bugs, one at a time, in a copy of the project (75 off Windows: one is in a line only Windows runs) | 76 of 76 caught |
| `python main.py` | the zero-config demo, which checks itself | 13 of 13, exit 0 |

`python tests/run_all.py` runs all four. It was run before every commit, on
a copy of exactly what was about to be committed.

| Before this round | After |
|---|---|
| 121 end-to-end checks (125 with the optional SDK scenarios) | 333 |
| 49 unit cases | 141 |
| 20 mutants | 76 |

## Platforms

| Host | Demo | Unit | End to end | Mutation | Not run there |
|---|---|---|---|---|---|
| Windows 11, Python 3.13.15 | 13 of 13 | 141 (1 skipped) | 333 of 333 | 76 of 76 | Ctrl-C on a foreground run (a console event cannot be sent to one child process); `approve` at a real terminal (a console cannot be given to a child process without a window); a second interpreter for the sandbox (one Python is installed) |
| Linux (Ubuntu 24.04 in a container, kernel 7.0), Python 3.12.3 | 13 of 13 | 141 | 325 of 325 | not run | the optional scenarios that need the MCP SDK, node or the network; a second interpreter |
| Linux (the same), host on Python 3.9.25, sandbox under Python 3.8.20 | 13 of 13 | 141 | 326 of 326 | not run | the same optional scenarios |
| macOS | | | | | everything: no machine was available |

Notes on the rows:

- **Windows** is where the work was done. The optional scenarios ran there:
  the official MCP Python SDK 2.3.0 (both directions), the reference
  filesystem server 2.4.0 under node 26, and `pip install` of this project
  into a fresh virtual environment, with a run through the installed
  `codemode` entry point. The tooling lives inside the project folder
  (`.venv-e2e/`, `.e2e-tools/`), not on the machine.
- **Linux** ran in a container that is thrown away afterwards. The source
  went in as a tar stream; Python was installed inside the container. This
  run found two real problems that Windows did not show, both fixed and both
  now in `tests/FAILURE_MODES.md`: C11 (a frame sent before the sandbox was
  ready could be swallowed by the loader's buffer, which hung `--check`) and
  R11 (a process that exited and was never collected still looked alive).
- **Python 3.9 with a 3.8 sandbox** is the oldest combination the code
  claims: the host needs 3.9, and `sandbox_runner.py` is written in syntax
  3.8 accepts. Before this round nothing below 3.11 had been run. The first
  run there found one more bug (E7): `init` could not write the config under
  3.9. The row shows the run after the fix.
- **To repeat a Linux row**: copy the project into a fresh `ubuntu:24.04`
  container, install `python3` (or `python3.9` and `python3.8` from the
  deadsnakes archive) and `git`, and run `python3 tests/run_all.py --quick`
  as an ordinary user. Start the container with an init process
  (`docker run --init`), or processes that have exited are never collected.
- **macOS** shares the POSIX code path with Linux (process groups, signals,
  no `/proc`). It was not run. What differs from Linux and is therefore
  unverified: detecting a process that exited and was not collected relies on
  `/proc`, which macOS does not have, so there the plain signal test is used.

## What the end-to-end suite covers

Each check quotes the invariant or failure mode it covers. By scenario:

| Scenario | Checks | What is established |
|---|---|---|
| A. Zero-config demo, help, configuration | 23 | The demo passes; ten kinds of bad config are rejected with exit 2 and a message naming the problem; the shipped example config loads; the README's quick-start commands behave as written. |
| B. Discovery and the tool description | 4 | Search, describe, close-name suggestions; the tool description is short and contains no tool schema. |
| C. What a program can do | 17 | Eleven calls in one program; bodies stay in the sandbox; state across programs and per session; error payloads with program-only tracebacks; hints for `return` and `await`; repr fallback; result budget. |
| D. The sandbox boundary | 4 | Host environment variables are absent in the sandbox; frame-shaped prints, raw descriptor writes and `input()` cannot corrupt or forge the protocol; a 2 MB print flood is bounded. |
| E. Policy, approval and replay | 18 | Pause at exit 3 with no side effect; an approved call runs once and earlier calls are replayed (counted in files outside the process); reject; deny; batch pause; interactive approval; observe mode. |
| F. Replay under nondeterminism | 5 | Divergence is detected; `step()` fixes it; a tool call inside `step()` is refused; `verify` exits 4 on an unexplained result; 25 seeded random programs all replay to the same result. |
| G. Injected faults | 12 | Endless loop, stalled tool, dead sandbox, oversized result and arguments, call limit, four broken executors, a concurrent second pass. |
| H. A different sandbox than the host | 0 on Windows, 1 on Linux | The sandbox runs under another interpreter than the host, through `executor.command`. Skipped on the Windows machine, which has one Python; run in the third platform row, with the host on 3.9 and the sandbox on 3.8. |
| I. The runtime as an MCP server | 14 | One tool only; payload shape; pause, approve from the CLI, fetch by id; protocol errors; cancellation. |
| Y. The execute tool: arguments, background runs, progress, and a client that does several things at once | 15 | The `execute` tool's parameters: arguments, check, background, wait, select, reuse, cancel; a ping and a second program answered while one runs; progress notifications; hints that name the tool call. |
| J. MCP servers as tool sources | 10 | A tool on an MCP server called from a program; three kinds of unavailable server; the official SDK's client driving this server; this client driving a server built with the official SDK. |
| K. Snippets | 6 | Promote, find, run with arguments; refusal to promote an unfinished execution. |
| L. The optional model loop against a scripted OpenAI-compatible endpoint | 9 | A task run to a final answer against a scripted OpenAI-compatible endpoint; exactly one function offered; retry on 429; no retry on 401; malformed arguments; stop at max turns; refusal to start unconfigured. |
| N. Entry points, command hints and odd files | 17 | Isolated mode, `-m`, the package as a script; hints that work as pasted from another folder; byte-order marks and CRLF; a silent stdin; JSON usage errors; the installed `codemode` entry point. |
| O. Policy: server spellings, argument rules, approvals that need a terminal or expire | 15 on Windows, 16 on Linux | A rule in the server's spelling; argument rules; a deny that beats an approval; approve refused without a terminal, with a pipe and with the null device on stdin, and accepted at a real one (a pty, on POSIX); a pause that expires. |
| P. What a program is given: arguments, helpers, honest listings, variables kept on failure | 28 | Honest search totals; printable `describe`; `repeat`; `on_error`; program arguments, also across a pause; variables kept on failure and read with `--select`; the null-result warning; hints for shell-mangled strings; the sandbox exits when the host is killed. |
| Q. Calls: time limits per tool, queues, large results, continuing a failed program | 23 | A time limit per tool and per connector; a queue for a serial server; a batch that survives one timeout; the wind-down at the program's time limit; a saved oversize result; an oversized batch refused as one step; `--reuse`; `--check`; literal env values hidden. |
| R. Long runs: detach, progress, wait, cancel, and a host that is killed | 15 | `run --detach`, progress in `get`, `wait` and its exit 6, `cancel`; a host that is killed is recorded as Interrupted and can be continued; bounded live output; Ctrl-C (POSIX). |
| S. MCP servers as they really are: slow, noisy, crashing, text-only, huge, leaking | 17 | A stdio server that is slow, noisy, CRLF, crashes mid-call, returns text, has a 400-parameter schema, echoes its credential, reports progress, honours cancellation, ignores end of input, and has colliding tool names; several servers started side by side. |
| T. MCP over HTTP: streamable HTTP and HTTP+SSE | 12 | Streamable HTTP with JSON and event-stream answers, sessions, a forgotten session, missing credentials, a web page instead of an endpoint, a time limit; HTTP+SSE with its endpoint event and split data lines. |
| U. A project's layout: the config found from a subfolder, cached tool lists, an edited server | 11 | The config found from a subfolder and paths read from the root; `--config` unchanged; the search stops at the repository root; tool lists cached and dropped when the server script changes; a long-lived server process picks up an edited script. |
| V. A project's own commands as tools | 17 | The commands connector: arguments that stay one argument, refusals before anything runs, the environment allowlist, exit codes as data, output budgets with a saved file, a time limit that stops the process tree, odd encodings. |
| W. Project helpers: a project's own functions, in the same sandbox and under the same policy | 15 | Project helpers: called from programs, under the policy, paused and denied like any call; a helper edited during a pause; a broken helper file; discovery; names that cannot be modules. |
| X. A project that has never used the runtime: init, doctor, a first program | 24 | A fresh project: `init` from a server list with a written-out credential, the starting policy from annotations, `.gitignore`, `doctor` with and without the variable, a first program, a write that pauses and needs a person, `prune`, `instructions`; a repository with no server list; the reference filesystem server. |
| M. Every execution log written by this run | 2 | Every execution log written during the run: one start, at most one end, nothing after it, approval before any asked call, a write-ahead entry before any applied call, and a real source for every reused step. |

## MCP servers the runtime was run against

The brief asked for at least three. There are six, none of them written for
one another:

| Server | Transport | What it is | Where |
|---|---|---|---|
| This runtime's own `serve` | stdio | The `execute` tool, called from a program through a connector. | scenarios I, J, Y |
| `tests/fixtures/misbehaving_server.py` | stdio | Slow to start, noise on stdout, CRLF, exits mid-call, text results, a 400-parameter schema, echoes its credential, reports progress, honours cancellation, ignores end of input, colliding tool names. | scenario S |
| `tests/fixtures/http_mcp_server.py` | streamable HTTP and HTTP+SSE | Sessions, event-stream answers, bearer token, a forgotten session, a slow tool. | scenario T |
| A server written by the suite into a project folder | stdio | Edited between runs: the cached tool list and the long-lived server must follow. Annotations drive the starting policy. | scenarios U, X |
| A server built with the official MCP Python SDK | stdio | Interop in both directions. Optional. | scenario J |
| `@modelcontextprotocol/server-filesystem` | stdio, under node | A public server nobody here wrote: `init`, a read, a write that pauses. Optional. | scenario X |

The field project's own server was also started once, read-only, to measure
start-up cost: 0.17 s for a program that lists its 21 tools.

## What this round's verification found

Bugs found by running the tests, in the order they appeared. Each has an
entry in `tests/FAILURE_MODES.md` and a check that fails without the fix.

1. **A thread blocked on buffered stdin broke the exit code** of `run` with
   no program (E4). The interpreter aborted at shutdown instead of returning
   2. The reader now uses the raw descriptor.
2. **A reused step failed the log invariant** (C7): the suite's own check
   that every applied call has a write-ahead entry did not know a step taken
   from an earlier execution. The check was taught to verify the source
   instead; this is the one existing check that was changed on purpose.
3. **A server that had exited was found "running"** for a moment (M3): the
   end of its output arrives before the process is reported as exited, and a
   call in that window failed with "closed its input". The connector now
   treats the end of output as the end of the server, and retries a request
   that never reached it.
4. **A call that timed out over HTTP did not tell the server** (M8) when the
   time ran out before the response headers arrived.
5. **A lock file could be left behind on Windows** (U10): deleting it fails
   while another process is reading the owner's id from it, and the error
   was swallowed. In a process that keeps running (the MCP server mode) that
   lock would have blocked a later `approve` for good. Found by eight
   threads contending for one session; the release now retries.
6. **A stale lock found by several threads at once was sometimes taken by
   nobody** (R8): clearing failed while the file was open elsewhere. The
   lock now retries while the lock looks stale.
7. **`--check` could hang on Linux** (C11), described above.
8. **A dead process could look alive in a container** (R11), described
   above.
9. **The suite's own log check saw no logs** for scenarios that run in a
   project folder of their own, so a mutant run of such a scenario failed
   for the wrong reason. The search for logs is now recursive, and the
   mutant in question is caught by the check that was written for it.
10. **`.gitignore` line endings** (U6): `init` read the file as text, which
    hides CRLF, and appended LF lines to a CRLF file.
11. **`init` crashed under Python 3.9** (E7): it wrote the config with an
    argument that file writing gained in 3.10. Found by the first run of the
    suites under 3.9; nothing on a newer Python could have shown it.

Two expectations of existing checks were changed, both because the thing
they pin was extended on purpose, and both said so in their commits: the
log invariant (item 2), and the parameter list of this runtime's own
`execute` tool as seen through a connector, which gained optional
parameters.

## What the suites missed, found by a second user

Right after this round a tool that ships a copy of the runtime reported
that **an agent could approve its own request on Windows** (L16). The
checklist below called that hole closed (L13). It was closed for a pipe
only:

- The guard asked `isatty()`. On Windows that is true for every character
  device, and the null device is one. `approve ID < /dev/null` approved, and
  so did a plain `approve ID` from a shell tool that gives its commands no
  input. Both were reproduced here against the real CLI before the fix: a
  gated write ran with nobody at a terminal.
- The check for L13 gave the command a pipe. The null device was never
  tried, so the suite was green on the one platform where the guard failed.
- Now Windows itself is asked whether stdin is a console. The suite tries
  the null device on every platform and, where a terminal can be made
  without a window (POSIX, a pty), also checks that a real terminal is
  accepted. On Windows that side was checked by hand: the runtime's own
  check, run in a terminal, says yes.

What the guard cannot do is in "Not verified" below.

## Checklist: the brief for this round

| Asked for | Status | Evidence |
|---|---|---|
| One-command setup that writes the config from `.mcp.json`, adds ignore entries, checks the connection | done | `codemode init`; scenario X |
| Clear errors for a missing or bad config; a `doctor` check | done | `codemode doctor`; scenarios A, U, X |
| Installable as a package with an entry point | done | `pyproject.toml`; scenario N installs it and runs `codemode` |
| Several MCP servers per project | done | scenarios J, S, T run 5 or 6 connectors in one config |
| stdio, streamable HTTP, SSE | done | `connectors/mcp_http.py`; scenario T |
| Servers that crash, restart, start slowly, return text, have huge schemas | done | scenario S |
| Name clashes between servers | done | namespaces are per server; `init` cleans and de-duplicates names (U3); clashes inside one server (M10) |
| Per-project Python helpers, in the same sandbox and policy | done | `helpers.py`; scenario W |
| Allow, ask and deny by server and tool, plus argument patterns; safe defaults | done | scenario O; default `ask`, terminal required to approve (L13) |
| Windows, Linux, macOS: paths, encodings, line endings, process trees, long command lines | Windows and Linux tested; macOS not run | the table above; E3, K9, M9, K6 |
| Many agents: separate or shared data folders, locking, retention | done | `test_isolated.py` (R8, U9, U10); `codemode prune` |
| `repeat`, background runs, summaries of long text, honest truncation | done | scenarios P, R, V |
| Sandbox and policy holes | reviewed; five closed, one of them only in part until a second user reported it | L9 (a rule in the server's spelling was ignored), L10 (an approval beat a later deny), L13 (an agent with a shell could approve; on Windows it still could with the null device on stdin, see L16 above), C9 (credentials echoed by a server), K1 (a command tool whose program is a parameter) |
| Timeouts and cancellation | done | C1 to C4, M8, R4 |
| Error messages say what to do next | done | every `error.type` in `INTERFACE.md` has a hint where one applies; H10 |
| Truncation is clear and the rest can be fetched | done | H1, H12, C5, K5, M5 |
| Start-up cost and catalog caching | measured; done | 0.13 s per run with no server, 0.17 s with one; M11, M14 |
| A stable, documented CLI and JSON result; the MCP server mode | done | `INTERFACE.md`; scenarios I, Y |
| `ADOPTING.md` with a ready-made instructions block | done | the block is generated and checked against the guide (U11) |
| `SERVER_AUTHORS.md` | done | |
| Fixtures with at least three different MCP servers | done | six; see above |
| A fresh project set up end to end in a temporary folder | done | scenario X |
| Backward compatibility | kept | scenario A to M are the original checks, unchanged except for the two noted above |

## Not verified

Stated so that nobody assumes otherwise:

- **macOS.** Not run.
- **A container executor.** `executor.command` is tested with a second
  interpreter and with four deliberately broken commands, not with Docker as
  the sandbox. The Docker line in the README is an example.
- **OAuth, TLS client certificates, proxies** for remote MCP servers. Plain
  HTTP to a local fixture was run; HTTPS uses the standard library's default
  verification and was not exercised against a real remote server.
- **A real remote MCP service.** The HTTP transports were run against a
  fixture written from the specification, not against a deployed product.
- **A real model.** No model's behaviour with the new helpers was measured.
  Whether agents use `repeat`, `--arg`, `--reuse` and `wait` as intended is
  for the next round of field use to show; the response appended to the
  field report lists what to look for.
- **Very large data folders.** Retention was tested with a handful of
  executions, not with tens of thousands.
- **Hostile code.** Nothing here claims the default sandbox contains a
  program that is trying to escape. It is a process boundary; see the README.
- **An agent that types into a real terminal.** `approve` needs a terminal
  on stdin. That stops a shell tool; a tool that drives a terminal panel or
  a pseudo-terminal passes, and nothing in this runtime can tell it from a
  person. `ADOPTING.md` says what to do about it.
- **A console on Windows, in the suite.** A child process cannot be given a
  console there without opening a window, so the suite checks only the
  refusals on Windows. That a console is accepted was checked by hand.

---

# The original build (0.1.0)

Phase 5 of the build brief: what was run then, what it found, and the two
checklists.

## Results then

| Suite | What it is | Result |
|---|---|---|
| `python tests/e2e.py` | 125 checks, each on the real CLI started as a subprocess; 103 traced commands | 125 of 125 |
| `python -m unittest discover -s tests` | 49 cases for the pure modules, derived from `tests/FAILURE_MODES.md` | 49 of 49 |
| `python tests/mutation_check.py` | 20 deliberate bugs, one at a time, in a copy of the project | 20 of 20 caught |
| `python main.py` | the zero-config demo, which checks itself | 13 of 13, exit 0 |

Environments then: Windows 11 with Python 3.15.2 (and the MCP SDK), 3.11.18
and 3.14.10; Linux (WSL2 Ubuntu) with Python 3.14.4.

## What verification found then

Bugs in the application, found by running it and fixed in the code:

1. **Search missed the obvious.** `search("notes")` did not return
   `read_note`, because only exact word matches counted. Found by the demo's
   own first check. Fixed in `catalog._score`.
2. **`last` could name the wrong execution.** Ids had one-second resolution,
   so two executions started in the same second sorted by their random
   suffix. Ids now carry microseconds (`records.ExecutionStore.new_id`).
3. **A sandbox that never read its input hung the host past every timeout.**
   Writes now happen on a writer thread
   (`executor.SandboxProcess._write_frames`).
4. **Startup failures were misreported** as `SandboxCrashed` instead of
   `ProtocolError` or `ExecutorStartFailed`, depending on a race. Fixed by the
   same change.
5. **A syntax error showed the parser's stack frame** instead of only the
   program's line. Fixed in `sandbox_runner._describe_error`.
6. **A program could not be promoted to a snippet without being rewritten**,
   because snippet arguments were named `input`. Renamed to `args`, which is
   defined in every program.
7. **A key exported for another provider would have been sent to the
   configured endpoint**, through an `OPENAI_API_KEY` fallback. The fallback
   is gone and a unit case pins that.

## Checklist 1: principles

Numbers refer to the Master Principle List in [`DESIGN.md`](DESIGN.md).
"Checked by" names scenarios in `e2e.py` or classes in `test_pure_logic.py`.

| # | Principle | Status | Where | Checked by |
|---|---|---|---|---|
| P1 | One outer tool; the model writes a program | implemented | `mcp_server.McpServer._handle` (`tools/list`), `llm_agent.run_agent` | I, L, Y, mutant "also offers an approve tool" |
| P2 | Only the final value returns; budgets, marked truncation | implemented | `sandbox_runner._exec_block`, `truncate.budget_result`, `truncate.BoundedLog` | C, D, P, `OutputBudgets` |
| P3 | Discovery at run time | implemented | `catalog.Catalog.search`, `.describe`; `engine.Pass._on_search` | B, J, P |
| P4 | Signatures from JSON Schema, degrade to `Any` | implemented | `schema_sig.signature`, `type_of` | B, J, `SchemaSignature` |
| P5 | Chained and parallel calls | implemented | `sandbox_runner.parallel`, `engine.Pass._on_call` | C, E, Q |
| P6 | Separate credential-free executor, pluggable; honest about its limit | implemented | `executor.SubprocessExecutor.start`, `.argv`; README "The safety model, and its limit" | D, G, H |
| P7 | Per-call policy on the host; a refused call fails the call | implemented | `policy.Policy.decide`, `engine.Pass._classify` | E, O, `PolicyDecisions`, `PolicyRules` |
| P8 | Hard-to-undo actions wait for a human, out of the agent's reach | implemented | `engine.Pass._on_call` (pause), `runtime.Runtime.approve`, `cli._require_terminal` | E, I, O |
| P9 | Deterministic replay, divergence, `step()` | implemented | `engine.Pass._next`, `._on_step`; `sandbox_runner.step` | E, F |
| P10 | Append-only log outside the sandbox, per-operation telemetry | implemented | `records.ExecutionStore.append`, `engine.Pass._append` | E, M |
| P11 | Identity stamped by the gateway | implemented | `config.resolve_agent`, `runtime.Runtime.start` (`start` entry) | `show last` output in E; `SharedDataFolder` |
| P12 | Explicit bounds | implemented | `config.DEFAULT_LIMITS`, `Config.call_timeout`, `engine.Pass._recv`, `._await` | G, Q |
| P13 | Fail closed; failure as data; one terminal outcome | implemented | `policy.policy_from_dict` (default `ask`), `runtime.Runtime._payload`, `records.ExecutionView` | E, I, M |
| P14 | State in the runtime, not the context | implemented | `sessions.SessionStore.apply_delta`, `runtime.Runtime._state_delta` | C, W |
| P15 | Reusable code, promoted by the host | implemented | `runtime.Runtime.save_snippet`, `sandbox_runner.run`; project helpers | K, W |
| P16 | End-to-end verification, replayable trace, provenance stamp | implemented | `tests/e2e.py`, `docs/e2e-trace.txt`, `runtime.Runtime.verify` | E, F |
| P17 | The gate is out of reach of the work | implemented, within the limit stated under P6 | no sandbox function, MCP parameter or helper touches policy, approvals or records | I, K, W, Y |
| P18 | Observe before enforce | implemented | `policy.Decision.effective`, `Policy.mode` | E, `PolicyDecisions` |
| P19 | Invariants first; property and differential tests | implemented | `tests/FAILURE_MODES.md`; property loop in `replay_faults`; `verify` | F, M |
| P20 | Deliberate fault injection | implemented | `connectors/chaos.py`, `tests/fixtures/misbehaving_server.py` | G, S |
| P21 | Legible, few dependencies | implemented | standard library only | import scan |
| P22 | Small static tool description | implemented | `runtime._TOOL_DESCRIPTION`, `Runtime.tool_description` | B, W |
| P23 | Output cannot corrupt framing | implemented | `sandbox_runner._Channel.__init__` | D |
| P24 | Provider-neutral model layer with a scripted stand-in | implemented | `config._llm`, `llm_agent.resolve_llm`, `._chat`; `tests/fixtures/stub_openai.py` | L |
| P25 | A running execution can be stopped | implemented | `runtime.Runtime.cancel`, MCP `notifications/cancelled`, Ctrl-C | I, R, Y |
| P26 | Bounded attempts, then hand back | implemented | `llm_agent.run_agent` (`max_turns`) | L |
| P27 to P32 | Added after the first field report | implemented | see "After the first field report" in `DESIGN.md` | P, Q, R, S, V, W, X |

Deferred:

| What | Reason |
|---|---|
| Rollback of applied calls | Needs a compensating action per tool; MCP defines none. Reject and cancel stop, they do not undo. |
| Wake-ups | A detached program runs to its end; the caller asks. Calling the agent back would need a channel into the agent's host. |
| Live objects and processes across executions | Not replayable and not portable to a container executor. `state` holds JSON only. |
| Model-based review of code or actions | Would put a model in the safety path. The audit log is the integration point. |
| Multi-agent coordination: claims, crews, merge gates | The runtime is a shared, safe substrate; orchestration is another application. |
| OAuth for remote servers | A flow with a browser and a callback does not fit a runtime with no user interface. A token goes in a header. |

## Checklist 2: agnosticism

The nine portability constraints of the original brief, each with evidence.

| # | Constraint | Holds | Evidence |
|---|---|---|---|
| 1 | No vendor lock-in | yes | No SDK is imported: only standard-library imports in `main.py` and the package. A search of the code path for vendor and model names finds none; "OpenAI" appears only as the name of the wire format. |
| 2 | Open standards first | yes | Capabilities are exposed over MCP stdio and a CLI. Tools are consumed over MCP: stdio, streamable HTTP and HTTP+SSE. Both directions were exercised against the official MCP SDK (scenario J). |
| 3 | Configurable provider layer | yes | `config._llm` reads `base_url`, `model`, `api_key` from the config or the `CODEMODE_*` variables. Scenario L runs it against a local scripted endpoint. |
| 4 | Standard, boring tech | yes | Python, standard library, no build step to run it. See the platform table. |
| 5 | One-command run | yes | `python main.py` runs the demo and exits 0. `requirements.txt` is empty. No account, key or network is needed. |
| 6 | Human- and agent-readable | yes | `README.md`, `ADOPTING.md`, `INTERFACE.md`. Exit codes are fixed in `errors.py`. Every command takes `--json`. |
| 7 | Agent-workable codebase | yes | One responsibility per module (table in the README); the pure ones have no I/O. Errors carry `type`, `message` and `hint`. `AGENTS.md` states the rules for changing the code. |
| 8 | Demo by default | yes | `demo.run_demo`: seven steps, thirteen self-checks, no configuration, no model. |
| 9 | License and attribution | yes | `LICENSE` (MIT); an SPDX line at the top of every source file; the sources are cited in the README. |

# Verification report

What was run, where, and what it found. Everything here refers to the code as
committed. The first part is the state after the two field reports, a
third note and the connector for HTTP APIs (version 0.4.0); the report on
the original build (0.1.0) follows it.

## Results

| Suite | What it is | Result |
|---|---|---|
| `python tests/e2e.py` | 415 checks, each on the real CLI started as a subprocess | 415 of 415 |
| `python -m unittest discover -s tests` | 170 cases: pure logic with many inputs, and the states a command line cannot produce (`test_isolated.py`) | 170 of 170 |
| `python tests/mutation_check.py` | 118 deliberate bugs, one at a time, in a copy of the project (one fewer off Windows: one is in a line only Windows runs) | 118 of 118 caught |
| `python main.py` | the zero-config demo, which checks itself | 13 of 13, exit 0 |

`python tests/run_all.py` runs all four. It was run before every commit, on
a copy of exactly what was about to be committed.

| | The original build | After the first report | After the second | After the third note | With HTTP APIs |
|---|---|---|---|---|---|
| End-to-end checks | 121 (125 with the optional SDK scenarios) | 332 | 371 | 381 | 415 |
| Unit cases | 49 | 141 | 150 | 155 | 170 |
| Mutants | 20 | 75 | 98 | 104 | 118 |

## Platforms

| Host | Demo | Unit | End to end | Mutation | Not run there |
|---|---|---|---|---|---|
| Windows 11, Python 3.13.15 | 13 of 13 | 170 (1 skipped) | 415 of 415 | 118 of 118 | Ctrl-C on a foreground run (a console event cannot be sent to one child process); `approve` at a real terminal (a console cannot be given to a child process without a window); a second interpreter for the sandbox (one Python is installed) |
| Linux (Ubuntu 24.04 in a container, kernel 7.0), Python 3.12.3 | 13 of 13 | 170 | 407 of 407 | not run | the optional scenarios that need the MCP SDK, node or the network; a second interpreter |
| Linux (the same), host on Python 3.9.25, sandbox under Python 3.8.20 | 13 of 13 | 170 | 408 of 408 | not run | the same optional scenarios |
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
| N. Entry points, command hints and odd files | 21 | Isolated mode, `-m`, the package as a script; hints that work as pasted from another folder; a project's own command name in hints, `--help` and the instructions; byte-order marks and CRLF; a silent stdin; JSON usage errors; the installed `codemode` entry point. |
| O. Policy: server spellings, argument rules, approvals that need a terminal or expire | 26 on Windows, 27 on Linux | A rule in the server's spelling; argument rules; a deny that beats an approval; approve refused without a terminal, with a pipe and with the null device on stdin, and accepted at a real one (a pty, on POSIX); a pause that expires; approvals in advance: what each form covers, that a deny rule, another execution and the time limit end them, that a project can narrow them, and what they leave in the log. |
| P. What a program is given: arguments, helpers, honest listings, variables kept on failure | 39 | Honest search totals; one namespace's tools from a program and from the command line; `on_error` on `parallel()`; the project's folder, and what a relative path that is not there says; printable `describe`; `repeat`; `on_error`; program arguments, also across a pause; variables kept on failure and read with `--select`; the null-result warning; hints for shell-mangled strings; the sandbox exits when the host is killed. |
| Q. Calls: time limits per tool, queues, large results, continuing a failed program | 23 | A time limit per tool and per connector; a queue for a serial server; a batch that survives one timeout; the wind-down at the program's time limit; a saved oversize result; an oversized batch refused as one step; `--reuse`; `--check`; literal env values hidden. |
| R. Long runs: detach, progress, wait, cancel, and a host that is killed | 15 | `run --detach`, progress in `get`, `wait` and its exit 6, `cancel`; a host that is killed is recorded as Interrupted and can be continued; bounded live output; Ctrl-C (POSIX). |
| S. MCP servers as they really are: slow, noisy, crashing, text-only, huge, leaking | 24 | A stdio server that is slow, noisy, CRLF, crashes mid-call, returns text, has a 400-parameter schema, echoes its credential, reports progress, honours cancellation, ignores end of input, and has colliding tool names; several servers started side by side; which host variables a server gets, which it is kept from, and that an inherited credential it echoes is removed; whole descriptions from a listing on request. |
| T. MCP over HTTP: streamable HTTP and HTTP+SSE | 12 | Streamable HTTP with JSON and event-stream answers, sessions, a forgotten session, missing credentials, a web page instead of an endpoint, a time limit; HTTP+SSE with its endpoint event and split data lines. |
| U. A project's layout: the config found from a subfolder, cached tool lists, an edited server | 11 | The config found from a subfolder and paths read from the root; `--config` unchanged; the search stops at the repository root; tool lists cached and dropped when the server script changes; a long-lived server process picks up an edited script. |
| V. A project's own commands as tools | 17 | The commands connector: arguments that stay one argument, refusals before anything runs, the environment allowlist, exit codes as data, output budgets with a saved file, a time limit that stops the process tree, odd encodings. |
| Z. An HTTP API as tools, from its OpenAPI document | 34 | The openapi connector, against an API the suite starts: names that start with the method; parameters, bodies and schemas that are references; path values kept to one segment; query lists, a form, text, JSON; refusals before anything is sent; answers that are JSON, text, bytes or nothing; error statuses, a redirect that is not followed, a time limit; credentials in a header and in the query, never echoed back, never sent with the request for the document; a document that names another host, is YAML, is Swagger 2.0, or is missing; a read that is not sent again after an approval; a document edited behind a cached tool list. |
| W. Project helpers: a project's own functions, in the same sandbox and under the same policy | 17 | Project helpers: called from programs, under the policy, paused and denied like any call; a helper edited during a pause; a broken helper file; discovery; names that cannot be modules. |
| X. A project that has never used the runtime: init, doctor, a first program | 37 | A fresh project: `init` from a server list with a written-out credential, the starting policy from annotations, `.gitignore`, `doctor` with and without the variable, a first program, a write that pauses and needs a person, `prune`, `instructions`; a second project laid out its own way (`init --out`, its own data and work folders, its own text for its agents); a repository with no server list; the reference filesystem server. |
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

One tool source is not an MCP server: `tests/fixtures/openapi_server.py`,
an HTTP API of 21 operations with its OpenAPI document, which wants a
credential, echoes it, redirects, fails and stalls on request (scenario Z).

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

## The second report: what was built, and how each part is checked

The same report listed what its tool had to replace in the runtime to fit
it in. Each became a setting that any project can use; none names that
tool. One commit each, the whole suite before each; the last commit of the
round carries the version, these documents and one follow-through (`init`
and the project's own command).

| What | Failure mode | Checks | Mutants |
|---|---|---|---|
| `inherit_env` on stdio servers, shared with the commands connector; the base list of host variables | M17, M18 | 2 unit cases over a made-up environment; 4 end to end against the misbehaving server | 5, one of them an older one moved to the shared line |
| `CODEMODE_COMMAND`: the project's own command in hints, `--help` and the instructions; `init` knows the runtime's own server under that command | E8 | 2 unit cases; 4 end to end | 3 |
| `search --namespace`, `search(namespace=...)` | H13 | 7 end to end, one of them that a search without a namespace is recorded as before | 2 |
| `init --out`, `work_dir`; `.gitignore` and the instructions follow the project's folders | U12 | 2 unit cases; 7 end to end in a project laid out its own way | 4 |
| `guidance`: the project's own text in the tool description and the instructions | U13 | 1 unit case; 5 end to end | 3 |
| `approve --same-tool`, `--all-remaining`; `approval.standing` | L17 | 2 unit cases over made-up logs; 11 end to end | 6 |

What the runs of this round found: no fault in the runtime. They found two
mistakes in the new tests before anything was committed: a new check that
expected one match where the fixture has two, and a mutant whose anchor
had moved with the line it names. No existing check was changed. One was
about to be: a unit case pins the default approval settings as an exact
dict, and the new `approval.standing` setting would have changed it. The
setting was given a field of its own in the config object instead.

One check of the suite was made stricter. The log invariant asked that a
call recorded as pending has an approval before it runs. A call approved in
advance is never recorded as pending, so the invariant now also asks: a
call the policy asked about, and enforced, has an approval entry before it.

## The third note: five fixes

From the same tool, after it moved to 0.3.0. Two commits: what a program
is given, and what a tool that wraps the runtime needs. The whole suite
ran before each.

| What | Failure mode | Checks | Mutants |
|---|---|---|---|
| `parallel(..., on_error=...)`, the option `call()` has | H14 | 1 unit case; 2 end to end | 1 |
| `project_root` in programs and helpers; a hint for a relative path that is not there | H15 | 1 unit case; 4 end to end, one of them a program resumed from another folder | 2 |
| `search --full`: whole descriptions from a listing | H16 | 1 unit case; 2 end to end | 1, and an older one moved with its line |
| `!NAME` in `inherit_env` | M19 | 1 unit case; 1 end to end | 1, and two older ones moved with their line |
| No "pass `--config`" note from `init --out` under a command that finds the config by itself | U14 | 1 unit case; 1 end to end | 1 |

The runs found no fault in the runtime. One new unit case expected the
wrong answer for a value it had made up, and was corrected before the
commit. No existing check was changed.

The whole-suite run before the second commit caught 103 of the 104
planted bugs and called one stale: the line it changes had itself been
changed, so its text was no longer found. Its anchor was corrected and it
was run again by itself, and caught. The other 103 were not run a second
time.

## The connector for HTTP APIs: what was built, and how each part is checked

Built after the package this design started from was read again (see
`DESIGN.md`, P35 to P37). One commit; the whole suite ran before it.

| What | Failure mode | Checks | Mutants |
|---|---|---|---|
| Tool names that start with the method; two paths with one name; the root path | A1 | 1 unit case; 3 end to end, one of them a deny rule over a method | 1 |
| Parameters and bodies that are references, a schema that refers to itself, a body that reaches hundreds of schemas, the outline of an answer | A2 | 3 unit cases; 4 end to end | |
| A path value is one segment, or refused | A3 | 1 unit case; 2 end to end | 2 |
| Query values; a value the config sets is not a caller's | A4 | 1 unit case; 3 end to end | 2 |
| Headers a caller may and may not set | A5 | 1 unit case; 2 end to end | 2 |
| JSON, form and text bodies; one that cannot be sent | A6 | 1 unit case; 2 end to end | |
| Arguments checked before anything is sent | A7 | 1 unit case; 1 end to end | |
| Answers, and a bound on what is read of one | A8 | 2 unit cases; 1 end to end | 1 |
| Error statuses, a time limit | A9 | 1 unit case; 4 end to end | |
| A redirect is not followed | A10 | 1 end to end, which also asks the API whether the address was visited | 1 |
| Where requests go; the document is fetched without the credentials | A11 | 2 unit cases; 5 end to end | 3 |
| A document that cannot be used; a config that names none | A12 | 1 unit case; 4 end to end | |
| A credential the API echoes back | A13 | 1 end to end | 1 |
| An edited document behind a cached tool list | A14 | 1 end to end | 1 |
| A read is not sent again after an approval | A15 | 2 end to end, counted by the API | |

What the work found, before the suite was written: the root path was named
`get`, which the rule `api.get_*` does not match, so a GET of `/` was asked
about under a policy that allows every GET. It is `get_root` now, and A1
says that every name starts with the method and an underscore. A measurement
then changed one number: a document of 2000 operations whose bodies reach a
web of 300 schemas gave a tool list of 35 MB, because a schema that many
operations share is copied into each. The schemas one tool carries are now
held to 8000 characters, nearest first, and each one left out says where it
is in the document; the same document gives a list of 12 MB in 0.25 s. No
existing check was changed.

## The fourth report: what was built, and how each part is checked

Rounds 6 to 10 of the first user, rows 63 to 90 of
[`FEEDBACK_TRIAGE.md`](FEEDBACK_TRIAGE.md). Six agents built the parts side
by side, each in a worktree of its own, a seventh reviewed the merged code,
and the whole suite ran on the merged tree before it went to the main
branch.

| What | Failure mode | Checks | Mutants |
|---|---|---|---|
| A number from `--arg` that also answers as its text; `--arg-text`, `--arg-json`; booleans that stay booleans, with a warning | H17 | unit; end to end in scenario P, across a pause, `verify` and the recorded start entry | 8 |
| A top-level `return`; the value of a trailing `if`, `with` or `try` | H18, H19 | unit over about 24 programs; end to end in P and C | 5 |
| `wait --until-done`; `--select` on `wait` and for several paths; reads of a running execution; a log file's name as an id; a note for a program that called no tool | R12 to R15, E9 | unit; end to end in scenarios R and N | |
| Output on a legacy code page | E10 | end to end in N | |
| Copies of a one-at-a-time server (`processes`), tools that take turns (`serial`) | M20 to M22 | unit; isolated; end to end in scenario S against a server that records who ran when | 6 |
| A server that ignores a cancelled call (`on_abandon`); on Windows, a process whose parent had exited | M23, M24 | isolated; end to end in S | 6, two of them only on Windows |
| Hints on tool errors; the shape of a tool's last result | H20, H21 | isolated; end to end in scenarios B and Q, across a pause and `verify` | 3 |
| Relative paths of a config that was named | M15 | end to end in scenario U: a server's command and folder, a command's folder, helpers, a guidance file, an API document | 3 |
| A project's helpers in its instructions | P8 | unit; end to end in scenario W | |

Existing checks that were changed, each on purpose:

- **M15** (scenario U) pinned that with `--config` a server is not found
  from a subfolder. That was the limit the report complained about. It now
  checks the new rule from both sides: a file in the current folder still
  wins, and one that is not there is found next to the config.
- **H9** (scenario P and a unit case) expected null from a program that
  ends with an `if`. Such a program now gives the value of the branch that
  ran; the check uses a loop, which still gives null and still warns.
- **X10** (scenario C) expected a top-level `return` to be a syntax error.
  It checks a `return` in a class body, which still is one.
- **A9** (scenario Z) expected no hint on a plain HTTP 500. Every tool
  error with no hint of its own now gets the tool's signature.

What the review of the merged code found, all fixed before the merge:

- **Text by default for `--arg` broke stored programs silently.** It was
  built that way first, with the old answers kept for `==` and a truth
  test. The reviewer ran it: `args["n"] * 2` gave `"33"`, a list became its
  characters, a set lookup missed. The design was turned round: the value
  stays typed and learns to answer as its text. The two existing checks
  the first design had changed (H7, H12) are back to their old text.
- **The turn of a serial tool ended when the gateway gave up on the call**,
  while the call was still running, so the next build could start beside
  it. The turn now ends when the connector's call has returned.
- **A call that found no free copy of a server waited without a limit**
  and could start after its program had ended. It now fails at its time
  limit and never starts.
- **A cancel could reach the same step of another execution** in a process
  that serves several, because calls in flight were named by their step
  alone. With `on_abandon: restart` that would have stopped the wrong
  server. They are named by execution and step.
- **A Windows job handle could be closed twice** by two threads.
- **A top-level `return` was caught by a bare `except:`** in the program.

What the merged run found in the suite itself: a new check wrote down the
process ids of a minute's worth of servers and, at the end, called any id
that was alive a leftover and stopped it. An id is given out again when
its process ends. The check failed once in a long run, and what it stopped
then was not one of ours. It now asks what runs under an id before it
calls it a leftover. Nothing else in the suite stops a process by an id
that is more than a few seconds old. A second new check counted server
starts from one file that three servers appended to at the same moment,
and once a line was lost; each server process now writes a file of its
own.

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
- **A real HTTP API.** The `openapi` connector was run against a fixture
  written for the suite, over plain HTTP, with a document in OpenAPI 3.0.
  No deployed service, no HTTPS, no document in 3.1 and no public document
  with hundreds of operations was tried; the large one above was made up to
  be measured, not called.
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
- **The tool that sent the second report.** Its copy of the runtime was not
  updated or run here, and nothing in its folder was changed. Whether it
  can drop each of the names it replaced is for its own tests to show; the
  settings were checked in projects the suite makes for itself.
- **A real wrapper around the runtime.** `CODEMODE_COMMAND` was set as a
  variable and the hints were read; no wrapper script or task runner was
  put in front of the runtime to run a hint as printed.
- **Approvals in advance at a terminal.** The suite gives them from a
  subprocess, in a config that allows approving without a terminal; that
  they need one otherwise is the same guard as every approval (L13, L16).

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
| 7 | Agent-workable codebase | yes | One responsibility per module (table in `AGENTS.md`); the pure ones have no I/O. Errors carry `type`, `message` and `hint`. `AGENTS.md` states the rules for changing the code. |
| 8 | Demo by default | yes | `demo.run_demo`: seven steps, thirteen self-checks, no configuration, no model. |
| 9 | License and attribution | yes | `LICENSE` (MIT); an SPDX line at the top of every source file; the sources are cited in the README. |

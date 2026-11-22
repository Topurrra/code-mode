# Field report triage

One row per point in the field report of 2026-11-20 to 2026-11-22: five rounds
of heavy use in one project (a C monorepo), with a conductor agent and up to
15 agents in parallel, each running programs against one stdio MCP server
written for that project. Later rounds revise earlier ones; where they do, the
row follows the latest round.

That project is the first user. Its report is evidence, not the spec: every
row states the general lesson, and nothing built for a row names that
project, its tools or its layout.

**Owner** says where the fix belongs:

- **core**: this runtime (CLI, sandbox, records, policy, output, errors, docs).
- **server**: the project's own MCP server. The lesson goes into
  [`SERVER_AUTHORS.md`](SERVER_AUTHORS.md) as guidance for anyone writing tools.
- **host**: the agent application or the shell around it. Not this runtime.

**Status**: *fixed* (with the IDs of the checks in
[`tests/FAILURE_MODES.md`](../tests/FAILURE_MODES.md)), *guidance* (written
up for server authors), *rejected* (with the reason), or *open*.

"Count" is how many agents reported the point in the round where it was
reported most, as the report gives it.

## Core

| # | Problem | Rounds | Count | General lesson | Status |
|---|---|---|---|---|---|
| 1 | Long runs block and show nothing. A program over about three minutes outlives the agent's shell, which backgrounds or kills it; the agent then waits blind. Wanted: `run --detach`, `get` with the calls so far, progress. | 1 to 5 | 6 of 6, 7 of 10, 6 of 7; the top gap in three rounds | A caller with a short command timeout needs to start, ask and wait in separate commands, and "running" has to mean a process is running it. | **Fixed.** `run --detach`, `wait ID --timeout S` (exit 6 while running), `cancel ID`; `get` shows calls done, the call in flight and for how long, and the output so far; a server's progress notifications are shown; an execution whose process was killed is recorded as `Interrupted` instead of staying "running". R1 to R10, M7. |
| 2 | One failing call loses the whole program. A 40-minute run was lost to one 400 s timeout because the results sat in a variable. Wanted: partial results, or `on_error="return"`. | 4 | 3, and the conductor | Work that finished before a failure must be recoverable without the program having planned for it. | **Fixed**, four ways. The top-level variables of a failed program are kept; the outcome shows their shapes and `get ID --select variables.NAME` returns one (H8). `run --reuse ID` runs the fixed program and takes every step that matches from the earlier log, so nothing that worked runs twice (C7). `call(..., on_error="return")` (H6). A program cut off by `limits.timeout_s` during a call gets two seconds to return what it has (C3). |
| 3 | Programs take no arguments. One program was edited with `sed` 17, 6 and 9 times to change a branch name. | 1, 2, 3, 5 | the conductor each round, 1 agent | A program is a function. Its inputs must not be edited into its text. | **Fixed.** `run --arg NAME=VALUE`, `--args-json`, `--args-file`; the program reads `args`. A paused execution resumes with the same ones. H7. |
| 4 | `search("")` silently stops at 10 tools; the five that change things were missing and nothing said so. | 1, 2 | 5 | A list that was cut must say that it was cut, by how much, and how to get the rest. | **Fixed.** The list carries `.total` and `.more`, a note goes to the logs, and an empty query lists up to 50. H1. The same rule now holds for results over the output budget (shape plus a command that reads a part) and for the commands connector's output. |
| 5 | Result shapes are guessed. `run_command` returns `stdout` and `exit`; agents tried `output`, `exit_code`, `rc`. `describe()` returned a dict that was sliced like text; one program's logs "came back as single characters". | 1, 2, 3 | 3 | A result with no published shape costs one failed program per agent. | **Fixed** in core as far as core can: `shape(value)` shows keys and types; a `KeyError` on a result carries a hint that names it; `describe()` prints as text, says when a tool publishes no result schema, and explains itself when sliced (H2, H4, H10). The rest is **guidance**: publish an output schema. The "single characters" report could not be reproduced; logs are one string end to end, and iterating a string result gives exactly that, which is this row. |
| 6 | Repeat-N helper. Flaky tests needed a hand-written loop each time (40 runs, then 5); wanted `repeat(call, n)` returning only the failures. | 4, 5 | 3 | Running one call many times and keeping only what failed is general. | **Fixed.** `repeat(path, args, n, ok=..., until_fail=..., concurrency=...)`. H5. The "under CPU load" variant is **rejected**: a load generator on a shared machine is what round 4 reports as having slowed nine other agents. |
| 7 | The approve hint drops `--home`; run as printed it would not find the execution. | 1 | 1 | A hint is a command. It has to work as printed, from any folder. | **Fixed.** Every hint repeats the launcher, `--config` and `--home` of the process, with absolute paths. E2. |
| 8 | Paused executions stay for ever; after the policy changed, an old pause could still be approved. | 1 | 1 | An approval answers a question that was asked at one moment, under one policy. | **Fixed.** A deny rule beats an approval (L10); the outcome says the policy changed; a pause older than `approval.ttl_s` needs `approve --stale` (L14); `prune` removes old records. |
| 9 | `py -I main.py` cannot import the package. | 1 | 1 | An entry script must not depend on how the interpreter was started. | **Fixed.** `main.py` puts its own folder on the path; there is a `codemode` entry point and `python -m codemode_runtime`. E1, E6. |
| 10 | Reading one field of the result needed a second process. Wanted `--select result.x`. | 1 | 1 | The caller should be able to ask for the part it needs. | **Fixed.** `--select PATH` on `run` and `get` prints one part: `result.count`, `variables.res`, `logs`. H12. |
| 11 | No dry run: broken quoting was found only by running. Wanted `run --check`. | 1 | 1 | Syntax and tool names can be checked without side effects. | **Fixed.** `run --check` compiles in the sandbox and looks up every tool the program names. C8. |
| 12 | The last-expression rule: `return` is a syntax error, and an expression inside an `if` returns null. | 4 | 3 | A silent null where a value was meant is a wrong result. | **Fixed.** The null result comes with a warning that says why and what to write instead (H9). `return` already had a hint. |
| 13 | A backslash in a program string (`\U` in a Windows path) was a syntax error. | 5 | 1 | An error should name its likely cause. | **Fixed.** The hint names forward slashes and raw strings. H10. |
| 14 | Programs mangled by a shell: `\n` in a heredoc became a real line break. | 2 to 5 | 8 of 10 at the worst | The shell between an agent and a program is a place where code changes. | **Fixed** for programs, as far as a runtime can: a string broken by a line break is a syntax error whose hint names the shell and the fix (H10); the docs say it first; and the MCP `execute` tool takes the program as JSON, where no shell is involved. "Take programs from a file only" is **rejected**: inline code and stdin are documented and in use. Edits to a project's *sources* through a shell are **host**, row 49. |
| 15 | Two tool calls for every step: write the file, then run it. | 1 | 1 | Round trips are the cost code mode exists to cut. | **Fixed** where it can be: a program reused with other arguments is one call (row 3), and over MCP `execute` takes the code in the call. A new program through the CLI is still a file and a command. |
| 16 | Long builds need big limits: the project set `call_timeout_s` to 3000 for every tool. | 1 | note | One time limit cannot fit a status query and a half-hour build. | **Fixed.** `limits.call_timeouts` per tool, `call_timeout_s` per connector, and a timeout error that names the limit that fired and how to raise that one. C1, C3. |
| 17 | The server runs one call at a time; a commit queued behind a test run timed out. | 4 | 1 | `parallel()` against a server that takes one request at a time is sequential, and the wait counts against each call's limit. | **Fixed** on this side: `max_concurrency` on a connector queues calls here, and a queued call's clock starts when the call does (C2). The cause is **guidance**: handle requests concurrently. |
| 18 | A `cli` connector: the project has no MCP server, so about 500 lines of server were written just to wrap commands. | 1 | 1 | A project whose work is commands should be able to adopt without writing a server. | **Fixed.** The `commands` connector turns declared command lines into tools: declared parameters, no shell, environment by allowlist, a structured result with honest truncation. K1 to K10. |
| 19 | About 8 to 10 s of overhead per `run`. | 3 | 1 | Measure before optimising. | **Rejected as a core problem**, with the measurement: on the reporting machine an empty program takes 0.13 s and one that starts that project's server and lists its 21 tools takes 0.17 s. The rest is the caller's shell and the tools themselves. What core did anyway, for projects with several servers: servers start side by side, and tool lists are cached on disk (M11, M14). |
| 20 | Bare `python` ran by mistake and opened an installer. | 2 to 5 | 4 | A tool's own messages should not teach the wrong command. | **Fixed** on this side: hints print the interpreter that is running, or `codemode`, never a bare `python`; the docs use `codemode`. The shell guard itself is **host**. |
| 21 | An empty heredoc left an interpreter waiting on stdin until stopped. | 5 | the conductor | A command given nothing should not wait for ever. | **Fixed** for this CLI: `run` with no program and an open, silent stdin fails after 5 s (E4). The interpreter's own behaviour is **host**. |

## Server: guidance for tool authors

These belong in the project's tools. Each is written up, as a rule with the
reason, in [`SERVER_AUTHORS.md`](SERVER_AUTHORS.md). Where this runtime can
soften the problem from its side, the row says how.

| # | Problem | Rounds | Count | General lesson | Status |
|---|---|---|---|---|---|
| 22 | `run_command` has no `env` argument: every runner call was wrapped in a shell that sets variables first. | 1 to 5 | 10, 6 of 6, 7 of 7 (about 70 calls); the most frequent point | A tool that runs a command takes its environment, working directory and time limit as arguments. | **Guidance.** The `commands` connector does it: `allow_env` names what a caller may set. |
| 23 | The schema lists only some accepted values: a build type that works was not listed, and one agent concluded there was no test build. | 2, 3, 4 | 6 | Agents believe the schema. An enum must be complete or absent. | **Guidance.** |
| 24 | The test tool gives too little on failure: empty output, no single-test run, no rerun. | 2, 3 | 5 | A failure result carries the evidence: which test, the first failing lines. | **Guidance.** `repeat()` covers reruns from this side. |
| 25 | Build warnings are noise: notes shown as warnings on every build, and nothing tells new from old. | 2, 3, 4 | 6 | Classify at the source; compare against a baseline. | **Guidance.** |
| 26 | A tool ran stale inputs and did not say so: the test tool ran an old build (53 tests where there were 56), three rounds in a row. | 3, 4, 5 | 3 rounds | A tool must say what it did not do. | **Guidance.** |
| 27 | `ok` without the work: a build returned ok without relinking when the output was locked. | 4 | 3 | `ok` means the thing happened. | **Guidance.** |
| 28 | No PASS/FAIL field: a runner returns one big string, and an empty filtered result was ambiguous. | 4, 5 | 4 | The outcome is a field, not something to find in text. | **Guidance.** The `commands` connector returns `ok` and `exit`. |
| 29 | A capped list with dishonest counts: the board listing returned at most about 50 done tasks, and programs reported "done 50" for three rounds. | 5 | the conductor | When a list is capped, return the real count next to it. | **Guidance.** The core form of this is row 4. |
| 30 | No get-one call: the listing has no descriptions, so an update replaced text it had never seen. | 5 | the conductor | Offer a list call and a get-one call; make replace and append different operations. | **Guidance.** |
| 31 | Repo-level scripts return unparsed text: a check printed about 15 kB and the word FAILED. | 2 to 5 | 2 | Wrap a script in a tool that parses its output once. | **Guidance.** `summarize()` and `grep()` help from this side. |
| 32 | A commit tool under load and with CRLF: it timed out at 60 s while ten agents ran, stripped line endings it should have kept (a 941-line diff), and refused a tracked file. | 4, 5 | 6 | A write tool keeps what it was not asked to change, and has a time limit that fits the machine it runs on. | **Guidance.** Row 16 lets the project give one tool more time. |
| 33 | A commit left 13 new files out and its branch did not build. | 3, 4 | the conductor | A write tool reports what it left out. | **Guidance.** |
| 34 | Results are large by default: about 2 kB of artifact list per build, about 12 kB of stdout per runner. | 4 | 6 | Small by default, detail on request. | **Guidance.** |
| 35 | A command cannot be stopped early: a 19-minute rerun. | 4 | 4 | Honour cancellation. | **Guidance.** This runtime now tells the server when it gives up on a call (M8), and the `commands` connector stops the process tree. |
| 36 | Two builds at once fail on a shared output folder. | 4 | 6 | A tool that needs something exclusively takes a lock, or says it is busy. | **Guidance.** |
| 37 | Field names differ between tools (`app`, not `name`); sizes are rounded; a cap of about 1.5 kB on one result was undocumented. | 1, 3, 4 | 1 each | Consistent names, exact numbers, declared limits. | **Guidance.** |
| 38 | A file tool returned parsed data for a file that happened to hold JSON. | 2 | 3 | Say whether a tool returns text or data. | **Guidance**, and a switch here: `parse_json_text: false` on a connector keeps text as text (M4). |
| 39 | A quirky API (tags as one string, a new task always lands in one column) cost a broken task. | 1 | 1 | Wrap the quirks in the tool, once. | **Guidance.** |
| 40 | Tools asked for by name: a script runner with `env` and `only`, a baseline build of the merge base, a size diff, a symbol breakdown, credential cleanup, a status and a diff tool. | 2, 4 | 5 | Tools follow the project's workflow. | **Project.** The `commands` connector makes the simple ones a few lines of config. |
| 41 | An escape-free edit tool, and an `fs` connector, so that "write the measured numbers into the docs" stays one program. | 1, 4 | 8 of 10 | Exact, encoding-preserving file edits are a tool like any other. | **Guidance**, and not built in: file tools exist as MCP servers. The reference filesystem server was run through this runtime (see `VERIFICATION.md`). |
| 42 | Workspaces and merges are hand-made every round: a merge program grew to 37 runners and a three-way changelog merge. | 5 | the conductor | A project's own recurring procedure belongs to the project. | **Fixed as a mechanism.** A project keeps such functions in a helpers folder and programs call them as `helpers.<file>.<function>()`; they run in the sandbox under the same policy. The procedures themselves stay the project's. |

## Host: not this runtime

| # | Problem | Rounds | Count | Status |
|---|---|---|---|---|
| 43 | Foreground commands are moved to the background at about 180 s; a `sleep` monitor returns at once; a backgrounded shell job reports done at once. | 4, 5 | 6 | **Host.** Row 1 is this runtime's answer: `wait ID --timeout S` fits any command timeout. |
| 44 | The edit tool needs a prior read and fails multi-line matches on CRLF files. | 5 | 4 | **Host.** |
| 45 | Resource hygiene: one agent searched the whole disk for an hour, one ran busy loops on purpose. | 4 | 2 | **Host and brief.** The instruction block in `ADOPTING.md` says to stay inside the project. |
| 46 | A "finished" notice arrived about four hours late. | 5 | 1 | **Host.** |
| 47 | A skill path in a project file does not exist; one project file says `python` where the brief says `py`. | 3, 4 | 1 | **Project documents.** |
| 48 | The project's UI test driver: a key left held, clicks that need a priming move, a hidden desktop that cannot be captured. | 2 | several | **Project test harness.** |
| 49 | Project sources corrupted by `sed` and heredocs: `\n`, `\t`, `\\` changed or dropped; one silent escape compiled and broke a feature. | 2 to 5 | 8 of 10 | **Host.** The agent's file tool is the escape-free path. Row 14 covers the part that concerns programs. |

## What the report said worked

Kept, and checked by the suites so that it stays true:

- Many calls and little output: 44 calls in 1.5 s with 5 kB returned; a
  5.6-minute test run returned twenty lines.
- `parallel()` with one line per call. (Against a server that takes one
  request at a time the calls ran one after another without anyone noticing:
  row 17.)
- `state` across programs; Python glue inside the program.
- A policy that stays out of the way when the project allows everything.
- Isolation of parallel agents through one environment variable in the
  connector's `env`, each agent with its own config and data directory.
- Errors that read as a one-line fix.
- Dedicated tools that enforce the repository's rules, where raw commands
  did not.

## Second report: a tool that ships a copy of the runtime

A command-line tool written in another language copied this runtime at one
commit, starts it under its own command name, and writes the config itself.
Its notes (2026-11-22) list four field findings and seven suggestions. It
changed no copied file; what it needed it did by replacing nine private
names at start-up and reading a tenth, which is the evidence here: each of
those names is a missing public setting.

| # | Point | General lesson | Status |
|---|---|---|---|
| 50 | **An agent approved its own request.** `approve ID < /dev/null`, run by an agent, approved. So did a plain `approve ID` from a shell tool that gives its commands no input. | "Is stdin a terminal" has to be asked of the system, not of `isatty()`: on Windows that is true for every character device, and the null device is one. A guard must be tested with each thing a shell tool can put on stdin. | **Fixed.** On Windows the console is asked (`GetConsoleMode`); a pipe, a file and the null device are refused on every platform. L16, with a mutant. Reproduced before the fix and repeated after it; a real console is still accepted. The limit is now written down in `ADOPTING.md`: a tool that types into a real terminal looks like a person. |
| 51 | Shells change backslashes in a program passed through a heredoc. | Row 14. | **Already covered**: `run -f FILE`, `run -`, and the instructions tell agents to write programs with their file tool. The tool's own check confirms it. |
| 52 | A stdio server starts with a short list of host variables. The tool wrote one `${NAME}` reference per variable its server needs, and the Windows folder variables the list leaves out. | A server started by the runtime should find its folders as it would when started by anything else, and a project should not have to name forty variables one by one. | **Fixed.** `inherit_env` on `mcp-stdio` connectors, as the commands connector has, from one shared function: names and globs, compared as the platform compares them; an inherited variable whose name says it holds a credential is removed from results. The base list now holds the folder variables of each platform (the Windows program and profile folders, `XDG_*`), the locale and the time zone. M17, M18. |
| 53 | One approval per gated call is tedious when calls come one after another. Wanted: `approve --all-remaining`, or an approval for one tool for the rest of one execution. | An approval covers what the person was shown. | **Open, the owner's call; not recommended as asked.** `--all-remaining` approves calls nobody has seen, with arguments the program computes later. What exists: calls in one `parallel()` pause together and take one approval; `run --interactive` answers each call as it comes; an `allow` rule with argument patterns lets the safe ones through for good. |
| 54 | Text of the project's own in the tool description and in the instructions (two functions replaced). | A project has things to tell its agents that the runtime cannot know. Today that works only where the project owns an instructions file; an agent that reaches the runtime as an MCP server reads nothing but the tool description. | **Open, proposed**: a config key for a short text of the project's own, added to both, with a size limit that says when it cut. |
| 55 | `init --out FILE`, and the `.gitignore` entries as a parameter (three names replaced). | A project may keep tool configs in a folder of its own. | **Fixed.** `init --out FILE` writes the config anywhere, and the commands it prints name it. The three places the runtime used to fix are now the project's to choose: the config (`--out`, then `--config`), the data folder (`--home`), and the folder agents write programs into (the new `work_dir` key, `init --work-dir`). `.gitignore` gets the folders really in use, none for one outside the project, and `instructions` names the same folders. U12. |
| 56 | A public way to list one namespace's tools with descriptions (a private attribute was read). | | **Fixed.** It was mostly there: `search "" --limit N --json` gives path, signature, the first 160 characters of the description and the policy decision for up to 200 tools, and `describe NAMESPACE` the signatures. What was missing is the question itself, "what does this one server offer": `search --namespace NAME` on the command line and `search(namespace="name")` in a program answer it, in the server's order, with the honest total. A search without a namespace is recorded as before, so older executions still replay. H13. |
| 57 | A command name that can be set from outside, so that hints name the embedding tool's command (three names replaced), also in a detached worker. | A hint has to be a command that works where it is read. Under another tool's command name only that tool knows what it is. | **Fixed.** `CODEMODE_COMMAND` holds the command line people type; hints, `--help` and `instructions` print it. `{options}` in it is where `--config` and `--home` go; without it nothing is added, because such a command chooses them itself. A detached worker inherits the variable and prints the same. E8. |

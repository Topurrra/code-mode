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
| 53 | One approval per gated call is tedious when calls come one after another. Wanted: `approve --all-remaining`, or an approval for one tool for the rest of one execution. | An approval covers what the person was shown. | **Built, on the owner's decision**, in both forms the report named, with the limits that keep it one person's choice about one execution. `approve --same-tool` covers later calls to the tools approved now; `approve --all-remaining` every later call the policy asks about. Both hold for that execution only and until `approval.ttl_s`; neither covers a deny rule; both need a terminal, and a program or the MCP tool cannot give one; each call that runs under one gets its own approval entry in the log, before it runs, naming the person; every outcome lists them (`standingApprovals`); `approval.standing` (`same-tool`, `off`) narrows what a project accepts. The cost is stated in the README: with `--all-remaining`, calls the policy asks about run without having been shown to anyone. Narrower ways stay: one `parallel()`, `run --interactive`, an `allow` rule with argument patterns. L17. |
| 54 | Text of the project's own in the tool description and in the instructions (two functions replaced). | A project has things to tell its agents that the runtime cannot know. Today that works only where the project owns an instructions file; an agent that reaches the runtime as an MCP server reads nothing but the tool description. | **Fixed.** `guidance` in the config, a text or `{"file": ...}`, is added to the tool description and, under its own heading, to the instructions in both forms. A file is read when needed; a text over `limits.max_guidance_chars` (4000) is cut and says how much is missing and where it is; a file that cannot be read fails `instructions` and `doctor`, and the tool description says so instead of failing. U13. |
| 55 | `init --out FILE`, and the `.gitignore` entries as a parameter (three names replaced). | A project may keep tool configs in a folder of its own. | **Fixed.** `init --out FILE` writes the config anywhere, and the commands it prints name it. The three places the runtime used to fix are now the project's to choose: the config (`--out`, then `--config`), the data folder (`--home`), and the folder agents write programs into (the new `work_dir` key, `init --work-dir`). `.gitignore` gets the folders really in use, none for one outside the project, and `instructions` names the same folders. U12. |
| 56 | A public way to list one namespace's tools with descriptions (a private attribute was read). | When two users each build the same list by hand (the first kept one in its instructions file), the tool is missing an answer. | **Fixed.** It was mostly there: `search "" --limit N --json` gives path, signature, the first 160 characters of the description and the policy decision for up to 200 tools, and `describe NAMESPACE` the signatures. What was missing is the question itself, "what does this one server offer": `search --namespace NAME` on the command line and `search(namespace="name")` in a program answer it, in the server's order, with the honest total. A search without a namespace is recorded as before, so older executions still replay. H13. |
| 57 | A command name that can be set from outside, so that hints name the embedding tool's command (three names replaced), also in a detached worker. | A hint has to be a command that works where it is read. Under another tool's command name only that tool knows what it is. | **Fixed.** `CODEMODE_COMMAND` holds the command line people type; hints, `--help` and `instructions` print it. `{options}` in it is where `--config` and `--home` go; without it nothing is added, because such a command chooses them itself. A detached worker inherits the variable and prints the same. E8. |

### For anyone who wraps or ships the runtime

What that tool replaced, and what to use instead (from version 0.3.0). None
of the private names is promised to stay.

| Replaced or read | Use |
|---|---|
| The launcher's prefix, its global options and its program name | `CODEMODE_COMMAND`, set in the environment the runtime is started in |
| The function that builds the tool description, to add text | `guidance` in the config: a text, or a file the wrapper writes |
| The function that prints the instructions, to add text and to name another folder for programs | `guidance`, and `work_dir` in the config |
| The config's file name, to write it somewhere else | `init --out FILE`, then `--config FILE` or `CODEMODE_CONFIG` |
| The list of `.gitignore` entries | Nothing to set: the entries follow `--home` and `work_dir` |
| The check for a terminal | Nothing to set: fixed |
| The catalog's private table of tools, to list one server's tools | `search --namespace NAME --json`, or `Runtime.search("", limit, namespace)` in the same process; with `--full` (`full=True`) each description is whole |
| One `${NAME}` reference per variable a server needs | `inherit_env` on the connector, with names and globs, and `!NAME` for one to leave out |

`init` leaves the runtime's own server out of the config by itself, under
its own name and, with `CODEMODE_COMMAND` set, under the wrapper's. Two
things the notes describe stay as they are: leaving any other server out is
done by editing the config after `init`, and there is no setting for it;
and what a wrapper passes to its own server in `env` is the wrapper's
business.

## Third note: after the same tool moved to 0.3.0

Five points, from its agents using the new settings (2026-11-23).

| # | Point | General lesson | Status |
|---|---|---|---|
| 58 | One option has two names: `call(..., on_error="return")` and `parallel(..., return_errors=True)`. | An agent that learned the option on one function writes it on the other. Two names for one thing is an error waiting for every new reader. | **Fixed.** `parallel()` takes `on_error` with the same values and the same complaint about a misspelt one. `return_errors=True` still works; the two disagreeing is an error. H14. |
| 59 | A relative path fails in a program: `open("modules/x")` does not find a file of the project. Wanted: run programs from the project root, or tell them where it is. | The program runs in an empty folder on purpose, so that what it writes by a relative path does not land in the project. Then it has to be told where the project is, and the failure has to explain itself. | **Fixed, the second way.** `project_root` is the project's folder, in programs and in helpers. It is recorded with the execution, so a program resumed from another folder is told the folder it started with. A file not found by a relative path fails with a hint that says what happened and what to write. The working folder stays empty. H15. |
| 60 | A listing cuts each description to 160 characters, so a tool that builds a prompt from the list calls `describe` once per tool. | A short listing is right for a program and wrong for a caller that is building something out of it. | **Fixed.** `search --full`, and `full=True` on `Runtime.search`, give whole descriptions with the limit and the marker `describe` has. The listing a program gets from `search()` stays one line each: it has `describe()` for the one tool it is about to call. H16. |
| 61 | `inherit_env` cannot leave a variable out of a family: every `MYTOOL_*` but one. | A glob that takes a family in needs a way to keep one member out, or the family gets spelled out name by name again. | **Fixed.** A name or a glob after `!` is left out, whatever else matches it, the base list included: `["MYTOOL_*", "!MYTOOL_DEBUG"]`, `["*", "!AWS_*"]`. The order does not matter. M19. |
| 62 | `init --out` always tells the person to pass `--config` on every command, also under a command that finds the config by itself. | Advice that is right for one way of starting the runtime is noise, or wrong, for another. | **Fixed.** With `CODEMODE_COMMAND` set and no `{options}` in it the note is not printed; with `{options}`, and with no such command, it is. U14. |

## Fourth report: rounds 6 to 10 of the first user

Five more rounds in the same project (2026-11-22 to 2026-11-25), the first
ones on versions 0.2.0 to 0.4.0: between 4 and about 30 agents a round, all
through this runtime. The report first answers the six questions the first
triage left for it. `run --detach` with `wait` ended the blind waiting,
`--arg` ended the editing of programs, `repeat()` and `--reuse` were used
and did what was wanted, and `max_concurrency: 1` stopped queued calls from
timing out. The rows below are what it found wrong after that.

"Count" is as the report gives it; "several" where it gives no number.

### Core

| # | Point | Rounds | Count | General lesson | Status |
|---|---|---|---|---|---|
| 63 | **A value given with `--arg` was read as JSON.** `dry=1` became a number, `args["dry"] == "1"` was silently false, and a dry run committed. `[..]` became a list and `json.loads` failed. | 6, 7, 8, 10 | 4, several, 3 | What is typed on a command line is text. A tool that guesses a type makes the same text mean two things, and the reader finds out by an accident. | **Fixed**, and the one change of behaviour in 0.5.0. `--arg` gives text; `--arg-json NAME=JSON` gives a typed value. A program written for the old reading keeps its answer where the difference would be silent (`== 1`, a truth test of `false` or `0`, `range(args["n"])`) and gets a warning that says what to write; where the difference is loud (arithmetic, a typed tool argument) it fails with a hint. H17. |
| 64 | **`wait` is cut by the caller's shell.** A shell tool ends a command after two or three minutes, so a ten-minute suite needed a loop of `wait --timeout 100`, which every agent wrote again. | 6, 7, 8, 10 | every agent | A wait is one command that ends when the work ends, and shows that it is alive while it waits. | **Fixed.** `wait ID --until-done` has no time limit and writes a line to stderr every 30 s (`--every`): how long, the calls done, the call in flight. Run as one background command, it ends when the program does. A plain `wait` that runs out of time names it in its hint. R12. The cap of the shell is the host's; row 87. |
| 65 | `wait` had no `--select`; `--select a,b` was not supported; `--select` did nothing with `--detach`, without a word. | 6, 7, 8 | several | An option that one command of a family takes, the others take. An option that cannot do anything says so. | **Fixed.** `--select` on `wait`; several paths give `values` by path, one path keeps its shape; `run --detach --select` answers with the `wait` command to use. R13. |
| 66 | The id of a run is easy to lose. `ls .codemode/executions` shows file names that `wait` refused. Two agents did not find `executions`. A foreground run that the shell moved to the background had printed no id. | 6, 7 | 2 and the conductor | Whatever a person has in hand that names the thing is accepted as its name. | **Fixed.** A log file's name or path is taken as the id everywhere. `last` names a run that is still going, and the instructions say so under "lost an id". E9. Printing the id on stderr at the start of every run was **rejected**: callers read stdout and stderr together as one JSON value. |
| 67 | No partial results while a program runs: `get --select variables.X` was refused. | 8 | several | What exists is readable; what does not exist yet says when it will. | **Fixed in part.** While a program runs, `logs`, `progress`, `calls`, `args` and `code` can be selected. `result` and `variables` are in the sandbox until the program ends: asking for them gives the running outcome with a hint (print in the program; `--select logs`). R14. |
| 68 | Errors from tools had no hint: an unknown tool had its close names only in the message; a tool that refused its arguments said nothing to act on. Hints were followed wherever there was one. | 6 | several | A hint that exists gets used, so every failure a program can act on carries one, also when the tool gave none. | **Fixed.** An unknown tool has the close names in the hint. A `ToolError` with no hint of its own gets the tool's signature and the `describe()` call. H20. |
| 69 | Most tools publish no result schema, so programs guessed `failures`, `output` or `stdout` and lost runs. | 6, 10 | several | When the author of a tool has not said what comes back, the last answer is the best evidence there is, as long as it is shown as evidence. | **Fixed.** After a call of such a tool succeeds, `describe()` shows `lastResultShape`: keys and types, no content, with a note that it is not a promise. H21. The rule for authors stands: publish an `outputSchema` (row 5). |
| 70 | A server that takes one request at a time blocks everything: a suite of 3 to 13 minutes held up reads, commits and searches, and agents sat idle. The largest cost of round 10. | 6, 10 | every agent | Row 17 gave the queue. A queue in front of a slow thing should hold the slow things only. When the server cannot be changed, the caller can still run more than one of it. | **Fixed** from this side. `"processes": N` starts up to N copies of a stdio server and gives each one call at a time; `"serial": [patterns]` keeps the tools named there one at a time. With both, a read passes while a build runs, and two builds never overlap. Not for a server that keeps something in memory between calls. M20 to M22. The rule for authors stands: answer reads while a long call runs. |
| 71 | `cancel` ended the execution and left the server's call, and the processes under it, running; the next caller of a one-at-a-time server waited behind work nobody wanted. | 8 | several | Telling a server to stop is a request. When it matters that the work stops, there has to be a way that does not need the server to agree. | **Fixed.** `"on_abandon": "restart"` on a stdio connector: a server that has not ended a call within 2 s of being told is stopped with everything it started, and the next call starts a new one. On Windows, stopping a server now reaches a process whose parent had already exited (a build behind a wrapper), which `taskkill /T` does not. M23, M24. |
| 72 | A relative `cwd` in a config was read from the folder of the process, not of the config: `"cwd": "."` worked from the project folder and nowhere else. | 6 | 1 | A path in a file is read from where the file is. | **Fixed**, in a way that breaks nothing that worked: with a config named by `--config`, a relative path is read from the current folder first and from the config's folder when it is not there. A missing file's error names both folders. M15. |
| 73 | The result is the last expression, so a value inside an `if` gave null, and `return` was a SyntaxError. Hit again after a warning was added. | 4, 5, 10 | 3, 1, the conductor | Row 12 answered this with a warning. A rule that people keep tripping over after being told is the wrong rule. | **Fixed.** `return VALUE` at the top level ends the program with that value. A program that ends with an `if`, a `with` or a `try` gives the value of the branch that ran. A value at the end of a loop still does not count, and the warning says so. H18, H19. |
| 74 | A flag kept in `state` made a release program skip all its work and return the earlier result in one second. It looked like a success. | 9 | the conductor | A result that came from memory looks the same as one that came from work, unless something says which it was. | **Fixed** as a signal: a completed program that called no tool, with a `state` that was not empty, gets a warning that says so. R15. Keying `state` by an argument is the program's own business. |
| 75 | Printing a result with non-Latin text crashed a pipe on a legacy code page. | 6 | 1 | Output must not depend on the console. | **Not reproduced**; the streams have written what they cannot hold as backslash escapes since 0.2.0. Now covered by checks on results, logs, hints and selected values. E10. |
| 76 | Running this project's own tests rewrote a tracked file, the end-to-end trace. | 6 | 1 | A test run leaves the working tree as it found it. | **Fixed.** The trace goes to `.e2e/`; `--update-trace` writes the tracked copy. |
| 77 | The server's environment is a short list, and a missing `OS` cost an hour. | 6 | 1 | Row 52. | **Fixed in 0.3.0**: a wider base list (with `OS`), `inherit_env`, M17. Listing the variables in `doctor` was not built. |
| 78 | `instructions` prints the full path of one Python. | 6 | 1 | Row 57. | **Fixed in 0.3.0**: `CODEMODE_COMMAND` sets the command every hint and the instructions use. |
| 79 | Project helpers went unused: the merge program and the workspace script stayed scripts. | 6 | the conductor | A feature nobody is told about at the moment they need it is not there. | **Fixed** for the telling: `instructions` lists a project's helper functions when it has any. Suggesting the folder after ten runs of one program was not built. |
| 80 | A script that only edits files wanted `run --cwd`, so that its relative paths work. | 9 | the conductor | Row 59. | **Rejected.** A program that calls no tool is not a program for this runtime, and the instructions say so. `project_root` is there for the file a program does need. |

### Server: guidance, not this runtime

| # | Point | Rounds | Count | General lesson | Status |
|---|---|---|---|---|---|
| 81 | Build and test tools with no options for a build folder, extra defines, a label or an exclude filter; every special build went through a raw command. | 6, 7, 8 | 7 | A tool that wraps a command passes through the two or three options people reach for, or people go around the tool. | **Guidance.** |
| 82 | A test tool that reports "1 passed" when 52 tests were never built, an empty `failures` for a failing test, a crash that is in no list, no output for a passing test. | 7, 8, 10 | several | Rows 24 and 26: a failure carries its evidence, and a tool says what it did not do. | **Guidance.** |
| 83 | A build tool that hides the configure step's error and says only that the build failed. | 10 | 2 | The first error is the result. | **Guidance.** |
| 84 | A command tool with no `env`, a relative program that is not found, a search path without the toolchain, output cut to the last 120 lines, a wait for orphaned children. | 6 to 10 | 7 of 7 at most | Rows 22 and 28. The `commands` connector of this runtime does each of these. | **Guidance.** |
| 85 | A commit tool that commits everything for an empty path list, leaves new files out without a word, fails on a renamed file, and cannot commit part of a file. | 7, 8, 10 | several | Row 33: a write tool reports what it left out, and an empty selection is not "everything". | **Guidance.** |
| 86 | A listing that returns an empty list for a path that does not exist, and a test list that is empty before the first build. | 6, 7 | several | Nothing found and nothing there are two answers. | **Guidance.** |
| 87 | Tools asked for by name: a window-test runner with a pass or fail for each script, a size and symbol diff against another ref, a build of another ref, a debugger helper, a first-frame measure, a quick compile of changed files, a release lister. A shared compiler cache for new worktrees. | 6 to 10 | 1 to 5 each | Row 40. | **Project.** |

### Host

| # | Point | Rounds | Count | Status |
|---|---|---|---|---|
| 88 | A shell tool ends a command after two or three minutes and reports a backgrounded job as done at once. | 6 to 10 | every agent | **Host.** `wait --until-done` as a background command is the answer from this side (row 64). |
| 89 | Git Bash rewrote a `C:/...` value of `--arg` into another path. | 7 | 1 | **Host.** `ADOPTING.md`, under Platforms, names the variable that turns it off. |
| 90 | A flaky test under the load of an eleven-app run; a conflict marker committed by a hand-written merge script. | 6, 9 | the conductor | **Project.** `repeat()` judges a flake in one call. |

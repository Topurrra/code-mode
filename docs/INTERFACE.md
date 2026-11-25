# Interface reference

What a caller can rely on: the commands, the JSON that comes back, the exit
codes, and the `execute` MCP tool. Things listed here are stable: they are
added to, not renamed or removed. Everything is exercised by
[`tests/e2e.py`](../tests/e2e.py).

## The outcome of an execution

Every surface returns the same object.

```text
{"status": "completed", "executionId": "ex_...", "result": ..., "logs": "...", "stats": {...}, "verification": "none"}
{"status": "paused",    "executionId": "ex_...", "pending": [{"seq": 2, "tool": "ns.tool", "args": {...}, "rule": "default"}], "hint": "..."}
{"status": "error",     "executionId": "ex_...", "error": {"type": "...", "message": "...", "hint": "...", "traceback": "..."}}
{"status": "rejected",  "executionId": "ex_...", "error": {"type": "Rejected", "message": "...", "hint": "..."}}
{"status": "running",   "executionId": "ex_...", "progress": {...}, "logs": "...", "hint": "..."}
```

| Field | When | Meaning |
|---|---|---|
| `status` | always | `completed`, `paused`, `error`, `rejected` or `running`. |
| `executionId` | always, once an execution exists | Its id. Sortable by start time. |
| `result` | completed | The value of the program's last top-level expression, or of the last expression of the `if`, `with` or `try` branch that ran when the program ends with one, or the value of a top-level `return`. |
| `resultTruncated`, `resultChars`, `resultShape` | completed, result over `max_result_chars` | `result` is then a text preview. The shape shows what is inside; `--select` reads a part. |
| `resultIsRepr` | completed | The result was not JSON; `result` is its `repr`. |
| `resultOmitted` | completed | The result was too large to keep in the log. |
| `warnings` | any | A list of texts: a null result from a value at the end of a loop, of a `finally` block or of a `try` body that has an `else`; state that did not fit; a policy that changed before an approval; a call cut off by the program's time limit; a program that called no tool although its session `state` was not empty; a value given with `--arg` that was compared, tested or used as a number (see `--arg`). |
| `verification` | completed | `none`, `replay-ok` or `replay-failed`. |
| `pending` | paused | The calls waiting for approval: `seq`, `tool`, `args` (cut to 2000 characters, then `argsTruncated`), `rule`. |
| `standingApprovals` | any, once a person approved in advance | A list of `{tools, by, given, expires, calls}`: the tools covered (`["*"]` is every tool), who approved, when, until when (`null`: no limit), and how many calls have run under it. |
| `error.type` | error, rejected | A stable name. See below. |
| `error.message` | error, rejected | What happened. |
| `error.hint` | when there is one | What to do next. |
| `error.traceback` | program errors | The program's own frames only. |
| `variables` | error, when the program had any | The shapes of its top-level variables at the failure. `--select variables.NAME` returns a value. |
| `progress` | running | `phase` (`starting` or `running`), `elapsedS`, `callsDone`, `callsFailed`, `running`: a list of `{seq, tool, forS, reported}`. |
| `logs` | when there is output | What the program printed, cut to `max_log_chars` keeping both ends. For a running execution: so far. |
| `logsDroppedChars` | when logs were cut | How much was dropped. |
| `stats` | after a pass | `toolCalls`, `replayed`, `reused` (with `--reuse`), `toolResultBytes`, `durationMs`. |
| `hint` | when there is one | What to do next with this outcome: how to approve, how to read the rest, how to continue. |

With `--select PATH` the object is
`{"status", "executionId", "select": PATH, "value": ...}`, plus
`valueTruncated`, `valueChars` and `valueShape` when the value was over the
budget. Paths are dotted, with list indexes: `result.rows.0.id`,
`variables.res`, `logs`, `error.message`, `stats.toolCalls`, `args`, `code`,
`calls`. The part `calls` is a list of `{seq, kind, name, state}`, one for
each request the program has made.

`--select` may be repeated or written `a,b`. The object is then
`{"status", "executionId", "select": [PATH, ...], "values": {PATH: ...}}`,
plus `valuesTruncated` (the paths that were cut, each with its size) and a
`hint` when a value was over the budget. One path keeps the shape above.

On an execution that is still running, `logs`, `progress`, `args`, `code`
and `calls` can be selected. `result` and `variables` do not exist yet: the
answer is the outcome of the running execution (`status: running`, exit 0,
no `select` key) with a `hint` in front that says when they exist and what
to read meanwhile.

### Error types

| `error.type` | Meaning |
|---|---|
| a Python exception name (`KeyError`, `SyntaxError`, ...) | The program raised it. |
| `ToolError` | A tool reported a failure, or the server returned a protocol error. When the server gave no hint, the `hint` shows the tool's signature and says that `describe()` shows the schema. From an `openapi` connector: the API answered with an error status or a redirect, and the message starts with it (`HTTP 404 Not Found from GET /notes/{id}: ...`). |
| `ToolTimeout` | A call did not answer within its time limit. The message names the limit. With `on_abandon` set to `restart` it also says that the server is stopped if it does not end the call within 2 seconds. |
| `ToolCrashed` | A connector failed in an unexpected way. |
| `PolicyDenied` | The policy denied the call, or a person refused it. Final. |
| `UnknownTool` | No such namespace or tool. The message and the `hint` suggest close names. |
| `ConnectorUnavailable` | A server could not be started or reached, or the OpenAPI document of an API could not be used. |
| `ValueTooLarge` | Arguments or a result over `max_value_bytes`. A result is saved; the message says where. Also an answer of an HTTP API over 16 MB, which is not read to its end. |
| `CallLimitExceeded` | More than `max_calls` tool calls. |
| `BadArguments` | From the `commands` and `openapi` connectors: the arguments do not fit the tool. Nothing ran and nothing was sent. |
| `CommandNotFound`, `CommandFailedToStart` | From the `commands` connector, before anything ran. |
| `StepError` | The function given to `step()` raised, or made a request. |
| `Timeout` | The program ran longer than `timeout_s`. |
| `Cancelled` | The execution was cancelled. |
| `Interrupted` | The process running the execution was killed, or interrupted from the keyboard. |
| `SandboxCrashed`, `ExecutorStartFailed`, `ProtocolError` | The sandbox process died, did not start, or did not speak the protocol. |
| `ReplayDivergence`, `IndeterminateCall` | A resumed program did not make the same requests; or a call was started and its outcome never recorded. |
| `Rejected` | A person rejected the execution. |
| `UsageError`, `ConfigError`, `StateError`, `TerminalRequired`, `NotFound`, `Busy` | The command could not be carried out. No execution was changed. |

The kinds from `ToolError` to `StepError` in this table are raised inside
the program as `ToolError`, with `.kind` set to the name, and `.message` and
`.hint`. A program can catch them. Uncaught, one ends the program and
becomes its `error`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | completed (also: a command that only reports, such as `get` on a running execution) |
| 1 | the program failed, the execution was rejected or cancelled, `doctor` found a failure |
| 2 | usage or configuration error, or a command that does not apply |
| 3 | paused, waiting for approval |
| 4 | `verify` did not reproduce the recorded result |
| 5 | unknown execution, snippet or tool |
| 6 | `wait` or `cancel`: the execution is still running |
| 130 | interrupted from the keyboard |

## Commands

`--json`, `--config FILE`, `--home DIR` and `--agent NAME` go before or
after any command. With `--json` every command prints one JSON value on
stdout, a usage error included.

| Command | What it does |
|---|---|
| `run [CODE \| -f FILE \| -]` | Run a program. Options below. |
| `get ID` | The current outcome. `--full`: do not cut the result. `--select PATH`: one part; repeat it, or write `a,b`, for several. |
| `wait ID [--timeout S \| --until-done [--every S]]` | Wait for a running execution, then its outcome. Exit 6 if still running after `--timeout` (default 60); the `hint` names the time that ran out. `--until-done` has no time limit: it writes one progress line to stderr every `--every` seconds (default 30) and the outcome once on stdout. It cannot be combined with `--timeout`. Ctrl-C stops the wait and leaves the execution running. `--select PATH`: one part, as for `get`. |
| `cancel ID` | Stop a running execution; reject a paused one. |
| `approve ID` | Approve the pending calls and continue. Needs a terminal on stdin: a pipe, a file and the null device are refused with `TerminalRequired`. `--stale`, `--by NAME`. `--same-tool` also approves, in advance, later calls of this execution to the tools approved now; `--all-remaining` every later call the policy asks about. Either is refused with `StateError` where `approval.standing` does not allow it. |
| `reject ID` | Refuse a paused execution. `--reason`, `--by`. |
| `verify ID` | Replay a completed execution from its log and compare. Exit 4 on a mismatch. |
| `executions [--limit N]` | Recent executions. |
| `show ID` | The audit log of one execution. |
| `search [QUERY] [--limit N] [--namespace NAME] [--full]` | Find tools, as `search()` does in a program. With `--json`, a list of `{path, signature, description, policy}`. `--namespace` keeps it to one namespace (or `helpers`); with no query that is everything the server offers, in its order. An unknown namespace exits 5. A description is one line of at most 160 characters; `--full` gives it whole, as `describe` does. |
| `describe TARGET` | One tool, a namespace, `snippet:NAME`, `helpers`, `helpers.MODULE`. For a tool with no `outputSchema`, once a call of it has succeeded, the result also has `lastResultShape` (keys and types of that result, no content) and `lastResultNote` (it was seen in an earlier call and is not a promise). |
| `init [--from FILE] [--force] [--out FILE] [--work-dir DIR]` | Write `codemode.json` for the project in the current folder, or the file `--out` names. `--work-dir` sets the config's `work_dir`. `.gitignore` gets the data folder and the work folder as this project has them. |
| `doctor [--refresh]` | Check the setup. Exit 1 if a check failed. |
| `prune [--older-than DAYS] [--max-executions N] [--dry-run]` | Remove old records. |
| `instructions [--mcp]` | Print the agent instructions for this project. |
| `config` | Print the resolved configuration, credentials hidden. |
| `prompt` | Print the description of the `execute` tool. |
| `serve` | Serve the `execute` tool over MCP (stdio). |
| `snippet save NAME --from ID`, `snippet list`, `snippet delete NAME` | Reusable programs. |
| `agent TASK` | Run a task with a model on an OpenAI-compatible endpoint. |
| `demo` (or no command) | The zero-config demo. |

`ID` is an execution id, a unique prefix of one, or `last` (the last
execution of the calling agent, when it has a name). The name or the path of
the log file (`ex_....jsonl`, as `executions` lists it) is accepted in its
place by every command that takes an id.

### `run` options

| Option | Meaning |
|---|---|
| `-f FILE` | Read the program from a file. The reliable way: no shell touches the text. |
| `--session NAME` | Which `state` dict the program sees. Default `default`. |
| `--arg NAME=VALUE` | An argument, read as `args[NAME]`. VALUE is always text, whatever it looks like: `--arg n=40` gives `"40"`. Repeatable. Three uses of such a text keep the answer they had when `--arg` read JSON, and each adds a warning to `warnings` (one for each name): `==` or `!=` (or `in`) against a number, `true`, `false` or `null`; a truth test of a text that reads as `false`, `0` or `null`; and use where Python needs a whole number (`range(args["n"])`). Arithmetic with it, or passing it to a tool argument that wants a number, fails. A program that fails with a `TypeError` gets a `hint` to write `int(args['n'])` or to pass `--arg-json`. |
| `--arg-json NAME=JSON` | An argument with a typed value: `--arg-json n=40` gives the number 40. JSON that does not parse is a usage error (exit 2). Repeatable. |
| `--args-json JSON`, `--args-file FILE` | Arguments as one JSON object, typed. `--arg` and `--arg-json` win over both; for one name the later of the two wins. |
| `--select PATH` | Print one part of the outcome. Repeat it, or write `a,b`, for several. With `--detach` nothing exists to select yet: the outcome carries a `hint` with the `wait --until-done --select` command that does it. |
| `--check` | Do not run: compile the program and look up the tools it names. |
| `--reuse ID` | Take results from an earlier execution for as long as the program makes the same requests in the same order. |
| `--detach` | Return at once with the id; the program runs in the background. |
| `--interactive` | Ask on the terminal when a call needs approval. Needs a terminal. |

`--check` prints
`{"status": "completed", "check": true, "result": {"tools": [{"tool", "signature", "policy"}], "unknown": []}}`,
or `status: error` with `SyntaxError` or `UnknownTool`.

### Environment variables

| Variable | Meaning |
|---|---|
| `CODEMODE_CONFIG` | The config file, when `--config` is not given. |
| `CODEMODE_HOME` | The data directory, when `--home` is not given. |
| `CODEMODE_AGENT` | The caller's name, when `--agent` is not given. |
| `CODEMODE_COMMAND` | The command line people type to start the runtime in this project, when it is not the runtime's own (a wrapper, a task runner, an embedding tool). Hints, `--help` and `instructions` print it, followed by the subcommand. `{options}` in it is replaced by `--config` and `--home`; without it nothing is added. |
| `CODEMODE_BASE_URL`, `CODEMODE_MODEL`, `CODEMODE_API_KEY` | The model endpoint for `agent`. |

## The `execute` MCP tool

`codemode serve` is a stdio MCP server with one tool. Its result carries the
outcome object as `structuredContent` and as JSON text; `isError` is true
for `error` and `rejected`.

| Argument | With | Meaning |
|---|---|---|
| `code` | | The program. Exactly one of `code` and `executionId` is given. |
| `executionId` | | Ask about an earlier execution. |
| `session` | `code` | Which `state` the program sees. |
| `args` | `code` | Arguments, read as `args[name]`. Typed, as `--args-json` is. |
| `background` | `code` | Return the id at once; the program keeps running. |
| `reuse` | `code` | An earlier execution to take matching steps from. |
| `check` | `code` | Compile and look up tool names; do not run. |
| `wait` | `executionId` | Wait up to this many seconds (at most 600) for it to stop running. |
| `select` | `executionId` | One part of the outcome, by path. Several paths may be written `a,b`. |
| `cancel` | `executionId` | Stop it. |

A client that sends a `progressToken` gets `notifications/progress` every
few seconds while a foreground program runs. `notifications/cancelled`
stops the program of that request. There is no tool that approves, changes
the policy or writes the log.

## Inside a program

| Name | Signature |
|---|---|
| `tools` | `tools.<namespace>.<tool>(**arguments)` |
| `call` | `call(path, args=None, on_error="raise", **kwargs)` |
| `parallel` | `parallel([(path, args), ...], on_error="raise")`. `on_error="return"` puts a failed call's `ToolError` in its place, as for `call`. `return_errors=True` is the older spelling of the same and still works. |
| `repeat` | `repeat(path, args=None, n=5, ok=None, until_fail=False, concurrency=1, keep=5)` returns `{runs, passed, failed, failures, failures_omitted, stopped_early}` |
| `search` | `search(query="", limit=None, namespace=None)` returns a list of `{path, signature, description, policy}` with `.total` and `.more` |
| `describe` | `describe(target)` returns a dict; `str()` of it is a short text |
| `shape` | `shape(value, depth=3, keys=12)` |
| `grep` | `grep(text, pattern, context=0, limit=50, ignore_case=False, invert=False)` returns a list with `.total` and `.more` |
| `tail`, `head` | `tail(text, n=20)`, `head(text, n=20)` |
| `summarize` | `summarize(text, patterns=None, head=3, tail=5, limit=5, width=200)` returns `{chars, lines, head, tail, counts, matches}` |
| `step` | `step(name, fn)` |
| `run` | `run(snippet_name, **args)` |
| `helpers` | `helpers.<module>.<function>(...)` |
| `state`, `args` | dicts |
| `project_root` | The project's folder: an absolute path with forward slashes. It is the folder the execution was started for, also on a later pass. A program runs in an empty folder of its own, so this is how it names a project file. |
| `ToolError` | `.kind`, `.message`, `.hint` |

A program ends in one of three ways. `return VALUE` at its top level (not in
a function, a lambda or a class) ends it with that value; a bare `return`
gives `null`. Or its last top-level statement is an expression, and that is
the result. Or it ends with an `if`/`elif`/`else`, a `with` or a `try`, and
the result is the last expression of the branch that ran. A value at the end
of a loop, of a `finally` block, or of a `try` body that has an `else` is
not the result, and the outcome says so in `warnings`.

## The data directory

`.codemode/` by default. Files are for reading; the runtime is the only
writer.

| Path | Content |
|---|---|
| `executions/ex_*.jsonl` | One append-only log per execution. Entry types: `start`, `pass_start`, `call`, `approval`, `pass_end`, `end`, `verify`, `log`, `progress`. |
| `executions/ex_*.lock`, `.cancel`, `.worker.log` | A running pass, a cancel request, the output of a detached worker. |
| `sessions/NAME.json` | The `state` of a session. |
| `snippets/NAME.json` | A promoted program. |
| `blobs/ID-SEQ.json` | A tool result that was over `max_value_bytes`. |
| `outputs/ID-SEQ.txt` | The whole output of a command whose result was cut. |
| `helpers/SHA.json` | The helper sources some execution started with. |
| `cache/tools-NAME.json` | A cached tool list. |
| `cache/shape-*.json` | The shape of the last successful result of a tool that publishes no `outputSchema`: keys and types, no content. `describe` shows it as `lastResultShape`. |

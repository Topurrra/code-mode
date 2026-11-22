# Writing MCP tools that work well in code mode

For anyone who writes an MCP server, or adds a tool to one, that agents will
call from programs.

In code mode a tool is not called by a model that reads the answer. It is
called by a program, written a moment ago by an agent that has seen only your
tool's name, schema and one-line description. The program indexes the
result, loops over it, compares it and passes it on; the agent sees only
what the program returns. That changes what a good tool is.

A tool that works well here also works well when a model calls it directly.
Nothing below is specific to this runtime.

## The short version

1. Return structured data with an `ok` field, and publish an output schema.
2. When a list is capped, return the real count next to it.
3. Offer a list call and a get-one call.
4. Let a long job be started, asked about, and stopped.
5. A tool that runs a command takes `env`, `cwd` and a time limit as arguments.
6. Say what you did, and what you did not do.
7. Keep stdout for the protocol, answer requests concurrently, and exit when
   your input ends.

Each rule comes from something that went wrong in the field. The last
section lists what, and how often.

## 1. Results a program can use

**Return structured content, not a block of text.** A program can do
`result["failed"]`; it cannot do anything reliable with 12 kB of console
output except search it for words. In MCP terms: send `structuredContent`,
and a text block with the same JSON for older clients.

**Publish an output schema** (`outputSchema` on the tool). Without one the
agent has to guess the result's shape, and a wrong guess costs a failed
program. In the field, agents tried `output`, `exit_code` and `rc` on a
result whose fields were `stdout` and `exit`. This runtime shows a tool's
return type in its one-line signature and its full result schema in
`describe()`, so a published schema is read before the first call.

**Put the outcome in a field.** `{"ok": false, "failed": ["test_login"]}`,
not a string in which `FAILED` may or may not appear. A program that filters
text for `FAIL` and finds nothing cannot tell "passed" from "printed
something else". `repeat()` in this runtime counts a run as failed when the
result's `ok` is false.

**`ok` means the thing happened.** A build that could not write its output
because the file was locked, and returned ok, is the worst kind of result:
everything after it is built on something that is not there.

**Use the same names everywhere.** If one tool's items have `name`, the next
tool's should not have `app`. Exact numbers, not rounded ones (bytes, not
"about 300 kB"): a program compares them.

**Be small by default.** Return the summary; let the caller ask for the
detail (`verbose: true`, `include: ["artifacts"]`). A result that carries
two kilobytes of file listing on every call is paid for on every call. The
agent can always narrow the result in its program, but only after the whole
thing has crossed the wire and been logged.

**Say whether you return text or data.** A tool that reads a file returns
text, even when the file holds JSON. If your server returns JSON documents as
plain text, the adopter can set `"parse_json_text": false` on your connector
so that this runtime does not parse them; better, return
`structuredContent: {"content": "..."}` and there is nothing to guess.

**Report failures as failures.** Set `isError` and say what to do next:
"build directory does not exist; run configure first" lets a program's
author fix it in one step. "cannot change working directory" does not.

## 2. Lists, and honest counts

**When a list is capped, say so in the result.** Return
`{"items": [...], "total": 312, "returned": 50, "next": "cursor"}`. A cap
with no count is a wrong answer waiting to happen: in the field a board
listing returned at most about fifty finished tasks, and programs counted
them and reported "50 done" for three rounds while the real number grew.

Counting is what programs do with lists. If the caller is likely to count by
a field, return those counts yourself (`"counts": {"done": 312, "open": 9}`):
they are right even when the items are capped.

**Offer a list call and a get-one call.** The list returns identifiers and a
few fields; get-one returns everything about one item. Without get-one, an
update tool forces the caller to overwrite a field it has never read.

**Make replace and append different operations.** `update(description=...)`
that replaces is fine if `get` exists and `append_description` exists too.

**Take filters and limits as arguments** (`status`, `since`, `limit`,
`cursor`), so the caller fetches what it needs instead of everything.

## 3. Long jobs

A call that runs for twenty minutes is hard on everyone: the caller's time
limit has to be huge, nothing shows progress, and a dropped connection loses
the result.

**Preferred: start, ask, stop.** Three tools.

```text
start_build(target) -> {"job": "b-41"}                      returns at once
get_job(job)        -> {"state": "running" | "done" | "failed", "progress": "...", "result": {...}}
cancel_job(job)     -> {"state": "cancelled"}
```

The job must outlive the call that started it, and should outlive your
server process if the server can be restarted (this runtime's CLI starts
your server for each program and stops it afterwards). A program polls:

```python
job = tools.ci.start_build(target="all")["job"]
import time
while True:
    status = tools.ci.get_job(job=job)
    if status["state"] != "running":
        break
    time.sleep(10)
status["result"]
```

**If a call has to be long, report progress.** Send
`notifications/progress` for the `progressToken` in the request's `_meta`.
This runtime asks for progress on every call and shows the last report to
anyone who looks at the running execution.

**Honour cancellation.** When the caller gives up on a call (its time limit,
or the user stopped the program) it sends `notifications/cancelled` with the
request id. Stop the work: kill the child process, release the lock. A
server that ignores it leaves a nineteen-minute job running for nobody.

**Give every tool a time limit of its own, sized for a busy machine.** A
commit tool that allowed `git status` sixty seconds timed out twelve times
when ten agents were building at once. If the limit is an argument, the
caller can raise it.

## 4. Tools that run commands

Most project servers have one. It is the tool agents fight with most.

**Take the environment as an argument.** `env: {"NAME": "value"}`. Without
it every caller wraps the command in a shell that sets variables first,
which brings quoting, a second process and a different exit code. This was
the single most frequent complaint in the field: every agent, about seventy
calls in one round.

**Take the working directory and the time limit as arguments.**

**Take a list of arguments, never one command string**, and start the
program directly, not through a shell. A value with a space or a `;` in it
is then one argument and nothing more.

**Stop the whole process tree** at the time limit and on cancellation: the
command, and what it started.

**Return a structured result**: `ok`, `exit`, `timed_out`, `seconds`,
`stdout`, `stderr`.

**Cut long output honestly.** Keep the start and the end (the end of a build
log is where the failure is), put a marker in the middle that says how much
is gone, return the real sizes, and say where the whole output can be read.
A cut in the middle with no marker reads as complete output.

**Normalize line ends and survive odd encodings.** Compilers on Windows
print in the console code page.

This runtime's own `commands` connector
([`connectors/commands.py`](../codemode_runtime/connectors/commands.py)) is a
worked example of every point in this section, in about three hundred lines.
A project that only needs to wrap command lines can use it instead of
writing a server: see [`ADOPTING.md`](ADOPTING.md).

## 5. Tools that change things

**Say what you did, and what you left out.** A commit tool that commits the
paths it was given and says nothing about thirteen new files next to them
produces a branch that does not build. Return
`{"committed": [...], "left_out": {"untracked": [...], "modified": [...]}}`.

**Say what you did not do.** A test tool that ran against an old build
should not report "53 passed" as if it had built first. Return
`"built": false`, or `"stale": true`, or refuse.

**Say when a result depends on hidden state.** An incremental build reports
the warnings of the files it compiled this time. That is not "the build has
no warnings"; say `"incremental": true` and which files were compiled.

**Keep what you were not asked to change.** A tool that normalizes line
endings on a file stored with CRLF turns a one-line change into a
941-line diff. Read how the file is stored and write it back the same way.

**Take a lock, or say you are busy**, when two calls cannot run at once
(two builds into one output folder). An error from deep inside the build
tool is not a useful way to learn that.

**Offer a dry run** for anything destructive, and make create operations
safe to repeat where you can (return the existing item instead of making a
second one).

**An exact-replace edit tool** is worth having if agents edit files through
your server: `replace(path, old, new)` that fails when `old` is not found
exactly once, compares without regard to line-ending style, and writes the
file back in the encoding and line endings it had.

## 6. Schemas and names

**An enum must be complete or absent.** Agents believe the schema. When a
build tool's schema listed three build types and a fourth one worked, agents
concluded the fourth did not exist; one decided the project had no test
build at all.

**Describe each parameter in a sentence**, and the tool in one or two. The
first sentence of the description is what `search()` shows; make it say
what the tool is for. Hundreds of kilobytes of schema help nobody: this
runtime will shorten an oversized schema when a program asks to see it.

**Name tools as identifiers** (`list_tasks`, not `list-tasks` or
`tasks/list`). Other names work, since they are converted, but the program
then calls a name that is not the one in your documentation. Policies here
match both spellings.

**Use verbs that say whether a tool reads or writes**: `list_`, `get_`,
`search_`, `read_` for reads. Adopters write policy rules as patterns over
names, and a `get_` tool that writes will be allowed by a rule meant for
reads. Set the `readOnlyHint` and `destructiveHint` annotations too:
`codemode init` uses them to propose a starting policy.

**Declare your limits.** If a result is capped at 1.5 kB, or a log line at
900 bytes, say so in the description and in the result.

## 7. Being a well-behaved server

- **stdout is for protocol messages only.** Logs go to stderr. This runtime
  skips lines it cannot parse and counts them, but a banner printed before
  the handshake has broken many clients.
- **Answer requests concurrently.** If your server handles one request at a
  time, `parallel()` calls run one after another and each waits while the
  ones before it finish, with its time limit running. If you cannot, say so
  in your README, so adopters set `"max_concurrency": 1` on the connector.
- **Exit when stdin closes.** The client stops a stdio server by closing its
  input. A server that keeps running is killed, with everything it started.
- **Start fast**, and do the slow setup on first use. A handshake that waits
  on a download fails at the client's startup time limit.
- **Never put a credential in a result or an error message.** This runtime
  removes the values it passed you from what you return, but it cannot know
  about secrets you obtained yourself.
- **Scope by an environment variable** if your tools act on a workspace
  (`PROJECT_ROOT`). Fifteen agents ran in parallel against fifteen copies of
  one project this way, each with its own config, and never touched each
  other's files.
- **Test the tool where it will run**: in a second worktree, on a file with
  CRLF, with ten copies running at once.

## What the first field report taught

Five rounds, a conductor and up to fifteen agents in parallel, one project
server. The full triage is [`FEEDBACK_TRIAGE.md`](FEEDBACK_TRIAGE.md). These
are the points that were about the tools, most frequent first:

| What went wrong | How often | Rule |
|---|---|---|
| The command tool took no environment; every call was wrapped in a shell. | every agent, about 70 calls in one round | 4: `env`, `cwd`, time limit as arguments |
| Long calls showed nothing and could not be stopped. | 6 or 7 agents in each of four rounds | 3: start, ask, stop; progress; cancellation |
| A build type that worked was not in the schema. | 6 agents | 6: complete enums |
| Test failures came back with no evidence. | 5 agents | 1: structured failures |
| Notes reported as warnings on every build; no way to tell new from old. | 6 agents | 1: small by default; classify at the source |
| A commit tool timed out under load, rewrote line endings, dropped a required trailer. | 6 agents | 3: time limits; 5: keep what you were not asked to change |
| Runner output with no pass or fail field. | 4 agents | 1: the outcome is a field |
| The test tool ran an old build and reported its numbers. | three rounds in a row | 5: say what you did not do |
| A build returned ok without relinking. | 3 agents | 1: `ok` means it happened |
| Result fields were guessed. | 3 agents | 1: publish an output schema |
| A capped list was counted as if it were complete. | three rounds | 2: honest counts |
| An update replaced text nobody had read. | once, three descriptions | 2: get-one; append |
| A commit left new files out. | once, and the branch did not build | 5: say what you left out |
| One call at a time: a commit queued behind a test run and timed out. | once | 7: answer concurrently |

What worked, and is worth copying: dedicated tools that enforce the
repository's rules (commits came out right every time through the commit
tool, and wrong twice through the raw command); one environment variable to
scope a server to a workspace; and structured results with an `ok` flag,
which let one line in a forty-line summary show the one thing that broke.

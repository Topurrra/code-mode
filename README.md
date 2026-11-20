# codemode-runtime

**One tool for any agent: `execute(code)`. Every tool call the code makes
crosses a gateway you control.**

Give an agent a list of tools and it makes one call per round trip, and every
result lands in its context. Give it a program instead and it can chain calls,
run them in parallel, keep the intermediate data to itself and hand back two
lines. That is code mode. This project is a small, portable runtime for it:

- **Any agent, any model.** It is an MCP server and a CLI. Nothing in it knows
  which model is on the other side.
- **Any tools.** Point it at MCP servers. Their schemas are never loaded up
  front; the program looks them up when it needs them.
- **A real boundary.** The program runs in a separate process with no
  credentials. Each tool call is checked on the host against a
  deny / ask / allow policy, bounded by limits, and written to an append-only
  log.
- **Approvals that do not block.** A call that needs a human pauses the
  execution. After approval the same program is replayed from the log, so
  nothing runs twice.
- **Nothing to install.** Python 3 standard library only.

```text
python main.py
```

runs a demo with no configuration, no network and no model.

## Contents

- [Quick start](#quick-start)
- [What a program can do](#what-a-program-can-do)
- [Using it from an agent](#using-it-from-an-agent)
- [Configuration](#configuration)
- [The safety model, and its limit](#the-safety-model-and-its-limit)
- [Outcomes and exit codes](#outcomes-and-exit-codes)
- [Tests](#tests)
- [Extending it](#extending-it)
- [Where the design comes from](#where-the-design-comes-from)
- [Not built](#not-built)

## Quick start

Python 3.9 or newer (tested on 3.11, 3.14 and 3.15 on Windows and on 3.14 on
Linux; written to run on 3.9, not tested below 3.11). There are no
dependencies; `requirements.txt` is empty on purpose.

```text
pip install -r requirements.txt     # installs nothing; kept so the usual command works
python main.py                      # the demo: 7 steps, 13 self-checks
python main.py --help
```

Run a program against the built-in notes store:

```text
python main.py run "notes = tools.demo.list_notes(); len(notes)"
python main.py run -f my_program.py --json
```

A write is not on the demo's allow list, so it pauses:

```text
python main.py run "tools.demo.create_note(title='Hi', body='There')"   # exit 3, status paused
python main.py approve last                                             # runs it, exit 0
python main.py show last                                                # the audit log
python main.py verify last                                              # replay from the log, compare
```

## What a program can do

The program is ordinary Python. These names are defined for it:

| Name | What it does |
|---|---|
| `tools.<namespace>.<tool>(**arguments)` | Call a tool. Returns a plain Python value. |
| `call("namespace.tool", {...})` | The same, by name. Use it when an argument name is not a valid identifier. |
| `parallel([("namespace.tool", {...}), ...])` | Run independent calls at the same time. Results come back in order. |
| `search("words")` | Find tools and snippets. Returns one-line signatures. |
| `describe("namespace.tool")` | Signature, description, schema and policy for one tool. |
| `state` | A dict that persists between programs in the same session. |
| `step("name", fn)` | Record the value of `fn()`: use it for time, randomness, anything that changes between runs. |
| `run("snippet", **args)` | Run a saved snippet. Inside it, the arguments are the dict `args`. |
| `ToolError` | Raised when a call is denied, fails or is unknown. Has `.kind` and `.message`. |

The value of the last expression is the result. `print()` goes to the logs.
Calls are synchronous: there is no `await`.

```python
notes = tools.demo.list_notes()
bodies = parallel([("demo.read_note", {"id": n["id"]}) for n in notes])
state["open_actions"] = [
    line[7:].strip() for b in bodies for line in b["body"].splitlines() if line.startswith("ACTION:")
]
len(state["open_actions"])          # the only thing the model reads
```

Eleven tool calls, one round trip, and about 4.7 kB of note text that never
reaches the model's context.

## Using it from an agent

### Over MCP (any MCP client)

Register the server the way your client registers any stdio MCP server:

```json
{
  "mcpServers": {
    "codemode": {
      "command": "python",
      "args": ["/absolute/path/to/main.py", "--config", "/absolute/path/to/codemode.json", "serve"]
    }
  }
}
```

The agent sees one tool, `execute`. It takes `code` to run a program, or
`executionId` to fetch the outcome of an earlier one (for example after a
human approved it). There is no approve tool: approval is a CLI command, out
of the agent's reach.

### Over the CLI (any agent that can run a command)

```text
python main.py run --json -f program.py
python main.py get --json <executionId>
```

One JSON object on stdout, and an exit code that says which outcome happened.
`python main.py prompt` prints the tool description, for a harness that
registers the tool itself.

### With the built-in loop (any OpenAI-compatible endpoint)

```text
CODEMODE_BASE_URL=http://localhost:11434/v1 CODEMODE_MODEL=<model name> python main.py agent "Summarise the open actions"
```

`CODEMODE_API_KEY` is sent as a bearer token if set. Hosted APIs, gateways
and local servers that speak the chat-completions format all work; the loop
uses `urllib` and no SDK. It stops at `--max-turns` (12 by default).

## Configuration

One JSON file. The runtime looks at `--config`, then `$CODEMODE_CONFIG`, then
`./codemode.json`, and falls back to the built-in demo config. Unknown keys
are errors. See [`codemode.example.json`](codemode.example.json);
`python main.py config` prints what was resolved.

```json
{
  "connectors": {
    "demo":  {"type": "demo"},
    "files": {"type": "mcp-stdio", "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."],
              "env": {"SOME_TOKEN": "${SOME_TOKEN}"}}
  },
  "policy": {
    "mode": "enforce",
    "default": "ask",
    "allow": ["demo.list_notes", "demo.read_note", "files.read_*"],
    "deny":  ["*.delete_*"]
  },
  "limits": {"timeout_s": 60, "call_timeout_s": 30, "max_calls": 200}
}
```

- **`connectors`**: a namespace per tool source. `mcp-stdio` starts any MCP
  server; `demo` is the notes store; `chaos` is a fault-injection fixture for
  tests. `${NAME}` in `env` is read from the host environment when the server
  starts, so secrets stay out of the file and out of the sandbox.
- **`policy`**: glob patterns over `namespace.tool`. Deny wins over ask, ask
  over allow. `default` applies when nothing matches and is `ask` if you leave
  it out. `"mode": "observe"` logs what would have been asked or denied and
  lets the call through: use it to find false positives before you enforce.
- **`limits`**: `timeout_s` (one pass), `call_timeout_s` (one tool call),
  `startup_timeout_s`, `max_calls`, `max_value_bytes` (one argument object or
  tool result), `max_result_chars` and `max_log_chars` (what is returned),
  `max_state_bytes`.
- **`executor`**: `command` wraps the sandbox in something else (see below);
  `env_passthrough` names host variables the sandbox may see.
- **`llm`**: `base_url`, `model`, `api_key`, `max_turns`, `timeout_s`, for the
  built-in loop only. The `CODEMODE_*` environment variables override it.
- **`agent`**: the identity written into the audit log. `--agent` and
  `$CODEMODE_AGENT` override it. The program cannot set it.

Data lives in `./.codemode` (`--home` or `$CODEMODE_HOME` to move it): one
JSONL file per execution, a JSON file per session, a JSON file per snippet.

## The safety model, and its limit

What the runtime enforces:

- The program's process starts with a scrubbed environment and an empty
  working directory. Host variables, API keys included, are not passed.
- Connector credentials live in the host process. A program can use a tool; it
  cannot read the token behind it.
- Every tool call is decided on the host, under the tool's own name, at the
  moment it is made, with the real arguments.
- A call the policy asks about does not run until a human approves it from the
  CLI. Neither the sandbox API nor the MCP server can approve, edit policy or
  touch the log.
- Every execution has a wall-clock limit, a call limit and size limits.
- The log names each side effect before it happens (`executing`), then how it
  ended.

**What it does not do: the default sandbox is a process, not a jail.** It runs
as your OS user. A hostile program can open files and sockets directly,
without going through a tool. For code you do not trust, wrap the sandbox:

```json
{"executor": {"command": ["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
                           "python:3.12-alpine", "python", "-I", "-u", "-c", "{bootstrap}"]}}
```

`{bootstrap}` is a one-line loader; the rest of the protocol is NDJSON on
stdin and stdout, so anything that can run `python -c` works: a container, a
jail, a remote shell. With no network in the sandbox, the tools become the
only way out, and the policy covers everything. The Docker line above is an
example and was not run in this project's tests; the mechanism was tested by
running the sandbox under a different interpreter and under deliberately
broken commands.

## Outcomes and exit codes

Every surface returns the same payload:

```text
{"status": "completed", "executionId": "...", "result": ..., "logs": "...", "stats": {...}}
{"status": "paused",    "executionId": "...", "pending": [{"seq": 2, "tool": "...", "args": {...}}]}
{"status": "error",     "executionId": "...", "error": {"type": "...", "message": "...", "hint": "..."}}
```

| Exit | Meaning |
|---|---|
| 0 | completed |
| 1 | the program failed, or the execution was rejected |
| 2 | usage or configuration error |
| 3 | paused, waiting for approval |
| 4 | `verify` did not reproduce the recorded result |
| 5 | unknown execution, snippet or tool |

Results over `max_result_chars` are cut and flagged with `resultTruncated`;
`python main.py get <id> --full` prints the stored value.

## Tests

```text
python tests/e2e.py                               # 125 checks on the real CLI, about 45 s
python -m unittest discover -s tests              # 49 cases for the pure modules
python tests/mutation_check.py                    # plants 20 bugs, checks the suites catch them
```

`tests/e2e.py` starts the real program as a subprocess for every check and
writes [`docs/e2e-trace.txt`](docs/e2e-trace.txt): each command, its exit code
and its output. The cases in both suites come from
[`tests/FAILURE_MODES.md`](tests/FAILURE_MODES.md), a list of invariants and
failure modes written before the code. Set `CODEMODE_E2E_MCP_PYTHON` to an
interpreter that has the official `mcp` package to include the interop
scenarios. Results are in [`docs/VERIFICATION.md`](docs/VERIFICATION.md).

## Extending it

- **A new capability**: write or pick an MCP server and add an `mcp-stdio`
  connector. No code change.
- **A new connector type**: subclass `Connector` in
  [`codemode_runtime/connectors/`](codemode_runtime/connectors/__init__.py)
  (two methods: `list_tools`, `call`), register it in `build_connector`, and
  add its allowed keys to `CONNECTOR_KEYS` in `config.py`.
- **A different sandbox**: set `executor.command`. For a sandbox in another
  language, implement the frames in the docstring of
  [`sandbox_runner.py`](codemode_runtime/sandbox_runner.py).
- **A new surface**: call the five methods of `Runtime`
  ([`runtime.py`](codemode_runtime/runtime.py)); the CLI and the MCP server
  are both thin wrappers over them.

| File | Responsibility |
|---|---|
| `config.py` | Load and validate the config. |
| `policy.py`, `schema_sig.py`, `truncate.py`, `jsonutil.py` | Pure logic: decisions, signatures, budgets, hashes. |
| `catalog.py` | Tool names, search, describe. |
| `connectors/` | `demo`, `chaos`, `mcp_stdio`. |
| `executor.py`, `sandbox_runner.py` | Start the sandbox; the API the program sees. |
| `engine.py` | One pass: replay, policy, limits, recording. |
| `records.py`, `sessions.py` | The execution log; session state and snippets. |
| `runtime.py` | `execute`, `get`, `approve`, `reject`, `verify`. |
| `mcp_server.py`, `llm_agent.py`, `cli.py`, `demo.py` | The surfaces. |

Agents working on this code should read [`AGENTS.md`](AGENTS.md) first.

## Where the design comes from

Six sources. [`docs/DESIGN.md`](docs/DESIGN.md) lists the 26 principles taken
from them, where they contradict each other, and which side this project took.

- Claude Code documentation, [Code Mode](https://code.claude.com/docs/en/code-mode):
  one program per step, the last expression as the result, each inner call
  checked under its own tool name.
- Cloudflare Agents documentation, [Code Mode](https://developers.cloudflare.com/agents/tools/codemode/):
  a single outer tool, signatures from JSON Schema, discovery at run time, a
  stateless executor, approval by deterministic replay.
- [Using a chaos loop to harden the Claude Docs launch](https://claude.dev/blog/chaos-loop-claude-docs-launch/):
  invariants first, per-operation telemetry, deliberate fault injection.
- [How the Claude Code team thinks about verification](https://claude.dev/blog/how-the-claude-code-team-thinks-about-verification/):
  end-to-end evidence, property and differential tests, a gate the change
  cannot edit, observe before enforce.
- [Safely scaling multi-agent teams with Claude Fable 5.2](https://claude.dev/blog/safely-scaling-multi-agent-teams-with-fable-5-2/):
  a credential-free sandbox behind a gateway that stamps identity and logs
  everything.
- [Unharnessed](https://github.com/unharnessed-agent/Unharnessed): one tool,
  state in the runtime instead of the context, MCP reached from code, and a
  wire protocol that user output cannot corrupt.

## Not built

- **Rollback.** Rejecting an execution stops it; it does not undo calls that
  already ran. MCP has no notion of a compensating call to hang this on.
- **Background executions and wake-ups.** A pass is foreground and bounded.
- **Live objects between executions.** `state` holds JSON values, not open
  connections or running processes.
- **A model-based reviewer.** Policy is deterministic rules. The audit log is
  where such a monitor would read from.
- **MCP over HTTP.** The stdio transport only, both as client and as server.
- **Multi-agent orchestration.** Several agents can share one runtime safely;
  it does not coordinate them.

## License

MIT. See [`LICENSE`](LICENSE).

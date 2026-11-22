# Working on this codebase

For an agent (or a person) changing this code with no prior context.

## What it is

A code-mode runtime: one tool, `execute(code)`, with a policy gateway between
the program and the tools. Read `README.md` for use and `docs/DESIGN.md` for
why each part exists. The module table in the README says where things live.
`docs/INTERFACE.md` is the contract with callers: commands, JSON fields,
exit codes. `docs/ADOPTING.md` is how a project sets it up.

## Commands

```text
python main.py                          # demo; exits 0 only if its 13 checks hold
python tests/run_all.py                 # every suite below, one line each; run it before a commit
python tests/run_all.py --quick         # the same without the mutation check
python tests/e2e.py                     # end to end, on the real CLI; rewrites docs/e2e-trace.txt
python tests/e2e.py approval faults     # only some scenarios (does not rewrite the trace)
python -m unittest discover -s tests    # pure-logic cases, and the isolated ones
python tests/mutation_check.py          # do the suites catch planted bugs?
python tests/sync_docs.py               # rewrite the generated block in docs/ADOPTING.md
```

Optional scenarios run when their tooling is there. Keep it inside this
folder: `.venv-e2e/` (a virtual environment with the `mcp` package, named in
`CODEMODE_E2E_MCP_PYTHON`), `.e2e-tools/node_modules` (the reference
filesystem server, named in `CODEMODE_E2E_NODE_MODULES`), and
`CODEMODE_E2E_INSTALL=1` to install the package into a fresh environment.

## Rules

- **Standard library only.** No dependency may be added. `requirements.txt`
  stays empty.
- **No vendor or model names in the code path.** The runtime talks MCP and the
  OpenAI-compatible chat format, both configured at run time.
- **Nothing project-specific.** No project's names, tools, paths or habits in
  the code, the defaults or the examples. A lesson from one project goes in
  as the general rule behind it.
- **`sandbox_runner.py` must stand alone.** Its source is sent to the sandbox
  and executed there, possibly by an older Python. No imports from this
  package, no syntax newer than 3.8.
- **Nothing the program can reach may approve, edit policy or write the log.**
  If you add a sandbox function, an MCP tool or a parameter of `execute`,
  check it against this. Stopping (reject, cancel) is allowed from anywhere;
  approving is for a person at a terminal.
- **An approval belongs to one execution.** One given in advance
  (`approve --same-tool`, `--all-remaining`) is an entry in that execution's
  log and nowhere else: not in the policy, the config or a session. A call
  that runs under it gets an approval entry of its own first, and a deny
  rule is checked before any approval.
- **Project code is program code.** Helpers from a project's folder run in
  the sandbox with the program. The host reads them; it never executes them.
- **Every host interaction of a program gets a sequence number and a log
  entry.** Anything a program can observe that is not recorded will break
  replay. If you add a request type, route it through `Pass._next`. What a
  program cannot observe (live output, progress) may be logged without one.
- **Errors are data.** A failure a program or an agent can act on needs a
  stable `type`, a message and a `hint` that says what to do next. Exit codes
  are fixed in `errors.py`.
- **A value that was cut says so.** A list, a result, an output or a schema
  that is shortened carries the real size and a way to get the rest.
- **Credentials are references.** `${NAME}` in the config, resolved on the
  host. Never write, print or record a token; not in code, tests or docs.
- **Stay compatible.** Commands, JSON fields and exit codes in
  `docs/INTERFACE.md` are added to, not renamed or removed.
- **Every platform.** Start child processes through `procutil` (process
  trees, no console windows), read text through `textio` (byte-order marks,
  line endings), and remember that Windows will not delete or replace a file
  another process has open.

## Tests

- Verify behaviour end to end, in `tests/e2e.py`, by running the real CLI.
- Before testing an isolated piece, add how it can fail to
  `tests/FAILURE_MODES.md`, then write the case from that entry.
- Unit cases are for pure logic with many inputs (`test_pure_logic.py`). Do
  not add unit tests that only walk through code you just wrote.
- A state the command line cannot produce on purpose (a race, a half-written
  file, a process that died at one exact moment) goes in `test_isolated.py`,
  with real files in a temporary folder.
- Never change an existing check to make it pass. Fix the code, or, if the
  check itself was wrong, say so in the change description.
- When you add a safety property, add a mutant for it in
  `tests/mutation_check.py`. A mutant has to be caught every time: if a check
  catches it only when a race goes one way, it is not a mutant.
- A scenario that starts processes cleans them up, also when its check fails.

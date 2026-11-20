# Working on this codebase

For an agent (or a person) changing this code with no prior context.

## What it is

A code-mode runtime: one tool, `execute(code)`, with a policy gateway between
the program and the tools. Read `README.md` for use and `docs/DESIGN.md` for
why each part exists. The module table in the README says where things live.

## Commands

```text
python main.py                          # demo; exits 0 only if its 13 checks hold
python tests/e2e.py                     # end to end, on the real CLI; rewrites docs/e2e-trace.txt
python tests/e2e.py approval faults     # only some scenarios (does not rewrite the trace)
python -m unittest discover -s tests    # pure-logic cases
python tests/mutation_check.py          # do the suites catch planted bugs?
```

## Rules

- **Standard library only.** No dependency may be added. `requirements.txt`
  stays empty.
- **No vendor or model names in the code path.** The runtime talks MCP and the
  OpenAI-compatible chat format, both configured at run time.
- **`sandbox_runner.py` must stand alone.** Its source is sent to the sandbox
  and executed there, possibly by an older Python. No imports from this
  package, no syntax newer than 3.8.
- **Nothing the program can reach may approve, edit policy or write the log.**
  If you add a sandbox function or an MCP tool, check it against this.
- **Every host interaction of a program gets a sequence number and a log
  entry.** Anything a program can observe that is not recorded will break
  replay. If you add a request type, route it through `Pass._next`.
- **Errors are data.** A failure a program or an agent can act on needs a
  stable `type`, a message and, where there is one, a `hint`. Exit codes are
  fixed in `errors.py`.

## Tests

- Verify behaviour end to end, in `tests/e2e.py`, by running the real CLI.
- Before testing an isolated piece, add how it can fail to
  `tests/FAILURE_MODES.md`, then write the case from that entry.
- Unit cases are for pure logic with many inputs (`test_pure_logic.py`). Do
  not add unit tests that only walk through code you just wrote.
- Never change an existing check to make it pass. Fix the code, or, if the
  check itself was wrong, say so in the change description.
- When you add a safety property, add a mutant for it in
  `tests/mutation_check.py`.

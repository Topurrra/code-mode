# Failure modes and invariants

Written before the code. The unit cases in `test_pure_logic.py` and the
scenarios in `e2e.py` come from this list, not from reading the implementation.
Each item has an ID that the tests quote.

## Invariants (asserted end to end, on the real CLI)

- **I1** An execution has exactly one `start` and at most one `end`. A pass
  ends in exactly one of completed, paused, error.
- **I2** A call whose decision is "ask" has no side effect before an approval
  for its sequence number is in the log.
- **I3** Replaying a completed execution from its log, with no live calls,
  gives the same result hash.
- **I4** A tool result reaches the caller only if the code returns or prints
  it, and the returned payload respects the output budget.
- **I5** The sandbox process does not see the host's environment variables.
- **I6** Nothing the code prints, including JSON-looking lines and raw
  descriptor writes, breaks the protocol.
- **I7** Exit codes are 0 completed, 1 failed, 2 usage or config, 3 paused,
  4 verification mismatch, 5 not found.
- **I8** After any failure the next command works.
- **I9** An approved call runs exactly once, and calls recorded before the
  pause do not run again.

## Schema to signature (`schema_sig.py`)

- **S1** The schema is missing, `None`, a boolean or a list. Must not raise.
- **S2** `type` is a list such as `["string", "null"]`.
- **S3** There is no `type` but there are `properties`.
- **S4** A name is in `required` but not in `properties`.
- **S5** Required parameters must come before optional ones, each group in
  schema order.
- **S6** A property name is not a Python identifier (`weird-name`, `2fa`,
  `has space`) or is a keyword (`class`, `from`). The signature must not show
  it as a bare parameter.
- **S7** `enum` with mixed types, with quotes inside strings, or with many
  values.
- **S8** Nested arrays, and arrays with no `items`.
- **S9** `anyOf` or `oneOf`, with duplicates and with null; `allOf`.
- **S10** `$ref` into `$defs` or `definitions`; a ref whose target is missing;
  a schema that refers to itself.
- **S11** A schema nested far deeper than any sane tool. Must terminate with
  bounded output.
- **S12** A tool with a very large number of parameters. The compact form must
  stay short.
- **S13** `const`.
- **S14** `type` is not a string, or is a name JSON Schema does not define.
- **S15** `additionalProperties` carrying a type.
- **S16** No output schema.

## Tool name sanitizing (`catalog.py`)

- **N1** Dashes, dots, slashes, spaces.
- **N2** A leading digit.
- **N3** A Python keyword.
- **N4** Empty, or nothing but symbols.
- **N5** Two names that collide after sanitizing. Both must stay callable and
  the outcome must not depend on listing order.
- **N6** Leading underscores and dunder names, which would shadow attributes
  of the proxy object.
- **N7** A sanitized name must always be a valid identifier.

## Policy (`policy.py`)

- **L1** A path matches allow and deny: deny wins. Ask and allow: ask wins.
- **L2** Nothing matches: the default applies, and the default is "ask" when
  the config does not set one.
- **L3** Matching must be case-sensitive on every platform.
- **L4** `*` crosses the dot between connector and tool.
- **L5** A misspelt mode, default or decision is a config error, never a
  silent allow.
- **L6** A pattern that is not a string is a config error.
- **L7** An unbalanced `[` in a pattern must not raise.
- **L8** Observe mode computes the same decision and marks it not enforced.
- **L9** A rule is written with the server's spelling of a tool name
  (`files.delete-file`) and the program uses the sanitized one
  (`files.delete_file`). The rule must apply under both spellings.
- **L10** A paused call is approved after the policy changed to deny it.
  Deny must win over the approval, and the outcome must say the policy
  changed.
- **L11** A rule with argument patterns matches only when every named
  argument is present and matches: nested paths, list indexes, values that
  are not strings, a missing argument, an index out of range.
- **L12** A rule object with an unknown key, no tool, or patterns that are
  not strings is a config error.
- **L13** `approve`, or `run --interactive`, is started by a process with no
  terminal on stdin, which is what an agent's shell tool is. It must be
  refused unless the config allows it, and nothing pending may run.
- **L14** A paused execution is approved long after it paused. Past the
  time-to-live the approval is refused unless the person insists.
- **L15** The decision shown by `describe()` is for the tool alone. When a
  rule for that tool looks at arguments, the output must say so.

## Output budgets (`truncate.py`)

- **T1** Text at or under the budget comes back unchanged and unflagged.
- **T2** A budget of zero, a negative budget, or one smaller than the marker.
  Must not raise or loop, and the output stays bounded.
- **T3** A dict or list over budget. The result must be flagged and must not
  be a cut-off string passed off as the value.
- **T4** The marker says how much was dropped.
- **T5** Head-and-tail log truncation keeps both ends.
- **T6** Lengths exactly at the budget and one over.
- **T7** The bounded log buffer under a flood: memory stays bounded and the
  dropped count is right.

## Fingerprints (`jsonutil.py`)

- **F1** Key order must not change a fingerprint.
- **F2** Different arguments, a different name or a different kind must change
  it.
- **F3** NaN and infinities are refused rather than hashed.
- **F4** Non-ASCII text hashes the same on every platform.

## Configuration (`config.py`)

- **G1** An unknown top-level key, or an unknown key inside a section.
- **G2** A connector name that is not an identifier or is reserved.
- **G3** An unknown connector type.
- **G4** A limit that is zero, negative, or not a number.
- **G5** `${VAR}` where the variable is not set.
- **G6** A config file that is not valid JSON, or is not an object.
- **G7** `executor.command` without the `{bootstrap}` placeholder.

## Entry points, command hints, odd files (`main.py`, `launcher.py`, `textio.py`, `cli.py`)

- **E1** The interpreter is started in isolated mode (`python -I main.py`),
  which leaves the script's folder off `sys.path`. The package must still
  import.
- **E2** A hint names a command for a human to run. Pasted as printed, from
  another folder, it must reach the same execution: it has to repeat the
  launcher, `--config` and `--home` of the process that printed it.
- **E3** A config or a program file starts with a byte-order mark (UTF-8 from
  an editor, UTF-16 from a shell redirect), or has CRLF line endings.
- **E4** `run` is given no program and stdin is a pipe that stays open and
  empty. It must fail in bounded time instead of waiting for ever.
- **E5** A usage error with `--json` on the command line must still be one
  JSON object on stdout.
- **E6** `python -m codemode_runtime`, the package's `__main__.py` run as a
  script, and the installed `codemode` entry point must all be the same CLI.

## Fault scenarios (`e2e.py`, from the chaos list)

- **X1** The code loops forever.
- **X2** A tool stalls past its call timeout.
- **X3** The code floods stdout.
- **X4** The code writes straight to file descriptors 1 and 2.
- **X5** The sandbox process exits without finishing (`os._exit`).
- **X6** The executor prints garbage instead of frames.
- **X7** The executor never answers.
- **X8** A tool returns a value over the size limit.
- **X9** The code makes more calls than the limit.
- **X10** The code is not valid Python, or uses `return` or `await` at top
  level.
- **X11** The result is not JSON-serializable.
- **X12** The code changes its behaviour between the paused pass and the
  resumed pass (replay divergence).
- **X13** A second pass is started on an execution that is already running.
- **X14** An MCP server that fails to start, or a tool call the server rejects.
- **X15** An MCP client cancels a running execution.
- **X16** The model endpoint returns an error, or a tool call with malformed
  arguments.

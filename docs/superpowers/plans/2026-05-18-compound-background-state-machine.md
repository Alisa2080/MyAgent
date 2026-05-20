# Compound Background State Machine Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the regex-based compound background command rewrite with the Hermes state-machine implementation so `A && B &` and similar forms are rewritten correctly without corrupting quoted strings, redirects, grouped commands, comments, or simple background commands.

**Architecture:** Keep the existing call site in `BaseEnvironment.execute()` unchanged: every foreground shell command still passes through `rewrite_compound_background()` after sudo preparation and before shell wrapping. Limit the change to `agent_tools/hermes_terminal_toolkit/command_utils.py` plus focused unit coverage. Do not introduce a full shell tokenizer; port Hermes' lightweight scanner/state machine that tracks quote/token boundaries, comments, parentheses, brace groups, chain operators, redirects, and real background `&` operators.

**Tech Stack:** Python 3.11, vendored Hermes terminal toolkit, pytest.

---

## Current State

- Current regex implementation lives in `agent_tools/hermes_terminal_toolkit/command_utils.py`.
- Current caller is `agent_tools/hermes_terminal_toolkit/environments/base.py`, which imports and calls `rewrite_compound_background(exec_command)`.
- The regex only matches one-line shapes like `^(.*(?:&&|\|\||;)) tail &$`.
- The regex can mis-handle shell syntax because it does not know whether `&` appears inside quotes, comments, redirects (`&>`, `2>&1`), parenthesized subshells, or existing brace groups.
- Hermes reference file `/home/miku/projects/hermes-agent-main/tools/terminal_tool.py` has the desired state-machine version under `_rewrite_compound_background()`.

## Behavioral Contract

- Rewrite at depth 0:
  - `A && B &` -> `A && { B & }`
  - `A || B &` -> `A || { B & }`
  - `A; B && C &` -> `A; B && { C & }`
- Preserve simple background commands:
  - `cmd &` stays `cmd &`
- Preserve redirects:
  - `A && B &> out` stays unchanged for the `&>` redirect
  - `A && B 2>&1` stays unchanged for fd redirection
  - `A && B > out &` rewrites to `A && { B > out & }`
- Ignore operators inside quoted strings:
  - `printf 'a && b &'` unchanged
  - `printf "a && b &"` unchanged
- Reset state across statement boundaries:
  - newline, semicolon, and pipeline boundaries should not rewrite across statements/pipelines.
- Be idempotent:
  - `A && { B & }` stays exactly the same.
- Be intentionally conservative:
  - Do not rewrite inside `(...)` subshells or existing `{ ... }` groups in this change.
  - Do not build a complete shell parser/tokenizer.

## File Structure

- Modify `agent_tools/hermes_terminal_toolkit/command_utils.py`: replace regex `rewrite_compound_background()` with Hermes' state-machine implementation adapted to this module's public function name.
- Modify `agent_tools/hermes_terminal_toolkit/command_utils.py`: add `_looks_like_env_assignment()` only if also porting Hermes' sudo env-assignment fix in the same file; otherwise leave sudo behavior unchanged to keep scope tight.
- Create `tests/test_command_utils.py`: focused unit tests for compound background rewriting.
- Extend `tests/test_terminal_tools.py` only if an integration-level assertion is needed; unit tests should be enough because `BaseEnvironment.execute()` already calls the helper.

---

## Task 1: Add Focused Tests for Compound Background Rewriting

**Files:**
- Create: `tests/test_command_utils.py`
- Test: `tests/test_command_utils.py`

- [ ] **Step 1: Write table-driven rewrite tests**

Create `tests/test_command_utils.py`:

```python
import pytest

from agent_tools.hermes_terminal_toolkit.command_utils import rewrite_compound_background


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -m pip install -e . && python -m http.server &", "python -m pip install -e . && { python -m http.server & }"),
        ("test -f app.py || python -m http.server &", "test -f app.py || { python -m http.server & }"),
        ("cd web; npm run dev &", "cd web; { npm run dev & }"),
        ("python -m http.server &", "python -m http.server &"),
        ("printf 'a && b &'\npython -m http.server &", "printf 'a && b &'\npython -m http.server &"),
        ('printf "a && b &"', 'printf "a && b &"'),
        ("echo ok && { python -m http.server & }", "echo ok && { python -m http.server & }"),
        ("echo ok && python -m http.server > server.log &", "echo ok && { python -m http.server > server.log & }"),
        ("echo ok && python -m http.server &> server.log", "echo ok && python -m http.server &> server.log"),
        ("echo ok && python -m http.server 2>&1", "echo ok && python -m http.server 2>&1"),
        ("echo ok && python -m http.server & # keep serving", "echo ok && { python -m http.server & } # keep serving"),
        ("echo ok && python -m http.server &\necho done", "echo ok && { python -m http.server & }\necho done"),
        ("echo ok | grep ok && python -m http.server &", "echo ok | grep ok && { python -m http.server & }"),
        ("echo ok && (python -m http.server &)", "echo ok && (python -m http.server &)"),
    ],
)
def test_rewrite_compound_background(command, expected):
    assert rewrite_compound_background(command) == expected
```

- [ ] **Step 2: Add idempotence test**

Append:

```python
def test_rewrite_compound_background_is_idempotent():
    command = "echo ok && { python -m http.server & }"

    assert rewrite_compound_background(rewrite_compound_background(command)) == command
```

- [ ] **Step 3: Run tests to verify current regex gaps**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_command_utils.py -v
```

Expected before implementation: at least the quoted, redirect, brace-group, or subshell cases fail with the current regex implementation.

- [ ] **Step 4: Commit failing tests**

```bash
git add tests/test_command_utils.py
git commit -m "test: cover compound background rewrite edge cases"
```

---

## Task 2: Replace Regex Rewrite with Hermes State Machine

**Files:**
- Modify: `agent_tools/hermes_terminal_toolkit/command_utils.py`
- Test: `tests/test_command_utils.py`

- [ ] **Step 1: Replace `rewrite_compound_background()` implementation**

In `agent_tools/hermes_terminal_toolkit/command_utils.py`, replace the current regex implementation with this adapted Hermes implementation. Keep the public function name `rewrite_compound_background` because `BaseEnvironment.execute()` imports that name.

```python
def rewrite_compound_background(command: str) -> str:
    """Wrap `A && B &` (or `A || B &`) to `A && { B & }` at depth 0.

    Bash parses ``A && B &`` with `&&` tighter than `&`, so it forks a
    subshell for the whole `A && B` compound and backgrounds it. Inside
    the subshell, `B` runs foreground, so the subshell waits for `B` to
    finish. Rewriting the tail to `A && { B & }` preserves chain semantics
    while avoiding the subshell wait trap.
    """
    n = len(command)
    i = 0
    paren_depth = 0
    brace_depth = 0
    last_chain_op_end = -1
    rewrites: list[tuple[int, int]] = []

    while i < n:
        ch = command[i]

        if ch == "\n" and paren_depth == 0 and brace_depth == 0:
            last_chain_op_end = -1
            i += 1
            continue

        if ch.isspace():
            i += 1
            continue

        if ch == "#":
            nl = command.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue

        if ch == "\\" and i + 1 < n:
            i += 2
            continue

        if ch in ("'", '"'):
            _, next_i = _read_shell_token(command, i)
            i = max(next_i, i + 1)
            continue

        if ch == "(":
            paren_depth += 1
            i += 1
            continue

        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            i += 1
            continue

        if ch == "{" and i + 1 < n and (command[i + 1].isspace() or command[i + 1] == "\n"):
            brace_depth += 1
            i += 1
            continue

        if ch == "}" and brace_depth > 0:
            brace_depth -= 1
            last_chain_op_end = -1
            i += 1
            continue

        if paren_depth > 0 or brace_depth > 0:
            i += 1
            continue

        if command.startswith("&&", i) or command.startswith("||", i):
            last_chain_op_end = i + 2
            i += 2
            continue

        if ch == ";":
            last_chain_op_end = -1
            i += 1
            continue

        if ch == "|":
            last_chain_op_end = -1
            i += 1
            continue

        if ch == "&":
            if i + 1 < n and command[i + 1] == ">":
                i += 2
                continue

            j = i - 1
            while j >= 0 and command[j].isspace():
                j -= 1
            if j >= 0 and command[j] in "<>":
                i += 1
                continue

            if last_chain_op_end >= 0:
                rewrites.append((last_chain_op_end, i))
            last_chain_op_end = -1
            i += 1
            continue

        _, next_i = _read_shell_token(command, i)
        i = max(next_i, i + 1)

    if not rewrites:
        return command

    result = command
    for chain_end, amp_pos in reversed(rewrites):
        insert_pos = chain_end
        while insert_pos < amp_pos and result[insert_pos].isspace():
            insert_pos += 1
        prefix = result[:insert_pos]
        middle = result[insert_pos:amp_pos]
        suffix = result[amp_pos + 1 :]
        result = prefix + "{ " + middle + "& }" + suffix

    return result
```

- [ ] **Step 2: Remove now-unused regex dependency if possible**

Check whether `re` is still used elsewhere in `command_utils.py`.

Run:

```bash
rg -n "\bre\." agent_tools/hermes_terminal_toolkit/command_utils.py
```

Expected:

- If no matches remain, remove `import re`.
- If `re` is still used by another helper, keep it.

- [ ] **Step 3: Run focused tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_command_utils.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit implementation**

```bash
git add agent_tools/hermes_terminal_toolkit/command_utils.py tests/test_command_utils.py
git commit -m "fix: rewrite compound background commands with scanner"
```

---

## Task 3: Add Execution-path Regression Coverage

**Files:**
- Modify: `tests/test_command_utils.py`
- Optional Modify: `tests/test_terminal_tools.py`

- [ ] **Step 1: Add regression for the original terminal hang shape**

Append to `tests/test_command_utils.py`:

```python
def test_rewrite_compound_background_handles_common_server_start_shape():
    command = "cd /tmp && python3 -m http.server 8123 > /tmp/server.log 2>&1 &"

    assert rewrite_compound_background(command) == (
        "cd /tmp && { python3 -m http.server 8123 > /tmp/server.log 2>&1 & }"
    )
```

- [ ] **Step 2: Add multiple rewrite coverage**

Append:

```python
def test_rewrite_compound_background_handles_multiple_lines_independently():
    command = "setup && server_one &\nprepare || server_two &"

    assert rewrite_compound_background(command) == "setup && { server_one & }\nprepare || { server_two & }"
```

- [ ] **Step 3: Run focused regression tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_command_utils.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Run terminal toolkit smoke tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py tests/test_terminal_process_policy.py tests/test_terminal_lifecycle.py -v
```

Expected: all tests pass; this verifies the LangChain wrappers and lifecycle behavior were not affected.

- [ ] **Step 5: Commit regression coverage**

```bash
git add tests/test_command_utils.py
git commit -m "test: cover compound background terminal regressions"
```

---

## Task 4: Decide Whether to Port Hermes' Sudo Env-assignment Scanner Fix

**Files:**
- Optional Modify: `agent_tools/hermes_terminal_toolkit/command_utils.py`
- Optional Modify: `tests/test_command_utils.py`

- [ ] **Step 1: Compare current sudo scanner with Hermes reference**

Current project `_rewrite_real_sudo_invocations()` treats command start as false after any token, so it does not rewrite:

```bash
FOO=bar sudo apt-get update
```

Hermes reference adds `_looks_like_env_assignment()` and keeps `command_start=True` across leading `NAME=value` tokens.

- [ ] **Step 2: If scope allows, add sudo env-assignment tests**

Append:

```python
from agent_tools.hermes_terminal_toolkit.command_utils import _rewrite_real_sudo_invocations


def test_rewrite_real_sudo_invocations_after_env_assignment():
    rewritten, found = _rewrite_real_sudo_invocations("DEBIAN_FRONTEND=noninteractive sudo apt-get update")

    assert found is True
    assert rewritten == "DEBIAN_FRONTEND=noninteractive sudo -S -p '' apt-get update"
```

- [ ] **Step 3: If test is accepted, port `_looks_like_env_assignment()`**

Add:

```python
def _looks_like_env_assignment(token: str) -> bool:
    """Return True when *token* is a leading shell environment assignment."""
    if "=" not in token or token.startswith("="):
        return False
    name, _value = token.split("=", 1)
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))
```

Then update `_rewrite_real_sudo_invocations()` after appending each token:

```python
if command_start and _looks_like_env_assignment(token):
    command_start = True
else:
    command_start = False
```

- [ ] **Step 4: Run focused tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_command_utils.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit only if included**

```bash
git add agent_tools/hermes_terminal_toolkit/command_utils.py tests/test_command_utils.py
git commit -m "fix: preserve sudo rewrite after env assignments"
```

If keeping scope strictly to compound background rewriting, skip this task and leave sudo behavior unchanged.

---

## Task 5: Final Verification

**Files:**
- Verify: `agent_tools/hermes_terminal_toolkit/command_utils.py`
- Verify: `agent_tools/hermes_terminal_toolkit/environments/base.py`
- Verify: `tests/test_command_utils.py`

- [ ] **Step 1: Run focused test suite**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_command_utils.py tests/test_terminal_tools.py tests/test_terminal_lifecycle.py tests/test_terminal_process_policy.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Compile touched Python modules**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m py_compile agent_tools/hermes_terminal_toolkit/command_utils.py agent_tools/hermes_terminal_toolkit/environments/base.py
```

Expected: no output and exit code `0`.

- [ ] **Step 3: Inspect final diff**

```bash
git diff -- agent_tools/hermes_terminal_toolkit/command_utils.py tests/test_command_utils.py
```

Expected:

- `rewrite_compound_background()` is state-machine based.
- No unrelated terminal wrapper, process registry, lifecycle, or approval changes.
- Tests cover chain operators, simple background, quotes, redirects, comments, idempotence, multiline input, and parenthesized/brace-group conservative behavior.

- [ ] **Step 4: Run repository search for old regex pattern**

```bash
rg -n "rewrite_compound_background|compound that then waits|A && B &|\\(\\?P<prefix\\)" agent_tools tests docs
```

Expected:

- No old named-group regex remains in implementation.
- The helper name remains `rewrite_compound_background`.
- Docs/plans may still mention the old regex as historical context.

- [ ] **Step 5: Final commit**

```bash
git status --short
git add agent_tools/hermes_terminal_toolkit/command_utils.py tests/test_command_utils.py
git commit -m "fix: harden compound background command rewrite"
```

---

## Risks and Mitigations

- Risk: The scanner is still not a complete shell parser.
  - Mitigation: The goal is not full parsing; tests document conservative boundaries. Keep scope limited to the known `A && B &` / `A || B &` subshell-wait trap.
- Risk: Rewriting commands with comments or redirects changes shell meaning.
  - Mitigation: Add explicit tests for `&>`, `2>&1`, comments, and `> file &`.
- Risk: Idempotence regression causes repeated wrappers like `A && { { B & } & }`.
  - Mitigation: Track brace depth and include an idempotence test.
- Risk: Changing sudo scanner while fixing background rewrite broadens scope.
  - Mitigation: Make sudo env-assignment port optional and commit separately.

## Self-review

- Spec coverage: The plan directly replaces the regex compound-background rewrite with the Hermes state-machine version and keeps project-specific public names/call sites intact.
- Placeholder scan: No implementation step depends on TBD behavior; code snippets and commands are concrete.
- Type/signature consistency: Existing public function name `rewrite_compound_background(command: str) -> str` is preserved, so `BaseEnvironment.execute()` requires no change.


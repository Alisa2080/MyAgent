# Remove Execute Command Tool Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the model-visible `execute_command` tool and migrate project guidance, limits, approval wiring, tests, and docs to the stronger Hermes-backed `terminal` and `process` tools without losing foreground shell execution, background process management, session isolation, human approval, or safety behavior.

**Architecture:** Make `terminal` the single model-visible shell execution entry point. Keep `process` for background process lifecycle operations. Remove `execute_command` from parent-agent tool registration, human approval config, tool-call limits, system prompt, README runtime contract, and compatibility exports. Decide whether to delete `agent_tools/shell.py` and `agent_tools/hermes_shell_adapter.py` outright or retain them as unregistered internal legacy modules for one release; the recommended path is direct deletion after tests are migrated because `terminal` already provides foreground execution, background execution, runtime-derived `task_id`, Hermes guards, and non-local backend support.

**Tech Stack:** Python 3.11, LangChain/LangGraph `ToolRuntime`, vendored Hermes terminal toolkit, pytest.

---

## Current Execute Command References

- `agent_core/delegation.py`: imports `execute_command` and includes it in `BASE_TOOLS`.
- `agent_core/builders.py`: `HUMAN_INTERRUPT_ON` includes `execute_command`.
- `agent_core/tool_limits.py`: defines `EXECUTE_COMMAND_*` limits and builds a middleware entry for `execute_command`.
- `agent_core/system_prompt.py`: tells the parent agent to use `execute_command`.
- `agent_tools/shell.py`: defines the LangChain `execute_command` tool and its stricter project-side command blocker.
- `agent_tools/hermes_shell_adapter.py`: local-only adapter used by `execute_command`.
- `agent_tools/general.py`: compatibility export includes `execute_command`.
- `README.md`: documents `execute_command` as one of three shell tools.
- Tests:
  - `tests/test_execute_command_runtime_smoke.py`
  - `tests/test_shell_task_id.py`
  - `tests/test_hermes_shell_adapter_task_id.py`
  - existing terminal tests indirectly assume parent tools include terminal/process.

## Policy Decisions

- `terminal` becomes the only model-visible shell execution tool.
- `process` remains model-visible for background process control.
- `execute_command` should not remain in `BASE_TOOLS`.
- `execute_command` should not remain in `HUMAN_INTERRUPT_ON`.
- `execute_command` should not remain in `ToolCallLimitMiddleware`.
- Prompt guidance should say:
  - use filesystem tools for file reads/searches/writes;
  - use `terminal(background=False)` for tests, builds, scripts, package commands, and foreground shell work;
  - use `terminal(background=True)` for long-running servers/watchers/jobs;
  - use `process(action="poll" | "log" | "wait" | "kill")` to manage background sessions;
  - use `pty=True` only for interactive CLI/REPL-style commands;
  - do not use terminal for routine `ls/find/grep/rg/cat` exploration when file tools can do it.
- Keep Hermes built-in guard plus human approval as the safety model.
- Preserve runtime-derived `task_id` isolation through `terminal` and `process`.
- Preserve backend support by using `terminal` directly instead of the local-only `hermes_shell_adapter`.
- Treat direct Python callers of `execute_command.invoke(...)` as unsupported after this migration unless the user explicitly requests a backward-compatible internal shim.

---

## Task 1: Remove Execute Command From Model-visible Tools

**Files:**
- Modify: `agent_core/delegation.py`
- Modify: `agent_tools/general.py`
- Test: `tests/test_terminal_tools.py` or new `tests/test_shell_tool_registration.py`

- [x] **Step 1: Write registration tests**

Assert:

- `BASE_TOOLS` contains `terminal` and `process`.
- `BASE_TOOLS` does not contain `execute_command`.
- `READ_ONLY_TOOLS` still does not contain shell tools.
- `agent_tools.general` no longer exports `execute_command` if the module is kept.

Suggested test:

```python
def test_parent_shell_tools_are_terminal_and_process_only():
    from agent_core.delegation import BASE_TOOLS, READ_ONLY_TOOLS

    parent_names = {tool.name for tool in BASE_TOOLS}
    read_only_names = {tool.name for tool in READ_ONLY_TOOLS}

    assert "terminal" in parent_names
    assert "process" in parent_names
    assert "execute_command" not in parent_names
    assert "terminal" not in read_only_names
    assert "process" not in read_only_names
    assert "execute_command" not in read_only_names
```

- [x] **Step 2: Remove registration**

In `agent_core/delegation.py`:

- remove `from agent_tools.shell import execute_command`;
- remove `execute_command` from `BASE_TOOLS`;
- keep `terminal` and `process`.

In `agent_tools/general.py`:

- remove `execute_command` import/export, or delete the compatibility export module if no other symbols remain.

- [x] **Step 3: Run registration tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py -v
```

Expected: tests pass.

---

## Task 2: Migrate Human Approval And Tool Limits

**Files:**
- Modify: `agent_core/builders.py`
- Modify: `agent_core/tool_limits.py`
- Test: `tests/test_terminal_tools.py` or new `tests/test_shell_tool_registration.py`

- [x] **Step 1: Write approval/limit tests**

Assert:

- `HUMAN_INTERRUPT_ON` contains `terminal` and `process`.
- `HUMAN_INTERRUPT_ON` does not contain `execute_command`.
- `build_tool_call_limit_middleware()` includes limits for `terminal` and `process`.
- `build_tool_call_limit_middleware()` does not include a limit for `execute_command`.

If `ToolCallLimitMiddleware` internals are hard to inspect, monkeypatch `ToolCallLimitMiddleware` in `agent_core.tool_limits` to capture constructor args.

- [x] **Step 2: Update approval config**

In `agent_core/builders.py`:

- delete the `execute_command` entry from `HUMAN_INTERRUPT_ON`;
- keep `terminal` and `process`;
- optionally refine descriptions:
  - `terminal`: “Review this Hermes terminal command before it executes.”
  - `process`: “Review this Hermes background process action before it executes.”

- [x] **Step 3: Update tool limits**

In `agent_core/tool_limits.py`:

- remove `EXECUTE_COMMAND_RUN_LIMIT` and `EXECUTE_COMMAND_THREAD_LIMIT`;
- add:

```python
TERMINAL_RUN_LIMIT = 8
TERMINAL_THREAD_LIMIT = 16
PROCESS_RUN_LIMIT = 12
PROCESS_THREAD_LIMIT = 32
```

- add `ToolCallLimitMiddleware(tool_name="terminal", ...)`;
- add `ToolCallLimitMiddleware(tool_name="process", ...)`;
- remove `ToolCallLimitMiddleware(tool_name="execute_command", ...)`.

Rationale:

- `terminal` replaces foreground command usage and should inherit the old foreground command limits.
- `process` may need a higher limit because polling/logging can require multiple calls during one run.

- [x] **Step 4: Run approval/limit tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py -v
```

Expected: tests pass.

---

## Task 3: Rewrite System Prompt Shell Guidance

**Files:**
- Modify: `agent_core/system_prompt.py`
- Test: `tests/test_system_prompt.py` if present, otherwise add focused tests.

- [x] **Step 1: Add prompt tests**

Assert the parent prompt:

- contains `terminal`;
- contains `process`;
- contains `background=True`;
- contains `poll`, `log`, `wait`;
- contains `pty=True`;
- does not contain `execute_command`.

- [x] **Step 2: Replace execute_command guidance**

Replace the filesystem/shell policy with guidance equivalent to:

```text
For filesystem work, prefer purpose-built tools: search_files, read_file, patch, and write_file.
Use terminal(background=False) for tests, builds, package scripts, and commands that need a shell.
Use terminal(background=True) for long-running servers, watchers, or jobs, then use process(action="poll"), process(action="log"), process(action="wait"), or process(action="kill") to manage the returned session_id.
Use pty=True only for interactive CLI tools or REPL-like commands.
Do not use terminal for routine ls/find/grep/rg/cat exploration when filesystem tools can do it.
```

- [x] **Step 3: Run prompt tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_system_prompt.py -v
```

If there is no existing prompt test file, run the newly added prompt test file.

---

## Task 4: Remove Or Retire Execute Command Implementation

**Files:**
- Delete or retire: `agent_tools/shell.py`
- Delete or retire: `agent_tools/hermes_shell_adapter.py`
- Delete/migrate tests:
  - `tests/test_execute_command_runtime_smoke.py`
  - `tests/test_shell_task_id.py`
  - `tests/test_hermes_shell_adapter_task_id.py`

- [x] **Step 1: Decide deletion vs internal legacy shim**

Recommended: delete `agent_tools/shell.py` and `agent_tools/hermes_shell_adapter.py` because:

- `terminal` covers foreground execution;
- `terminal` supports background execution;
- `terminal` uses runtime-derived `task_id`;
- `terminal` supports Hermes non-local backends;
- `execute_command` had a local-only adapter and duplicated safety policy.

Alternative: keep `_execute_command_impl` as an unregistered internal function for one release if external direct Python callers exist. If retained, it must not be imported by `BASE_TOOLS`, `HUMAN_INTERRUPT_ON`, prompt, or README as a supported tool.

- [x] **Step 2: Migrate tests to terminal**

Delete execute-command-specific tests or convert their important assertions into terminal tests:

- Runtime injection: already covered by `test_terminal_toolnode_injects_runtime_thread`.
- Dangerous command guard: already covered by `test_terminal_preserves_hermes_guard_block_response`, but add one direct test that `terminal` passes `force=False`.
- Direct `.invoke()` compatibility: no longer required for removed tool.
- Local-only adapter behavior: delete because terminal should support Hermes backends.

- [x] **Step 3: Remove files if no imports remain**

Run:

```bash
rg -n "agent_tools\\.shell|hermes_shell_adapter|execute_command|run_foreground_command" .
```

Then delete implementation files only if the remaining matches are old docs/plans that do not affect runtime. If old plan docs keep historical mentions, leave them unless the project prefers rewriting historical plans.

- [x] **Step 4: Run import/compile checks**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m py_compile agent_core/delegation.py agent_core/builders.py agent_core/tool_limits.py agent_core/system_prompt.py agent_tools/terminal_tools.py
```

Expected: no import errors.

---

## Task 5: Update README And Runtime Contract

**Files:**
- Modify: `README.md`

- [x] **Step 1: Rewrite shell tools section**

Change from “three shell-related tools” to “two shell-related tools”:

- `terminal`: Hermes terminal tool for foreground and background commands.
- `process`: Hermes process tool for background process polling, logs, waiting, stdin, and killing.

Remove:

- `execute_command`: compatibility tool...
- `execute_command` remains as a compatibility layer...

Add:

- `terminal(background=False)` replaces former simple foreground shell execution.
- `terminal(background=True)` should be used for long-running commands.
- `process(...)` manages background sessions.

- [x] **Step 2: Update lifecycle and governance docs**

Ensure README still documents:

- runtime-derived `task_id`;
- `terminal/process` hidden task_id schema;
- process ownership checks;
- human approval;
- per-task background process quota;
- `end_terminal_session(thread_id)`;
- `terminal_execution_scope(thread_id)`;
- `interrupt_terminal_wait_for_thread_id(thread_id)`;
- restart recovery.

---

## Task 6: Full Verification

**Files:**
- All modified files

- [x] **Step 1: Run focused terminal/process tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_terminal_tools.py tests/test_terminal_lifecycle.py tests/test_terminal_process_policy.py -v
```

- [x] **Step 2: Run prompt/tool-limit tests**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_system_prompt.py tests/test_tool_limits.py -v
```

Use the actual new test filenames if different.

- [x] **Step 3: Run remaining test suite or broad smoke subset**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests -v
```

If the full suite is too slow or has unrelated failures, document the exact subset run and residual risk.

- [x] **Step 4: Compile touched modules**

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m py_compile agent_core/delegation.py agent_core/builders.py agent_core/tool_limits.py agent_core/system_prompt.py agent_tools/terminal_tools.py
```

- [x] **Step 5: Check references**

```bash
rg -n "execute_command|EXECUTE_COMMAND|hermes_shell_adapter|run_foreground_command" agent_core agent_tools tests README.md
```

Expected:

- no runtime references to `execute_command`;
- no prompt/docs references to `execute_command`;
- old historical `docs/superpowers/plans/*` references are acceptable only if deliberately left as historical records.

- [x] **Step 6: Check whitespace**

```bash
git diff --check
```

---

## Acceptance Criteria

- Parent agent exposes `terminal` and `process`, not `execute_command`.
- Read-only subagents still expose no shell execution tools.
- Human approval intercepts `terminal` and `process`, not `execute_command`.
- Tool-call limits exist for `terminal` and `process`.
- System prompt instructs the model to use `terminal` for shell work and `process` for background process management.
- System prompt no longer mentions `execute_command`.
- README no longer lists `execute_command` as a supported shell tool.
- `terminal` tests cover runtime `task_id` injection, Hermes guard passthrough, foreground command failure behavior, background quota, and ToolNode runtime injection.
- `process` tests cover scoped list/action behavior and cross-task session denial.
- No runtime imports of `agent_tools.shell`, `agent_tools.hermes_shell_adapter`, or `execute_command` remain.
- Existing functionality is preserved through `terminal(background=False)` for foreground shell commands and `terminal(background=True)` plus `process(...)` for long-running processes.

## Risks And Mitigations

- **Risk:** `execute_command` had stricter project-side blocking than Hermes guard.
  - **Mitigation:** rely on Hermes `force=False` plus human approval; if stricter local policy is still required, move the non-overlapping blocker rules into `terminal` before deleting `shell.py`.

- **Risk:** External direct Python callers might import `agent_tools.shell.execute_command`.
  - **Mitigation:** run `rg`; if no internal callers exist, delete. If external callers are expected, keep a deprecated unregistered shim for one release.

- **Risk:** Tool output schema changes from `execute_command` to `terminal`.
  - **Mitigation:** update prompt/tests to expect `terminal`’s normalized `tool_ok/tool_error` response and avoid relying on legacy `stdout/stderr` fields.

- **Risk:** Tool-call limits for `process` can be too low for polling-heavy workflows.
  - **Mitigation:** start with higher process limits than terminal limits and adjust after real usage.

- **Risk:** Historical plan docs still mention `execute_command`.
  - **Mitigation:** leave historical docs unchanged unless the user wants documentation cleanup beyond runtime/current README.

## Implementation Order

1. Add failing registration, approval, tool-limit, and prompt tests.
2. Remove `execute_command` from `BASE_TOOLS`, approval config, and tool limits.
3. Rewrite system prompt shell guidance to `terminal/process`.
4. Update README.
5. Delete or retire `agent_tools/shell.py` and `agent_tools/hermes_shell_adapter.py`.
6. Delete or migrate execute-command-specific tests.
7. Run full verification and reference scan.

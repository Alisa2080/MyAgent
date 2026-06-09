# Code Execution Tool Migration Design

## Scope

This design covers stages 0-3 of migrating Hermes `code_execution_tool` behavior into this project.

The goal is to make `execute_code` a project-native LangChain tool that preserves the Hermes product model: the model can generate a Python script, the script can call a constrained set of project tools through RPC, and only the script's final output is returned to the model. This reduces multi-step tool-call context growth while preserving the current project's permission model.

Stages 4-6, including non-local backend execution and file-based remote RPC, are out of scope for this implementation spec.

## Decisions

- Use a project-native implementation, not a direct Hermes file transplant.
- Register `execute_code` as a LangChain-facing tool in the current tool catalog.
- Use local POSIX child-process execution with Unix domain socket RPC for stages 1-3.
- Do not support Windows or non-local terminal backends in stages 1-3.
- Enable `code_execution` by default for `dev` and `test` runtime profiles.
- Do not enable it by default for `hosted` or `prod`, but allow explicit opt-in.
- Stage 1 starts with the file development whitelist: `read_file`, `search_files`, `write_file`, `patch`, and `terminal`.
- Stage 3 adds `web_search` and `web_extract`.

## Architecture

`execute_code` should be exposed from a new public tool module, for example:

- `agent_tools/public/code_execution.py`

It should be registered from `agent_core/tool_catalog.py` as:

- name: `execute_code`
- toolset: `code_execution`
- read-only: `False`
- risk level: `high`, with the practical risk still enforced by the internal tool calls

The implementation should be split into focused units:

- Public wrapper: LangChain `@tool` entrypoint, input validation, runtime parsing, standard `ToolMessage` output.
- Local executor: temporary directory creation, script/stub writes, child Python process launch, timeout, stdout/stderr collection, cleanup.
- UDS RPC server: parent-side socket listener that receives script tool calls.
- Tool dispatcher: maps RPC tool names to existing project public wrappers.
- Stub generator: generates `hermes_tools.py` for the allowed tools.
- Safety helpers: environment filtering, output truncation, sensitive text redaction, terminal argument normalization, and max tool-call counting.

The implementation must not introduce Hermes' `tools.registry` or `handle_function_call` patterns. Tool dispatch must use this project's public wrappers and runtime identity model.

## Stage 0: Migration Spec

Stage 0 produces this design and freezes the stage 1-3 implementation scope.

Acceptance:

- The design names the stage 1-3 feature boundary.
- The design explains how Hermes semantics map onto this project's LangChain tool architecture.
- The design lists explicit non-goals.

## Stage 1: Local MVP Executor

Stage 1 implements a local-only `execute_code` tool using POSIX child processes and UDS RPC.

Visible sandbox tools:

- `read_file`
- `search_files`
- `write_file`
- `patch`
- `terminal`

Required behavior:

1. The model calls `execute_code(code=...)`.
2. The wrapper resolves `RuntimeContext` from the LangChain `ToolRuntime`.
3. The executor creates a temporary directory.
4. The executor writes `script.py`.
5. The executor writes generated `hermes_tools.py`.
6. The parent starts a UDS RPC server.
7. The child process runs `script.py`.
8. Script calls to `hermes_tools` cross the socket to the parent process.
9. The parent dispatches those calls to the existing public wrappers.
10. The child script prints final output.
11. `execute_code` returns only the final stdout/stderr summary and structured metadata to the model.

Stage 1 acceptance:

- `execute_code` appears in default tools for `dev` and `test`.
- `execute_code` does not appear by default for `hosted` or `prod`.
- Local execution returns stdout for a simple Python script.
- `hermes_tools.py` includes only the file development whitelist.
- Script code can call `read_file` and `search_files`.
- Script code can call foreground `terminal`.
- Non-zero child exit and timeout return stable `tool_failure` results.
- stdout and stderr limits are enforced.

## Stage 2: Permission And Tool-Bus Integration

Stage 2 ensures `execute_code` does not bypass existing project policy.

Internal dispatch should call the existing public wrappers for each tool:

- `agent_tools.public.files.read_file`
- `agent_tools.public.files.search_files`
- `agent_tools.public.files.write_file`
- `agent_tools.public.files.patch`
- `agent_tools.public.terminal.terminal`

The dispatch runtime must preserve the current thread/task identity through `RuntimeContext`. Tool call identity can be represented with deterministic nested IDs for RPC calls, as long as policy grants and result artifacts remain coherent.

Policy requirements:

- `read_file` and `search_files` still obey workspace read policy.
- `write_file` still obeys workspace write policy and approval rules.
- `patch` still obeys workspace write policy and approval rules.
- `terminal` still obeys command policy, human approval, network grant handling, and runtime profile policy.
- Any policy rejection or approval requirement is returned to the script as a structured failure dict.

Terminal restrictions:

- Script-side `terminal()` must not support background or interactive execution.
- Parent-side dispatch must force or strip:
  - `background=False`
  - `pty=False`
  - `notify_on_complete=False`
  - `watch_patterns=None`

Stage 2 acceptance:

- A dangerous terminal command still returns `approval_required` or `policy_denied`.
- A safe foreground terminal command can succeed.
- Workspace-local write and patch behavior matches direct tool behavior.
- Out-of-workspace writes are denied or require approval exactly as direct tool calls do.
- Tool-call count limits are enforced.
- Secret-like environment variables are not inherited by the child process.
- Returned stdout/stderr is redacted before being exposed to the model.

## Stage 3: Complete Stage Whitelist And Dynamic Schema

Stage 3 extends the sandbox tool surface to match the Hermes whitelist for the local backend.

Additional visible tools:

- `web_search`
- `web_extract`

Full stage 3 whitelist:

- `web_search`
- `web_extract`
- `read_file`
- `write_file`
- `search_files`
- `patch`
- `terminal`

The actual visible tool set must be:

```text
stage whitelist intersect current enabled tools intersect runtime-profile allowed tools
```

The generated `execute_code` schema should:

- Explain that `execute_code` is useful for 3+ tool calls, loops, filtering, batching, retries, and result compression.
- Explain that direct tool calls are better for single simple operations.
- Explain that interactive terminal sessions and background services are not supported.
- List only the actual tools visible to the current invocation.

The generated `hermes_tools.py` must follow the same visible tool set as the schema.

Stage 3 acceptance:

- `web_search` and `web_extract` stubs are generated when web tools are enabled.
- Web stubs are not generated when web tools are unavailable.
- The schema does not claim unavailable tools.
- Web tool success and failure artifacts are converted into script-friendly dicts.
- Tool documentation explains when to use `execute_code`.

## Script Return Shape

RPC tool calls should return plain Python dictionaries to the script instead of LangChain objects.

Success shape:

```python
{
    "ok": True,
    "tool": "read_file",
    "message": "File read.",
    "data": {},
    "meta": {},
}
```

Failure shape:

```python
{
    "ok": False,
    "tool": "terminal",
    "message": "Approval required.",
    "error": {"code": "approval_required", "message": "Approval required."},
    "data": {},
    "meta": {},
}
```

The dispatcher should derive this shape from `ToolMessage.artifact` when available. If a tool returns an unexpected value, the dispatcher should normalize it into a failure or conservative string payload.

## Configuration

Configuration should use the existing project configuration flow where possible.

Suggested keys:

```yaml
code_execution:
  enabled: auto
  mode: project
  timeout_seconds: 300
  max_tool_calls: 50
  stdout_limit_chars: 50000
  stderr_limit_chars: 10000
```

Semantics:

- `enabled: auto`: enabled by default for `dev` and `test`; disabled by default for `hosted` and `prod`.
- `enabled: true`: explicitly enabled for all profiles.
- `enabled: false`: explicitly disabled for all profiles.
- `mode: project`: default. The child process runs with the project/session working directory and uses the active virtualenv or conda Python when detectable, otherwise `sys.executable`.
- `mode: strict`: reserved for a more isolated mode. Stage 1-3 may parse and document it, but do not need full strict-mode parity.

Invalid config values should fall back to safe defaults and log warnings.

## Safety Defaults

Default limits:

- timeout: 300 seconds
- max RPC tool calls: 50
- stdout limit: 50,000 characters
- stderr limit: 10,000 characters

Environment handling:

- Pass only safe environment variables such as `PATH`, `HOME`, `USER`, `LANG`, `LC_*`, `TERM`, `TMPDIR`, `TMP`, `TEMP`, `VIRTUAL_ENV`, `CONDA_PREFIX`, and similar interpreter/runtime essentials.
- Do not pass variables whose names include secret-like substrings such as `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, or `AUTH`.

Output handling:

- Strip ANSI control sequences before returning output.
- Truncate stdout and stderr independently.
- Redact sensitive text before returning any output to the model.

Cleanup:

- Clean temporary directories after execution.
- Close the UDS server after execution.
- Kill the child process group on timeout or interruption.

## Non-Goals For Stages 0-3

- Windows support.
- Non-local terminal backend support.
- Docker, SSH, Modal, Daytona, or other remote execution backends.
- File-based RPC.
- Background process management inside `execute_code`.
- Interactive terminal or PTY support inside scripts.
- OS-level sandbox escape prevention.
- A separate Hermes-style registry or parallel tool execution stack.

Stages 0-3 rely on child-process isolation, environment filtering, output redaction, and reuse of existing tool permissions. They are not a complete operating-system sandbox.

## Test Plan

Unit tests:

- Stub generation includes expected tools.
- Stub generation excludes unavailable tools.
- Dynamic schema lists only available tools.
- Terminal blocked parameters are stripped.
- Environment filtering removes secret-like variables.
- stdout/stderr truncation works.
- ANSI cleanup and redaction are applied.
- Tool-call count limit returns a structured failure.

Integration tests:

- Simple script returns stdout.
- Script calls `read_file`.
- Script calls `search_files`.
- Script calls safe foreground `terminal`.
- Script attempts dangerous `terminal` and receives policy failure or approval requirement.
- Script writes workspace-local file through `write_file`.
- Script attempts out-of-workspace write and receives the same policy result as direct `write_file`.
- Script applies `patch` through existing patch policy.
- Stage 3 script calls `web_search` and `web_extract` when web tools are enabled.

Catalog/config tests:

- `dev` and `test` include `execute_code` by default.
- `hosted` and `prod` do not include `execute_code` by default.
- Explicit opt-in enables `code_execution` for hosted/prod.
- Explicit disable removes `execute_code` for all profiles.

Documentation tests or checks:

- Public tool documentation states the 3+ tool-call use case.
- Public tool documentation states that single simple operations should use direct tools.
- Public tool documentation states that background and interactive terminal sessions are not supported.

# Code Execution Tool Stages 4-6 Design

## Scope

This design covers stages 4-6 of the Hermes-style `execute_code` migration.
Stages 0-3 already introduced the project-native local executor, Unix domain
socket RPC, dynamic visible tools, and permission-preserving dispatch through
this project's public tool wrappers.

Stages 4-6 extend that foundation to Docker execution, product-level
configuration, lifecycle behavior, and maintainable tests and documentation.

## Goals

- Preserve the current project architecture: `execute_code` remains a public
  LangChain tool registered through `agent_core.tool_catalog`.
- Reuse terminal toolkit lifecycle. Docker execution must use the active
  terminal environment for the current `task_id`, not create a second container
  manager.
- Use backend-aware transport: local backends continue to use UDS RPC, Docker
  backends use file-based RPC.
- Fully support Docker in stage 4. Other non-local backends are explicitly
  unsupported for now, with interfaces shaped so later backend support does not
  require another rewrite.
- Make `project` the default execution mode and `strict` an explicit isolated
  mode.
- Keep project policy intact: file, terminal, patch, and web calls from scripts
  must still route through the existing public wrappers and permission model.
- Provide observable safe fallback for invalid configuration.
- Ensure timeout, interruption, output redaction, and cleanup behavior are
  deterministic enough to test.

## Non-Goals

- Stage 4 does not support ssh, singularity, modal, or daytona execution.
- Stage 4 does not introduce a new Docker runner or duplicate terminal toolkit
  lifecycle.
- Stage 4 does not make arbitrary host files visible to the child script.
- Stage 5 does not support secret forwarding into the child process.
- Stage 6 does not enable `code_execution` by default for hosted or production
  profiles.

## Architecture

The stage 0-3 implementation should be split into focused units before adding
Docker behavior:

- `CodeExecutionConfig`: resolves mode, timeout, tool-call limits, output
  limits, environment allowlist, and secret denylist.
- `CodeExecutionRunner`: selects the correct runner for the active backend.
- `LocalUdsRunner`: retains the existing local UDS execution path.
- `DockerFileRpcRunner`: executes scripts inside the active Docker terminal
  toolkit environment and uses file-based RPC.
- `CodeExecutionDispatcher`: continues to call the current public wrappers.
- `FileRpcBridge`: parent-side request poller that reads `req_*` files,
  dispatches tool calls, and writes matching `res_*` files.
- Stub generators: generate either UDS or file-RPC `hermes_tools.py` modules
  from the same visible tool set.

The visible tool set remains:

```text
stage whitelist intersect current enabled tools intersect runtime-profile allowed tools
```

## Stage 4: Docker File-RPC Execution

Stage 4 implements Docker as the first complete non-local backend.

Backend selection:

1. Resolve `RuntimeContext` from the incoming `ToolRuntime`.
2. Resolve the active terminal backend through terminal toolkit metadata.
3. If the backend is `local`, use `LocalUdsRunner`.
4. If the backend is `docker`, use `DockerFileRpcRunner`.
5. For every other backend, return a stable `unsupported_backend` failure with
   the backend type in result data.

Docker environment lifecycle:

- The runner must call terminal toolkit's active environment path, currently
  `get_or_create_active_env(task_id)`.
- The runner must not call Docker directly to create a separate container.
- The Docker environment must be persistent and expose a host-visible
  `/workspace` directory from terminal toolkit's persistent sandbox.
- If the active Docker env is not persistent, or the host-side `/workspace`
  location cannot be determined, return a configuration failure.

Docker run layout:

```text
container: /workspace/.code_execution/<run_id>/
host:      <terminal-toolkit-docker-workspace>/.code_execution/<run_id>/

script.py
hermes_tools.py
sitecustomize.py
rpc/
  req_<request_id>.json
  res_<request_id>.json
```

File-RPC protocol:

- The script writes JSON request files under `rpc/req_<id>.json`.
- The script waits for `rpc/res_<id>.json` until its per-call deadline expires.
- The parent `FileRpcBridge` polls request files, atomically claims each new
  request, dispatches it through `CodeExecutionDispatcher`, and writes the
  response file.
- Request and response payloads use the existing script-friendly success and
  failure dictionary shapes.
- Frame and file size limits must be enforced so a script cannot exhaust parent
  memory with a large request file.
- Responses should be written atomically with a temporary file plus rename.

Docker script execution:

- `project` mode runs inside the active Docker environment's current working
  directory unless overridden by safe configuration.
- `strict` mode runs from the isolated code execution run directory.
- The command should execute the container's Python interpreter against the
  generated script path.
- The generated `PYTHONPATH` must make the generated stub and policy hook
  importable inside the container.
- Script stdout/stderr are collected from terminal toolkit execution results.

Cleanup:

- On success or failure, stop the `FileRpcBridge`, remove the run directory, and
  report cleanup warnings in result metadata if cleanup fails.
- On timeout, terminate the script command, stop the bridge, and clean the run
  directory.
- The terminal toolkit environment itself is not cleaned by `execute_code`;
  existing terminal lifecycle remains responsible for session cleanup.

Acceptance:

- `TERMINAL_ENV=docker` can execute a simple Python script.
- `hosted` and `prod` profiles can execute when `code_execution` is explicitly
  enabled and their default terminal backend resolves to Docker.
- A script call to `hermes_tools.read_file` crosses file-RPC back to the parent
  process and is dispatched through the existing public file tool wrapper.
- Timeout and cleanup leave no active bridge thread and no run directory.
- Non-Docker non-local backends return `unsupported_backend`.

## Stage 5: Modes, Configuration, And Lifecycle

Stage 5 turns stage 4 into product-level behavior.

Execution modes:

- `project` is the default.
  - Local: use the session working directory and prefer the active project
    Python, virtualenv, or conda interpreter when detectable.
  - Docker: use the active Docker environment and its current working
    directory.
- `strict` is explicit.
  - Local: use an isolated temporary directory and the current interpreter.
  - Docker: use the persistent sandbox run directory and avoid direct project
    cwd access; project files remain accessible only through RPC tools.

Configuration priority:

```text
explicit tool/runtime configuration > environment variables > safe defaults
```

Configuration fields:

- `mode`
- `timeout_seconds`
- `max_tool_calls`
- `stdout_limit_chars`
- `stderr_limit_chars`
- `output_limit_chars`
- `env_allowlist`
- `secret_denylist`

Environment variables:

- `CODE_EXECUTION_MODE`
- `CODE_EXECUTION_TIMEOUT_SECONDS`
- `CODE_EXECUTION_MAX_TOOL_CALLS`
- `CODE_EXECUTION_STDOUT_LIMIT_CHARS`
- `CODE_EXECUTION_STDERR_LIMIT_CHARS`
- `CODE_EXECUTION_OUTPUT_LIMIT_CHARS`
- `CODE_EXECUTION_ENV_ALLOWLIST`
- `CODE_EXECUTION_SECRET_DENYLIST`

Safe fallback:

- Invalid mode falls back to `project`.
- Invalid or non-positive limits fall back to defaults.
- Extremely large limits are capped.
- Fallbacks are logged and returned in result `meta.warnings`.

Environment handling:

- Child processes inherit only allowlisted environment variables.
- Denylist matching wins over allowlist matching.
- Names containing secret-like markers such as `KEY`, `TOKEN`, `SECRET`,
  `PASSWORD`, `CREDENTIAL`, `PASSWD`, or `AUTH` are denied by default.
- Stage 5 does not provide a secret forwarding mechanism.

Output handling:

- Strip ANSI control sequences before returning output.
- Redact sensitive text before exposing stdout, stderr, exceptions, or RPC
  payloads to the model.
- Truncate stdout and stderr independently.
- Apply the optional total output limit after independent truncation.

Lifecycle and interruption:

- Local timeout terminates the child process group, closes UDS, and removes the
  temp directory.
- Docker timeout terminates the container-side script command, stops the
  file-RPC bridge, and removes the run directory.
- Long waits should participate in `terminal_execution_scope` so upstream
  thread interruption can break out of blocking terminal waits.
- Cleanup failure should not mask the primary script result; return warnings in
  metadata.

Acceptance:

- Config parsing errors use safe fallback and produce observable warnings.
- API key, token, password, credential, auth, and similar variables are not
  inherited by scripts.
- Secret-like output is redacted from stdout/stderr and RPC-derived content.
- Long-running scripts can be timed out or interrupted without leaking child
  processes, bridge threads, or RPC directories.

## Stage 6: Tests, Documentation, And Enablement Policy

Stage 6 makes the feature maintainable.

Unit tests:

- UDS and file-RPC stub generation.
- Visible tool whitelist intersection.
- Terminal argument normalization that strips background, PTY,
  notifications, and watch patterns.
- Config parsing fallback and warnings.
- Environment allowlist and secret denylist behavior.
- Output redaction and truncation.
- RPC request size and malformed request handling.

Local integration tests:

- `execute_code` can call file, terminal, patch, and web tools when enabled.
- Dangerous terminal calls still return approval or policy failures.
- Out-of-workspace writes and patches are still denied or reviewed according to
  direct tool behavior.

Docker integration tests:

- Tests that require Docker should skip when Docker is unavailable.
- `TERMINAL_ENV=docker` executes a simple script.
- Docker script calls cross file-RPC back to the parent process.
- Timeout removes run directory and stops the bridge thread.
- Docker configuration errors return stable failure codes.

Catalog and import tests:

- Public imports remain valid.
- Tool catalog tests preserve `dev` and `test` default enablement.
- `hosted` and `prod` do not enable `code_execution` by default.
- Explicit `enabled_toolsets=["code_execution"]` still enables the tool in
  hosted and production profiles.

Documentation:

- README documents local vs Docker execution.
- README documents `project` and `strict` modes.
- README documents Docker persistent `/workspace` requirements.
- Tool docs explain when `execute_code` is appropriate and when direct tools are
  better.
- Tool docs state that interactive terminal sessions and background services are
  not supported from scripts.
- Documentation explains that all script tool calls go through existing project
  permission and policy systems.

Default enablement:

- Keep `code_execution` enabled by default for `dev` and `test`.
- Keep it disabled by default for `hosted` and `prod`.
- Allow explicit hosted/prod opt-in through the `code_execution` toolset.
- Hosted/prod default enablement is a later product decision because
  `execute_code` remains a high-risk tool aggregation surface.

Acceptance:

- Pytest covers the core local and Docker migration paths.
- Public imports and tool catalog tests are updated.
- Documentation is sufficient for both users and models to understand use
  cases, backend requirements, and safety boundaries.
- The default enablement strategy is explicit and tested.

# LangChain Agent Workspace

## Runtime Layout

- `agent.py`: LangGraph entrypoint. Keep this file small because `langgraph.json` loads `agent` from here.
- `agent_core/`: agent construction, prompts, middleware helpers, model config, memory, shared schemas, and workspace path handling.
- `agent_tools/`: LangChain tools exposed to the agent, including file tools, skill tools, shell/web/memory tools, and skill management.
  - `file_tools.py`: LangChain-facing standard file tools (`list_directory`, `read_file`, `write_file`, `patch`, `search_files`, `file_info`) plus workspace policy.
  - `file_policy.py`: workspace path policy and environment setup for file tools.
  - `shell.py`: shell command execution and command safety checks.
  - `web.py`: TinyFish-backed web search and fetch tools.
  - `memory_tools.py`: durable memory tool wrapper for `memory_manage`.
  - `skills.py` and `skill_manage.py`: skill discovery, viewing, creation, editing, and deletion.
  - `common.py`: shared JSON response, truncation, path metadata, backup, trash, and text decoding helpers.
- `agent_tools/file_toolkit/`: internal file operation implementation used by `agent_tools/file_tools.py`.
  - `file_tools.py`: JSON-returning primitive orchestration functions for read/write/patch/search. This is not a LangChain tool module.
  - `file_operations.py`: shell-backed low-level file operations.
  - `result_models.py`: result dataclasses shared by low-level operations and patch application.
  - `terminal_environment.py`: local shell execution backend used by file operations.
  - `file_state.py`: cross-agent read/write coordination and stale-write detection.
  - `patch_parser.py`: V4A patch parsing and application.
  - `fuzzy_match.py`: fuzzy replacement strategies and no-match hints.
  - `file_safety.py`, `binary_extensions.py`, `redact.py`, `tool_output_limits.py`: focused safety, binary detection, redaction, and output-limit helpers.
- `skills/`: local procedural skills loaded by `skills_list` and `skill_view`.
- `base/` and `projects/`: examples, notebooks, and experiments. These are not part of the agent runtime.

## Import Rules

- Runtime code should import through `agent_core.*` and `agent_tools.*`.
- Keep root-level Python files limited to entrypoints.
- Put new LangChain tool schemas close to the tool implementation unless they are shared across modules.
- Keep LangChain-facing file tool policy in `agent_tools/file_tools.py`; keep low-level file operation mechanics in `agent_tools/file_toolkit/`.
- Keep workspace path policy in `agent_tools/file_policy.py`; low-level toolkit modules should not know about project-specific workspace rules.
- Avoid adding new behavior to `agent_tools/general.py`; it exists only as a compatibility export layer.

## Hermes Terminal Session Contract

The project exposes three shell-related tools:

- `execute_command`: compatibility tool for foreground commands with the legacy output schema and stricter project-side blocking.
- `terminal`: first-class Hermes terminal tool for foreground and background commands.
- `process`: first-class Hermes process tool for background process polling, logs, waiting, stdin, and killing.

Runtime session isolation is derived from the LangGraph execution thread:

- When LangChain provides `ToolRuntime.execution_info.thread_id`, terminal tools hash that thread id into a path-safe Hermes `task_id`.
- If `execution_info.thread_id` is unavailable, tools fall back to `runtime.config["configurable"]["thread_id"]`.
- The raw thread id is not exposed to the model and is not written into Hermes paths or checkpoints.
- If no runtime thread id is available, tools fall back to the Hermes `default` task id. This fallback is intended for local tests and direct implementation calls only.
- Production callers should provide a stable LangGraph `thread_id` for each user conversation/session.

`terminal` and `process` do not expose `task_id` in their tool schemas. `process` validates that `session_id` belongs to the current runtime-derived `task_id` before allowing `poll`, `log`, `wait`, `kill`, `write`, `submit`, or `close`.

Security and approval:

- `terminal` uses Hermes built-in command guards by calling Hermes with `force=False`.
- `terminal` and `process` are intercepted by the human-in-the-loop middleware before execution.
- `execute_command` remains as a compatibility layer and should not be used for new long-running/background workflows.

Lifecycle policy:

- Normal agent turns preserve background processes.
- Explicit user-session shutdown should call `cleanup_terminal_session_for_thread_id(thread_id)` or `cleanup_terminal_session_for_runtime(runtime)`.
- Explicit cleanup kills running processes for the session task id and cleans the active Hermes environment.
- Parent agent startup calls `recover_terminal_processes()`. Hermes can recover host-backed background processes as detached sessions after restart; sandbox-backed processes are skipped by Hermes because their in-sandbox PIDs are not meaningful after restart.

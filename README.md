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

The shell tool uses Hermes terminal toolkit under the hood. Runtime session isolation is derived from the LangGraph execution thread:

- When LangChain provides `ToolRuntime.execution_info.thread_id`, the shell tool hashes that thread id into a path-safe Hermes `task_id`.
- The raw thread id is not exposed to the model and is not written into Hermes paths or checkpoints.
- If no runtime thread id is available, tools fall back to the Hermes `default` task id. This fallback is intended for local tests and direct function calls only.
- Production callers should provide a stable LangGraph `thread_id` for each conversation/run thread.

Future `terminal` and `process` tools must use the same helper in `agent_core.session_context` and must not expose `task_id` as a model-controlled argument.

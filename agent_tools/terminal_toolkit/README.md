# terminal-toolkit

Standalone terminal and process tools with optional LangChain adapters.

This package carries its own:

- `terminal_tool` foreground/background execution flow
- `process_registry` background process management
- environment backends for `local`, `docker`, `singularity`, and `ssh`
- dangerous-command guard, PTY handling, sudo rewriting, ANSI stripping, and output redaction

## Direct usage

```python
from agent_tools.terminal_toolkit import run_process, run_terminal

result = run_terminal("python -c \"print('hello')\"")
```

## LangChain usage

```python
from agent_tools.terminal_toolkit import build_langchain_tools

tools = build_langchain_tools(default_task_id="session-123")
```

If you want the model to control task/session isolation explicitly:

```python
tools = build_langchain_tools(
    default_task_id="session-123",
    expose_task_id=True,
)
```

## Shared envs

`get_or_create_active_env(task_id)` is the shared env entry point used by the
terminal tools and the file toolkit. Cached envs are refreshed on access and
cleared through the terminal cleanup paths.

## Supported env vars

- `TERMINAL_ENV`: `local`, `docker`, `singularity`, `ssh`
- `TERMINAL_CWD`
- `TERMINAL_TIMEOUT`
- `TERMINAL_LIFETIME_SECONDS`
- `TERMINAL_SSH_HOST`
- `TERMINAL_SSH_USER`
- `TERMINAL_SSH_PORT`
- `TERMINAL_SSH_KEY`
- `TERMINAL_DOCKER_IMAGE`
- `TERMINAL_SINGULARITY_IMAGE`
- `TERMINAL_DOCKER_VOLUMES`
- `TERMINAL_DOCKER_FORWARD_ENV`
- `TERMINAL_TOOLKIT_HOME`
- `TERMINAL_TOOLKIT_MAX_BYTES`
- `TERMINAL_TOOLKIT_ENV_PASSTHROUGH`
- `TERMINAL_TOOLKIT_ISOLATE_HOME`
- `TERMINAL_TOOLKIT_SUBPROCESS_HOME`

For most LangChain agents, keep `expose_task_id=False` and bind one stable
`default_task_id` per agent session. That preserves environment reuse and
background process tracking semantics.

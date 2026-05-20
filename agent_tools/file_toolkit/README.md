# File Toolkit

Internal file operation toolkit used by `agent_tools/file_tools.py`.

## Direct use

```python
from agent_tools.file_toolkit import read_file_tool, write_file_tool, patch_tool, search_tool

write_file_tool("notes.txt", "hello\n", task_id="session-1")
read_file_tool("notes.txt", task_id="session-1")
patch_tool(path="notes.txt", old_string="hello", new_string="hi", task_id="session-1")
search_tool("hi", path=".", task_id="session-1")
```

All tool functions return JSON strings. LangChain schemas are intentionally
defined only in the outer `agent_tools/file_tools.py` adapter.

LangChain-facing wrappers live in `agent_tools/file_tools.py`. Keep this
subpackage focused on file operation mechanics and JSON-returning primitives.

## Runtime model

File operations are backed by shared Hermes active envs created through
`agent_tools.hermes_terminal_toolkit.terminal_tool.get_or_create_active_env()`.
The wrappers follow the live env cwd, refresh activity on access, and clear
cached wrappers when Hermes cleans up a task.

Low-level safe write roots are backend-aware:

- Local low-level write safety enforces the shared denylist and, when set,
  `AGENT_WRITE_SAFE_ROOT`.
- Docker file tools also allow backend paths under `/workspace` and the
  container active or configured cwd.
- Singularity file tools also allow only the active or configured container cwd;
  `/workspace` is not automatically trusted unless it is that cwd.
- SSH file tools also allow only the active or configured remote cwd;
  `/workspace` remains denied for SSH.

Public read/search admission in `agent_tools/public/files.py` additionally
blocks internal skill cache paths such as `skills/.hub/index-cache` under each
allowed workspace root. That block is not a general low-level file toolkit
policy and does not apply to public write/patch admission. Public write/patch
admission still enforces allowed workspace roots before calling the low-level
file toolkit.

## Environment

Useful environment variables:

- `TERMINAL_CWD`: base directory for relative paths.
- `AGENT_WRITE_SAFE_ROOT`: if set, writes and patches are constrained to this directory tree.
- `TERMINAL_ENV`: backend used by the shared Hermes env.
- `AGENT_FILE_READ_MAX_CHARS`: max characters returned by a single `read_file`.
- `AGENT_FILE_MAX_LINES`: max lines returned by `read_file`.
- `AGENT_FILE_MAX_LINE_LENGTH`: max characters per returned line.
- `AGENT_REDACT_SECRETS=true`: redact likely secrets in tool output.

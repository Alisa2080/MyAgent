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

## Environment

Useful environment variables:

- `TERMINAL_CWD`: base directory for relative paths.
- `AGENT_WRITE_SAFE_ROOT`: if set, writes and patches are constrained to this directory tree.
- `AGENT_FILE_READ_MAX_CHARS`: max characters returned by a single `read_file`.
- `AGENT_FILE_MAX_LINES`: max lines returned by `read_file`.
- `AGENT_FILE_MAX_LINE_LENGTH`: max characters per returned line.
- `AGENT_REDACT_SECRETS=true`: redact likely secrets in tool output.

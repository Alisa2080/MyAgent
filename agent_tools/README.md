# Agent Tools Package

`agent_tools` contains LangChain-facing tools plus the implementation packages those tools wrap.

## Public Tool Facades

New runtime code should import LangChain tools from `agent_tools.public`:

- `agent_tools.public.files`: `list_directory`, `read_file`, `write_file`, `patch`, `search_files`, `file_info`
- `agent_tools.public.terminal`: `terminal`, `process`
- `agent_tools.public.web`: `web_search`, `web_fetch`
- `agent_tools.public.memory`: `memory_manage`
- `agent_tools.public.skills`: `skills_list`, `skill_view`, `skill_manage`

## Shared Helpers

First-party helpers live under `agent_tools.shared`:

- `common`: truncation, path metadata, and common formatting helpers.
- `file_policy`: workspace path policy for file tools.
- `tool_output`: normalized JSON success/error responses.

## Internal Toolkits

- `file_toolkit/` is the internal implementation for workspace file operations. It is not a LangChain tool registration boundary.
- `hermes_terminal_toolkit/` is the imported Hermes terminal toolkit. Project-native LangChain wrappers live in `agent_tools.public.terminal`.

## Compatibility Imports

Top-level modules such as `agent_tools.file_tools`, `agent_tools.terminal_tools`, and `agent_tools.web` are kept as compatibility shims during migration. Do not add new implementation logic to those files.

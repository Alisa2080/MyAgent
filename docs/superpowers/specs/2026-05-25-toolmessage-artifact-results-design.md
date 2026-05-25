# ToolMessage Artifact Results Design

Date: 2026-05-25

## Goal

Migrate public agent tools from JSON-string tool results to LangChain standard `ToolMessage` results.

Public tools should return a short model-facing summary in `ToolMessage.content` and the complete structured result in `ToolMessage.artifact`. This separates context-efficient model output from full machine-readable tool data.

The migration is intentionally a public-contract change. Tests and callers that currently parse `json.loads(raw)` should move to `ToolMessage` assertions and artifact access.

## Non-Goals

- Do not redesign public tool input schemas.
- Do not require low-level toolkit modules to stop returning legacy JSON strings in the same change.
- Do not keep JSON strings as the public wrapper contract.
- Do not broaden tool behavior beyond result formatting and result normalization.
- Do not remove legacy `tool_ok` or `tool_error` helpers until all remaining internal callers are audited.

## Current Problem

Public wrappers currently return JSON strings produced by `tool_ok` and `tool_error`. Some wrappers also decode lower-level JSON strings and then encode a new JSON string.

Examples:

- `agent_tools/public/files.py` decodes file toolkit JSON in `_wrap_file_tool_result` and wraps it again as `tool_ok` or `tool_error`.
- `agent_tools/public/terminal.py` decodes Hermes terminal JSON in `_decode_hermes_payload`, maps statuses, and wraps the result again.
- `agent_tools/public/cronjob.py` converts cron implementation dictionaries into `tool_ok` or `tool_error`.

This creates three concrete problems:

1. The model sees the same large payload that callers need for machine processing.
2. Each wrapper repeats JSON decoding, error mapping, and standard envelope construction.
3. Tests and callers are coupled to JSON-string output instead of LangChain's tool-result contract.

Large-output tools make the issue more visible. Terminal stdout/stderr, file search matches, file reads, and cron job details can all place high-volume data directly in model context.

## Recommended Design

Use LangChain `ToolMessage` as the public tool result contract.

Each public tool returns:

- `content`: concise model-facing summary.
- `artifact`: complete structured payload.
- `status`: LangChain success or error status.
- `name`: tool name where available.
- `tool_call_id`: runtime tool call id where available.

The artifact preserves the current standard envelope shape so downstream code can migrate predictably:

```python
{
    "ok": True,
    "tool": "terminal",
    "message": "Terminal command completed.",
    "data": {...},
    "error": None,
    "meta": {...},
}
```

Errors use the same envelope with `ok=False`:

```python
{
    "ok": False,
    "tool": "terminal",
    "message": "Command exited with code 1.",
    "data": {...},
    "error": {
        "code": "command_failed",
        "message": "Command exited with code 1.",
    },
    "meta": {...},
}
```

This keeps full structured data available without injecting full payloads into `content`.

## Components

### Shared Result Builder

Add a shared module, likely `agent_tools/shared/tool_result.py`, that owns public result construction.

It should provide helpers equivalent to:

```python
def tool_success(
    tool: str,
    *,
    message: str,
    data: Any = None,
    meta: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolMessage: ...


def tool_failure(
    tool: str,
    message: str,
    *,
    code: str = "tool_error",
    data: Any = None,
    meta: dict[str, Any] | None = None,
    runtime: ToolRuntime | None = None,
) -> ToolMessage: ...
```

The helpers should:

1. Build the standard artifact envelope.
2. Derive `tool_call_id` from `RuntimeContext` or `ToolRuntime` when available.
3. Set LangChain status to success or error.
4. Keep `content` short and deterministic.

The module should also provide a legacy adapter:

```python
def from_legacy_json(
    tool: str,
    raw: str,
    *,
    success_message: str,
    meta_keys: tuple[str, ...] = (),
    runtime: ToolRuntime | None = None,
    summary: Callable[[dict[str, Any]], str] | None = None,
) -> ToolMessage: ...
```

This adapter allows public wrappers to consume lower-level toolkit JSON without each wrapper hand-writing JSON decode and error mapping.

### Summary Policy

`ToolMessage.content` is not a JSON dump. It is a short summary that helps the model decide the next step.

Default content rules:

- Success: use the operation message, optionally enriched with a small count or target.
- Error: use the human-readable error message.
- Decode failure: say the backend returned an invalid response.
- Truncation: mention that output was truncated when the artifact metadata says so.

Tool-specific summaries should be small and predictable:

- `terminal`: command completion, exit code, timeout, or background session id.
- `process`: action completion and session id when relevant.
- `search_files`: match count, file count, and truncation.
- `read_file`: path and line range.
- `list_directory`: entry count and truncation.
- `cronjob`: action result and job id/name when available.
- `web_search`: result count.
- `web_fetch`: fetched URL and content length or truncation.
- `skills_list`: skill count.
- `skill_view`: viewed skill and file path.
- `memory_manage`: action-level success or failure.
- `file_info`: inspected path and type.

Full stdout, stderr, file contents, search result lists, cron details, and fetched web content belong in `artifact.data`, not in `content`.

### Public Tool Scope

The migration covers all public tools in one change:

- `agent_tools/public/files.py`
  - `list_directory`
  - `read_file`
  - `write_file`
  - `patch`
  - `search_files`
  - `file_info`
- `agent_tools/public/terminal.py`
  - `terminal`
  - `process`
- `agent_tools/public/cronjob.py`
- `agent_tools/public/memory.py`
- `agent_tools/public/skills.py`
- `agent_tools/public/skill_manage_impl.py`
- `agent_tools/public/web.py`
- `agent_core/delegation.py`
  - `task`

The implementation should audit all `@tool(...)` definitions and make their public return annotations match the new contract.

### Low-Level Toolkit Boundary

Low-level toolkits may continue returning legacy JSON strings during this migration. Public wrappers own the boundary conversion.

This applies to:

- file toolkit functions such as `read_file_tool`, `write_file_tool`, `patch_tool`, and `search_tool`
- Hermes terminal functions such as `run_terminal` and `run_process`
- any other lower-level helper that is not itself exported as a public LangChain tool

The boundary rule is simple: public tools return `ToolMessage`; lower-level helpers can be migrated later.

## Error Handling

Errors should use LangChain's `ToolMessage.status="error"` where supported by the installed LangChain version.

The artifact remains the source of detailed machine-readable error data:

- `artifact["ok"]` is `False`
- `artifact["error"]["code"]` contains the stable error code
- `artifact["error"]["message"]` matches the human-readable message
- `artifact["data"]` contains relevant backend, policy, path, exit-code, or raw-response data
- `artifact["meta"]` contains non-primary metadata such as backend name and warnings

For invalid lower-level JSON:

- `content`: `Tool returned invalid response.`
- `status`: error
- `artifact.error.code`: `invalid_response`
- `artifact.data.raw`: original raw payload

For command failures:

- `content`: concise failure summary, such as `Command exited with code 1.`
- `artifact.data`: complete Hermes payload including stdout/stderr
- `artifact.error.code`: `command_failed` or `timeout`

For policy failures:

- `content`: policy human message
- `artifact.error.code`: `policy_denied` or `approval_required`
- `artifact.data`: policy decision data

## LangChain Compatibility

The implementation must verify the installed LangChain tool API before coding.

If public `@tool` functions can directly return `ToolMessage`, use direct `ToolMessage` returns.

If the installed API requires structured tool output through `response_format="content_and_artifact"`, then wrappers should return the official content/artifact tuple and let LangChain construct the `ToolMessage`.

The fallback must still preserve the design contract:

- model-visible content is concise
- full structured result is artifact
- public tool execution produces standard LangChain tool-result messages

The implementation should not fall back to JSON-string public output because that would preserve the problem this migration is meant to remove.

## Testing Strategy

Update tests that currently do:

```python
payload = json.loads(raw)
```

to assert the standard message contract:

```python
assert isinstance(result, ToolMessage)
assert result.status == "success"
assert result.artifact["ok"] is True
assert result.artifact["tool"] == "terminal"
```

Failure tests should assert:

```python
assert result.status == "error"
assert result.artifact["ok"] is False
assert result.artifact["error"]["code"] == "command_failed"
```

Add focused tests for the shared result builder:

- success artifact shape
- error artifact shape
- runtime tool call id propagation
- legacy JSON success conversion
- legacy JSON error conversion
- invalid legacy JSON conversion
- content does not include large data fields

Add public tool tests for each module:

- file tools return `ToolMessage` with data in artifact
- terminal/process keep stdout/stderr in artifact, not content
- cronjob returns action result in artifact
- memory, skills, skill manage, web, and delegation follow the same result contract

Add at least one agent-level regression test proving the final message list contains a standard `ToolMessage` with artifact data rather than a JSON string content dump.

## Migration Notes

This is a breaking contract change for direct tool callers.

Known caller changes:

- Replace `json.loads(result)` with `result.artifact`.
- Replace `payload["ok"]` with `result.status` or `result.artifact["ok"]`.
- Replace `payload["data"]` with `result.artifact["data"]`.
- Replace `payload["error"]["code"]` with `result.artifact["error"]["code"]`.

Where tests invoke implementation helpers directly, decide whether those helpers are public boundaries. Public helper functions should return `ToolMessage`; private lower-level helpers may keep dictionaries or strings if they are not exported as LangChain tools.

## Success Criteria

- Every public `@tool` in the audited scope returns LangChain standard tool-result output.
- `ToolMessage.content` is concise and does not contain full JSON payload dumps.
- Full tool results are available under `ToolMessage.artifact`.
- Stable error codes remain available in `artifact.error.code`.
- Public wrappers no longer repeat JSON decode and `tool_ok/tool_error` envelope construction.
- Tests no longer assume public tools return JSON strings.
- Large terminal, file search, file read, cron, and web payloads are not placed directly in model-visible content.

# ToolMessage Artifact Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate all public LangChain-facing tools from JSON-string results to standard `ToolMessage` results with concise `content` and full structured `artifact`.

**Architecture:** Add a shared result builder that owns the standard artifact envelope, `ToolMessage` construction, legacy JSON conversion, and short summary text. Then migrate every public `@tool` boundary to use that builder while allowing low-level toolkits to keep returning legacy JSON strings. Tests move from `json.loads(raw)` assertions to `ToolMessage.status`, `ToolMessage.content`, and `ToolMessage.artifact`.

**Tech Stack:** Python, LangChain `ToolMessage`, LangChain `@tool`, LangGraph `ToolNode`, pytest, existing agent tool wrappers.

---

## File Structure

- Create `agent_tools/shared/tool_result.py`: shared constructors for public `ToolMessage` results and legacy JSON conversion.
- Create `agent_tools/tool_result.py`: compatibility shim matching the existing `agent_tools/tool_output.py` pattern.
- Create `tests/test_tool_result.py`: focused tests for artifact envelope shape, status, runtime tool-call id propagation, summary behavior, and legacy conversion.
- Modify `agent_core/policy_tool_middleware.py`: policy short-circuit results should use `tool_failure` so middleware-produced errors also follow the artifact contract.
- Modify `tests/test_policy_tool_middleware.py`: assert policy errors through `ToolMessage.artifact` instead of JSON content.
- Modify `agent_tools/public/files.py`: return `ToolMessage` from all file public wrappers and private public-boundary implementations.
- Modify `tests/test_permissions_file_wrappers.py`, `tests/test_file_tools_runtime_task_id.py`, and file-related portions of `tests/test_file_tools_active_env.py`: migrate wrapper assertions to artifact access.
- Modify `agent_tools/public/terminal.py`: return `ToolMessage` from terminal and process wrappers.
- Modify `tests/test_terminal_tools.py`, `tests/test_permissions_terminal_wrappers.py`, `tests/test_permissions_process_wrappers.py`, and `tests/test_permissions_docker_network.py`: migrate terminal/process assertions.
- Modify `agent_tools/public/cronjob.py`, `agent_tools/public/memory.py`, `agent_tools/public/skills.py`, `agent_tools/public/skill_manage_impl.py`, `agent_tools/public/web.py`, and `agent_core/delegation.py`: migrate remaining public tools.
- Modify related tests for cron, memory, skills, web, and delegation if present; use `rg -n "json.loads\\(.*\\)|\\.content\\)" tests agent_core agent_tools` during the audit.
- Modify `tests/test_agent_tools_public_imports.py`: add public export assertions for `tool_result` helpers without changing lazy cron import behavior.

---

### Task 1: Add Shared ToolMessage Result Builder

**Files:**
- Create: `agent_tools/shared/tool_result.py`
- Create: `agent_tools/tool_result.py`
- Create: `tests/test_tool_result.py`
- Modify: `tests/test_agent_tools_public_imports.py`

- [ ] **Step 1: Write failing tests for the result builder**

Create `tests/test_tool_result.py`:

```python
import json
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from agent_tools.shared.tool_result import from_legacy_json, tool_failure, tool_success


def _runtime(thread_id: str = "tool-result-thread", tool_call_id: str = "call-result"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def test_tool_success_builds_standard_tool_message_artifact():
    result = tool_success(
        "terminal",
        message="Command completed.",
        data={"output": "large output\n", "exit_code": 0},
        meta={"backend": "test"},
        runtime=_runtime(),
    )

    assert isinstance(result, ToolMessage)
    assert result.content == "Command completed."
    assert result.name == "terminal"
    assert result.tool_call_id == "call-result"
    assert result.status == "success"
    assert result.artifact == {
        "ok": True,
        "tool": "terminal",
        "message": "Command completed.",
        "data": {"output": "large output\n", "exit_code": 0},
        "error": None,
        "meta": {"backend": "test"},
    }
    assert "large output" not in result.content


def test_tool_failure_builds_standard_error_artifact():
    result = tool_failure(
        "terminal",
        "Command exited with code 2.",
        code="command_failed",
        data={"output": "failed\n", "exit_code": 2},
        runtime=_runtime(tool_call_id="call-failed"),
    )

    assert result.content == "Command exited with code 2."
    assert result.name == "terminal"
    assert result.tool_call_id == "call-failed"
    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"] == {
        "code": "command_failed",
        "message": "Command exited with code 2.",
    }
    assert result.artifact["data"]["exit_code"] == 2
    assert "failed" not in result.content


def test_from_legacy_json_converts_success_and_extracts_meta_keys():
    raw = json.dumps(
        {
            "status": "success",
            "success": True,
            "message": "Read file.",
            "path": "README.md",
            "content": "full file contents",
            "_warning": "truncated",
            "_hint": "use offset",
            "truncated": True,
        }
    )

    result = from_legacy_json(
        "read_file",
        raw,
        success_message="File read.",
        meta_keys=("truncated",),
        runtime=_runtime(tool_call_id="call-read"),
        summary=lambda payload, meta: f"Read {payload.get('path', 'file')}.",
    )

    assert result.status == "success"
    assert result.content == "Read README.md."
    assert result.tool_call_id == "call-read"
    assert result.artifact["ok"] is True
    assert result.artifact["message"] == "Read file."
    assert result.artifact["data"] == {
        "path": "README.md",
        "content": "full file contents",
    }
    assert result.artifact["meta"] == {
        "warnings": ["truncated"],
        "hint": "use offset",
        "truncated": True,
    }
    assert "full file contents" not in result.content


def test_from_legacy_json_converts_error_payload():
    raw = json.dumps({"error": "missing file", "path": "missing.txt", "code": "not_found"})

    result = from_legacy_json("read_file", raw, success_message="File read.")

    assert result.status == "error"
    assert result.content == "missing file"
    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "tool_error"
    assert result.artifact["data"] == {"path": "missing.txt", "code": "not_found"}


def test_from_legacy_json_handles_invalid_json():
    result = from_legacy_json("read_file", "not-json", success_message="File read.")

    assert result.status == "error"
    assert result.content == "Tool returned invalid response."
    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "invalid_response"
    assert result.artifact["data"] == {"raw": "not-json"}
```

Modify `tests/test_agent_tools_public_imports.py` by extending `test_shared_policy_and_output_exports_existing_helpers`:

```python
    from agent_tools.shared.tool_result import tool_failure as public_tool_failure
    from agent_tools.shared.tool_result import tool_success as public_tool_success
    from agent_tools.tool_result import tool_failure, tool_success

    assert public_tool_success is tool_success
    assert public_tool_failure is tool_failure
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_tool_result.py tests/test_agent_tools_public_imports.py -q
```

Expected: FAIL because `agent_tools.shared.tool_result` and `agent_tools.tool_result` do not exist.

- [ ] **Step 3: Implement the shared result builder**

Create `agent_tools/shared/tool_result.py`:

```python
import json
from collections.abc import Callable
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage

from agent_core.session_context import RuntimeContext


SummaryBuilder = Callable[[dict[str, Any], dict[str, Any]], str]


def _tool_call_id(runtime: ToolRuntime | None) -> str:
    if runtime is None:
        return ""
    return RuntimeContext.from_runtime(runtime).tool_call_id or ""


def _content(message: str) -> str:
    return str(message or "").strip() or "Tool completed."


def _artifact(
    tool: str,
    *,
    ok: bool,
    message: str,
    data: Any = None,
    error: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "tool": tool,
        "message": message,
        "data": data,
        "error": error,
        "meta": meta or {},
    }


def tool_success(
    tool: str,
    *,
    message: str,
    data: Any = None,
    meta: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
    runtime: ToolRuntime | None = None,
    content: str | None = None,
) -> ToolMessage:
    payload = artifact or _artifact(tool, ok=True, message=message, data=data, error=None, meta=meta)
    return ToolMessage(
        content=_content(content or message),
        name=tool,
        tool_call_id=_tool_call_id(runtime),
        status="success",
        artifact=payload,
    )


def tool_failure(
    tool: str,
    message: str,
    *,
    code: str = "tool_error",
    data: Any = None,
    meta: dict[str, Any] | None = None,
    runtime: ToolRuntime | None = None,
    content: str | None = None,
) -> ToolMessage:
    payload = _artifact(
        tool,
        ok=False,
        message=message,
        data=data,
        error={"code": code, "message": message},
        meta=meta,
    )
    return ToolMessage(
        content=_content(content or message),
        name=tool,
        tool_call_id=_tool_call_id(runtime),
        status="error",
        artifact=payload,
    )


def _extract_meta(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}

    warning = payload.pop("_warning", None)
    if warning:
        meta["warnings"] = warning if isinstance(warning, list) else [warning]

    hint = payload.pop("_hint", None)
    if hint:
        meta["hint"] = hint

    for key in keys:
        if key in payload:
            meta[key] = payload.pop(key)

    return meta


def from_legacy_json(
    tool: str,
    raw: str,
    *,
    success_message: str,
    meta_keys: tuple[str, ...] = (),
    runtime: ToolRuntime | None = None,
    summary: SummaryBuilder | None = None,
) -> ToolMessage:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return tool_failure(
            tool,
            "Tool returned invalid response.",
            code="invalid_response",
            data={"raw": raw},
            runtime=runtime,
        )

    if not isinstance(payload, dict):
        return tool_failure(
            tool,
            f"Tool returned unexpected response type: {type(payload).__name__}.",
            code="invalid_response",
            data={"raw": raw},
            runtime=runtime,
        )

    message = str(payload.pop("message", success_message) or success_message)
    payload.pop("status", None)
    payload.pop("success", None)
    meta = _extract_meta(payload, *meta_keys)
    error_message = payload.pop("error", None)

    if error_message:
        return tool_failure(
            tool,
            str(error_message),
            code="tool_error",
            data=payload or None,
            meta=meta,
            runtime=runtime,
        )

    content = summary(payload, meta) if summary is not None else message
    return tool_success(
        tool,
        message=message,
        data=payload or None,
        meta=meta,
        runtime=runtime,
        content=content,
    )
```

Create `agent_tools/tool_result.py`:

```python
"""Compatibility shim. New code should import from agent_tools.shared.tool_result."""

import sys
from importlib import import_module

_impl = import_module("agent_tools.shared.tool_result")

sys.modules[__name__] = _impl
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_tool_result.py tests/test_agent_tools_public_imports.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/shared/tool_result.py agent_tools/tool_result.py tests/test_tool_result.py tests/test_agent_tools_public_imports.py
git commit -m "feat: add ToolMessage result builder"
```

---

### Task 2: Move Policy Middleware Short-Circuit Errors to Artifact Results

**Files:**
- Modify: `agent_core/policy_tool_middleware.py`
- Modify: `tests/test_policy_tool_middleware.py`

- [ ] **Step 1: Write failing policy middleware assertions**

In `tests/test_policy_tool_middleware.py`, remove JSON parsing for short-circuit errors and assert artifact data directly:

```python
def test_policy_tool_middleware_short_circuits_deny():
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    calls = []
    request = _request("terminal", {"command": "rm -rf /"}, tool_call_id="call-deny")

    result = middleware.wrap_tool_call(
        request,
        lambda received: calls.append(received),
    )

    assert result.status == "error"
    assert result.content
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "policy_denied"
    assert result.artifact["data"] is not None
    assert calls == []


def test_policy_tool_middleware_requires_approval_for_review():
    clear_approvals()
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    request = _request(
        "terminal",
        {"command": "touch approval-required.txt"},
        tool_call_id="call-review",
    )

    result = middleware.wrap_tool_call(
        request,
        lambda received: ToolMessage(content="unused", tool_call_id="x"),
    )

    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "approval_required"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_policy_tool_middleware.py::test_policy_tool_middleware_short_circuits_deny tests/test_policy_tool_middleware.py::test_policy_tool_middleware_requires_approval_for_review -q
```

Expected: FAIL because current middleware stores JSON in `content` and has no artifact.

- [ ] **Step 3: Update policy middleware to use `tool_failure`**

In `agent_core/policy_tool_middleware.py`, replace the `tool_error` import:

```python
from agent_tools.shared.tool_result import tool_failure
```

Update deny and approval-required branches:

```python
        if decision.outcome == "deny":
            return tool_failure(
                tool_name,
                decision.human_message,
                code="policy_denied",
                data=decision.data,
                runtime=request.runtime,
            )
```

```python
        if approval is None:
            return tool_failure(
                tool_name,
                decision.human_message,
                code="approval_required",
                data=decision.data,
                runtime=request.runtime,
            )
```

Delete the `_tool_message` static method if no code path uses it after the replacement.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_policy_tool_middleware.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/policy_tool_middleware.py tests/test_policy_tool_middleware.py
git commit -m "refactor: return artifact policy tool errors"
```

---

### Task 3: Migrate File Public Tools

**Files:**
- Modify: `agent_tools/public/files.py`
- Modify: `tests/test_permissions_file_wrappers.py`
- Modify: `tests/test_file_tools_runtime_task_id.py`
- Modify: `tests/test_file_tools_active_env.py`

- [ ] **Step 1: Add a local test helper for file result assertions**

At the top of each touched file test module, import `ToolMessage` and add:

```python
from langchain_core.messages import ToolMessage


def _artifact(result: ToolMessage) -> dict:
    assert isinstance(result, ToolMessage)
    assert result.content
    assert result.artifact is not None
    assert isinstance(result.artifact, dict)
    return result.artifact
```

Convert one representative test first, for example `tests/test_permissions_file_wrappers.py::test_workspace_write_does_not_need_approval`:

```python
    result = files._write_file_impl("notes.txt", "hello", runtime=_runtime())
    payload = _artifact(result)

    assert result.status == "success"
    assert payload["ok"] is True
    assert payload["data"]["path"] == "notes.txt"
    assert calls[0]["path"] == "notes.txt"
```

- [ ] **Step 2: Run the representative test to verify it fails**

Run:

```bash
pytest tests/test_permissions_file_wrappers.py::test_workspace_write_does_not_need_approval -q
```

Expected: FAIL because `_write_file_impl` returns `str`.

- [ ] **Step 3: Replace file wrapper result helpers**

In `agent_tools/public/files.py`, replace:

```python
from agent_tools.shared.tool_output import tool_error, tool_ok
```

with:

```python
from langchain_core.messages import ToolMessage

from agent_tools.shared.tool_result import from_legacy_json, tool_failure, tool_success
```

Delete `_extract_meta`; move `_wrap_file_tool_result` to the shared adapter:

```python
def _wrap_file_tool_result(
    tool_name: str,
    raw: str,
    *,
    success_message: str,
    meta_keys: tuple[str, ...] = (),
    runtime: ToolRuntime | None = None,
    summary=None,
) -> ToolMessage:
    return from_legacy_json(
        tool_name,
        raw,
        success_message=success_message,
        meta_keys=meta_keys,
        runtime=runtime,
        summary=summary,
    )
```

Add small summary builders:

```python
def _read_file_summary(payload: dict, meta: dict) -> str:
    path = payload.get("path") or "file"
    start = payload.get("start_line") or payload.get("offset")
    end = payload.get("end_line")
    if start and end:
        return f"Read {path} lines {start}-{end}."
    return f"Read {path}."


def _search_files_summary(payload: dict, meta: dict) -> str:
    total = payload.get("total") or payload.get("match_count") or payload.get("count")
    if total is not None:
        suffix = " Output was truncated." if meta.get("truncated") else ""
        return f"Search completed with {total} result(s).{suffix}"
    return "Search completed."
```

Change all public-boundary return annotations from `-> str` to `-> ToolMessage`, including `_list_directory_impl`, `list_directory`, `_read_file_impl`, `read_file`, `_write_file_impl`, `write_file`, `_patch_impl`, `patch`, `_search_files_impl`, `search_files`, `_file_info_impl` if present, and `file_info`.

Replace direct error returns:

```python
return tool_error("read_file", read_error, code=code)
```

with:

```python
return tool_failure("read_file", read_error, code=code, runtime=runtime)
```

Replace direct success returns:

```python
return tool_ok("file_info", data=info, message="Path inspected.")
```

with:

```python
return tool_success(
    "file_info",
    data=info,
    message="Path inspected.",
    runtime=runtime,
    content=f"Inspected {path}.",
)
```

Pass runtime through legacy conversions:

```python
    return _wrap_file_tool_result(
        "read_file",
        raw,
        success_message="File read.",
        meta_keys=("truncated",),
        runtime=runtime,
        summary=_read_file_summary,
    )
```

```python
    return _wrap_file_tool_result(
        "search_files",
        raw,
        success_message="Search completed.",
        meta_keys=("truncated",),
        runtime=runtime,
        summary=_search_files_summary,
    )
```

- [ ] **Step 4: Convert file tests module by module**

Use this mechanical replacement pattern in file tests:

```python
raw = files._patch_impl(...)
payload = json.loads(raw)
```

becomes:

```python
result = files._patch_impl(...)
payload = _artifact(result)
```

Use this assertion pattern:

```python
assert result.status == "error"
assert payload["ok"] is False
assert payload["error"]["code"] == "approval_required"
```

For ToolNode tests that currently do:

```python
payload = json.loads(result["messages"][-1].content)
```

change to:

```python
tool_message = result["messages"][-1]
assert isinstance(tool_message, ToolMessage)
payload = tool_message.artifact
assert tool_message.content
assert "{" not in tool_message.content[:1]
```

- [ ] **Step 5: Run file-focused tests**

Run:

```bash
pytest tests/test_permissions_file_wrappers.py tests/test_file_tools_runtime_task_id.py tests/test_file_tools_active_env.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/public/files.py tests/test_permissions_file_wrappers.py tests/test_file_tools_runtime_task_id.py tests/test_file_tools_active_env.py
git commit -m "refactor: return artifact results from file tools"
```

---

### Task 4: Migrate Terminal and Process Public Tools

**Files:**
- Modify: `agent_tools/public/terminal.py`
- Modify: `tests/test_terminal_tools.py`
- Modify: `tests/test_permissions_terminal_wrappers.py`
- Modify: `tests/test_permissions_process_wrappers.py`
- Modify: `tests/test_permissions_docker_network.py`

- [ ] **Step 1: Convert representative terminal tests first**

In `tests/test_terminal_tools.py`, add:

```python
from langchain_core.messages import ToolMessage


def _artifact(result: ToolMessage) -> dict:
    assert isinstance(result, ToolMessage)
    assert result.content
    assert result.artifact is not None
    return result.artifact
```

Change `test_terminal_foreground_nonzero_exit_is_tool_error` to:

```python
    result = terminal_tools._terminal_impl(
        command="false",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=None,
    )
    payload = _artifact(result)

    assert result.status == "error"
    assert result.content == "Command exited with code 2."
    assert payload["ok"] is False
    assert payload["error"]["code"] == "command_failed"
    assert payload["data"]["exit_code"] == 2
    assert "failed" not in result.content
```

- [ ] **Step 2: Run representative test to verify it fails**

Run:

```bash
pytest tests/test_terminal_tools.py::test_terminal_foreground_nonzero_exit_is_tool_error -q
```

Expected: FAIL because `_terminal_impl` returns `str`.

- [ ] **Step 3: Update terminal wrapper result construction**

In `agent_tools/public/terminal.py`, replace:

```python
from agent_tools.shared.tool_output import tool_error, tool_ok
```

with:

```python
from langchain_core.messages import ToolMessage

from agent_tools.shared.tool_result import tool_failure, tool_success
```

Change `_terminal_impl`, `terminal`, `_process_impl`, and `process` return annotations to `-> ToolMessage`.

Replace invalid JSON handling:

```python
def _decode_terminal_payload(raw: str) -> dict:
```

with:

```python
def _decode_terminal_payload(raw: str) -> tuple[dict, str | None]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}, "terminal returned invalid JSON."
    if not isinstance(payload, dict):
        return {"raw": raw}, f"terminal returned unexpected payload type: {type(payload).__name__}"
    return payload, None
```

Use the shared builder:

```python
    payload, decode_error = _decode_terminal_payload(raw)
    if decode_error:
        return tool_failure(
            "terminal",
            decode_error,
            code="invalid_response",
            data=payload,
            meta={"backend": "terminal_toolkit"},
            runtime=runtime,
            content="Tool returned invalid response.",
        )
```

For backend errors:

```python
    if payload.get("error"):
        return tool_failure(
            "terminal",
            str(payload["error"]),
            code=_status_code_from_payload(payload),
            data=payload,
            meta={"backend": "terminal_toolkit"},
            runtime=runtime,
        )
```

For nonzero exit:

```python
    if not background and exit_code not in (None, 0):
        code = "timeout" if exit_code == 124 else "command_failed"
        return tool_failure(
            "terminal",
            f"Command exited with code {exit_code}.",
            code=code,
            data=payload,
            meta={"backend": "terminal_toolkit"},
            runtime=runtime,
        )
```

For success:

```python
    message = "Terminal command completed." if not background else "Background process started."
    content = (
        f"Background process started: {payload.get('session_id')}."
        if background and payload.get("session_id")
        else f"Command completed with exit code {exit_code}."
        if not background and exit_code is not None
        else message
    )
    return tool_success(
        "terminal",
        data=payload,
        message=message,
        meta={"backend": "terminal_toolkit"},
        runtime=runtime,
        content=content,
    )
```

Apply the same pattern to `_process_impl`, with tool name `"process"` and success content:

```python
content = f"Process action completed: {action}."
```

- [ ] **Step 4: Convert terminal and process tests**

Use this replacement:

```python
raw = terminal_tools._terminal_impl(...)
payload = json.loads(raw)
```

becomes:

```python
result = terminal_tools._terminal_impl(...)
payload = _artifact(result)
```

For ToolNode tests:

```python
tool_message = result["messages"][-1]
assert isinstance(tool_message, ToolMessage)
payload = tool_message.artifact
assert tool_message.content
assert payload["ok"] is True
```

For all error tests:

```python
assert result.status == "error"
assert payload["ok"] is False
assert payload["error"]["code"] == "expected_code"
```

- [ ] **Step 5: Run terminal/process tests**

Run:

```bash
pytest tests/test_terminal_tools.py tests/test_permissions_terminal_wrappers.py tests/test_permissions_process_wrappers.py tests/test_permissions_docker_network.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/public/terminal.py tests/test_terminal_tools.py tests/test_permissions_terminal_wrappers.py tests/test_permissions_process_wrappers.py tests/test_permissions_docker_network.py
git commit -m "refactor: return artifact results from terminal tools"
```

---

### Task 5: Migrate Remaining Public Tools

**Files:**
- Modify: `agent_tools/public/cronjob.py`
- Modify: `agent_tools/public/memory.py`
- Modify: `agent_tools/public/skills.py`
- Modify: `agent_tools/public/skill_manage_impl.py`
- Modify: `agent_tools/public/web.py`
- Modify: `agent_core/delegation.py`
- Modify: matching tests found by `rg -n "cronjob|memory_manage|skills_list|skill_view|skill_manage|web_search|web_fetch|task\\(" tests`

- [ ] **Step 1: Add or update remaining public tool tests**

Create focused tests if no specific module tests exist. Use `tests/test_public_toolmessage_results.py`:

```python
from langchain_core.messages import ToolMessage


def _assert_tool_result(result: ToolMessage, tool: str, ok: bool):
    assert isinstance(result, ToolMessage)
    assert result.content
    assert result.artifact["ok"] is ok
    assert result.artifact["tool"] == tool
    assert result.status == ("success" if ok else "error")


def test_web_search_error_returns_tool_message(monkeypatch):
    import agent_tools.public.web as web

    monkeypatch.setattr(web, "_get_client", lambda: (_ for _ in ()).throw(RuntimeError("missing key")))

    result = web.web_search.func("langchain", limit=3)

    _assert_tool_result(result, "web_search", False)
    assert result.artifact["error"]["code"] == "tool_error"
    assert "missing key" in result.content


def test_skills_list_returns_tool_message():
    from agent_tools.public.skills import skills_list

    result = skills_list.func()

    _assert_tool_result(result, "skills_list", True)
    assert "skills" in result.artifact["data"]
    assert "categories" in result.artifact["data"]


def test_task_empty_summary_returns_tool_message(monkeypatch):
    import agent_core.delegation as delegation

    class FakeAgent:
        def invoke(self, *args, **kwargs):
            return {"messages": []}

    monkeypatch.setattr(delegation, "build_task_subagent", lambda: FakeAgent())

    result = delegation.task.func("inspect repo", "empty")

    _assert_tool_result(result, "task", True)
    assert result.artifact["data"]["description"] == "empty"
```

- [ ] **Step 2: Run remaining public tool tests to verify failures**

Run:

```bash
pytest tests/test_public_toolmessage_results.py -q
```

Expected: FAIL because these tools still return strings.

- [ ] **Step 3: Migrate web tools**

In `agent_tools/public/web.py`, replace `tool_error/tool_ok` imports with:

```python
from langchain_core.messages import ToolMessage

from agent_tools.shared.tool_result import tool_failure, tool_success
```

Change return annotations:

```python
def web_search(query: str, limit: int = 5) -> ToolMessage:
```

```python
def web_fetch(urls: list[str], max_chars_per_url: int = 4000) -> ToolMessage:
```

Use:

```python
    except Exception as exc:
        return tool_failure("web_search", f"web_search failed: {exc}")
```

```python
    return tool_success(
        "web_search",
        data={"query": query, "results": results, "total": len(results)},
        message="Search completed.",
        content=f"Search completed with {len(results)} result(s).",
    )
```

For fetch:

```python
    return tool_success(
        "web_fetch",
        data={"urls": urls, "results": results, "total": len(results)},
        message="Fetch completed.",
        content=f"Fetched {len(results)} URL(s).",
    )
```

- [ ] **Step 4: Migrate memory tool**

In `agent_tools/public/memory.py`, replace `tool_error/tool_ok` imports with:

```python
from langchain_core.messages import ToolMessage

from agent_tools.shared.tool_result import tool_failure, tool_success
```

Change:

```python
def memory_manage(action: str, target: str, content: str = "", old_text: str = "") -> ToolMessage:
```

Use:

```python
            return tool_success("memory_manage", data=result or None, message=message or "Memory updated.", meta=meta)
```

```python
        return tool_failure(
            "memory_manage",
            str(error_message or "Memory operation failed."),
            code="memory_error",
            data=result or None,
            meta=meta,
        )
```

```python
    except Exception as exc:
        return tool_failure("memory_manage", str(exc))
```

- [ ] **Step 5: Migrate skills and skill management tools**

In `agent_tools/public/skills.py` and `agent_tools/public/skill_manage_impl.py`, import:

```python
from langchain_core.messages import ToolMessage

from agent_tools.shared.tool_result import tool_failure, tool_success
```

Replace each `tool_ok(...)` with `tool_success(...)`, each `tool_error(...)` with `tool_failure(...)`, and change public return annotations to `ToolMessage`.

Use concise content for large skill bodies:

```python
        return tool_success(
            "skill_view",
            data={
                "name": meta["name"],
                "file": file_path,
                "content": target.read_text(encoding="utf-8")[:MAX_SKILL_CONTENT_CHARS],
            },
            message="Skill file loaded.",
            content=f"Loaded skill file {meta['name']}/{file_path}.",
        )
```

```python
    return tool_success(
        "skills_list",
        data={
            "skills": skills,
            "categories": sorted(categories),
            "count": len(skills),
        },
        message="Skills listed.",
        meta={"hint": "Use skill_view(name) to load full SKILL.md content."},
        content=f"Listed {len(skills)} skill(s).",
    )
```

- [ ] **Step 6: Migrate cronjob**

In `agent_tools/public/cronjob.py`, import `ToolMessage`, `tool_failure`, and `tool_success`.

Change the decorated function return annotation:

```python
) -> ToolMessage:
```

Replace final result conversion:

```python
    if result.get("success"):
        return tool_success(
            "cronjob",
            data=result,
            message=result.get("message", "Cron job action completed."),
            runtime=runtime,
            content=result.get("message", f"Cron job action completed: {action}."),
        )
    return tool_failure(
        "cronjob",
        result.get("error", "Cron job action failed."),
        code=result.get("code", "cron_error"),
        data=result,
        runtime=runtime,
    )
```

- [ ] **Step 7: Migrate delegation task tool**

In `agent_core/delegation.py`, import:

```python
from langchain_core.messages import ToolMessage

from agent_tools.shared.tool_result import tool_failure, tool_success
```

Change:

```python
def task(prompt: str, description: str = "subtask") -> ToolMessage:
```

Replace returns:

```python
        if not summary:
            logger.warning("task: delegated subagent task=%s returned no final text", description)
            return tool_success(
                "task",
                message="Subagent completed with no summary.",
                data={"description": description, "summary": "(no summary)"},
                content="Subagent completed with no summary.",
            )
        logger.info("task: completed delegated subagent task=%s", description)
        return tool_success(
            "task",
            message="Subagent task completed.",
            data={"description": description, "summary": summary},
            content=f"Subagent task completed: {description}.",
        )
```

```python
    except Exception as exc:
        logger.exception("task: delegated subagent task=%s failed: %s", description, exc)
        return tool_failure(
            "task",
            f"Subagent task failed ({description}): {exc}",
            code="subagent_failed",
            data={"description": description},
        )
```

- [ ] **Step 8: Run remaining public tool tests**

Run:

```bash
pytest tests/test_public_toolmessage_results.py tests/test_agent_tools_public_imports.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add agent_tools/public/cronjob.py agent_tools/public/memory.py agent_tools/public/skills.py agent_tools/public/skill_manage_impl.py agent_tools/public/web.py agent_core/delegation.py tests/test_public_toolmessage_results.py tests/test_agent_tools_public_imports.py
git commit -m "refactor: return artifact results from public tools"
```

---

### Task 6: Audit Callers, Remove Public JSON Result Assumptions, and Verify

**Files:**
- Modify: any remaining files found by the audit commands below.

- [ ] **Step 1: Search for remaining public JSON-result assumptions**

Run:

```bash
rg -n "json\\.loads\\((raw|result|tool_message\\.content|result\\[\"messages\"\\]\\[-1\\]\\.content)" tests agent_core agent_tools
```

Expected: Only low-level toolkit tests should remain. Public wrapper tests should not parse public tool result strings.

- [ ] **Step 2: Convert any remaining public wrapper assertions**

For each remaining public wrapper assertion, use this exact pattern:

```python
tool_message = result["messages"][-1]
assert isinstance(tool_message, ToolMessage)
payload = tool_message.artifact
assert payload["ok"] is True
```

For direct helper calls:

```python
result = module._impl_function(...)
assert isinstance(result, ToolMessage)
payload = result.artifact
assert payload["error"]["code"] == "expected_code"
```

- [ ] **Step 3: Search for public wrappers still importing old output helpers**

Run:

```bash
rg -n "from agent_tools\\.shared\\.tool_output|tool_ok\\(|tool_error\\(" agent_tools/public agent_core/delegation.py agent_core/policy_tool_middleware.py
```

Expected: no matches in public wrappers, delegation, or policy middleware. Matches in low-level toolkit modules are acceptable outside this command's target set.

- [ ] **Step 4: Search for public tools still annotated as strings**

Run:

```bash
rg -n "def .+\\) -> str:" agent_tools/public agent_core/delegation.py
```

Expected: no public `@tool` functions returning `str`.

- [ ] **Step 5: Run focused test suite**

Run:

```bash
pytest tests/test_tool_result.py tests/test_policy_tool_middleware.py tests/test_agent_tools_public_imports.py tests/test_public_toolmessage_results.py tests/test_permissions_file_wrappers.py tests/test_file_tools_runtime_task_id.py tests/test_terminal_tools.py tests/test_permissions_terminal_wrappers.py tests/test_permissions_process_wrappers.py -q
```

Expected: PASS.

- [ ] **Step 6: Run the full test suite**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 7: Commit audit fixes**

If Step 1 through Step 4 required edits, commit them:

```bash
git add tests agent_tools agent_core
git commit -m "test: remove public JSON result assumptions"
```

If there were no edits after the previous task commits, do not create an empty commit.

---

## Self-Review Notes

- Spec coverage: the plan adds a shared `ToolMessage` builder, preserves the existing artifact envelope, migrates all public tools in the approved full scope, keeps low-level toolkit JSON as an internal boundary, updates policy middleware short-circuit errors, and converts tests away from `json.loads(raw)` public result assumptions.
- Red-flag scan: no step relies on unspecified work; each code-changing step includes concrete imports, functions, or replacement patterns.
- Type consistency: public wrappers return `ToolMessage`; shared helpers accept optional `ToolRuntime`; artifacts retain `ok`, `tool`, `message`, `data`, `error`, and `meta`.

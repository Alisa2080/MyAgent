# ToolBus Phase 4-7 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add LangChain-native argument normalization, hook hardening, consecutive read-only governance, and migration tests for the existing ToolBus architecture.

**Architecture:** `agent_core/tool_arg_coercion.py` owns conservative schema-driven normalization and validation. `ToolBusMiddleware` delegates arg handling to that module, preserves hook/result semantics, and returns `invalid_input` failures before handler execution. `agent_core/tool_limits.py` gains a dedicated read-only loop middleware using `ToolSpec.read_only`, then `build_agent()` wires it before human-loop and ToolBus middleware.

**Tech Stack:** Python 3.11, LangChain `AgentMiddleware`, LangChain `ToolCallRequest`, LangChain Core `ToolMessage`, Pydantic v2-compatible schema APIs, LangGraph `Command`, pytest, project conda interpreter `/home/miku/miniforge3/envs/langchain/bin/python`.

---

## File Structure

- Create `agent_core/tool_arg_coercion.py`
  - Defines `ToolArgCoercionError`, `normalize_tool_args(...)`, JSON-schema extraction helpers, conservative coercion helpers, and Pydantic validation integration.
- Modify `agent_core/tool_bus_middleware.py`
  - Removes local coercion helpers, imports `normalize_tool_args`, catches `ToolArgCoercionError`, and returns `tool_failure(..., code="invalid_input")`.
- Modify `agent_core/tool_limits.py`
  - Adds `ConsecutiveReadOnlyToolLimitMiddleware` and default threshold constants while preserving `build_tool_call_limit_middleware(...)`.
- Modify `agent_core/builders.py`
  - Imports and wires `ConsecutiveReadOnlyToolLimitMiddleware(specs=tool_specs)` before human-loop middleware.
- Create `tests/test_tool_arg_coercion.py`
  - Unit-tests schema-driven coercion and validation independently from ToolBus.
- Create `tests/test_read_only_tool_limit_middleware.py`
  - Unit-tests consecutive read-only governance.
- Modify `tests/test_tool_bus_middleware.py`
  - Adjusts existing prepare-request exception behavior from `tool_exception` to `invalid_input`, verifies handler blocking on invalid input, and keeps existing hook/truncation coverage.
- Modify `tests/test_agent_cli_builders.py`
  - Locks middleware order and verifies both ToolBus and read-only middleware receive the same specs map.

## Task 1: Add Argument Coercion Tests

**Files:**
- Create: `tests/test_tool_arg_coercion.py`
- Read: `agent_core/tool_bus_middleware.py`
- Read: `agent_tools/shared/tool_result.py`

- [ ] **Step 1: Write failing tests for JSON-schema coercion**

Create `tests/test_tool_arg_coercion.py`:

```python
from __future__ import annotations

from types import SimpleNamespace


def test_normalize_tool_args_coerces_json_schema_string_values():
    from agent_core.tool_arg_coercion import normalize_tool_args

    tool = SimpleNamespace(
        name="fake_tool",
        args={
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "enabled": {"type": "boolean"},
            "items": {"type": "array"},
            "meta": {"type": "object"},
        },
    )
    original = {
        "count": "42",
        "ratio": "3.5",
        "enabled": "true",
        "items": "[1, 2]",
        "meta": "{\"a\": 1}",
    }

    normalized = normalize_tool_args(tool, original)

    assert normalized == {
        "count": 42,
        "ratio": 3.5,
        "enabled": True,
        "items": [1, 2],
        "meta": {"a": 1},
    }
    assert original["count"] == "42"


def test_normalize_tool_args_handles_anyof_and_nullable_values():
    from agent_core.tool_arg_coercion import normalize_tool_args

    tool = SimpleNamespace(
        name="terminal",
        args={
            "timeout": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "watch_patterns": {"anyOf": [{"type": "array"}, {"type": "null"}]},
            "optional": {"type": ["string", "null"]},
        },
    )

    assert normalize_tool_args(
        tool,
        {"timeout": "5", "watch_patterns": "[\"done\"]", "optional": "null"},
    ) == {"timeout": 5, "watch_patterns": ["done"], "optional": None}
```

- [ ] **Step 2: Write failing tests for conservative behavior**

Append to `tests/test_tool_arg_coercion.py`:

```python
def test_normalize_tool_args_keeps_unsafe_numeric_values_unchanged():
    from agent_core.tool_arg_coercion import normalize_tool_args

    tool = SimpleNamespace(
        name="fake_tool",
        args={
            "integer_decimal": {"type": "integer"},
            "not_number": {"type": "number"},
            "not_boolean": {"type": "boolean"},
        },
    )

    assert normalize_tool_args(
        tool,
        {
            "integer_decimal": "3.5",
            "not_number": "NaN",
            "not_boolean": "yes",
        },
    ) == {
        "integer_decimal": "3.5",
        "not_number": "NaN",
        "not_boolean": "yes",
    }


def test_normalize_tool_args_wraps_scalar_for_array_targets():
    from agent_core.tool_arg_coercion import normalize_tool_args

    tool = SimpleNamespace(name="fake_tool", args={"patterns": {"type": "array"}})

    assert normalize_tool_args(tool, {"patterns": "done"}) == {"patterns": ["done"]}
```

- [ ] **Step 3: Write failing tests for Pydantic validation**

Append to `tests/test_tool_arg_coercion.py`:

```python
def test_normalize_tool_args_uses_pydantic_schema_validation():
    from pydantic import BaseModel

    from agent_core.tool_arg_coercion import normalize_tool_args

    class Args(BaseModel):
        count: int
        enabled: bool
        tags: list[str]

    tool = SimpleNamespace(name="fake_tool", args_schema=Args)

    assert normalize_tool_args(tool, {"count": "7", "enabled": "false", "tags": "alpha"}) == {
        "count": 7,
        "enabled": False,
        "tags": ["alpha"],
    }


def test_normalize_tool_args_raises_controlled_error_for_invalid_input():
    import pytest
    from pydantic import BaseModel

    from agent_core.tool_arg_coercion import ToolArgCoercionError, normalize_tool_args

    class Args(BaseModel):
        count: int

    tool = SimpleNamespace(name="fake_tool", args_schema=Args)

    with pytest.raises(ToolArgCoercionError) as exc_info:
        normalize_tool_args(tool, {"count": "abc"})

    assert exc_info.value.tool_name == "fake_tool"
    assert "count" in str(exc_info.value)
```

- [ ] **Step 4: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_tool_arg_coercion.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.tool_arg_coercion'`.

- [ ] **Step 5: Commit failing tests**

```bash
git add tests/test_tool_arg_coercion.py
git commit -m "test: define tool argument coercion behavior"
```

## Task 2: Implement Argument Coercion Module

**Files:**
- Create: `agent_core/tool_arg_coercion.py`
- Test: `tests/test_tool_arg_coercion.py`

- [ ] **Step 1: Add the coercion module**

Create `agent_core/tool_arg_coercion.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolArgCoercionError(ValueError):
    tool_name: str
    field_errors: list[str]

    def __str__(self) -> str:
        joined = "; ".join(self.field_errors) if self.field_errors else "invalid arguments"
        return f"Invalid input for {self.tool_name}: {joined}"


def normalize_tool_args(tool: Any, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return args

    tool_name = str(getattr(tool, "name", getattr(tool, "__name__", "")) or "")
    schema = _tool_arg_schema(tool)
    if not schema:
        return dict(args)

    coerced = dict(args)
    for key, value in args.items():
        field_schema = schema.get(key)
        if not isinstance(field_schema, dict):
            continue
        coerced[key] = _coerce_value(value, field_schema)

    return _validate_with_pydantic(tool, coerced, tool_name)


def _tool_arg_schema(tool: Any) -> dict[str, Any]:
    args_schema = getattr(tool, "args_schema", None)
    model_json_schema = getattr(args_schema, "model_json_schema", None)
    if callable(model_json_schema):
        try:
            properties = (model_json_schema() or {}).get("properties")
            if isinstance(properties, dict):
                return properties
        except Exception:
            pass

    model_fields = getattr(args_schema, "model_fields", None)
    if isinstance(model_fields, dict):
        schema: dict[str, Any] = {}
        for name, field in model_fields.items():
            annotation = getattr(field, "annotation", None)
            schema[name] = _schema_from_annotation(annotation)
        return {key: value for key, value in schema.items() if value}

    args = getattr(tool, "args", None)
    if isinstance(args, dict):
        return args
    return {}


def _schema_from_annotation(annotation: Any) -> dict[str, Any]:
    origin = getattr(annotation, "__origin__", None)
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is list or origin is list:
        return {"type": "array"}
    if annotation is dict or origin is dict:
        return {"type": "object"}
    return {}


def _validate_with_pydantic(tool: Any, args: dict[str, Any], tool_name: str) -> dict[str, Any]:
    args_schema = getattr(tool, "args_schema", None)
    model_validate = getattr(args_schema, "model_validate", None)
    if not callable(model_validate):
        return args
    try:
        model = model_validate(args)
    except Exception as exc:
        errors = _format_validation_errors(exc)
        raise ToolArgCoercionError(tool_name=tool_name, field_errors=errors) from exc
    model_dump = getattr(model, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else args
    if isinstance(model, dict):
        return model
    return args


def _format_validation_errors(exc: Exception) -> list[str]:
    errors_method = getattr(exc, "errors", None)
    if not callable(errors_method):
        return [str(exc)]
    formatted = []
    for item in errors_method():
        loc = ".".join(str(part) for part in item.get("loc", ()))
        message = str(item.get("msg", "invalid value"))
        formatted.append(f"{loc}: {message}" if loc else message)
    return formatted or [str(exc)]


def _coerce_value(value: Any, schema: dict[str, Any]) -> Any:
    expected = _expected_type(schema)
    if _schema_allows_null(schema) and isinstance(value, str) and value.strip().lower() == "null":
        return None
    if expected == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            parsed = _coerce_json(value, list)
            if isinstance(parsed, list):
                return parsed
        return [value]
    if not isinstance(value, str):
        return value
    if expected == "integer":
        return _coerce_integer(value)
    if expected == "number":
        return _coerce_number(value)
    if expected == "boolean":
        return _coerce_boolean(value)
    if expected == "object":
        return _coerce_json(value, dict)
    return value


def _expected_type(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    value = schema.get("type")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        non_null = [str(item) for item in value if item != "null"]
        return non_null[0] if len(non_null) == 1 else None
    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if not isinstance(variants, list):
            continue
        expected_values = []
        for variant in variants:
            expected = _expected_type(variant)
            if expected is not None and expected != "null":
                expected_values.append(expected)
        unique = list(dict.fromkeys(expected_values))
        return unique[0] if len(unique) == 1 else None
    return None


def _schema_allows_null(schema: dict[str, Any]) -> bool:
    value = schema.get("type")
    if value == "null":
        return True
    if isinstance(value, list) and "null" in value:
        return True
    if schema.get("nullable") is True:
        return True
    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if isinstance(variants, list) and any(
            isinstance(variant, dict) and variant.get("type") == "null"
            for variant in variants
        ):
            return True
    return False


def _coerce_integer(value: str) -> Any:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return value
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return value
    if parsed == int(parsed):
        return int(parsed)
    return value


def _coerce_number(value: str) -> Any:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return value
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return value
    return int(parsed) if parsed == int(parsed) else parsed


def _coerce_boolean(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def _coerce_json(value: str, expected_python_type: type) -> Any:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    return parsed if isinstance(parsed, expected_python_type) else value
```

- [ ] **Step 2: Run argument coercion tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_tool_arg_coercion.py -q
```

Expected: PASS.

- [ ] **Step 3: Commit implementation**

```bash
git add agent_core/tool_arg_coercion.py tests/test_tool_arg_coercion.py
git commit -m "feat: add schema-driven tool argument coercion"
```

## Task 3: Wire Argument Coercion Into ToolBus

**Files:**
- Modify: `agent_core/tool_bus_middleware.py`
- Modify: `tests/test_tool_bus_middleware.py`
- Test: `tests/test_tool_bus_middleware.py`

- [ ] **Step 1: Update ToolBus invalid-input tests**

In `tests/test_tool_bus_middleware.py`, replace `test_prepare_request_exception_returns_tool_failure` with:

```python
def test_invalid_input_returns_tool_failure_without_calling_handler():
    from pydantic import BaseModel

    from agent_core.tool_bus_middleware import ToolBusMiddleware

    class Args(BaseModel):
        count: int

    request = _request("terminal", args={"count": "abc"})
    request.tool = SimpleNamespace(name="terminal", args_schema=Args)
    calls = []

    result = ToolBusMiddleware().wrap_tool_call(
        request,
        lambda req: calls.append(req) or _message(),
    )

    assert calls == []
    assert result.status == "error"
    assert result.tool_call_id == "call-toolbus"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "invalid_input"
    assert "count" in result.content
```

- [ ] **Step 2: Run the targeted ToolBus test and verify it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_tool_bus_middleware.py::test_invalid_input_returns_tool_failure_without_calling_handler -q
```

Expected: FAIL because `ToolBusMiddleware` still maps preparation failures to `tool_exception` or does not import `ToolArgCoercionError`.

- [ ] **Step 3: Modify ToolBus imports and preparation flow**

In `agent_core/tool_bus_middleware.py`, add imports:

```python
from agent_core.tool_arg_coercion import ToolArgCoercionError, normalize_tool_args
```

Update `wrap_tool_call` and `awrap_tool_call` to catch invalid input before the generic exception handler:

```python
        except ToolArgCoercionError as exc:
            logger.info("Tool %s received invalid input", _request_tool_name(request), exc_info=True)
            return _invalid_input_message(request, exc)
        except Exception as exc:
            logger.exception("Tool %s failed during ToolBusMiddleware request preparation", _request_tool_name(request))
            return _tool_exception_message(request, exc)
```

Apply the same shape in `awrap_tool_call`.

Update `_prepare_request()`:

```python
        if self.coerce_args:
            coerced_args = normalize_tool_args(request.tool, args)
            if coerced_args != args:
                tool_call = {**tool_call, "args": coerced_args}
                call_request = _override_request_tool_call(request, tool_call)
                args = coerced_args
```

Add helper:

```python
def _invalid_input_message(request: ToolCallRequest, exc: ToolArgCoercionError) -> ToolMessage:
    tool_name = _request_tool_name(request)
    tool_call_id = _request_tool_call_id(request)
    runtime = _runtime_with_tool_call_id(getattr(request, "runtime", None), tool_call_id)
    return tool_failure(
        tool_name,
        str(exc),
        code="invalid_input",
        runtime=runtime,
    )
```

- [ ] **Step 4: Remove local coercion helpers from ToolBus**

Delete these functions from `agent_core/tool_bus_middleware.py` after verifying imports are unused:

```python
def _coerce_tool_args(...)
def _tool_arg_schema(...)
def _expected_type(...)
def _coerce_value(...)
def _coerce_json(...)
```

Keep `_serialize_for_limit_check(...)`, `_truncate_artifact(...)`, `_request_tool_name(...)`, `_request_tool_call_id(...)`, `_tool_exception_message(...)`, `_override_request_tool_call(...)`, and runtime proxy helpers.

- [ ] **Step 5: Run ToolBus tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_tool_bus_middleware.py tests/test_tool_arg_coercion.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit ToolBus integration**

```bash
git add agent_core/tool_bus_middleware.py tests/test_tool_bus_middleware.py
git commit -m "feat: route toolbus argument validation through coercion module"
```

## Task 4: Add Consecutive Read-Only Middleware Tests

**Files:**
- Create: `tests/test_read_only_tool_limit_middleware.py`
- Read: `agent_core/tool_limits.py`
- Read: `agent_core/tool_catalog.py`

- [ ] **Step 1: Write failing read-only limit tests**

Create `tests/test_read_only_tool_limit_middleware.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import ToolMessage


def _request(tool_name: str, tool_call_id: str = "call-read-loop"):
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": {}, "id": tool_call_id},
        runtime=SimpleNamespace(tool_call_id=tool_call_id),
        tool=SimpleNamespace(name=tool_name),
        state={"consecutive_read_only_tool_count": 0},
    )


def _message(tool_name: str = "read_file"):
    return ToolMessage(
        content="ok",
        name=tool_name,
        tool_call_id="call-read-loop",
        status="success",
        artifact={"ok": True, "tool": tool_name, "message": "ok", "data": None, "error": None, "meta": {}},
    )


def _specs():
    from agent_core.tool_catalog import ToolSpec

    return {
        "read_file": ToolSpec(
            name="read_file",
            toolset="file_read",
            tool=SimpleNamespace(name="read_file"),
            read_only=True,
        ),
        "write_file": ToolSpec(
            name="write_file",
            toolset="file_write",
            tool=SimpleNamespace(name="write_file"),
            read_only=False,
        ),
    }


def test_consecutive_read_only_calls_are_blocked_after_threshold():
    from agent_core.tool_limits import ConsecutiveReadOnlyToolLimitMiddleware

    middleware = ConsecutiveReadOnlyToolLimitMiddleware(specs=_specs(), max_consecutive_read_only=2)
    calls = []

    def handler(req):
        calls.append(req.tool_call["name"])
        return _message(req.tool_call["name"])

    state = {"consecutive_read_only_tool_count": 0}
    first_request = _request("read_file")
    second_request = _request("read_file")
    third_request = _request("read_file")
    first_request.state = state
    second_request.state = state
    third_request.state = state

    first = middleware.wrap_tool_call(first_request, handler)
    second = middleware.wrap_tool_call(second_request, handler)
    third = middleware.wrap_tool_call(third_request, handler)

    assert first.status == "success"
    assert second.status == "success"
    assert third.status == "error"
    assert third.artifact["error"]["code"] == "read_loop_limit"
    assert calls == ["read_file", "read_file"]
```

- [ ] **Step 2: Write reset and unknown-tool tests**

Append to `tests/test_read_only_tool_limit_middleware.py`:

```python
def test_non_read_only_tool_resets_consecutive_read_only_counter():
    from agent_core.tool_limits import ConsecutiveReadOnlyToolLimitMiddleware

    middleware = ConsecutiveReadOnlyToolLimitMiddleware(specs=_specs(), max_consecutive_read_only=2)

    def handler(req):
        return _message(req.tool_call["name"])

    state = {"consecutive_read_only_tool_count": 0}
    requests = [
        _request("read_file"),
        _request("read_file"),
        _request("write_file"),
        _request("read_file"),
        _request("read_file"),
    ]
    for request in requests:
        request.state = state

    assert middleware.wrap_tool_call(requests[0], handler).status == "success"
    assert middleware.wrap_tool_call(requests[1], handler).status == "success"
    assert middleware.wrap_tool_call(requests[2], handler).status == "success"
    assert middleware.wrap_tool_call(requests[3], handler).status == "success"
    assert middleware.wrap_tool_call(requests[4], handler).status == "success"


def test_unknown_tool_defaults_to_non_read_only_and_resets_counter():
    from agent_core.tool_limits import ConsecutiveReadOnlyToolLimitMiddleware

    middleware = ConsecutiveReadOnlyToolLimitMiddleware(specs=_specs(), max_consecutive_read_only=2)

    def handler(req):
        return _message(req.tool_call["name"])

    state = {"consecutive_read_only_tool_count": 0}
    requests = [
        _request("read_file"),
        _request("read_file"),
        _request("unknown_tool"),
        _request("read_file"),
    ]
    for request in requests:
        request.state = state

    assert middleware.wrap_tool_call(requests[0], handler).status == "success"
    assert middleware.wrap_tool_call(requests[1], handler).status == "success"
    assert middleware.wrap_tool_call(requests[2], handler).status == "success"
    assert middleware.wrap_tool_call(requests[3], handler).status == "success"
```

- [ ] **Step 3: Add async parity test**

Append to `tests/test_read_only_tool_limit_middleware.py`:

```python
def test_async_consecutive_read_only_calls_are_blocked_after_threshold():
    import asyncio

    from agent_core.tool_limits import ConsecutiveReadOnlyToolLimitMiddleware

    async def run():
        middleware = ConsecutiveReadOnlyToolLimitMiddleware(specs=_specs(), max_consecutive_read_only=1)
        state = {"consecutive_read_only_tool_count": 0}
        first_request = _request("read_file")
        second_request = _request("read_file")
        first_request.state = state
        second_request.state = state

        async def handler(req):
            return _message(req.tool_call["name"])

        first = await middleware.awrap_tool_call(first_request, handler)
        second = await middleware.awrap_tool_call(second_request, handler)
        return first, second

    first, second = asyncio.run(run())

    assert first.status == "success"
    assert second.status == "error"
    assert second.artifact["error"]["code"] == "read_loop_limit"
```

- [ ] **Step 4: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_read_only_tool_limit_middleware.py -q
```

Expected: FAIL with `ImportError` for `ConsecutiveReadOnlyToolLimitMiddleware`.

- [ ] **Step 5: Commit failing tests**

```bash
git add tests/test_read_only_tool_limit_middleware.py
git commit -m "test: define consecutive read-only tool limits"
```

## Task 5: Implement Consecutive Read-Only Middleware

**Files:**
- Modify: `agent_core/tool_limits.py`
- Test: `tests/test_read_only_tool_limit_middleware.py`

- [ ] **Step 1: Implement middleware in tool_limits.py**

Add imports to `agent_core/tool_limits.py`:

```python
from __future__ import annotations

from typing import Annotated
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.channels.untracked_value import UntrackedValue
from langchain.agents.middleware.types import AgentState, PrivateStateAttr

from agent_core.tool_catalog import ToolSpec
from agent_tools.shared.tool_result import tool_failure
```

Keep the existing `ToolCallLimitMiddleware` import.

Add constants:

```python
DEFAULT_CONSECUTIVE_READ_ONLY_LIMIT = 10
READ_LOOP_LIMIT_ERROR_CODE = "read_loop_limit"
READ_ONLY_COUNT_STATE_KEY = "consecutive_read_only_tool_count"
```

Add state schema:

```python
class ConsecutiveReadOnlyToolLimitState(AgentState):
    consecutive_read_only_tool_count: Annotated[int, UntrackedValue, PrivateStateAttr]
```

Add class:

```python
class ConsecutiveReadOnlyToolLimitMiddleware(AgentMiddleware):
    state_schema = ConsecutiveReadOnlyToolLimitState

    def __init__(
        self,
        *,
        specs: Mapping[str, ToolSpec],
        max_consecutive_read_only: int = DEFAULT_CONSECUTIVE_READ_ONLY_LIMIT,
        include_unknown_as_read_only: bool = False,
    ) -> None:
        super().__init__()
        self.specs = dict(specs)
        self.max_consecutive_read_only = int(max_consecutive_read_only)
        self.include_unknown_as_read_only = include_unknown_as_read_only

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        blocked = self._before_tool_call(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        blocked = self._before_tool_call(request)
        if blocked is not None:
            return blocked
        return await handler(request)

    def _before_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = _request_tool_name(request)
        state = getattr(request, "state", None)
        if not isinstance(state, dict):
            state = {}
        if self._is_read_only(tool_name):
            count = int(state.get(READ_ONLY_COUNT_STATE_KEY, 0) or 0) + 1
            state[READ_ONLY_COUNT_STATE_KEY] = count
            if count > self.max_consecutive_read_only:
                return _read_loop_failure(request, count, self.max_consecutive_read_only)
            return None
        state[READ_ONLY_COUNT_STATE_KEY] = 0
        return None

    def _is_read_only(self, tool_name: str) -> bool:
        spec = self.specs.get(tool_name)
        if spec is None:
            return self.include_unknown_as_read_only
        return bool(spec.read_only)
```

Add helpers:

```python
def _request_tool_name(request: ToolCallRequest) -> str:
    tool_call = getattr(request, "tool_call", None) or {}
    if isinstance(tool_call, dict) and tool_call.get("name"):
        return str(tool_call["name"])
    tool = getattr(request, "tool", None)
    return str(getattr(tool, "name", "") or "")


def _read_loop_failure(request: ToolCallRequest, count: int, limit: int) -> ToolMessage:
    tool_name = _request_tool_name(request)
    runtime = getattr(request, "runtime", None)
    return tool_failure(
        tool_name,
        (
            f"Blocked after {count} consecutive read-only tool calls. "
            f"The limit is {limit}. Stop repeating read-only tools, summarize the context already gathered, "
            "and switch strategy or take a non-read action."
        ),
        code=READ_LOOP_LIMIT_ERROR_CODE,
        runtime=runtime,
    )
```

- [ ] **Step 2: Run read-only middleware tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_read_only_tool_limit_middleware.py -q
```

Expected: PASS.

- [ ] **Step 3: Commit implementation**

```bash
git add agent_core/tool_limits.py tests/test_read_only_tool_limit_middleware.py
git commit -m "feat: add consecutive read-only tool limit middleware"
```

## Task 6: Wire Read-Only Middleware Into Agent Builder

**Files:**
- Modify: `agent_core/builders.py`
- Modify: `tests/test_agent_cli_builders.py`
- Test: `tests/test_agent_cli_builders.py`

- [ ] **Step 1: Inspect current builder test fakes**

Run:

```bash
sed -n '1,140p' tests/test_agent_cli_builders.py
```

Expected: output includes fake middleware monkeypatches for `ToolBusMiddleware` and `PolicyToolMiddleware`.

- [ ] **Step 2: Extend builder test fake for read-only middleware**

In `tests/test_agent_cli_builders.py`, add a fake class near existing fake middleware:

```python
    class FakeReadOnlyLimit:
        def __init__(self, *, specs):
            self.specs = specs
```

Patch it:

```python
    monkeypatch.setattr(builders, "ConsecutiveReadOnlyToolLimitMiddleware", FakeReadOnlyLimit)
```

Extend middleware assertions:

```python
    read_only_index = next(
        index for index, item in enumerate(captured["middleware"])
        if isinstance(item, FakeReadOnlyLimit)
    )
    toolbus_index = next(
        index for index, item in enumerate(captured["middleware"])
        if isinstance(item, FakeToolBus)
    )
    policy_index = next(
        index for index, item in enumerate(captured["middleware"])
        if isinstance(item, FakePolicy)
    )

    assert read_only_index < toolbus_index < policy_index
    assert captured["middleware"][read_only_index].specs is captured["middleware"][toolbus_index].specs
```

- [ ] **Step 3: Run builder test and verify it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_builders.py -q
```

Expected: FAIL because `builders` does not import or instantiate `ConsecutiveReadOnlyToolLimitMiddleware`.

- [ ] **Step 4: Wire middleware in builders.py**

In `agent_core/builders.py`, update import:

```python
from agent_core.tool_limits import (
    ConsecutiveReadOnlyToolLimitMiddleware,
    build_tool_call_limit_middleware,
)
```

Insert middleware after `*build_tool_call_limit_middleware(include_task=True)`:

```python
            ConsecutiveReadOnlyToolLimitMiddleware(specs=tool_specs),
```

Keep existing order:

```python
            FlexibleHumanInTheLoopMiddleware(...),
            ToolBusMiddleware(specs=tool_specs),
            PolicyToolMiddleware(policy_tools=POLICY_REVIEW_TOOLS),
```

- [ ] **Step 5: Run builder tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_builders.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit builder wiring**

```bash
git add agent_core/builders.py tests/test_agent_cli_builders.py
git commit -m "feat: wire read-only loop governance into agent builder"
```

## Task 7: Final Regression and Cleanup

**Files:**
- Read: `agent_core/tool_arg_coercion.py`
- Read: `agent_core/tool_bus_middleware.py`
- Read: `agent_core/tool_limits.py`
- Read: `agent_core/builders.py`
- Test: focused test suite below

- [ ] **Step 1: Run focused regression suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_tool_arg_coercion.py \
  tests/test_tool_bus_middleware.py \
  tests/test_read_only_tool_limit_middleware.py \
  tests/test_tool_catalog.py \
  tests/test_agent_cli_builders.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run import smoke test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python - <<'PY'
from agent_core.tool_arg_coercion import ToolArgCoercionError, normalize_tool_args
from agent_core.tool_bus_middleware import ToolBusMiddleware
from agent_core.tool_limits import ConsecutiveReadOnlyToolLimitMiddleware, build_tool_call_limit_middleware

print(ToolArgCoercionError.__name__)
print(normalize_tool_args.__name__)
print(ToolBusMiddleware.__name__)
print(ConsecutiveReadOnlyToolLimitMiddleware.__name__)
print(callable(build_tool_call_limit_middleware))
PY
```

Expected output contains:

```text
ToolArgCoercionError
normalize_tool_args
ToolBusMiddleware
ConsecutiveReadOnlyToolLimitMiddleware
True
```

- [ ] **Step 3: Check for stale local coercion helpers**

Run:

```bash
rg -n "def _coerce_tool_args|def _tool_arg_schema|def _coerce_value|def _expected_type" agent_core/tool_bus_middleware.py
```

Expected: no output.

- [ ] **Step 4: Check working tree**

Run:

```bash
git status --short
```

Expected: no uncommitted changes after prior task commits.

- [ ] **Step 5: Report final implementation status**

Summarize:

```text
Implemented ToolBus phase 4-7:
- Added schema-driven tool argument coercion and invalid_input failures.
- Preserved ToolBus hook, Command, truncation, and tool_call_id semantics.
- Added consecutive read-only tool governance from ToolSpec metadata.
- Wired middleware into build_agent and verified focused regression suite.
```

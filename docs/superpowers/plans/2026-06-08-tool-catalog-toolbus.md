# Tool Catalog and ToolBus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lightweight LangChain-native tool catalog and ToolBus middleware inspired by Hermes without replacing LangChain tool dispatch.

**Architecture:** `agent_core/tool_catalog.py` owns tool metadata, toolset filtering, and check-function caching. `agent_core/builders.py` consumes `build_tools(...)` instead of manually concatenating tools. `agent_core/tool_bus_middleware.py` wraps LangChain tool calls for argument coercion, pre/post hooks, result transforms, exception normalization, and result limiting while preserving `ToolMessage` and `Command` semantics.

**Tech Stack:** Python, LangChain `create_agent`, LangChain `AgentMiddleware`, LangGraph `Command`, pytest, existing test stubs in `tests/conftest.py`.

---

## File Structure

- Create `agent_core/tool_catalog.py`
  - Defines `ToolSpec`, default spec construction, toolset filtering, and TTL cached `check_fn` evaluation.
- Modify `agent_core/builders.py`
  - Replaces direct `BASE_TOOLS + memory_manage + task` concatenation with `build_tools(...)`.
  - Adds `ToolBusMiddleware` to the middleware list after it exists.
- Create `agent_core/tool_bus_middleware.py`
  - Defines hook request/result dataclasses, hook collection, argument coercion, result limiting, and sync/async middleware wrappers.
- Create `tests/test_tool_catalog.py`
  - Tests metadata, default parity, toolset filtering, `cronjob`, and check cache behavior.
- Modify `tests/test_agent_cli_builders.py` or `tests/test_terminal_lifecycle.py`
  - Extends existing builder assertions for `build_tools(...)` usage and middleware ordering.
- Create `tests/test_tool_bus_middleware.py`
  - Tests pre hook blocking, exception normalization, post/transform hooks, coercion, truncation, `Command`, and async parity.

## Task 1: Add Tool Catalog Tests

**Files:**
- Create: `tests/test_tool_catalog.py`
- Read: `agent_core/delegation.py`
- Read: `agent_tools/public/__init__.py`

- [ ] **Step 1: Write failing tests for default tool parity and cron opt-in**

Create `tests/test_tool_catalog.py` with:

```python
from __future__ import annotations

from types import SimpleNamespace


def _tool_names(tools):
    return [getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools]


def test_build_tools_default_matches_current_agent_tool_set():
    from agent_core.delegation import BASE_TOOLS, task
    from agent_core.tool_catalog import build_tools
    from agent_tools.public.memory import memory_manage

    expected = _tool_names([*BASE_TOOLS, memory_manage, task])

    assert _tool_names(build_tools()) == expected


def test_build_tools_includes_cron_only_when_requested():
    from agent_core.tool_catalog import build_tools

    without_cron = _tool_names(build_tools(include_cron_tools=False))
    with_cron = _tool_names(build_tools(include_cron_tools=True))

    assert "cronjob" not in without_cron
    assert "cronjob" in with_cron
    assert with_cron[:-1] == without_cron
```

- [ ] **Step 2: Write failing tests for toolset filtering and spec lookup**

Append to `tests/test_tool_catalog.py`:

```python
def test_build_tools_filters_by_toolset():
    from agent_core.tool_catalog import build_tools

    assert _tool_names(build_tools(enabled_toolsets=["file_read"])) == [
        "list_directory",
        "search_files",
        "read_file",
        "file_info",
    ]
    assert _tool_names(build_tools(enabled_toolsets=["terminal"])) == [
        "terminal",
        "process",
    ]


def test_get_tool_spec_returns_metadata_by_name():
    from agent_core.tool_catalog import get_tool_spec

    spec = get_tool_spec("terminal")

    assert spec is not None
    assert spec.name == "terminal"
    assert spec.toolset == "terminal"
    assert spec.read_only is False
    assert spec.risk_level == "high"
    assert getattr(spec.tool, "name", "") == "terminal"
```

- [ ] **Step 3: Write failing tests for check_fn TTL caching**

Append to `tests/test_tool_catalog.py`:

```python
def test_build_tools_hides_tools_when_check_fn_fails():
    from agent_core.tool_catalog import ToolSpec, build_tools_from_specs

    fake_tool = SimpleNamespace(name="fake_tool")
    specs = [
        ToolSpec(
            name="fake_tool",
            toolset="fake",
            tool=fake_tool,
            check_fn=lambda: False,
        )
    ]

    assert build_tools_from_specs(specs, enabled_toolsets=["fake"]) == []


def test_check_fn_result_is_cached_until_cleared():
    from agent_core.tool_catalog import (
        ToolSpec,
        build_tools_from_specs,
        clear_tool_catalog_cache,
    )

    clear_tool_catalog_cache()
    calls = []

    def check():
        calls.append("called")
        return True

    fake_tool = SimpleNamespace(name="cached_tool")
    specs = [
        ToolSpec(
            name="cached_tool",
            toolset="fake",
            tool=fake_tool,
            check_fn=check,
        )
    ]

    assert build_tools_from_specs(specs, enabled_toolsets=["fake"]) == [fake_tool]
    assert build_tools_from_specs(specs, enabled_toolsets=["fake"]) == [fake_tool]
    assert calls == ["called"]

    clear_tool_catalog_cache()
    assert build_tools_from_specs(specs, enabled_toolsets=["fake"]) == [fake_tool]
    assert calls == ["called", "called"]
```

- [ ] **Step 4: Run tests and verify they fail**

Run:

```bash
pytest tests/test_tool_catalog.py -q
```

Expected: FAIL with `ModuleNotFoundError` or `ImportError` for `agent_core.tool_catalog`.

- [ ] **Step 5: Commit failing tests**

```bash
git add tests/test_tool_catalog.py
git commit -m "test: define tool catalog behavior"
```

## Task 2: Implement Tool Catalog

**Files:**
- Create: `agent_core/tool_catalog.py`
- Test: `tests/test_tool_catalog.py`

- [ ] **Step 1: Create the minimal catalog implementation**

Create `agent_core/tool_catalog.py`:

```python
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from agent_core.delegation import BASE_TOOLS, READ_ONLY_TOOLS, task
from agent_tools.public.memory import memory_manage


RiskLevel = Literal["low", "medium", "high"]
CHECK_FN_TTL_SECONDS = 30.0

_check_cache: dict[tuple[str, str | None], tuple[float, bool]] = {}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    toolset: str
    tool: Any
    check_fn: Callable[[], bool] | None = None
    max_result_size_chars: int | None = None
    read_only: bool = False
    risk_level: RiskLevel = "low"
    emoji: str = ""
    enabled_by_default: bool = True


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def clear_tool_catalog_cache() -> None:
    _check_cache.clear()


def _passes_check(spec: ToolSpec, *, runtime_profile: str | None = None) -> bool:
    if spec.check_fn is None:
        return True
    key = (spec.name, runtime_profile)
    now = time.monotonic()
    cached = _check_cache.get(key)
    if cached is not None:
        checked_at, value = cached
        if now - checked_at < CHECK_FN_TTL_SECONDS:
            return value
    try:
        value = bool(spec.check_fn())
    except Exception:
        value = False
    _check_cache[key] = (now, value)
    return value


def _spec(
    tool: Any,
    *,
    toolset: str,
    read_only: bool,
    risk_level: RiskLevel = "low",
    emoji: str = "",
    max_result_size_chars: int | None = None,
    enabled_by_default: bool = True,
    check_fn: Callable[[], bool] | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=_tool_name(tool),
        toolset=toolset,
        tool=tool,
        check_fn=check_fn,
        max_result_size_chars=max_result_size_chars,
        read_only=read_only,
        risk_level=risk_level,
        emoji=emoji,
        enabled_by_default=enabled_by_default,
    )


def default_tool_specs(*, include_cron_tools: bool = False) -> list[ToolSpec]:
    from agent_tools.public.files import (
        file_info,
        list_directory,
        patch,
        read_file,
        search_files,
        write_file,
    )
    from agent_tools.public.skills import skill_manage, skill_view, skills_list
    from agent_tools.public.terminal import process, terminal
    from agent_tools.public.web import web_extract, web_search
    from agent_tools.public.clarify import clarify

    specs = [
        _spec(list_directory, toolset="file_read", read_only=True, emoji="📁"),
        _spec(search_files, toolset="file_read", read_only=True, emoji="🔎"),
        _spec(read_file, toolset="file_read", read_only=True, emoji="📄"),
        _spec(file_info, toolset="file_read", read_only=True, emoji="ℹ️"),
        _spec(web_search, toolset="web", read_only=True, emoji="🌐"),
        _spec(web_extract, toolset="web", read_only=True, emoji="🌐"),
        _spec(skills_list, toolset="skills", read_only=True, emoji="🧰"),
        _spec(skill_view, toolset="skills", read_only=True, emoji="🧰"),
        _spec(write_file, toolset="file_write", read_only=False, risk_level="medium", emoji="✍️"),
        _spec(patch, toolset="file_write", read_only=False, risk_level="medium", emoji="🩹"),
        _spec(terminal, toolset="terminal", read_only=False, risk_level="high", emoji="💻", max_result_size_chars=100_000),
        _spec(process, toolset="terminal", read_only=False, risk_level="high", emoji="⚙️", max_result_size_chars=100_000),
        _spec(skill_manage, toolset="skills", read_only=False, risk_level="medium", emoji="🧰"),
        _spec(clarify, toolset="clarify", read_only=True, emoji="?"),
        _spec(memory_manage, toolset="memory", read_only=False, risk_level="medium", emoji="🧠"),
        _spec(task, toolset="delegation", read_only=True, emoji="🧭"),
    ]
    if include_cron_tools:
        from agent_tools.public.cronjob import cronjob

        specs.append(_spec(cronjob, toolset="cron", read_only=False, risk_level="medium", emoji="⏱️"))
    return specs


def get_tool_specs(*, include_cron_tools: bool = False) -> list[ToolSpec]:
    return default_tool_specs(include_cron_tools=include_cron_tools)


def get_tool_spec(name: str, *, include_cron_tools: bool = True) -> ToolSpec | None:
    for spec in get_tool_specs(include_cron_tools=include_cron_tools):
        if spec.name == name:
            return spec
    return None


def build_tools_from_specs(
    specs: Iterable[ToolSpec],
    enabled_toolsets: list[str] | None = None,
    *,
    runtime_profile: str | None = None,
) -> list[Any]:
    enabled = set(enabled_toolsets) if enabled_toolsets is not None else None
    tools = []
    for spec in specs:
        if enabled is None and not spec.enabled_by_default:
            continue
        if enabled is not None and spec.toolset not in enabled:
            continue
        if not _passes_check(spec, runtime_profile=runtime_profile):
            continue
        tools.append(spec.tool)
    return tools


def build_tools(
    enabled_toolsets: list[str] | None = None,
    *,
    include_cron_tools: bool = False,
    runtime_profile: str | None = None,
) -> list[Any]:
    return build_tools_from_specs(
        default_tool_specs(include_cron_tools=include_cron_tools),
        enabled_toolsets,
        runtime_profile=runtime_profile,
    )
```

- [ ] **Step 2: Run catalog tests**

Run:

```bash
pytest tests/test_tool_catalog.py -q
```

Expected: PASS.

- [ ] **Step 3: Run import health tests**

Run:

```bash
pytest tests/test_agent_tools_public_imports.py tests/test_agent_tools_structure.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit catalog implementation**

```bash
git add agent_core/tool_catalog.py tests/test_tool_catalog.py
git commit -m "feat: add tool catalog metadata"
```

## Task 3: Switch build_agent to build_tools

**Files:**
- Modify: `agent_core/builders.py`
- Modify: existing builder test file that asserts builder construction, preferably `tests/test_agent_cli_builders.py` or `tests/test_terminal_lifecycle.py`
- Test: `tests/test_tool_catalog.py`

- [ ] **Step 1: Write failing builder test for tool list construction**

Add this test to the existing builder test module that already imports
`agent_core.builders` with stubs:

```python
def test_build_agent_uses_tool_catalog_build_tools(monkeypatch):
    import agent_core.builders as builders

    fake_tool = object()
    calls = []

    def fake_build_tools(**kwargs):
        calls.append(kwargs)
        return [fake_tool]

    monkeypatch.setattr(builders, "build_tools", fake_build_tools)
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda target: "")
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: None)
    monkeypatch.setattr(builders, "load_project_instruction_blocks", lambda workdir: [])

    agent_config = builders.build_agent(include_cron_tools=True, checkpointer="cp")

    assert calls == [
        {
            "enabled_toolsets": None,
            "include_cron_tools": True,
            "runtime_profile": None,
        }
    ]
    assert agent_config["tools"] == [fake_tool]
    assert agent_config["checkpointer"] == "cp"
```

- [ ] **Step 2: Run the new builder test and verify it fails**

Run the exact test path added in Step 1, for example:

```bash
pytest tests/test_agent_cli_builders.py::test_build_agent_uses_tool_catalog_build_tools -q
```

Expected: FAIL because `builders.build_tools` is not imported or not used.

- [ ] **Step 3: Modify `agent_core/builders.py`**

Update imports:

```python
import os

from agent_core.tool_catalog import build_tools
```

Remove the now-unused imports if they become unused:

```python
from agent_core.delegation import BASE_TOOLS, task
from agent_tools.public.memory import memory_manage
```

Replace tool construction inside `build_agent()`:

```python
    tools = build_tools(
        enabled_toolsets=None,
        include_cron_tools=include_cron_tools,
        runtime_profile=os.environ.get("AGENT_RUNTIME_PROFILE"),
    )
```

Do not keep the old manual append block:

```python
    tools = [*BASE_TOOLS, memory_manage, task]
    if include_cron_tools:
        from agent_tools.public.cronjob import cronjob

        tools.append(cronjob)
```

- [ ] **Step 4: Run builder and catalog tests**

Run:

```bash
pytest tests/test_tool_catalog.py tests/test_agent_cli_builders.py tests/test_terminal_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit builder migration**

```bash
git add agent_core/builders.py tests/test_agent_cli_builders.py tests/test_terminal_lifecycle.py
git commit -m "feat: build agent tools from catalog"
```

If only one builder test file changed, add only that file.

## Task 4: Add ToolBusMiddleware Tests

**Files:**
- Create: `tests/test_tool_bus_middleware.py`

- [ ] **Step 1: Write failing tests for pre hook, exception normalization, and post hook**

Create `tests/test_tool_bus_middleware.py`:

```python
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import ToolMessage


def _runtime(tool_call_id="call-toolbus"):
    return SimpleNamespace(tool_call_id=tool_call_id)


def _request(tool_name="fake_tool", args=None, tool_call_id="call-toolbus"):
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": args or {}, "id": tool_call_id},
        runtime=_runtime(tool_call_id),
        tool=SimpleNamespace(name=tool_name, args={}),
        state={},
    )


def _message(tool="fake_tool", content="ok", status="success"):
    return ToolMessage(
        content=content,
        name=tool,
        tool_call_id="call-toolbus",
        status=status,
        artifact={
            "ok": status == "success",
            "tool": tool,
            "message": content,
            "data": None,
            "error": None if status == "success" else {"code": "x", "message": content},
            "meta": {},
        },
    )


def test_pre_hook_blocks_handler():
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware

    request = _request()
    blocked = _message(content="blocked", status="error")
    calls = []

    bus = ToolBusMiddleware(
        hooks=ToolBusHooks(pre_tool_call=[lambda req: blocked]),
    )

    result = bus.wrap_tool_call(
        request,
        lambda received: calls.append(received) or _message(),
    )

    assert result is blocked
    assert calls == []


def test_handler_exception_returns_tool_failure():
    from agent_core.tool_bus_middleware import ToolBusMiddleware

    def handler(_request):
        raise RuntimeError("boom")

    result = ToolBusMiddleware().wrap_tool_call(_request("terminal"), handler)

    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "tool_exception"
    assert "boom" in result.content


def test_post_hook_receives_result_and_duration():
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware

    captured = []

    def post(req, res):
        captured.append((req.tool_name, req.args, res.result.content, res.duration_ms))

    bus = ToolBusMiddleware(hooks=ToolBusHooks(post_tool_call=[post]))
    result = bus.wrap_tool_call(_request(args={"a": 1}), lambda req: _message(content="done"))

    assert result.content == "done"
    assert captured
    assert captured[0][0] == "fake_tool"
    assert captured[0][1] == {"a": 1}
    assert captured[0][2] == "done"
    assert isinstance(captured[0][3], int)
```

- [ ] **Step 2: Write failing tests for transform, coercion, truncation, Command, and async**

Append:

```python
def test_transform_hook_first_non_none_result_wins():
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware

    transformed = _message(content="transformed")
    bus = ToolBusMiddleware(
        hooks=ToolBusHooks(
            transform_tool_result=[
                lambda req, result: None,
                lambda req, result: transformed,
                lambda req, result: _message(content="ignored"),
            ]
        )
    )

    assert bus.wrap_tool_call(_request(), lambda req: _message(content="original")) is transformed


def test_coerces_simple_string_args_before_handler():
    from agent_core.tool_bus_middleware import ToolBusMiddleware

    request = _request(
        args={
            "count": "42",
            "ratio": "3.5",
            "enabled": "true",
            "items": "[1, 2]",
            "meta": "{\"a\": 1}",
        }
    )
    request.tool = SimpleNamespace(
        name="fake_tool",
        args={
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "enabled": {"type": "boolean"},
            "items": {"type": "array"},
            "meta": {"type": "object"},
        },
    )
    captured = {}

    def handler(req):
        captured.update(req.tool_call["args"])
        return _message()

    ToolBusMiddleware().wrap_tool_call(request, handler)

    assert captured == {
        "count": 42,
        "ratio": 3.5,
        "enabled": True,
        "items": [1, 2],
        "meta": {"a": 1},
    }


def test_result_limit_truncates_content_only_when_spec_sets_limit():
    from agent_core.tool_bus_middleware import ToolBusMiddleware
    from agent_core.tool_catalog import ToolSpec

    spec = ToolSpec(
        name="fake_tool",
        toolset="fake",
        tool=SimpleNamespace(name="fake_tool"),
        max_result_size_chars=10,
    )
    bus = ToolBusMiddleware(specs={"fake_tool": spec})

    result = bus.wrap_tool_call(_request(), lambda req: _message(content="abcdefghijklmnopqrstuvwxyz"))

    assert len(result.content) < 80
    assert "truncated" in result.content.lower()
    assert result.artifact["ok"] is True


def test_command_result_is_not_transformed_or_truncated():
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware
    from agent_core.tool_catalog import ToolSpec
    from langgraph.types import Command

    command = Command(update={"messages": []})
    spec = ToolSpec(
        name="fake_tool",
        toolset="fake",
        tool=SimpleNamespace(name="fake_tool"),
        max_result_size_chars=1,
    )
    bus = ToolBusMiddleware(
        specs={"fake_tool": spec},
        hooks=ToolBusHooks(transform_tool_result=[lambda req, result: _message(content="changed")]),
    )

    assert bus.wrap_tool_call(_request(), lambda req: command) is command


def test_async_wrapper_matches_sync_behavior():
    from agent_core.tool_bus_middleware import ToolBusMiddleware

    async def run():
        bus = ToolBusMiddleware()

        async def handler(req):
            return _message(content="async done")

        return await bus.awrap_tool_call(_request(), handler)

    result = asyncio.run(run())

    assert result.content == "async done"
```

- [ ] **Step 3: Run ToolBus tests and verify they fail**

Run:

```bash
pytest tests/test_tool_bus_middleware.py -q
```

Expected: FAIL because `agent_core.tool_bus_middleware` does not exist.

- [ ] **Step 4: Commit failing tests**

```bash
git add tests/test_tool_bus_middleware.py
git commit -m "test: define tool bus middleware behavior"
```

## Task 5: Implement ToolBusMiddleware

**Files:**
- Create: `agent_core/tool_bus_middleware.py`
- Test: `tests/test_tool_bus_middleware.py`

- [ ] **Step 1: Create middleware implementation**

Create `agent_core/tool_bus_middleware.py`:

```python
from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_core.tool_catalog import ToolSpec
from agent_tools.shared.tool_result import tool_failure


logger = logging.getLogger(__name__)

ToolResponse = ToolMessage | Command[Any]
PreToolHook = Callable[["ToolBusRequest"], ToolMessage | None]
PostToolHook = Callable[["ToolBusRequest", "ToolBusResult"], None]
TransformToolResultHook = Callable[["ToolBusRequest", ToolMessage], ToolMessage | None]


@dataclass(frozen=True)
class ToolBusRequest:
    tool_name: str
    args: dict[str, Any]
    tool_call_id: str
    runtime: Any
    request: ToolCallRequest
    spec: ToolSpec | None = None


@dataclass(frozen=True)
class ToolBusResult:
    result: ToolResponse
    duration_ms: int
    error: Exception | None = None


@dataclass
class ToolBusHooks:
    pre_tool_call: list[PreToolHook] = field(default_factory=list)
    post_tool_call: list[PostToolHook] = field(default_factory=list)
    transform_tool_result: list[TransformToolResultHook] = field(default_factory=list)


class ToolBusMiddleware(AgentMiddleware):
    def __init__(
        self,
        *,
        hooks: ToolBusHooks | None = None,
        specs: Mapping[str, ToolSpec] | None = None,
        coerce_args: bool = True,
    ) -> None:
        super().__init__()
        self.hooks = hooks or ToolBusHooks()
        self.specs = dict(specs or {})
        self.coerce_args = coerce_args

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolResponse],
    ) -> ToolResponse:
        bus_request = self._prepare_request(request)
        blocked = self._run_pre_hooks(bus_request)
        if blocked is not None:
            return blocked

        started = time.monotonic()
        error = None
        try:
            result = handler(request)
        except Exception as exc:
            error = exc
            logger.exception("Tool %s failed in ToolBusMiddleware", bus_request.tool_name)
            result = tool_failure(
                bus_request.tool_name,
                f"Tool execution failed: {type(exc).__name__}: {exc}",
                code="tool_exception",
                runtime=request.runtime,
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        return self._finalize(bus_request, result, duration_ms, error)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolResponse]],
    ) -> ToolResponse:
        bus_request = self._prepare_request(request)
        blocked = self._run_pre_hooks(bus_request)
        if blocked is not None:
            return blocked

        started = time.monotonic()
        error = None
        try:
            result = await handler(request)
        except Exception as exc:
            error = exc
            logger.exception("Tool %s failed in ToolBusMiddleware", bus_request.tool_name)
            result = tool_failure(
                bus_request.tool_name,
                f"Tool execution failed: {type(exc).__name__}: {exc}",
                code="tool_exception",
                runtime=request.runtime,
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        return self._finalize(bus_request, result, duration_ms, error)

    def _prepare_request(self, request: ToolCallRequest) -> ToolBusRequest:
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name") or "")
        raw_args = tool_call.get("args") or {}
        args = raw_args if isinstance(raw_args, dict) else {}
        if self.coerce_args:
            args = _coerce_tool_args(request.tool, args)
            tool_call["args"] = args
        return ToolBusRequest(
            tool_name=tool_name,
            args=args,
            tool_call_id=str(tool_call.get("id") or ""),
            runtime=request.runtime,
            request=request,
            spec=self.specs.get(tool_name),
        )

    def _run_pre_hooks(self, bus_request: ToolBusRequest) -> ToolMessage | None:
        for hook in self.hooks.pre_tool_call:
            try:
                result = hook(bus_request)
            except Exception:
                logger.warning("ToolBus pre hook failed", exc_info=True)
                continue
            if isinstance(result, ToolMessage):
                return result
        return None

    def _finalize(
        self,
        bus_request: ToolBusRequest,
        result: ToolResponse,
        duration_ms: int,
        error: Exception | None,
    ) -> ToolResponse:
        bus_result = ToolBusResult(result=result, duration_ms=duration_ms, error=error)
        for hook in self.hooks.post_tool_call:
            try:
                hook(bus_request, bus_result)
            except Exception:
                logger.warning("ToolBus post hook failed", exc_info=True)

        if not isinstance(result, ToolMessage):
            return result

        transformed = self._run_transform_hooks(bus_request, result)
        limited = self._limit_result(bus_request.spec, transformed)
        return limited

    def _run_transform_hooks(self, bus_request: ToolBusRequest, result: ToolMessage) -> ToolMessage:
        for hook in self.hooks.transform_tool_result:
            try:
                transformed = hook(bus_request, result)
            except Exception:
                logger.warning("ToolBus transform hook failed", exc_info=True)
                continue
            if isinstance(transformed, ToolMessage):
                return transformed
        return result

    def _limit_result(self, spec: ToolSpec | None, result: ToolMessage) -> ToolMessage:
        if spec is None or spec.max_result_size_chars is None:
            return result
        limit = int(spec.max_result_size_chars)
        if limit <= 0:
            return result
        content = str(getattr(result, "content", "") or "")
        artifact = getattr(result, "artifact", None)
        if len(content) <= limit and len(str(artifact)) <= limit:
            return result

        new_artifact = copy.deepcopy(artifact)
        if isinstance(new_artifact, dict):
            data = new_artifact.get("data")
            if data is not None and len(str(data)) > limit:
                new_artifact["data"] = {
                    "truncated": True,
                    "original_chars": len(str(data)),
                    "preview": str(data)[:limit],
                }

        truncated_content = content
        if len(truncated_content) > limit:
            truncated_content = (
                truncated_content[:limit]
                + f"\n\n[truncated: original content was {len(content)} chars]"
            )
        elif len(str(artifact)) > limit:
            truncated_content = (
                truncated_content
                + f"\n\n[truncated: artifact exceeded {limit} chars]"
            )

        return ToolMessage(
            content=truncated_content,
            name=getattr(result, "name", spec.name),
            tool_call_id=getattr(result, "tool_call_id", ""),
            status=getattr(result, "status", "success"),
            artifact=new_artifact,
        )


def _coerce_tool_args(tool: Any, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return args
    schema = _tool_arg_schema(tool)
    if not schema:
        return args
    coerced = dict(args)
    for key, value in args.items():
        if not isinstance(value, str):
            continue
        expected = _expected_type(schema.get(key))
        if expected is None:
            continue
        coerced[key] = _coerce_value(value, expected)
    return coerced


def _tool_arg_schema(tool: Any) -> dict[str, Any]:
    args = getattr(tool, "args", None)
    if isinstance(args, dict):
        return args
    args_schema = getattr(tool, "args_schema", None)
    model_fields = getattr(args_schema, "model_fields", None)
    if isinstance(model_fields, dict):
        schema = {}
        for name, field in model_fields.items():
            annotation = getattr(field, "annotation", None)
            if annotation is int:
                schema[name] = {"type": "integer"}
            elif annotation is float:
                schema[name] = {"type": "number"}
            elif annotation is bool:
                schema[name] = {"type": "boolean"}
            elif annotation is list:
                schema[name] = {"type": "array"}
            elif annotation is dict:
                schema[name] = {"type": "object"}
        return schema
    return {}


def _expected_type(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    value = schema.get("type")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            if item != "null":
                return str(item)
    return None


def _coerce_value(value: str, expected_type: str) -> Any:
    if expected_type == "integer":
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return value
        if parsed == int(parsed):
            return int(parsed)
        return value
    if expected_type == "number":
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return value
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            return value
        return int(parsed) if parsed == int(parsed) else parsed
    if expected_type == "boolean":
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return value
    if expected_type == "array":
        return _coerce_json(value, list)
    if expected_type == "object":
        return _coerce_json(value, dict)
    return value


def _coerce_json(value: str, expected_python_type: type) -> Any:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    return parsed if isinstance(parsed, expected_python_type) else value
```

- [ ] **Step 2: Run ToolBus tests**

Run:

```bash
pytest tests/test_tool_bus_middleware.py -q
```

Expected: PASS.

- [ ] **Step 3: Run policy middleware tests to check coexistence**

Run:

```bash
pytest tests/test_policy_tool_middleware.py tests/test_public_toolmessage_results.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit middleware implementation**

```bash
git add agent_core/tool_bus_middleware.py tests/test_tool_bus_middleware.py
git commit -m "feat: add tool bus middleware"
```

## Task 6: Insert ToolBusMiddleware into build_agent

**Files:**
- Modify: `agent_core/builders.py`
- Modify: existing builder test file used in Task 3

- [ ] **Step 1: Write failing middleware-order test**

Add to the builder test module:

```python
def test_build_agent_includes_tool_bus_before_policy(monkeypatch):
    import agent_core.builders as builders

    class FakeToolBus:
        pass

    class FakePolicy:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(builders, "ToolBusMiddleware", FakeToolBus)
    monkeypatch.setattr(builders, "PolicyToolMiddleware", FakePolicy)
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda target: "")
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: None)
    monkeypatch.setattr(builders, "load_project_instruction_blocks", lambda workdir: [])

    agent_config = builders.build_agent()
    middleware = agent_config["middleware"]
    tool_bus_index = next(i for i, item in enumerate(middleware) if isinstance(item, FakeToolBus))
    policy_index = next(i for i, item in enumerate(middleware) if isinstance(item, FakePolicy))

    assert tool_bus_index < policy_index
```

- [ ] **Step 2: Run the new test and verify it fails**

Run the exact test path added in Step 1, for example:

```bash
pytest tests/test_agent_cli_builders.py::test_build_agent_includes_tool_bus_before_policy -q
```

Expected: FAIL because `builders.ToolBusMiddleware` is not imported or not used.

- [ ] **Step 3: Modify `agent_core/builders.py`**

Add import:

```python
from agent_core.tool_bus_middleware import ToolBusMiddleware
```

Insert `ToolBusMiddleware()` immediately before `PolicyToolMiddleware(...)`:

```python
            FlexibleHumanInTheLoopMiddleware(
                interrupt_on=HUMAN_INTERRUPT_ON,
                policy_tools=POLICY_REVIEW_TOOLS,
                description_prefix="Approval required before tool execution",
            ),
            ToolBusMiddleware(),
            PolicyToolMiddleware(policy_tools=POLICY_REVIEW_TOOLS),
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
pytest tests/test_agent_cli_builders.py tests/test_terminal_lifecycle.py tests/test_tool_bus_middleware.py tests/test_policy_tool_middleware.py -q
```

Expected: PASS.

- [ ] **Step 5: Run broader regression slice**

Run:

```bash
pytest tests/test_tool_catalog.py tests/test_public_toolmessage_results.py tests/test_permissions_langchain_tools.py tests/test_agent_runner.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit middleware integration**

```bash
git add agent_core/builders.py tests/test_agent_cli_builders.py tests/test_terminal_lifecycle.py
git commit -m "feat: wire tool bus into agent builder"
```

If only one builder test file changed, add only that file.

## Task 7: Final Verification

**Files:**
- Review: `agent_core/tool_catalog.py`
- Review: `agent_core/tool_bus_middleware.py`
- Review: `agent_core/builders.py`
- Review: related tests

- [ ] **Step 1: Run all focused tests**

Run:

```bash
pytest tests/test_tool_catalog.py tests/test_tool_bus_middleware.py tests/test_policy_tool_middleware.py tests/test_public_toolmessage_results.py tests/test_agent_cli_builders.py tests/test_terminal_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 2: Run import and structure tests**

Run:

```bash
pytest tests/test_agent_tools_public_imports.py tests/test_agent_tools_structure.py tests/test_agent_cli_main.py -q
```

Expected: PASS.

- [ ] **Step 3: Inspect changed files**

Run:

```bash
git diff --stat HEAD~6..HEAD
git diff HEAD~6..HEAD -- agent_core/tool_catalog.py agent_core/tool_bus_middleware.py agent_core/builders.py
```

Expected: diff only covers catalog, ToolBus, builder wiring, and tests.

- [ ] **Step 4: Confirm no unrelated files are staged**

Run:

```bash
git status --short
```

Expected: only pre-existing unrelated workspace changes remain, or a clean tree if those were absent before implementation.

- [ ] **Step 5: Commit any final test/doc adjustments**

If final verification required small test or documentation fixes, commit them:

```bash
git add <changed-files>
git commit -m "test: verify tool catalog and tool bus integration"
```

If there are no final changes, skip this step.

## Self-Review

- Spec coverage:
  - Stage 1 `ToolSpec` metadata is covered by Tasks 1-2.
  - Stage 2 `build_tools(...)` and `build_agent()` migration are covered by Task 3.
  - Stage 3 `ToolBusMiddleware` hooks, coercion, exceptions, transforms, truncation, and `Command` behavior are covered by Tasks 4-6.
  - Testing and rollout requirements are covered by Tasks 1-7.
- Placeholder scan:
  - The plan contains no unresolved markers.
  - Each code-changing task includes concrete code snippets and exact commands.
- Type consistency:
  - `ToolSpec`, `ToolBusHooks`, `ToolBusRequest`, `ToolBusResult`, `ToolBusMiddleware`, `build_tools`, `build_tools_from_specs`, and `clear_tool_catalog_cache` names are consistent across tasks.

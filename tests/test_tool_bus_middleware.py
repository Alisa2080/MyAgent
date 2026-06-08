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


def test_prepare_request_exception_returns_tool_failure():
    from agent_core.tool_bus_middleware import ToolBusMiddleware

    request = _request("terminal", args={"count": "nan"})
    request.tool = SimpleNamespace(name="terminal", args={"count": {"type": "integer"}})

    result = ToolBusMiddleware().wrap_tool_call(request, lambda req: _message())

    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "tool_exception"
    assert "cannot convert float NaN to integer" in result.content


def test_handler_exception_uses_tool_call_id_when_runtime_lacks_it():
    from agent_core.tool_bus_middleware import ToolBusMiddleware

    request = _request("terminal", tool_call_id="call-from-tool-call")
    request.runtime = SimpleNamespace()

    def handler(_request):
        raise RuntimeError("boom")

    result = ToolBusMiddleware().wrap_tool_call(request, handler)

    assert result.status == "error"
    assert result.tool_call_id == "call-from-tool-call"


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


def test_coercion_uses_request_override_without_mutating_original_tool_call():
    from agent_core.tool_bus_middleware import ToolBusMiddleware

    class FakeRequest:
        def __init__(self):
            self.tool_call = {"name": "fake_tool", "args": {"count": "42"}, "id": "call-toolbus"}
            self.runtime = _runtime()
            self.tool = SimpleNamespace(name="fake_tool", args={"count": {"type": "integer"}})
            self.state = {}
            self.overrides = []

        def override(self, *, tool_call):
            self.overrides.append(tool_call)
            return SimpleNamespace(
                tool_call=tool_call,
                runtime=self.runtime,
                tool=self.tool,
                state=self.state,
            )

    request = FakeRequest()
    captured = {}

    def handler(req):
        captured.update(req.tool_call["args"])
        return _message()

    ToolBusMiddleware().wrap_tool_call(request, handler)

    assert captured == {"count": 42}
    assert request.tool_call["args"] == {"count": "42"}
    assert request.overrides == [{"name": "fake_tool", "args": {"count": 42}, "id": "call-toolbus"}]


def test_coerces_anyof_string_args_before_handler():
    from agent_core.tool_bus_middleware import ToolBusMiddleware

    request = _request(args={"timeout": "5", "watch_patterns": '["done"]'})
    request.tool = SimpleNamespace(
        name="terminal",
        args={
            "timeout": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "watch_patterns": {"anyOf": [{"type": "array"}, {"type": "null"}]},
        },
    )
    captured = {}

    def handler(req):
        captured.update(req.tool_call["args"])
        return _message()

    ToolBusMiddleware().wrap_tool_call(request, handler)

    assert captured == {"timeout": 5, "watch_patterns": ["done"]}


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

    assert result.content.startswith("abcdefghij")
    assert "original content was 26 chars" in result.content
    assert "artifact exceeded 10 chars" in result.content
    assert result.artifact["truncated"] is True
    assert result.artifact["original_chars"] > 10


def test_result_limit_replaces_large_artifact_even_outside_data_field():
    from agent_core.tool_bus_middleware import ToolBusMiddleware
    from agent_core.tool_catalog import ToolSpec

    spec = ToolSpec(
        name="fake_tool",
        toolset="fake",
        tool=SimpleNamespace(name="fake_tool"),
        max_result_size_chars=40,
    )
    bus = ToolBusMiddleware(specs={"fake_tool": spec})

    def handler(_req):
        return ToolMessage(
            content="ok",
            name="fake_tool",
            tool_call_id="call-toolbus",
            status="success",
            artifact={"meta": {"huge": "x" * 200}, "data": None},
        )

    result = bus.wrap_tool_call(_request(), handler)

    assert result.artifact["truncated"] is True
    assert result.artifact["original_chars"] > 40
    assert len(result.artifact["preview"]) == 40
    assert "artifact exceeded 40 chars" in result.content


def test_result_limit_replaces_large_transformed_artifact_with_nonstandard_shape():
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware
    from agent_core.tool_catalog import ToolSpec

    spec = ToolSpec(
        name="fake_tool",
        toolset="fake",
        tool=SimpleNamespace(name="fake_tool"),
        max_result_size_chars=30,
    )
    transformed = ToolMessage(
        content="ok",
        name="fake_tool",
        tool_call_id="call-toolbus",
        status="success",
        artifact={"payload": "y" * 120},
    )
    bus = ToolBusMiddleware(
        specs={"fake_tool": spec},
        hooks=ToolBusHooks(transform_tool_result=[lambda req, result: transformed]),
    )

    result = bus.wrap_tool_call(_request(), lambda req: _message())

    assert result.artifact["truncated"] is True
    assert result.artifact["original_chars"] > 30
    assert len(result.artifact["preview"]) == 30


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

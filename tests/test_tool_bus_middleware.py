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

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

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage


def _runtime():
    return SimpleNamespace()


def _message(tool_name: str):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": tool_name,
                "args": {},
                "id": f"call-{tool_name}",
            }
        ],
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


def test_after_model_blocks_consecutive_read_only_calls_after_threshold():
    from agent_core.tool_limits import (
        READ_ONLY_COUNT_STATE_KEY,
        ConsecutiveReadOnlyToolLimitMiddleware,
    )

    middleware = ConsecutiveReadOnlyToolLimitMiddleware(
        specs=_specs(),
        max_consecutive_read_only=2,
    )
    state = {"messages": [_message("read_file")]}

    first = middleware.after_model(state, _runtime())
    assert first == {READ_ONLY_COUNT_STATE_KEY: 1}

    state[READ_ONLY_COUNT_STATE_KEY] = first[READ_ONLY_COUNT_STATE_KEY]
    second = middleware.after_model(state, _runtime())
    assert second == {READ_ONLY_COUNT_STATE_KEY: 2}

    state[READ_ONLY_COUNT_STATE_KEY] = second[READ_ONLY_COUNT_STATE_KEY]
    third = middleware.after_model(state, _runtime())

    assert third is not None
    assert third[READ_ONLY_COUNT_STATE_KEY] == 3
    assert len(third["messages"]) == 1
    blocked = third["messages"][0]
    assert isinstance(blocked, ToolMessage)
    assert blocked.status == "error"
    assert blocked.name == "read_file"
    assert blocked.tool_call_id == "call-read_file"
    assert blocked.artifact["error"]["code"] == "read_loop_limit"


def test_after_model_non_read_only_tool_resets_consecutive_read_only_counter():
    from agent_core.tool_limits import (
        READ_ONLY_COUNT_STATE_KEY,
        ConsecutiveReadOnlyToolLimitMiddleware,
    )

    middleware = ConsecutiveReadOnlyToolLimitMiddleware(
        specs=_specs(),
        max_consecutive_read_only=2,
    )
    result = middleware.after_model(
        {
            "messages": [_message("write_file")],
            READ_ONLY_COUNT_STATE_KEY: 2,
        },
        _runtime(),
    )

    assert result == {READ_ONLY_COUNT_STATE_KEY: 0}


def test_after_model_unknown_tool_defaults_to_non_read_only_and_resets_counter():
    from agent_core.tool_limits import (
        READ_ONLY_COUNT_STATE_KEY,
        ConsecutiveReadOnlyToolLimitMiddleware,
    )

    middleware = ConsecutiveReadOnlyToolLimitMiddleware(
        specs=_specs(),
        max_consecutive_read_only=2,
    )
    result = middleware.after_model(
        {
            "messages": [_message("unknown_tool")],
            READ_ONLY_COUNT_STATE_KEY: 2,
        },
        _runtime(),
    )

    assert result == {READ_ONLY_COUNT_STATE_KEY: 0}


def test_after_model_handles_multiple_tool_calls_in_order():
    from agent_core.tool_limits import (
        READ_ONLY_COUNT_STATE_KEY,
        ConsecutiveReadOnlyToolLimitMiddleware,
    )

    middleware = ConsecutiveReadOnlyToolLimitMiddleware(
        specs=_specs(),
        max_consecutive_read_only=2,
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_file", "args": {}, "id": "call-1"},
            {"name": "write_file", "args": {}, "id": "call-2"},
            {"name": "read_file", "args": {}, "id": "call-3"},
        ],
    )

    result = middleware.after_model(
        {
            "messages": [message],
            READ_ONLY_COUNT_STATE_KEY: 1,
        },
        _runtime(),
    )

    assert result == {READ_ONLY_COUNT_STATE_KEY: 1}


def test_async_after_model_matches_sync_behavior():
    import asyncio

    from agent_core.tool_limits import (
        READ_ONLY_COUNT_STATE_KEY,
        ConsecutiveReadOnlyToolLimitMiddleware,
    )

    async def run():
        middleware = ConsecutiveReadOnlyToolLimitMiddleware(
            specs=_specs(),
            max_consecutive_read_only=1,
        )
        return await middleware.aafter_model(
            {
                "messages": [_message("read_file")],
                READ_ONLY_COUNT_STATE_KEY: 1,
            },
            _runtime(),
        )

    result = asyncio.run(run())

    assert result is not None
    assert result[READ_ONLY_COUNT_STATE_KEY] == 2
    assert result["messages"][0].artifact["error"]["code"] == "read_loop_limit"


def test_create_agent_blocks_read_only_loop_before_tool_execution():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool

    from agent_core.tool_catalog import ToolSpec
    from agent_core.tool_limits import ConsecutiveReadOnlyToolLimitMiddleware

    class ToolBindableFakeChatModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    calls = []

    @tool
    def read_file() -> str:
        """Read a file."""
        calls.append("read_file")
        return "ok"

    model = ToolBindableFakeChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {}, "id": "call-1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {}, "id": "call-2"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {}, "id": "call-3"}],
            ),
            AIMessage(content="done"),
        ]
    )
    specs = {
        "read_file": ToolSpec(
            name="read_file",
            toolset="file_read",
            tool=read_file,
            read_only=True,
        )
    }
    agent = create_agent(
        model=model,
        tools=[read_file],
        middleware=[
            ConsecutiveReadOnlyToolLimitMiddleware(
                specs=specs,
                max_consecutive_read_only=2,
            )
        ],
    )

    result = agent.invoke({"messages": [HumanMessage(content="go")]})

    tool_messages = [
        message for message in result["messages"]
        if isinstance(message, ToolMessage)
    ]
    assert calls == ["read_file", "read_file"]
    assert tool_messages[-1].tool_call_id == "call-3"
    assert tool_messages[-1].status == "error"
    assert tool_messages[-1].artifact["error"]["code"] == "read_loop_limit"

import json
import importlib.util

import pytest

from agent_core.session_context import hermes_task_id_from_thread_id

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None
    or importlib.util.find_spec("langgraph") is None
    or importlib.util.find_spec("pydantic") is None,
    reason="LangChain, LangGraph, and Pydantic are required for real execute_command smoke tests.",
)


def _decode_tool_result(raw: str) -> dict:
    return json.loads(raw)


def test_execute_command_uses_toolnode_runtime_thread_with_real_adapter(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.graph import MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    import agent_tools.shell as shell
    from agent_tools.shell import execute_command

    calls = []

    def fake_run_foreground_command(command, *, workdir, timeout=120, task_id="default"):
        calls.append(
            {
                "command": command,
                "workdir": workdir,
                "timeout": timeout,
                "task_id": task_id,
            }
        )
        return {"output": "shell-smoke-ok\n", "exit_code": 0, "error": None}

    monkeypatch.setattr(shell, "run_foreground_command", fake_run_foreground_command)

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([execute_command]))
    graph.set_entry_point("tools")
    graph.set_finish_point("tools")
    app = graph.compile()

    result = app.invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_command",
                            "args": {"command": "printf shell-smoke-ok"},
                            "id": "call-1",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": "smoke-thread-1"}},
    )

    payload = _decode_tool_result(result["messages"][-1].content)

    assert payload["ok"] is True
    assert payload["data"]["exit_code"] == 0
    assert "shell-smoke-ok" in payload["data"]["output"]
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("smoke-thread-1")


def test_execute_command_still_blocks_dangerous_commands_before_hermes():
    from agent_tools.shell import _execute_command_impl

    raw = _execute_command_impl("sudo ls")
    payload = _decode_tool_result(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "blocked_command"


def test_execute_command_direct_invoke_does_not_require_runtime(monkeypatch):
    import agent_tools.shell as shell
    from agent_tools.shell import execute_command

    calls = []

    def fake_run_foreground_command(command, *, workdir, timeout=120, task_id="default"):
        calls.append({"command": command, "task_id": task_id})
        return {"output": "direct-ok\n", "exit_code": 0, "error": None}

    monkeypatch.setattr(shell, "run_foreground_command", fake_run_foreground_command)

    raw = execute_command.invoke({"command": "printf direct-ok"})
    payload = _decode_tool_result(raw)

    assert payload["ok"] is True
    assert payload["data"]["output"] == "direct-ok\n"
    assert calls == [{"command": "printf direct-ok", "task_id": "default"}]

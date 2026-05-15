import json
from types import SimpleNamespace


def test_terminal_schema_does_not_expose_task_id():
    from agent_tools.terminal_tools import terminal

    assert "task_id" not in terminal.args
    assert "command" in terminal.args
    assert "background" in terminal.args
    assert "notify_on_complete" in terminal.args


def test_terminal_injects_runtime_thread_as_task_id(monkeypatch):
    import agent_tools.terminal_tools as terminal_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "ok\n", "exit_code": 0, "error": None})

    monkeypatch.setattr(terminal_tools, "run_terminal", fake_run_terminal)

    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="terminal-thread-1"),
        config={"configurable": {"thread_id": "ignored-config-thread"}},
    )

    raw = terminal_tools._terminal_impl(
        command="printf ok",
        background=False,
        timeout=30,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["data"]["output"] == "ok\n"
    assert "task_id" not in payload["meta"]
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("terminal-thread-1")
    assert calls[0]["force"] is False


def test_terminal_preserves_hermes_guard_block_response(monkeypatch):
    import agent_tools.terminal_tools as terminal_tools

    def fake_run_terminal(**kwargs):
        return json.dumps(
            {
                "output": "",
                "exit_code": -1,
                "error": "Command denied: destructive command",
                "status": "blocked",
            }
        )

    monkeypatch.setattr(terminal_tools, "run_terminal", fake_run_terminal)

    raw = terminal_tools._terminal_impl(
        command="rm -rf tmp",
        background=False,
        timeout=None,
        workdir=None,
        pty=False,
        notify_on_complete=False,
        watch_patterns=None,
        runtime=None,
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "blocked"
    assert payload["message"] == "Command denied: destructive command"
    assert "task_id" not in payload["meta"]


def test_terminal_toolnode_injects_runtime_thread(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.graph import MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    import agent_tools.terminal_tools as terminal_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.terminal_tools import terminal

    calls = []

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "toolnode-ok\n", "exit_code": 0, "error": None})

    monkeypatch.setattr(terminal_tools, "run_terminal", fake_run_terminal)

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([terminal]))
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
                            "name": "terminal",
                            "args": {"command": "printf toolnode-ok"},
                            "id": "call-1",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": "toolnode-terminal-thread"}},
    )

    payload = json.loads(result["messages"][-1].content)

    assert payload["ok"] is True
    assert "task_id" not in payload["meta"]
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("toolnode-terminal-thread")


def test_process_schema_does_not_expose_task_id():
    from agent_tools.terminal_tools import process

    assert "task_id" not in process.args
    assert "action" in process.args
    assert "session_id" in process.args


def test_process_list_is_scoped_to_runtime_task_id(monkeypatch):
    import agent_tools.terminal_tools as terminal_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []

    def fake_run_process(**kwargs):
        calls.append(kwargs)
        return json.dumps({"processes": []})

    monkeypatch.setattr(terminal_tools, "run_process", fake_run_process)
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="process-thread-1"))

    raw = terminal_tools._process_impl(
        action="list",
        session_id="",
        data="",
        timeout=None,
        offset=0,
        limit=200,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["data"]["processes"] == []
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("process-thread-1")


def test_process_rejects_cross_task_session(monkeypatch):
    import agent_tools.terminal_tools as terminal_tools

    class FakeSession:
        task_id = "lg_other_task"

    monkeypatch.setattr(terminal_tools.process_registry, "get", lambda session_id: FakeSession())
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="current-thread"))

    raw = terminal_tools._process_impl(
        action="poll",
        session_id="proc_abc",
        data="",
        timeout=None,
        offset=0,
        limit=200,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "access_denied"


def test_process_toolnode_injects_runtime_thread_for_list(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.graph import MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    import agent_tools.terminal_tools as terminal_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.terminal_tools import process

    calls = []

    def fake_run_process(**kwargs):
        calls.append(kwargs)
        return json.dumps({"processes": []})

    monkeypatch.setattr(terminal_tools, "run_process", fake_run_process)

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([process]))
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
                            "name": "process",
                            "args": {"action": "list"},
                            "id": "call-1",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": "toolnode-process-thread"}},
    )

    payload = json.loads(result["messages"][-1].content)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("toolnode-process-thread")


def _tool_names(tools):
    return {getattr(tool, "name", "") for tool in tools}


def test_parent_base_tools_include_terminal_and_process():
    from agent_core.delegation import BASE_TOOLS, READ_ONLY_TOOLS

    parent_names = _tool_names(BASE_TOOLS)
    read_only_names = _tool_names(READ_ONLY_TOOLS)

    assert "terminal" in parent_names
    assert "process" in parent_names
    assert "terminal" not in read_only_names
    assert "process" not in read_only_names


def test_human_interrupt_intercepts_terminal_and_process():
    from agent_core.builders import HUMAN_INTERRUPT_ON

    assert "terminal" in HUMAN_INTERRUPT_ON
    assert "process" in HUMAN_INTERRUPT_ON
    assert HUMAN_INTERRUPT_ON["terminal"]["allowed_decisions"] == ["approve", "edit", "reject", "respond"]
    assert HUMAN_INTERRUPT_ON["process"]["allowed_decisions"] == ["approve", "edit", "reject", "respond"]

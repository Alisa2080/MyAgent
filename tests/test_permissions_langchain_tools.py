def test_langchain_terminal_adapter_uses_policy_wrapper(monkeypatch):
    from agent_tools.terminal_toolkit import langchain_tools

    calls = []

    def fake_terminal_impl(**kwargs):
        calls.append(kwargs)
        return "terminal-result"

    monkeypatch.setattr(
        langchain_tools,
        "_load_wrappers",
        lambda: (lambda **kwargs: "process-result", fake_terminal_impl),
    )

    terminal_tool, _ = langchain_tools.build_langchain_tools(default_task_id="task-adapter")
    result = terminal_tool.invoke({"command": "pwd"})

    assert result == "terminal-result"
    assert calls[0]["command"] == "pwd"
    assert calls[0]["runtime"].config["configurable"]["thread_id"] == "task-adapter"


def test_langchain_process_adapter_uses_policy_wrapper(monkeypatch):
    from agent_tools.terminal_toolkit import langchain_tools

    calls = []

    def fake_process_impl(**kwargs):
        calls.append(kwargs)
        return "process-result"

    monkeypatch.setattr(
        langchain_tools,
        "_load_wrappers",
        lambda: (fake_process_impl, lambda **kwargs: "terminal-result"),
    )

    _, process_tool = langchain_tools.build_langchain_tools(default_task_id="task-adapter")
    result = process_tool.invoke({"action": "list"})

    assert result == "process-result"
    assert calls[0]["action"] == "list"
    assert calls[0]["runtime"].config["configurable"]["thread_id"] == "task-adapter"

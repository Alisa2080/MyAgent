def test_shell_tool_limits_target_terminal_and_process(monkeypatch):
    import agent_core.tool_limits as tool_limits

    calls = []

    class FakeToolCallLimitMiddleware:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(tool_limits, "ToolCallLimitMiddleware", FakeToolCallLimitMiddleware)

    tool_limits.build_tool_call_limit_middleware()

    by_name = {call["tool_name"]: call for call in calls}
    assert "terminal" in by_name
    assert "process" in by_name
    assert "execute_command" not in by_name
    assert by_name["terminal"]["run_limit"] == tool_limits.TERMINAL_RUN_LIMIT
    assert by_name["terminal"]["thread_limit"] == tool_limits.TERMINAL_THREAD_LIMIT
    assert by_name["process"]["run_limit"] == tool_limits.PROCESS_RUN_LIMIT
    assert by_name["process"]["thread_limit"] == tool_limits.PROCESS_THREAD_LIMIT

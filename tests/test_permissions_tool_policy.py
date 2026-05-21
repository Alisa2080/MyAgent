def test_read_tools_are_allowed():
    from agent_core.permissions.tool_policy import evaluate_tool_call

    for tool_name in ("read_file", "search_files", "list_directory", "file_info"):
        decision = evaluate_tool_call(tool_name, {"path": "."}, task_id="task-local")

        assert decision.outcome == "allow"
        assert decision.reason == "read_tool"


def test_workspace_write_is_allowed(monkeypatch):
    from agent_core.permissions.models import PolicyDecision
    import agent_core.permissions.tool_policy as tool_policy

    monkeypatch.setattr(
        tool_policy.file_policy,
        "classify_file_write",
        lambda path, *, task_id: PolicyDecision.allow("workspace_write"),
    )

    decision = tool_policy.evaluate_tool_call(
        "write_file",
        {"path": "notes.txt", "content": "hello"},
        task_id="task-local",
    )

    assert decision.outcome == "allow"


def test_patch_uses_all_patch_paths(monkeypatch):
    from agent_core.permissions.models import PolicyDecision
    import agent_core.permissions.tool_policy as tool_policy

    seen = []

    def fake_classify_file_write(path, *, task_id):
        seen.append(path)
        return PolicyDecision.allow("workspace_write")

    monkeypatch.setattr(tool_policy.file_policy, "classify_file_write", fake_classify_file_write)

    decision = tool_policy.evaluate_tool_call(
        "patch",
        {
            "mode": "patch",
            "patch": "*** Begin Patch\n*** Add File: a.txt\n+hello\n*** End Patch\n",
        },
        task_id="task-local",
    )

    assert decision.outcome == "allow"
    assert seen == ["a.txt"]


def test_terminal_uses_command_policy(monkeypatch):
    from agent_core.permissions.models import PolicyDecision
    import agent_core.permissions.tool_policy as tool_policy

    monkeypatch.setattr(
        tool_policy.command_policy,
        "classify_command",
        lambda command, *, background=False: PolicyDecision.review(
            "package_install", risk_tags=("package_install",)
        ),
    )

    decision = tool_policy.evaluate_tool_call(
        "terminal",
        {"command": "pip install rich"},
        task_id="task-local",
    )

    assert decision.outcome == "review"
    assert decision.risk_tags == ("package_install",)


def test_process_write_requires_review():
    from agent_core.permissions.tool_policy import evaluate_tool_call

    decision = evaluate_tool_call(
        "process",
        {"action": "submit", "session_id": "proc_1", "data": "rm -rf tmp"},
        task_id="task-local",
    )

    assert decision.outcome == "review"
    assert decision.reason == "process_stdin"
    assert "process_stdin" in decision.risk_tags

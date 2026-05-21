import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage


def _runtime(thread_id: str = "thread-1"):
    return SimpleNamespace(config={"configurable": {"thread_id": thread_id}})


def _terminal_policy_args(command: str) -> dict:
    from agent_core.permissions.tool_policy import canonical_tool_args

    return canonical_tool_args("terminal", {"command": command})


def test_policy_allow_does_not_interrupt(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.models import PolicyDecision

    def fail_interrupt(_payload):
        raise AssertionError("allow decisions should not interrupt")

    def fake_evaluate_tool_call(**kwargs):
        assert kwargs["tool_name"] == "terminal"
        assert kwargs["args"] == {"command": "pwd"}
        assert kwargs["tool_call_id"] == "call-1"
        return PolicyDecision.allow("read_tool")

    monkeypatch.setattr(human_loop, "interrupt", fail_interrupt)
    monkeypatch.setattr(
        human_loop.tool_policy,
        "evaluate_tool_call",
        fake_evaluate_tool_call,
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "terminal",
                "args": {"command": "pwd"},
                "id": "call-1",
            }
        ],
    )

    assert middleware.after_model({"messages": [message]}, _runtime()) is None


def test_policy_deny_synthesizes_tool_message(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.models import PolicyDecision

    def fail_interrupt(_payload):
        raise AssertionError("deny decisions should not interrupt")

    monkeypatch.setattr(human_loop, "interrupt", fail_interrupt)
    monkeypatch.setattr(
        human_loop.tool_policy,
        "evaluate_tool_call",
        lambda **_kwargs: PolicyDecision.deny(
            "hardline_destructive_command",
            risk_tags=("destructive_command",),
            message="Blocked hardline command.",
        ),
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "terminal",
                "args": {"command": "rm -rf /"},
                "id": "call-1",
            }
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    ai_message, tool_message = result["messages"]
    assert ai_message.tool_calls == []
    payload = json.loads(tool_message.content)
    assert payload["error"]["code"] == "policy_denied"
    assert payload["error"]["message"] == "Blocked hardline command."


def test_policy_review_records_approval(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import hermes_task_id_from_thread_id

    clear_approvals()

    monkeypatch.setattr(human_loop, "interrupt", lambda _payload: {"type": "approve"})
    monkeypatch.setattr(
        human_loop.tool_policy,
        "evaluate_tool_call",
        lambda **_kwargs: PolicyDecision.review(
            "package_install",
            risk_tags=("package_install",),
            requires_network=True,
            message="Package install requires review.",
        ),
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "terminal",
                "args": {"command": "pip install rich"},
                "id": "call-1",
            }
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    assert result["messages"][0].tool_calls[0]["id"] == "call-1"
    record = consume_approval(
        task_id=hermes_task_id_from_thread_id("thread-1"),
        tool_call_id="call-1",
        tool_name="terminal",
        args=_terminal_policy_args("pip install rich"),
        required_risk_tags=("package_install",),
    )
    assert record is not None
    assert record.allow_network_once is True


def test_policy_mixed_deny_and_review_preserves_approved_call(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.models import PolicyDecision

    def fake_evaluate_tool_call(**kwargs):
        if kwargs["args"]["command"] == "rm -rf /":
            return PolicyDecision.deny(
                "hardline_destructive_command",
                risk_tags=("hardline_destructive_command",),
                message="Blocked hardline command.",
            )
        return PolicyDecision.review(
            "package_install",
            risk_tags=("package_install",),
            message="Package install requires review.",
        )

    monkeypatch.setattr(human_loop, "interrupt", lambda _payload: {"type": "approve"})
    monkeypatch.setattr(human_loop.tool_policy, "evaluate_tool_call", fake_evaluate_tool_call)

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "terminal", "args": {"command": "rm -rf /"}, "id": "deny-call"},
            {"name": "terminal", "args": {"command": "pip install rich"}, "id": "review-call"},
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    assert [tool_call["id"] for tool_call in result["messages"][0].tool_calls] == ["review-call"]
    payload = json.loads(result["messages"][1].content)
    assert result["messages"][1].tool_call_id == "deny-call"
    assert payload["error"]["code"] == "policy_denied"


def test_non_policy_tool_still_uses_interrupt_on(monkeypatch):
    import agent_core.human_loop as human_loop

    seen_payloads = []

    def fake_interrupt(payload):
        seen_payloads.append(payload)
        return {"type": "approve"}

    monkeypatch.setattr(human_loop, "interrupt", fake_interrupt)

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={
            "memory_manage": {
                "allowed_decisions": ["approve", "edit", "reject", "respond"],
                "description": "Review memory change.",
            }
        },
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "memory_manage",
                "args": {"action": "write", "target": "memory", "content": "note"},
                "id": "memory-call",
            }
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    assert seen_payloads
    assert result["messages"][0].tool_calls[0]["id"] == "memory-call"


def test_policy_reject_does_not_record_approval(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import hermes_task_id_from_thread_id

    clear_approvals()
    monkeypatch.setattr(
        human_loop,
        "interrupt",
        lambda _payload: {"type": "reject", "message": "Rejected by reviewer."},
    )
    monkeypatch.setattr(
        human_loop.tool_policy,
        "evaluate_tool_call",
        lambda **_kwargs: PolicyDecision.review(
            "package_install",
            risk_tags=("package_install",),
            message="Package install requires review.",
        ),
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "terminal", "args": {"command": "pip install rich"}, "id": "call-1"}
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    assert result["messages"][0].tool_calls[0]["id"] == "call-1"
    assert result["messages"][1].tool_call_id == "call-1"
    assert result["messages"][1].status == "error"
    assert (
        consume_approval(
            task_id=hermes_task_id_from_thread_id("thread-1"),
            tool_call_id="call-1",
            tool_name="terminal",
            args=_terminal_policy_args("pip install rich"),
            required_risk_tags=("package_install",),
        )
        is None
    )


def test_policy_edit_does_not_record_approval(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import hermes_task_id_from_thread_id

    clear_approvals()
    monkeypatch.setattr(
        human_loop,
        "interrupt",
        lambda _payload: {
            "type": "edit",
            "edited_action": {
                "name": "terminal",
                "args": {"command": "pip install rich --dry-run"},
            },
        },
    )
    monkeypatch.setattr(
        human_loop.tool_policy,
        "evaluate_tool_call",
        lambda **_kwargs: PolicyDecision.review(
            "package_install",
            risk_tags=("package_install",),
            message="Package install requires review.",
        ),
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "terminal", "args": {"command": "pip install rich"}, "id": "call-1"}
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    assert result["messages"][0].tool_calls[0]["args"] == {"command": "pip install rich --dry-run"}
    assert (
        consume_approval(
            task_id=hermes_task_id_from_thread_id("thread-1"),
            tool_call_id="call-1",
            tool_name="terminal",
            args=_terminal_policy_args("pip install rich --dry-run"),
            required_risk_tags=("package_install",),
        )
        is None
    )


def test_policy_respond_does_not_record_approval(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import hermes_task_id_from_thread_id

    clear_approvals()
    monkeypatch.setattr(
        human_loop,
        "interrupt",
        lambda _payload: {"type": "respond", "message": "Handled by reviewer."},
    )
    monkeypatch.setattr(
        human_loop.tool_policy,
        "evaluate_tool_call",
        lambda **_kwargs: PolicyDecision.review(
            "package_install",
            risk_tags=("package_install",),
            message="Package install requires review.",
        ),
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "terminal", "args": {"command": "pip install rich"}, "id": "call-1"}
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    assert result["messages"][0].tool_calls[0]["id"] == "call-1"
    assert result["messages"][1].tool_call_id == "call-1"
    assert result["messages"][1].status == "success"
    assert (
        consume_approval(
            task_id=hermes_task_id_from_thread_id("thread-1"),
            tool_call_id="call-1",
            tool_name="terminal",
            args=_terminal_policy_args("pip install rich"),
            required_risk_tags=("package_install",),
        )
        is None
    )

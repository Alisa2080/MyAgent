import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage


def _runtime(thread_id: str = "thread-1"):
    return SimpleNamespace(config={"configurable": {"thread_id": thread_id}})


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
        args={"command": "pip install rich"},
        required_risk_tags=("package_install",),
    )
    assert record is not None
    assert record.allow_network_once is True

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage


def _runtime(thread_id: str = "thread-1"):
    return SimpleNamespace(config={"configurable": {"thread_id": thread_id}})


def _terminal_policy_args(command: str) -> dict:
    from agent_core.permissions.tool_policy import canonical_tool_args

    return canonical_tool_args("terminal", {"command": command})


def _assert_tool_outputs_match_calls(result: dict) -> None:
    ai_message = result["messages"][0]
    tool_call_ids = {tool_call["id"] for tool_call in ai_message.tool_calls}
    tool_output_ids = {
        message.tool_call_id
        for message in result["messages"][1:]
        if isinstance(message, ToolMessage)
    }
    assert tool_output_ids == tool_call_ids


def test_policy_allow_does_not_interrupt(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.models import PolicyDecision

    def fail_interrupt(_payload):
        raise AssertionError("allow decisions should not interrupt")

    def fake_evaluate_tool_call(**kwargs):
        assert kwargs["tool_name"] == "terminal"
        assert kwargs["args"] == _terminal_policy_args("pwd")
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


def test_policy_deny_does_not_interrupt(monkeypatch):
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

    assert middleware.after_model({"messages": [message]}, _runtime()) is None


def test_mixed_same_tool_policy_only_interrupts_review_call(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import hermes_task_id_from_thread_id

    clear_approvals()
    seen_payloads = []

    def fake_interrupt(payload):
        seen_payloads.append(payload)
        return {"type": "approve"}

    def fake_evaluate_tool_call(**kwargs):
        if kwargs["args"]["command"] == "pwd":
            return PolicyDecision.allow("read_tool")
        return PolicyDecision.review(
            "package_install",
            risk_tags=("package_install",),
            message="Package install requires review.",
        )

    monkeypatch.setattr(human_loop, "interrupt", fake_interrupt)
    monkeypatch.setattr(human_loop.tool_policy, "evaluate_tool_call", fake_evaluate_tool_call)

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "terminal", "args": {"command": "pwd"}, "id": "allow-call"},
            {
                "name": "terminal",
                "args": {"command": "pip install rich"},
                "id": "review-call",
            },
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    assert len(seen_payloads) == 1
    action_requests = seen_payloads[0]["action_requests"]
    assert [request["args"]["command"] for request in action_requests] == [
        "pip install rich"
    ]
    assert [tool_call["id"] for tool_call in result["messages"][0].tool_calls] == [
        "allow-call",
        "review-call",
    ]

    assert (
        consume_approval(
            task_id=hermes_task_id_from_thread_id("thread-1"),
            tool_call_id="allow-call",
            tool_name="terminal",
            args=_terminal_policy_args("pwd"),
            required_risk_tags=("package_install",),
        )
        is None
    )
    assert (
        consume_approval(
            task_id=hermes_task_id_from_thread_id("thread-1"),
            tool_call_id="review-call",
            tool_name="terminal",
            args=_terminal_policy_args("pip install rich"),
            required_risk_tags=("package_install",),
        )
        is not None
    )


def test_policy_review_selection_uses_policy_arg_builder(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.models import PolicyDecision

    seen_args = []

    def fake_builder(args):
        return {"command": args["command"], "builder_marker": "from-builder"}

    def fake_evaluate_tool_call(**kwargs):
        seen_args.append(kwargs["args"])
        return PolicyDecision.allow("read_tool")

    monkeypatch.setitem(human_loop.POLICY_ARG_BUILDERS, "terminal", fake_builder)
    monkeypatch.setattr(human_loop.tool_policy, "evaluate_tool_call", fake_evaluate_tool_call)
    monkeypatch.setattr(
        human_loop,
        "interrupt",
        lambda _payload: (_ for _ in ()).throw(AssertionError("should not interrupt")),
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "terminal", "args": {"command": "pwd"}, "id": "call-builder"}
        ],
    )

    assert middleware.after_model({"messages": [message]}, _runtime()) is None
    assert seen_args == [{"command": "pwd", "builder_marker": "from-builder"}]


def test_policy_approval_digest_uses_policy_arg_builder(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import hermes_task_id_from_thread_id

    clear_approvals()

    def fake_builder(args):
        return {"command": args["command"], "builder_marker": "from-builder"}

    monkeypatch.setitem(human_loop.POLICY_ARG_BUILDERS, "terminal", fake_builder)
    monkeypatch.setattr(human_loop, "interrupt", lambda _payload: {"type": "approve"})
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
            {
                "name": "terminal",
                "args": {"command": "pip install rich"},
                "id": "call-builder",
            }
        ],
    )

    result = middleware.after_model({"messages": [message]}, _runtime())

    assert result is not None
    assert (
        consume_approval(
            task_id=hermes_task_id_from_thread_id("thread-1"),
            tool_call_id="call-builder",
            tool_name="terminal",
            args={"command": "pip install rich", "builder_marker": "from-builder"},
            required_risk_tags=("package_install",),
        )
        is not None
    )
    assert (
        consume_approval(
            task_id=hermes_task_id_from_thread_id("thread-1"),
            tool_call_id="call-builder",
            tool_name="terminal",
            args=_terminal_policy_args("pip install rich"),
            required_risk_tags=("package_install",),
        )
        is None
    )


def test_policy_review_records_approval(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import hermes_task_id_from_thread_id

    clear_approvals()
    audit_events = []

    monkeypatch.setattr(human_loop, "interrupt", lambda _payload: {"type": "approve"})
    monkeypatch.setattr(
        human_loop,
        "audit_policy_event",
        lambda **kwargs: audit_events.append(kwargs),
    )
    monkeypatch.setattr(human_loop, "resolve_runtime_profile", lambda: "hosted")
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
    assert audit_events[-1]["profile"] == "hosted"
    assert audit_events[-1]["approved_by_human"] is True
    assert audit_events[-1]["network_once"] is True
    assert audit_events[-1]["extra"]["approval_id"]


def test_policy_audit_redacts_secret_preview(caplog):
    import logging

    from agent_core.permissions.audit import audit_policy_event
    from agent_core.permissions.models import PolicyDecision

    caplog.set_level(logging.INFO, logger="agent_core.permissions.audit")
    decision = PolicyDecision.review("network_access", risk_tags=("network_access",))

    audit_policy_event(
        profile="hosted",
        tool_name="terminal",
        task_id="task-1",
        decision=decision,
        preview="curl -H 'Authorization: Bearer sk-test1234567890abcdef' https://example.com?api_key=secretvalue",
    )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "sk-test1234567890abcdef" not in logged
    assert "api_key=secretvalue" not in logged
    assert "api_key=***" in logged


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


def test_lenient_resume_formats_approved(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import hermes_task_id_from_thread_id

    for resume_form in [
        {"decisions": [{"type": "approve"}]},
        [{"type": "approve"}],
        {"type": "approve"},
        "approve",
        "0",
        "yes",
    ]:
        clear_approvals()
        monkeypatch.setattr(
            human_loop,
            "interrupt",
            lambda _payload, f=resume_form: f,
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
            tool_calls=[{"name": "terminal", "args": {"command": "pip install rich"}, "id": "call-1"}],
        )

        result = middleware.after_model({"messages": [message]}, _runtime())
        assert result is not None
        record = consume_approval(
            task_id=hermes_task_id_from_thread_id("thread-1"),
            tool_call_id="call-1",
            tool_name="terminal",
            args=_terminal_policy_args("pip install rich"),
            required_risk_tags=("package_install",),
        )
        assert record is not None, f"Failed for resume form: {resume_form}"


def test_async_behavior_matches_sync(monkeypatch):
    import asyncio

    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision
    from agent_core.session_context import hermes_task_id_from_thread_id

    clear_approvals()
    monkeypatch.setattr(
        human_loop,
        "interrupt",
        lambda _payload: {"type": "approve"},
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
        tool_calls=[{"name": "terminal", "args": {"command": "pip install rich"}, "id": "call-1"}],
    )

    sync_result = middleware.after_model({"messages": [message]}, _runtime())

    clear_approvals()

    async def test_async():
        return await middleware.aafter_model({"messages": [message]}, _runtime())

    async_result = asyncio.get_event_loop().run_until_complete(test_async())

    assert sync_result is not None
    assert async_result is not None
    assert sync_result["messages"][0].tool_calls[0]["id"] == async_result["messages"][0].tool_calls[0]["id"]

    record_sync = consume_approval(
        task_id=hermes_task_id_from_thread_id("thread-1"),
        tool_call_id="call-1",
        tool_name="terminal",
        args=_terminal_policy_args("pip install rich"),
        required_risk_tags=("package_install",),
    )
    assert record_sync is not None

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from agent_core.permissions.approvals import clear_approvals
from agent_core.permissions.tool_grants import clear_tool_policy_grants
from agent_core.session_context import runtime_task_id_from_thread_id


def setup_function():
    clear_approvals()
    clear_tool_policy_grants()


def teardown_function():
    clear_approvals()
    clear_tool_policy_grants()


def _runtime(thread_id: str = "policy-toolbus-thread", tool_call_id: str = "call-policy-toolbus"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def _request(
    tool_name: str,
    args: dict,
    *,
    thread_id: str = "policy-toolbus-thread",
    tool_call_id: str = "call-policy-toolbus",
):
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": args, "id": tool_call_id},
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
        tool=SimpleNamespace(name=tool_name, args={}),
        state={},
    )


def _success_message(tool_name: str, tool_call_id: str) -> ToolMessage:
    return ToolMessage(
        content="ok",
        name=tool_name,
        tool_call_id=tool_call_id,
        status="success",
        artifact={
            "ok": True,
            "tool": tool_name,
            "message": "ok",
            "data": None,
            "error": None,
            "meta": {},
        },
    )


def _run_nested_policy_toolbus(request, handler, *, post_calls: list | None = None):
    from agent_core.policy_tool_middleware import PolicyToolMiddleware
    from agent_core.tool_bus_middleware import ToolBusHooks, ToolBusMiddleware

    policy = PolicyToolMiddleware(policy_tools={"terminal", "process", "write_file", "patch"})

    hooks = ToolBusHooks()
    if post_calls is not None:
        hooks.post_tool_call.append(
            lambda bus_request, bus_result: post_calls.append(
                (
                    bus_request.tool_name,
                    bus_result.result.status,
                    bus_result.result.artifact["error"]["code"]
                    if bus_result.result.artifact.get("error")
                    else None,
                )
            )
        )

    bus = ToolBusMiddleware(hooks=hooks)
    return bus.wrap_tool_call(request, lambda call_request: policy.wrap_tool_call(call_request, handler))


def test_nested_toolbus_policy_allows_handler_and_records_grant():
    from agent_core.permissions.approvals import make_args_digest
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args

    request = _request("terminal", {"command": "pwd"}, tool_call_id="call-allow")
    calls = []
    post_calls = []

    result = _run_nested_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", "call-allow"),
        post_calls=post_calls,
    )

    assert result.artifact["ok"] is True
    assert calls == [request]
    assert post_calls == [("terminal", "success", None)]

    args = canonical_tool_args("terminal", {"command": "pwd"})
    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id("policy-toolbus-thread"),
        tool_call_id="call-allow",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
    assert grant.risk_tags == ()


def test_nested_toolbus_policy_deny_blocks_handler_and_posts_result():
    request = _request("terminal", {"command": "rm -rf /"}, tool_call_id="call-deny")
    calls = []
    post_calls = []

    result = _run_nested_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", "call-deny"),
        post_calls=post_calls,
    )

    assert calls == []
    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "policy_denied"
    assert post_calls == [("terminal", "error", "policy_denied")]


def test_nested_toolbus_policy_review_without_approval_blocks_handler():
    request = _request(
        "terminal",
        {"command": "touch approval-required.txt"},
        tool_call_id="call-review-missing",
    )
    calls = []
    post_calls = []

    result = _run_nested_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", "call-review-missing"),
        post_calls=post_calls,
    )

    assert calls == []
    assert result.status == "error"
    assert result.artifact["ok"] is False
    assert result.artifact["tool"] == "terminal"
    assert result.artifact["error"]["code"] == "approval_required"
    assert post_calls == [("terminal", "error", "approval_required")]


def test_nested_toolbus_policy_consumes_approval_and_records_grant():
    from agent_core.permissions.approvals import ApprovalRecord, consume_approval, make_args_digest, record_approval
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args

    thread_id = "policy-toolbus-network-thread"
    tool_call_id = "call-network"
    args = canonical_tool_args("terminal", {"command": "curl https://example.com"})
    record_approval(
        ApprovalRecord(
            approval_id="approval-network",
            decision_id="decision-network",
            task_id=runtime_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="terminal",
            args_digest=make_args_digest(args),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )
    request = _request(
        "terminal",
        {"command": "curl https://example.com"},
        thread_id=thread_id,
        tool_call_id=tool_call_id,
    )
    calls = []

    result = _run_nested_policy_toolbus(
        request,
        lambda received: calls.append(received) or _success_message("terminal", tool_call_id),
    )

    assert result.artifact["ok"] is True
    assert calls == [request]
    assert (
        consume_approval(
            task_id=runtime_task_id_from_thread_id(thread_id),
            tool_call_id=tool_call_id,
            tool_name="terminal",
            args=args,
            required_risk_tags=("network_access",),
        )
        is None
    )

    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id(thread_id),
        tool_call_id=tool_call_id,
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
    assert grant.risk_tags == ("network_access",)
    assert grant.allow_network_once is True

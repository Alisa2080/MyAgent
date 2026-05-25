import json
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from agent_core.permissions.approvals import (
    ApprovalRecord,
    clear_approvals,
    make_args_digest,
    record_approval,
)
from agent_core.permissions.tool_grants import (
    ToolPolicyGrant,
    consume_tool_policy_grant,
    record_tool_policy_grant,
)
from agent_core.permissions.tool_policy import canonical_tool_args
from agent_core.policy_tool_middleware import PolicyToolMiddleware
from agent_core.session_context import hermes_task_id_from_thread_id


def _runtime(thread_id="policy-thread", tool_call_id="call-policy"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def _request(tool_name, args, *, thread_id="policy-thread", tool_call_id="call-policy"):
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": args, "id": tool_call_id},
        runtime=_runtime(thread_id=thread_id, tool_call_id=tool_call_id),
        tool=None,
        state={},
    )


def test_policy_tool_middleware_passes_allow_to_handler():
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    request = _request("terminal", {"command": "pwd"})
    calls = []

    def handler(received):
        calls.append(received)
        return ToolMessage(content='{"ok": true}', name="terminal", tool_call_id="call-policy")

    result = middleware.wrap_tool_call(request, handler)

    assert result.content == '{"ok": true}'
    assert calls == [request]
    args = canonical_tool_args("terminal", {"command": "pwd"})
    grant = consume_tool_policy_grant(
        task_id=hermes_task_id_from_thread_id("policy-thread"),
        tool_call_id="call-policy",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
    assert grant.risk_tags == ()


def test_policy_tool_middleware_short_circuits_deny():
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    calls = []
    request = _request("terminal", {"command": "rm -rf /"}, tool_call_id="call-deny")

    result = middleware.wrap_tool_call(
        request,
        lambda received: calls.append(received),
    )

    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "policy_denied"
    assert result.status == "error"
    assert calls == []


def test_policy_tool_middleware_requires_approval_for_review():
    clear_approvals()
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    request = _request(
        "terminal",
        {"command": "touch approval-required.txt"},
        tool_call_id="call-review",
    )

    result = middleware.wrap_tool_call(
        request,
        lambda received: ToolMessage(content="unused", tool_call_id="x"),
    )

    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"


def test_policy_tool_middleware_consumes_approval_and_records_grant():
    clear_approvals()
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    request = _request(
        "terminal",
        {"command": "curl https://example.com"},
        thread_id="thread-network",
        tool_call_id="call-network",
    )
    args = canonical_tool_args("terminal", {"command": "curl https://example.com"})
    record_approval(
        ApprovalRecord(
            approval_id="approval-network",
            decision_id="decision-network",
            task_id=hermes_task_id_from_thread_id("thread-network"),
            tool_call_id="call-network",
            tool_name="terminal",
            args_digest=make_args_digest(args),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )

    result = middleware.wrap_tool_call(
        request,
        lambda received: ToolMessage(
            content='{"ok": true}',
            name="terminal",
            tool_call_id="call-network",
        ),
    )

    assert json.loads(result.content)["ok"] is True
    grant = consume_tool_policy_grant(
        task_id=hermes_task_id_from_thread_id("thread-network"),
        tool_call_id="call-network",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
    assert grant.allow_network_once is True
    assert grant.risk_tags == ("network_access",)


def test_tool_policy_grant_args_digest_mismatch_does_not_consume():
    task_id = hermes_task_id_from_thread_id("thread-digest")
    grant = ToolPolicyGrant(
        task_id=task_id,
        tool_call_id="call-digest",
        tool_name="write_file",
        args_digest=make_args_digest({"path": "a.txt"}),
        risk_tags=(),
    )
    record_tool_policy_grant(grant)

    assert (
        consume_tool_policy_grant(
            task_id=task_id,
            tool_call_id="call-digest",
            tool_name="write_file",
            args={"path": "b.txt"},
        )
        is None
    )

    assert (
        consume_tool_policy_grant(
            task_id=task_id,
            tool_call_id="call-digest",
            tool_name="write_file",
            args={"path": "a.txt"},
        )
        == grant
    )


def test_policy_tool_middleware_ignores_unsupported_tool():
    middleware = PolicyToolMiddleware(policy_tools={"terminal"})
    request = _request("read_file", {"path": "README.md"}, tool_call_id="call-read")

    result = middleware.wrap_tool_call(
        request,
        lambda received: ToolMessage(
            content='{"ok": true}',
            name="read_file",
            tool_call_id="call-read",
        ),
    )

    assert json.loads(result.content)["ok"] is True

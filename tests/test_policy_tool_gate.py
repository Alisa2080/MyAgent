from __future__ import annotations

from types import SimpleNamespace

from agent_core.permissions.approvals import clear_approvals
from agent_core.permissions.tool_grants import clear_tool_policy_grants
from agent_core.session_context import runtime_task_id_from_thread_id


def setup_function():
    clear_approvals()
    clear_tool_policy_grants()


def teardown_function():
    clear_approvals()
    clear_tool_policy_grants()


def _runtime(thread_id: str = "policy-gate-thread", tool_call_id: str = "call-policy-gate"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=thread_id),
        tool_call_id=tool_call_id,
    )


def _gate_request(tool_name: str, args: dict, *, tool_call_id: str = "call-policy-gate", runtime=None):
    from agent_core.policy_tool_gate import PolicyToolGateRequest

    return PolicyToolGateRequest(
        tool_name=tool_name,
        args=args,
        tool_call_id=tool_call_id,
        runtime=runtime or _runtime(tool_call_id=tool_call_id),
        request=None,
    )


def test_policy_tool_gate_allows_and_records_grant():
    from agent_core.permissions.approvals import make_args_digest
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.policy_tool_gate import run_policy_tool_gate

    result = run_policy_tool_gate(
        _gate_request("terminal", {"command": "pwd"}, tool_call_id="call-allow"),
        policy_tools={"terminal"},
    )

    assert result is None
    args = canonical_tool_args("terminal", {"command": "pwd"})
    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
        tool_call_id="call-allow",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
    assert grant.risk_tags == ()


def test_policy_tool_gate_denies_without_recording_grant():
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.policy_tool_gate import run_policy_tool_gate

    result = run_policy_tool_gate(
        _gate_request("terminal", {"command": "rm -rf /"}, tool_call_id="call-deny"),
        policy_tools={"terminal"},
    )

    assert result is not None
    assert result.status == "error"
    assert result.artifact["error"]["code"] == "policy_denied"
    assert (
        consume_tool_policy_grant(
            task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
            tool_call_id="call-deny",
            tool_name="terminal",
            args=canonical_tool_args("terminal", {"command": "rm -rf /"}),
        )
        is None
    )


def test_policy_tool_gate_ignores_unsupported_tool():
    from agent_core.policy_tool_gate import run_policy_tool_gate

    result = run_policy_tool_gate(
        _gate_request("read_file", {"path": "README.md"}, tool_call_id="call-read"),
        policy_tools={"terminal"},
    )

    assert result is None


def test_policy_tool_gate_requires_approval_for_review_decision():
    from agent_core.policy_tool_gate import run_policy_tool_gate

    result = run_policy_tool_gate(
        _gate_request(
            "terminal",
            {"command": "touch approval-required.txt"},
            tool_call_id="call-review",
        ),
        policy_tools={"terminal"},
    )

    assert result is not None
    assert result.status == "error"
    assert result.artifact["error"]["code"] == "approval_required"


def test_policy_tool_gate_consumes_approval_and_records_grant():
    from agent_core.permissions.approvals import ApprovalRecord, make_args_digest, record_approval
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.policy_tool_gate import run_policy_tool_gate

    args = canonical_tool_args("terminal", {"command": "curl https://example.com"})
    record_approval(
        ApprovalRecord(
            approval_id="approval-gate-network",
            decision_id="decision-gate-network",
            task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
            tool_call_id="call-network",
            tool_name="terminal",
            args_digest=make_args_digest(args),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )

    result = run_policy_tool_gate(
        _gate_request(
            "terminal",
            {"command": "curl https://example.com"},
            tool_call_id="call-network",
        ),
        policy_tools={"terminal"},
    )

    assert result is None
    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
        tool_call_id="call-network",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)
    assert grant.risk_tags == ("network_access",)
    assert grant.allow_network_once is True


def test_policy_tool_gate_uses_runtime_tool_call_id_when_request_id_is_empty():
    from agent_core.permissions.approvals import make_args_digest
    from agent_core.permissions.tool_grants import consume_tool_policy_grant
    from agent_core.permissions.tool_policy import canonical_tool_args
    from agent_core.policy_tool_gate import run_policy_tool_gate

    runtime = _runtime(tool_call_id="call-from-runtime")

    result = run_policy_tool_gate(
        _gate_request("terminal", {"command": "pwd"}, tool_call_id="", runtime=runtime),
        policy_tools={"terminal"},
    )

    assert result is None
    args = canonical_tool_args("terminal", {"command": "pwd"})
    grant = consume_tool_policy_grant(
        task_id=runtime_task_id_from_thread_id("policy-gate-thread"),
        tool_call_id="call-from-runtime",
        tool_name="terminal",
        args=args,
    )
    assert grant is not None
    assert grant.args_digest == make_args_digest(args)

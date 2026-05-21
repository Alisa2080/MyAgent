import pytest


@pytest.fixture(autouse=True)
def clear_approval_state():
    from agent_core.permissions.approvals import clear_approvals

    clear_approvals()
    yield
    clear_approvals()


def test_approval_is_single_use_and_argument_bound():
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        consume_approval,
        make_args_digest,
        record_approval,
    )

    record = ApprovalRecord(
        approval_id="approval-1",
        decision_id="decision-1",
        task_id="task-1",
        tool_call_id="call-1",
        tool_name="terminal",
        args_digest=make_args_digest({"command": "pip install rich"}),
        risk_tags=("package_install",),
        allow_network_once=True,
    )
    record_approval(record)

    consumed = consume_approval(
        task_id="task-1",
        tool_call_id="call-1",
        tool_name="terminal",
        args={"command": "pip install rich"},
        required_risk_tags=("package_install",),
    )

    assert consumed is not None
    assert consumed.allow_network_once is True
    assert consume_approval(
        task_id="task-1",
        tool_call_id="call-1",
        tool_name="terminal",
        args={"command": "pip install rich"},
        required_risk_tags=("package_install",),
    ) is None


def test_approval_rejects_changed_arguments():
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        consume_approval,
        make_args_digest,
        record_approval,
    )

    record_approval(
        ApprovalRecord(
            approval_id="approval-2",
            decision_id="decision-2",
            task_id="task-1",
            tool_call_id="call-2",
            tool_name="write_file",
            args_digest=make_args_digest({"path": "/tmp/a.txt", "content": "a"}),
            risk_tags=("writes_outside_workspace",),
            allow_network_once=False,
        )
    )

    assert consume_approval(
        task_id="task-1",
        tool_call_id="call-2",
        tool_name="write_file",
        args={"path": "/tmp/a.txt", "content": "b"},
        required_risk_tags=("writes_outside_workspace",),
    ) is None
    assert consume_approval(
        task_id="task-1",
        tool_call_id="call-2",
        tool_name="write_file",
        args={"path": "/tmp/a.txt", "content": "a"},
        required_risk_tags=("writes_outside_workspace",),
    ) is None


def test_approval_rejects_missing_risk_tag():
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        consume_approval,
        make_args_digest,
        record_approval,
    )

    record_approval(
        ApprovalRecord(
            approval_id="approval-3",
            decision_id="decision-3",
            task_id="task-1",
            tool_call_id="call-3",
            tool_name="terminal",
            args_digest=make_args_digest({"command": "curl https://example.com"}),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )

    assert consume_approval(
        task_id="task-1",
        tool_call_id="call-3",
        tool_name="terminal",
        args={"command": "curl https://example.com"},
        required_risk_tags=("package_install",),
    ) is None
    assert consume_approval(
        task_id="task-1",
        tool_call_id="call-3",
        tool_name="terminal",
        args={"command": "curl https://example.com"},
        required_risk_tags=("network_access",),
    ) is None

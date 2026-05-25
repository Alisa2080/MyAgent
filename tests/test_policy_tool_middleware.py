from agent_core.permissions.tool_grants import (
    ToolPolicyGrant,
    clear_tool_policy_grants,
    consume_tool_policy_grant,
    record_tool_policy_grant,
)


def setup_function():
    clear_tool_policy_grants()


def teardown_function():
    clear_tool_policy_grants()


def test_tool_policy_grant_is_consumed_once():
    grant = ToolPolicyGrant(
        task_id="task-grant",
        tool_call_id="call-grant",
        tool_name="terminal",
        risk_tags=("network_access",),
        allow_network_once=True,
    )

    record_tool_policy_grant(grant)

    consumed = consume_tool_policy_grant(
        task_id="task-grant",
        tool_call_id="call-grant",
        tool_name="terminal",
    )
    assert consumed == grant
    assert (
        consume_tool_policy_grant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="terminal",
        )
        is None
    )


def test_tool_policy_grant_requires_matching_tool_name():
    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="terminal",
            risk_tags=("destructive_command",),
        )
    )

    assert (
        consume_tool_policy_grant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="process",
        )
        is None
    )


def test_tool_policy_grant_requires_matching_risk_tags():
    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="write_file",
            risk_tags=(),
        )
    )

    assert (
        consume_tool_policy_grant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="write_file",
            required_risk_tags=("external_file_write",),
        )
        is None
    )
    assert (
        consume_tool_policy_grant(
            task_id="task-grant",
            tool_call_id="call-grant",
            tool_name="write_file",
        )
        is not None
    )

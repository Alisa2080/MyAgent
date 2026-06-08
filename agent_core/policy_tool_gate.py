from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage

from agent_core.permissions import tool_policy
from agent_core.permissions.approvals import consume_approval, make_args_digest
from agent_core.permissions.tool_grants import ToolPolicyGrant, record_tool_policy_grant
from agent_core.session_context import RuntimeContext
from agent_tools.shared.tool_result import tool_failure


@dataclass(frozen=True)
class PolicyToolGateRequest:
    tool_name: str
    args: dict[str, Any]
    tool_call_id: str | None
    runtime: Any | None
    request: ToolCallRequest | None = None


def terminal_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("terminal", args)


def process_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("process", args)


def write_file_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("write_file", args)


def patch_policy_args(args: dict[str, Any]) -> dict[str, Any]:
    return tool_policy.canonical_tool_args("patch", args)


POLICY_ARG_BUILDERS = {
    "terminal": terminal_policy_args,
    "process": process_policy_args,
    "write_file": write_file_policy_args,
    "patch": patch_policy_args,
}


def run_policy_tool_gate(
    gate_request: PolicyToolGateRequest,
    *,
    policy_tools: set[str],
) -> ToolMessage | None:
    tool_name = gate_request.tool_name
    if tool_name not in policy_tools:
        return None

    builder = POLICY_ARG_BUILDERS.get(tool_name)
    if builder is None:
        return None

    runtime_context = RuntimeContext.from_runtime(gate_request.runtime)
    tool_call_id = gate_request.tool_call_id or runtime_context.tool_call_id
    policy_args = builder(gate_request.args)
    decision = tool_policy.evaluate_tool_call(
        tool_name,
        policy_args,
        runtime_context.task_id,
        tool_call_id=tool_call_id,
    )

    if decision.outcome == "deny":
        return tool_failure(
            tool_name,
            decision.human_message,
            code="policy_denied",
            data=decision.data,
            runtime=gate_request.runtime,
        )

    if decision.outcome == "allow":
        if tool_call_id:
            record_tool_policy_grant(
                ToolPolicyGrant(
                    task_id=runtime_context.task_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args_digest=make_args_digest(policy_args),
                    risk_tags=decision.risk_tags,
                )
            )
        return None

    approval = consume_approval(
        task_id=runtime_context.task_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=policy_args,
        required_risk_tags=decision.risk_tags,
    )
    if approval is None:
        return tool_failure(
            tool_name,
            decision.human_message,
            code="approval_required",
            data=decision.data,
            runtime=gate_request.runtime,
        )

    record_tool_policy_grant(
        ToolPolicyGrant(
            task_id=runtime_context.task_id,
            tool_call_id=tool_call_id or "",
            tool_name=tool_name,
            args_digest=make_args_digest(policy_args),
            risk_tags=approval.risk_tags,
            allow_network_once=approval.allow_network_once,
        )
    )
    return None

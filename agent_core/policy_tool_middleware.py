from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_core.permissions import tool_policy
from agent_core.permissions.approvals import consume_approval, make_args_digest
from agent_core.permissions.tool_grants import ToolPolicyGrant, record_tool_policy_grant
from agent_core.session_context import RuntimeContext
from agent_tools.shared.tool_output import tool_error


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


class PolicyToolMiddleware(AgentMiddleware):
    def __init__(self, *, policy_tools: set[str]) -> None:
        super().__init__()
        self.policy_tools = set(policy_tools)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        gated = self._gate_tool_call(request)
        if gated is not None:
            return gated
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        gated = self._gate_tool_call(request)
        if gated is not None:
            return gated
        return await handler(request)

    def _gate_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_call = request.tool_call
        tool_name = tool_call["name"]
        if tool_name not in self.policy_tools:
            return None

        builder = POLICY_ARG_BUILDERS.get(tool_name)
        if builder is None:
            return None

        runtime_context = RuntimeContext.from_runtime(request.runtime)
        tool_call_id = tool_call.get("id") or runtime_context.tool_call_id
        policy_args = builder(tool_call.get("args") or {})
        decision = tool_policy.evaluate_tool_call(
            tool_name,
            policy_args,
            runtime_context.task_id,
            tool_call_id=tool_call_id,
        )

        if decision.outcome == "deny":
            return self._tool_message(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                content=tool_error(
                    tool_name,
                    decision.human_message,
                    code="policy_denied",
                    data=decision.data,
                ),
                status="error",
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
            return self._tool_message(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                content=tool_error(
                    tool_name,
                    decision.human_message,
                    code="approval_required",
                    data=decision.data,
                ),
                status="error",
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

    @staticmethod
    def _tool_message(
        *,
        tool_name: str,
        tool_call_id: str | None,
        content: str,
        status: str,
    ) -> ToolMessage:
        return ToolMessage(
            content=content,
            name=tool_name,
            tool_call_id=tool_call_id or "",
            status=status,
        )

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_core.policy_tool_gate import (
    POLICY_ARG_BUILDERS,
    PolicyToolGateRequest,
    patch_policy_args,
    process_policy_args,
    run_policy_tool_gate,
    terminal_policy_args,
    write_file_policy_args,
)


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
        raw_args = tool_call.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        return run_policy_tool_gate(
            PolicyToolGateRequest(
                tool_name,
                args,
                tool_call.get("id"),
                request.runtime,
                request,
            ),
            policy_tools=self.policy_tools,
        )

from __future__ import annotations

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from agent_tools.shared.tool_result import tool_failure


class ExecuteCodeInput(BaseModel):
    code: str = Field(description="Python code to execute with constrained project tool access.")


@tool("execute_code", args_schema=ExecuteCodeInput)
def execute_code(code: str, runtime: ToolRuntime) -> object:
    """Execute Python code locally with constrained access to selected project tools."""
    return tool_failure(
        "execute_code",
        "execute_code is registered but the local executor is not implemented yet.",
        code="not_implemented",
        runtime=runtime,
    )

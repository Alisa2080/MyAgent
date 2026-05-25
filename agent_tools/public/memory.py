import json

from langchain.tools import tool
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from agent_core.memory import memory_store, memory_tool as run_memory_tool
from agent_tools.shared.tool_result import tool_failure, tool_success


class MemoryManageInput(BaseModel):
    action: str = Field(description="Memory action: add, replace, or remove.")
    target: str = Field(description="Memory target: memory for project/agent notes, or user for user profile/preferences.")
    content: str = Field(default="", description="Entry content. Required for add and replace.")
    old_text: str = Field(default="", description="Short unique substring identifying the entry to replace or remove.")


@tool("memory_manage", args_schema=MemoryManageInput)
def memory_manage(action: str, target: str, content: str = "", old_text: str = "") -> ToolMessage:
    """Save, replace, or remove durable memory. target must be 'memory' or 'user'."""
    try:
        raw = run_memory_tool(
            action=action,
            target=target,
            content=content or None,
            old_text=old_text or None,
            store=memory_store,
        )
        result = json.loads(raw)
        ok = bool(result.pop("success", False))
        message = result.pop("message", "")
        error_message = result.pop("error", None)
        meta = {"available": "next_agent_session"}
        if ok:
            meta["system_prompt_note"] = (
                "Memory change was written to disk. It will not modify the current session's "
                "frozen system prompt and will be available after the next agent session starts."
            )
            return tool_success("memory_manage", data=result or None, message=message or "Memory updated.", meta=meta)

        meta["system_prompt_note"] = (
            "No memory change was written. The current session's frozen system prompt is unchanged."
        )
        return tool_failure(
            "memory_manage",
            str(error_message or "Memory operation failed."),
            code="memory_error",
            data=result or None,
            meta=meta,
        )
    except Exception as exc:
        return tool_failure("memory_manage", str(exc))

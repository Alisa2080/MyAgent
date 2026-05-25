import json

from langchain.tools import tool
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from agent_core.memory import memory_store, memory_tool as run_memory_tool
from agent_tools.shared.tool_result import from_legacy_json, tool_failure


class MemoryManageInput(BaseModel):
    action: str = Field(description="Memory action: add, replace, or remove.")
    target: str = Field(description="Memory target: memory for project/agent notes, or user for user profile/preferences.")
    content: str = Field(default="", description="Entry content. Required for add and replace.")
    old_text: str = Field(default="", description="Short unique substring identifying the entry to replace or remove.")


def _memory_summary(payload: dict, meta: dict) -> str:
    action = meta.get("_action", "operation")
    if action == "read":
        return f"Memory read: {len(payload.get('entries', []))} entries."
    return "Memory updated."


def _memory_manage_impl(
    action: str,
    target: str,
    content: str = "",
    old_text: str = "",
) -> ToolMessage:
    raw = run_memory_tool(
        action=action,
        target=target,
        content=content or None,
        old_text=old_text or None,
        store=memory_store,
    )
    return from_legacy_json(
        "memory_manage",
        raw,
        success_message="Memory updated.",
        meta_keys=("available", "_action"),
    )


@tool("memory_manage", args_schema=MemoryManageInput)
def memory_manage(action: str, target: str, content: str = "", old_text: str = "") -> ToolMessage:
    """Save, replace, or remove durable memory. target must be 'memory' or 'user'."""
    try:
        result = _memory_manage_impl(action, target, content, old_text)
        if result.artifact["ok"]:
            meta = dict(result.artifact.get("meta") or {})
            meta["system_prompt_note"] = (
                "Memory change was written to disk. It will not modify the current session's "
                "frozen system prompt and will be available after the next agent session starts."
            )
            return ToolMessage(
                name="memory_manage",
                tool_call_id=result.tool_call_id or "",
                content=result.content,
                status="success",
                artifact={
                    **result.artifact,
                    "meta": meta,
                },
            )
        return result
    except Exception as exc:
        return tool_failure("memory_manage", str(exc))

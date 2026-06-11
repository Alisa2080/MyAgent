from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage

from agent_core.tool_catalog import ToolSpec


def limit_tool_result(spec: ToolSpec | None, result: ToolMessage) -> ToolMessage:
    if spec is None or spec.max_result_size_chars is None:
        return result
    limit = int(spec.max_result_size_chars)
    if limit <= 0:
        return result
    content = str(getattr(result, "content", "") or "")
    artifact = getattr(result, "artifact", None)
    serialized_artifact = serialize_for_limit_check(artifact)
    if len(content) <= limit and len(serialized_artifact) <= limit:
        return result

    truncated_artifact, artifact_was_truncated = truncate_artifact(artifact, limit)

    truncated_content = content
    if len(truncated_content) > limit:
        truncated_content = (
            truncated_content[:limit]
            + f"\n\n[truncated: original content was {len(content)} chars]"
        )
    if artifact_was_truncated:
        suffix = (
            f"[truncated: artifact exceeded {limit} chars"
            f"; original artifact was {len(serialized_artifact)} chars]"
        )
        truncated_content = f"{truncated_content}\n\n{suffix}" if truncated_content else suffix

    return ToolMessage(
        content=truncated_content,
        name=getattr(result, "name", spec.name),
        tool_call_id=getattr(result, "tool_call_id", ""),
        status=getattr(result, "status", "success"),
        artifact=truncated_artifact,
    )


def serialize_for_limit_check(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def truncate_artifact(artifact: Any, limit: int) -> tuple[Any, bool]:
    serialized = serialize_for_limit_check(artifact)
    if len(serialized) <= limit:
        return artifact, False
    return {
        "truncated": True,
        "original_chars": len(serialized),
        "preview": serialized[:limit],
    }, True

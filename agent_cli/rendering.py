from __future__ import annotations

from typing import Any


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or message.get("type") or "")
    return str(getattr(message, "type", "") or getattr(message, "role", ""))


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        return _content_text(message.get("content"))
    return _content_text(getattr(message, "content", ""))


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_content_text(block) for block in content]
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("content"), str):
            return content["content"]
        return ""
    return str(content)


def latest_ai_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result) if result is not None else ""
    messages = result.get("messages") or []
    for message in reversed(messages):
        role = _message_role(message)
        if role in {"assistant", "ai"} or message.__class__.__name__ == "AIMessage":
            return _message_content(message)
    final = result.get("final_response")
    return str(final) if final else ""


def latest_user_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    messages = result.get("messages") or []
    for message in reversed(messages):
        role = _message_role(message)
        if role == "user":
            return _message_content(message)
    return ""


def format_sessions(rows: list[Any]) -> str:
    if not rows:
        return "No sessions found."
    lines = ["Recent sessions:"]
    for row in rows:
        preview = f" - {row.last_message_preview}" if row.last_message_preview else ""
        lines.append(f"{row.session_id}  {row.updated_at}  {row.title}{preview}")
    return "\n".join(lines)


def _request_preview(request: Any) -> str:
    if not isinstance(request, dict):
        return str(request)
    name = str(request.get("name") or request.get("tool") or "request")
    args = request.get("args")
    if isinstance(args, dict):
        for key in ("command", "path", "query"):
            if key in args and args[key]:
                return f"{name}: {args[key]}"
    return f"{name}: {request}"


def format_interrupt_summary(requests: list[Any]) -> str:
    lines = ["Approval required:"]
    for idx, request in enumerate(requests, start=1):
        lines.append(f"  {idx}. {_request_preview(request)}")
    return "\n".join(lines)

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from dateutil import parser as dateutil_parser


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


def _parse_datetime(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value
    return dateutil_parser.isoparse(value)


def _truncate_text(text: str, *, max_length: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."


def format_relative_time(value: str | datetime, *, now: datetime | None = None) -> str:
    target = _parse_datetime(value)
    reference = now or datetime.now(timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    delta_seconds = max(0, int((reference - target).total_seconds()))
    if delta_seconds < 60:
        return "just now"
    if delta_seconds < 3600:
        return f"{delta_seconds // 60}m ago"
    if delta_seconds < 86400:
        return f"{delta_seconds // 3600}h ago"
    return f"{delta_seconds // 86400}d ago"


def format_session_picker_label(row: Any, *, now: datetime | None = None, max_prompt_length: int = 60) -> str:
    prompt = getattr(row, "first_user_prompt_preview", "") or getattr(row, "title", "") or "New session"
    return f"{format_relative_time(row.updated_at, now=now)}  {_truncate_text(prompt, max_length=max_prompt_length)}"


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


def format_sessions(rows: list[Any], *, now: datetime | None = None) -> str:
    if not rows:
        return "No sessions found."
    lines = ["Recent sessions:"]
    for row in rows:
        lines.append(format_session_picker_label(row, now=now, max_prompt_length=72))
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

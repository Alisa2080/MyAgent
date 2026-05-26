from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dateutil import parser as dateutil_parser

from agent_cli.session_store import SessionStore


@dataclass
class SessionStatus:
    session_id: str
    title: str
    created_at: datetime
    message_count: int
    turn_count: int
    model_name: str | None


def _parse_datetime(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    return dateutil_parser.isoparse(value)


class Session:
    def __init__(
        self,
        *,
        session_store: SessionStore,
        session_id: str,
        model_name: str | None = None,
        session_store_for_checkpoints: Any = None,
    ):
        self.session_store = session_store
        self.session_id = session_id
        self.model_name = model_name
        self.session_store_for_checkpoints = session_store_for_checkpoints
        self._record = session_store.create_session(
            session_id=session_id,
            workdir=".",
            model=model_name,
        )

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Get session history from checkpointer."""
        from agent_cli.checkpoints import extract_messages_from_checkpoints

        checkpointer = self.session_store_for_checkpoints
        messages = extract_messages_from_checkpoints(checkpointer, self.session_id)
        if limit:
            return messages[-limit:]
        return messages

    def export_markdown(self, path: str | Path) -> str:
        """Export session history to a Markdown file."""
        from pathlib import Path

        messages = self.history()
        lines = [f"# Session: {self.session_id}", ""]

        for msg in messages:
            role = msg.get("type", msg.get("role", "unknown"))
            content = msg.get("content", "")

            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = "\n".join(text_parts)
            elif isinstance(content, dict):
                content = content.get("text", str(content))

            lines.append(f"## {role.upper()}")
            lines.append("")
            lines.append(str(content))
            lines.append("")

        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines))

        return f"Exported {len(messages)} messages to {path}"

    def status(self) -> SessionStatus:
        record = self.session_store.get_session(self.session_id)
        return SessionStatus(
            session_id=self.session_id,
            title=record.title if record else "New session",
            created_at=_parse_datetime(record.created_at if record else None),
            message_count=0,
            turn_count=0,
            model_name=self.model_name,
        )

    def set_title(self, title: str) -> None:
        self.session_store.touch_session(self.session_id, title=title)
        self._record = self.session_store.get_session(self.session_id)


def render_session_status(session: Session) -> str:
    status = session.status()
    return "\n".join([
        "Session Status:",
        f"  Session ID: {status.session_id}",
        f"  Title: {status.title}",
        f"  Created: {status.created_at.strftime('%Y-%m-%d %H:%M')}",
        f"  Model: {status.model_name or 'default'}",
        f"  Turns: {status.turn_count}",
    ]) + "\n"


def update_session_title(session: Session, title: str) -> str:
    if not title.strip():
        return "Usage: /title <name>\n"
    session.set_title(title)
    return f"Session title set to: {title}\n"


def render_history(session: Session, limit: int | None = None) -> str:
    """Render session history as formatted text."""
    messages = session.history(limit=limit)
    if not messages:
        return "No messages in session history.\n"

    lines = ["Session History:", ""]
    for i, msg in enumerate(messages, 1):
        role = msg.get("type", msg.get("role", "unknown"))
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            content = "\n".join(text_parts)
        elif isinstance(content, dict):
            content = content.get("text", str(content))

        lines.append(f"[{i}] {role.upper()}:")
        content_str = str(content)
        if len(content_str) > 200:
            content_str = content_str[:200] + "..."
        lines.append(f"    {content_str}")
        lines.append("")

    return "\n".join(lines)


def export_to_markdown(session: Session, path: str) -> str:
    """Export session history to a Markdown file."""
    try:
        return session.export_markdown(path)
    except Exception as e:
        return f"Export failed: {e}\n"

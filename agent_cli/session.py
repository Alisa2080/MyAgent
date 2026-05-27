from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dateutil import parser as dateutil_parser

from agent_cli.history import (
    load_thread_transcript,
    render_history_markdown,
    render_history_text,
    transcript_stats,
)
from agent_cli.session_store import SessionStore


def _parse_datetime(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    return dateutil_parser.isoparse(value)


@dataclass
class SessionStatus:
    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    workdir: str
    message_count: int
    turn_count: int
    model_name: str | None
    profile: str | None = None
    display_theme: str = "default"
    cli_home: str | None = None
    db_path: str | None = None
    checkpointer_available: bool = False


class Session:
    def __init__(
        self,
        *,
        session_store: SessionStore,
        session_id: str,
        model_name: str | None = None,
        session_store_for_checkpoints: Any = None,
        workdir: str = ".",
        profile: str | None = None,
        display_theme: str = "default",
        cli_home: str | None = None,
        db_path: str | None = None,
    ):
        self.session_store = session_store
        self.session_id = session_id
        self.model_name = model_name
        self.session_store_for_checkpoints = session_store_for_checkpoints
        self.workdir = workdir
        self.profile = profile
        self.display_theme = display_theme
        self.cli_home = cli_home
        self.db_path = db_path
        self._record = session_store.get_or_create_session(
            session_id=session_id,
            workdir=workdir,
            model=model_name,
        )

    def transcript(self):
        return load_thread_transcript(self.session_store_for_checkpoints, self.session_id)

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        transcript = self.transcript()
        if limit:
            transcript = transcript[-limit:]
        return [
            {"role": message.role, "content": message.content}
            for message in transcript
        ]

    def export_markdown(self, path: str | Path) -> str:
        transcript = self.transcript()
        if not transcript:
            return "No messages to export."

        record = self.session_store.get_session(self.session_id)
        title = record.title if record else "New session"
        output_path = Path(path)
        if not output_path.is_absolute():
            output_path = Path(self.workdir) / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        exported_at = datetime.now(timezone.utc).isoformat()
        output_path.write_text(
            render_history_markdown(
                transcript,
                session_id=self.session_id,
                title=title,
                workdir=self.workdir,
                model=self.model_name,
                exported_at=exported_at,
            ),
            encoding="utf-8",
        )
        return f"Exported {len(transcript)} messages to {output_path}"

    def status(self) -> SessionStatus:
        record = self.session_store.get_session(self.session_id)
        transcript = self.transcript()
        stats = transcript_stats(transcript)
        return SessionStatus(
            session_id=self.session_id,
            title=record.title if record else "New session",
            created_at=_parse_datetime(record.created_at if record else None),
            updated_at=_parse_datetime(record.updated_at if record else None),
            workdir=record.workdir if record else self.workdir,
            message_count=stats.message_count,
            turn_count=stats.turn_count,
            model_name=self.model_name,
            profile=self.profile,
            display_theme=self.display_theme,
            cli_home=self.cli_home,
            db_path=self.db_path,
            checkpointer_available=self.session_store_for_checkpoints is not None,
        )

    def set_title(self, title: str) -> None:
        self.session_store.touch_session(self.session_id, title=title)
        self._record = self.session_store.get_session(self.session_id)


def render_session_status(session: Session) -> str:
    status = session.status()
    return "\n".join(
        [
            "Session Status:",
            f"  Session ID: {status.session_id}",
            f"  Title: {status.title}",
            f"  Created: {status.created_at.strftime('%Y-%m-%d %H:%M')}",
            f"  Updated: {status.updated_at.strftime('%Y-%m-%d %H:%M')}",
            f"  Workdir: {status.workdir}",
            f"  Model: {status.model_name or 'default'}",
            f"  Profile: {status.profile or 'default'}",
            f"  Theme: {status.display_theme}",
            f"  CLI Home: {status.cli_home or 'default'}",
            f"  DB Path: {status.db_path or 'default'}",
            f"  Messages: {status.message_count}",
            f"  Turns: {status.turn_count}",
            f"  Checkpointer: {'available' if status.checkpointer_available else 'unavailable'}",
        ]
    ) + "\n"


def update_session_title(session: Session, title: str) -> str:
    if not title.strip():
        return "Usage: /title <name>\n"
    session.set_title(title)
    return f"Session title set to: {title}\n"


def render_history(session: Session, limit: int | None = None) -> str:
    transcript = session.transcript()
    if limit:
        transcript = transcript[-limit:]
    return render_history_text(transcript)


def export_to_markdown(session: Session, path: str) -> str:
    try:
        return session.export_markdown(path)
    except Exception as e:
        return f"Export failed: {e}\n"

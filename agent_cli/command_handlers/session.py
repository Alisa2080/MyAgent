from __future__ import annotations

import os

from agent_cli.rendering import format_sessions
from agent_cli.session import (
    Session,
    export_to_markdown,
    render_history,
    render_session_status,
    update_session_title,
)
from agent_cli.text_input import render_usage_summary


def session_handlers():
    return {
        "new": handle_new,
        "sessions": handle_sessions,
        "resume": handle_resume,
        "status": handle_status,
        "title": handle_title,
        "history": handle_history,
        "export": handle_export,
        "clear": handle_clear,
        "retry": handle_retry,
        "usage": handle_usage,
    }


def handle_new(cli, arg, command):
    record = cli.session_store.create_session(
        workdir=cli.workdir,
        model=cli.model_name,
        title=cli.default_title,
    )
    cli._set_session(record.session_id)
    return f"Started session: {record.session_id}"


def handle_sessions(cli, arg, command):
    return format_sessions(cli.session_store.list_sessions())


def handle_resume(cli, arg, command):
    if not arg:
        return "Usage: /resume <session_id>"
    record = cli.session_store.get_session(arg)
    if record is None:
        return f"Unknown session: {arg}"
    cli.session_store.touch_session(record.session_id)
    cli._set_session(record.session_id)
    return f"Resumed session: {record.session_id}"


def handle_status(cli, arg, command):
    if cli.session is None:
        cli.ensure_session()
    return render_session_status(cli.session)


def handle_title(cli, arg, command):
    if cli.session is None:
        cli.ensure_session()
    return update_session_title(cli.session, arg)


def handle_history(cli, arg, command):
    if cli.session is None:
        cli.ensure_session()
    limit = None
    if arg:
        try:
            limit = int(arg)
        except ValueError:
            return "Usage: /history [N]"
    return render_history(cli.session, limit=limit)


def handle_export(cli, arg, command):
    if cli.session is None:
        cli.ensure_session()
    if not arg:
        return "Usage: /export <path.md>\n"
    return export_to_markdown(cli.session, arg)


def handle_clear(cli, arg, command):
    os.system("cls" if os.name == "nt" else "clear")
    return None


def handle_retry(cli, arg, command):
    if not cli.last_user_message:
        return "No user message available to retry."
    return cli.submit_message(cli.last_user_message)


def handle_usage(cli, arg, command):
    if arg:
        return "Usage: /usage"
    if cli.session is None:
        cli.ensure_session()
    transcript = []
    if cli.session is not None:
        try:
            transcript = cli.session.transcript()
        except Exception:
            transcript = []
    transcript_text = "\n".join(message.content for message in transcript)
    return render_usage_summary(
        session_id=cli.session_id or "",
        model_name=cli.model_name,
        message_count=len(transcript),
        turn_count=sum(1 for message in transcript if message.role == "user"),
        transcript_text=transcript_text,
        checkpointer_available=cli.checkpointer is not None,
        last_call_elapsed_seconds=cli.last_call_elapsed_seconds,
        assistant_reply_count=len(cli.assistant_replies),
        usage_metadata=cli.last_usage_metadata,
    )

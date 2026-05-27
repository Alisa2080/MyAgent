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


def handle_new(ctx, arg, command):
    record = ctx.session_store.create_session(
        workdir=ctx.workdir,
        model=ctx.model_name,
        title=ctx.default_title,
    )
    ctx.set_session(record.session_id)
    return f"Started session: {record.session_id}"


def handle_sessions(ctx, arg, command):
    return format_sessions(ctx.session_store.list_sessions())


def handle_resume(ctx, arg, command):
    if not arg:
        return "Usage: /resume <session_id>"
    record = ctx.session_store.get_session(arg)
    if record is None:
        return f"Unknown session: {arg}"
    ctx.session_store.touch_session(record.session_id)
    ctx.set_session(record.session_id)
    return f"Resumed session: {record.session_id}"


def handle_status(ctx, arg, command):
    if ctx.session is None:
        ctx.ensure_session()
    return render_session_status(ctx.session)


def handle_title(ctx, arg, command):
    if ctx.session is None:
        ctx.ensure_session()
    return update_session_title(ctx.session, arg)


def handle_history(ctx, arg, command):
    if ctx.session is None:
        ctx.ensure_session()
    limit = None
    if arg:
        try:
            limit = int(arg)
        except ValueError:
            return "Usage: /history [N]"
    return render_history(ctx.session, limit=limit)


def handle_export(ctx, arg, command):
    if ctx.session is None:
        ctx.ensure_session()
    if not arg:
        return "Usage: /export <path.md>\n"
    return export_to_markdown(ctx.session, arg)


def handle_clear(ctx, arg, command):
    os.system("cls" if os.name == "nt" else "clear")
    return None


def handle_retry(ctx, arg, command):
    if not ctx.last_user_message:
        return "No user message available to retry."
    return ctx.submit_message(ctx.last_user_message)


def handle_usage(ctx, arg, command):
    if arg:
        return "Usage: /usage"
    if ctx.session is None:
        ctx.ensure_session()
    transcript = []
    if ctx.session is not None:
        try:
            transcript = ctx.session.transcript()
        except Exception:
            transcript = []
    transcript_text = "\n".join(message.content for message in transcript)
    return render_usage_summary(
        session_id=ctx.session_id or "",
        model_name=ctx.model_name,
        message_count=len(transcript),
        turn_count=sum(1 for message in transcript if message.role == "user"),
        transcript_text=transcript_text,
        checkpointer_available=ctx.checkpointer is not None,
        last_call_elapsed_seconds=ctx.last_call_elapsed_seconds,
        assistant_reply_count=len(ctx.assistant_replies),
        usage_metadata=ctx.last_usage_metadata,
    )

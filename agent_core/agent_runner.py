from __future__ import annotations

import logging
import os
from typing import Any

from agent_core.progress import (
    FallbackEvent,
    ModelStartEvent,
    ProgressObserver,
    TokenDeltaEvent,
    TurnCompleteEvent,
    emit_progress,
    mark_streamed_output,
    set_progress_observer,
)
from agent_core.session_context import RuntimeContext
from agent_core.terminal_lifecycle import (
    cleanup_task_resources_for_thread_id,
    terminal_execution_scope,
)
from agent_core.process_lifecycle import is_process_shutdown_requested
from agent_core.terminal_notifications import (
    drain_terminal_notifications_for_thread_id,
    format_terminal_notification_message,
)

DEFAULT_MAX_AUTO_RESUMES = 3
MAX_AUTO_RESUMES_ENV = "TERMINAL_MAX_AUTO_RESUMES"
PER_TURN_CLEANUP_ENV = "TERMINAL_PER_TURN_CLEANUP"
logger = logging.getLogger(__name__)


def _thread_id_from_config(config: dict[str, Any] | None) -> str | None:
    return RuntimeContext.from_config(config).thread_id


def max_terminal_auto_resumes() -> int:
    raw = os.environ.get(MAX_AUTO_RESUMES_ENV)
    if raw is None:
        return DEFAULT_MAX_AUTO_RESUMES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_AUTO_RESUMES
    return max(0, value)


def per_turn_cleanup_enabled() -> bool:
    raw = os.environ.get(PER_TURN_CLEANUP_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _invoke_agent_turn(
    agent: Any,
    input_data: dict[str, Any],
    config: dict[str, Any] | None,
    thread_id: str | None,
    *,
    cleanup_reason: str,
    observer: ProgressObserver | None = None,
) -> Any:
    try:
        with terminal_execution_scope(thread_id):
            return _invoke_or_stream_agent(agent, input_data, config, thread_id, observer)
    finally:
        if thread_id and per_turn_cleanup_enabled():
            try:
                cleanup_task_resources_for_thread_id(thread_id, reason=cleanup_reason)
            except Exception:
                logger.exception("Failed to run per-turn terminal cleanup for thread %s.", thread_id)


def _invoke_or_stream_agent(
    agent: Any,
    input_data: dict[str, Any],
    config: dict[str, Any] | None,
    thread_id: str | None,
    observer: ProgressObserver | None,
) -> Any:
    if observer is None:
        return agent.invoke(input_data, config)

    stream = getattr(agent, "stream", None)
    if not callable(stream):
        emit_progress(observer, FallbackEvent("stream unavailable", thread_id=thread_id))
        return agent.invoke(input_data, config)

    emit_progress(observer, ModelStartEvent(thread_id=thread_id))
    streamed_text = False
    final_result: Any = None
    stream_config = set_progress_observer(config, observer)

    for chunk in stream(input_data, stream_config):
        final_result = chunk
        for text in _extract_token_deltas(chunk):
            if not text:
                continue
            streamed_text = True
            emit_progress(observer, TokenDeltaEvent(text=text, thread_id=thread_id))

    emit_progress(observer, TurnCompleteEvent(thread_id=thread_id, streamed_output=streamed_text))
    if final_result is None:
        return mark_streamed_output({"messages": []}) if streamed_text else {"messages": []}
    return mark_streamed_output(final_result) if streamed_text else final_result


def _extract_token_deltas(chunk: Any) -> list[str]:
    messages = _chunk_messages(chunk)
    if messages:
        text_parts = []
        for message in messages:
            if _is_ai_chunk(message):
                text_parts.append(_message_content(message))
        return [part for part in text_parts if part]
    if _is_ai_chunk(chunk):
        text = _message_content(chunk)
        return [text] if text else []
    return []


def _chunk_messages(chunk: Any) -> list[Any]:
    if isinstance(chunk, dict):
        value = chunk.get("messages")
        if isinstance(value, list):
            return value
        if value is not None:
            return [value]
    value = getattr(chunk, "messages", None)
    if isinstance(value, list):
        return value
    if value is not None:
        return [value]
    return []


def _is_ai_chunk(message: Any) -> bool:
    if isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "")
        return role == "AIMessageChunk"
    name = message.__class__.__name__
    role = str(getattr(message, "type", "") or getattr(message, "role", ""))
    return name == "AIMessageChunk" or role == "AIMessageChunk"


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", "")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content)


def invoke_agent_with_terminal_notifications(
    agent: Any,
    input_data: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    max_auto_resumes: int | None = None,
    observer: ProgressObserver | None = None,
) -> Any:
    """Invoke an agent and continue the same thread for terminal notifications.

    When the resume limit is reached, the runner intentionally stops draining so
    later terminal notifications remain queued for a future invocation.
    """
    thread_id = _thread_id_from_config(config)
    resume_limit = (
        max_terminal_auto_resumes()
        if max_auto_resumes is None
        else max(0, max_auto_resumes)
    )

    result = _invoke_agent_turn(
        agent,
        input_data,
        config,
        thread_id,
        cleanup_reason="turn_finished",
        observer=observer,
    )

    if not thread_id or is_process_shutdown_requested():
        return result

    for _ in range(resume_limit):
        if is_process_shutdown_requested():
            break

        events = drain_terminal_notifications_for_thread_id(thread_id)
        if not events:
            break

        message = format_terminal_notification_message(events)
        if not message:
            break

        if is_process_shutdown_requested():
            break

        resume_input = {"messages": [{"role": "user", "content": message}]}
        result = _invoke_agent_turn(
            agent,
            resume_input,
            config,
            thread_id,
            cleanup_reason="terminal_notification_resume_finished",
            observer=observer,
        )

    return result

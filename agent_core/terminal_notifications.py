from __future__ import annotations

from collections import defaultdict, deque
from queue import Empty
import threading
import time
from typing import Any

from agent_core.session_context import hermes_task_id_from_thread_id
from agent_tools.hermes_terminal_toolkit.process_registry import process_registry

_GLOBAL_TASK_ID = "__global__"
_UNROUTED_TASK_ID = "__unrouted__"
_MAX_PENDING_EVENTS_PER_TASK = 100
_EVENT_TTL_SECONDS = 3600
DEFAULT_MAX_NOTIFICATION_EVENTS = 10
DEFAULT_MAX_MESSAGE_CHARS = 6000
_MAX_EVENT_TYPE_CHARS = 80
_MAX_SESSION_ID_CHARS = 120
_MAX_COMMAND_CHARS = 240
_MAX_PATTERN_CHARS = 240
_MAX_MESSAGE_FIELD_CHARS = 300
_MAX_SMALL_FIELD_CHARS = 40
_QUEUED_AT_KEY = "_queued_at"
_pending_events_lock = threading.Lock()
_pending_events_by_task: dict[str, deque[dict[str, Any]]] = defaultdict(deque)


def _event_task_id(event: dict[str, Any]) -> str | None:
    task_id = event.get("task_id")
    if isinstance(task_id, str) and task_id:
        return task_id

    thread_id = event.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        return hermes_task_id_from_thread_id(thread_id)

    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id:
        session = process_registry.get(session_id)
        if session is not None and getattr(session, "task_id", None):
            return str(session.task_id)

    event_type = event.get("type")
    if event_type in {"watch_overflow_tripped", "watch_overflow_released"}:
        return _GLOBAL_TASK_ID

    return None


def _pending_queue_for_task(task_id: str) -> deque[dict[str, Any]]:
    """Return a task queue. Caller must hold _pending_events_lock."""
    queue = _pending_events_by_task.get(task_id)
    if queue is None:
        queue = deque()
        _pending_events_by_task[task_id] = queue
    return queue


def _append_pending_event(task_id: str, event: dict[str, Any], now: float) -> None:
    """Append a timestamped event with bounded retention.

    Caller must hold _pending_events_lock.
    """
    queue = _pending_queue_for_task(task_id)
    buffered_event = dict(event)
    buffered_event[_QUEUED_AT_KEY] = now
    queue.append(buffered_event)
    while len(queue) > _MAX_PENDING_EVENTS_PER_TASK:
        queue.popleft()


def _prune_expired_locked(now: float) -> None:
    """Drop expired pending events. Caller must hold _pending_events_lock."""
    expires_before = now - _EVENT_TTL_SECONDS
    empty_task_ids: list[str] = []

    for task_id, queue in _pending_events_by_task.items():
        while queue and float(queue[0].get(_QUEUED_AT_KEY, 0.0)) < expires_before:
            queue.popleft()
        if not queue:
            empty_task_ids.append(task_id)

    for task_id in empty_task_ids:
        _pending_events_by_task.pop(task_id, None)


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    public = dict(event)
    public.pop(_QUEUED_AT_KEY, None)
    return public


def _completion_was_consumed(event: dict[str, Any]) -> bool:
    if event.get("type") != "completion":
        return False

    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return False

    is_completion_consumed = getattr(process_registry, "is_completion_consumed", None)
    if not callable(is_completion_consumed):
        return False

    try:
        return bool(is_completion_consumed(session_id))
    except Exception:
        return False


def _drain_completion_queue(*, max_drain: int = 100) -> None:
    now = time.time()
    drained = 0
    with _pending_events_lock:
        _prune_expired_locked(now)
        while drained < max_drain:
            try:
                event = process_registry.completion_queue.get_nowait()
            except Empty:
                return
            drained += 1
            if not isinstance(event, dict):
                continue
            if _completion_was_consumed(event):
                continue
            task_id = _event_task_id(event) or _UNROUTED_TASK_ID
            _append_pending_event(task_id, event, now)


def drain_terminal_notifications_for_thread_id(
    thread_id: str | None,
    *,
    max_drain: int = 100,
    max_events: int = 10,
    include_global: bool = True,
) -> list[dict[str, Any]]:
    """Return queued Hermes terminal events for this LangGraph thread.

    This drains the global Hermes queue into per-task buffers without dropping
    events for other sessions.
    """
    if not thread_id:
        return []

    _drain_completion_queue(max_drain=max_drain)
    task_id = hermes_task_id_from_thread_id(thread_id)
    events: list[dict[str, Any]] = []

    with _pending_events_lock:
        _prune_expired_locked(time.time())
        task_queue = _pending_queue_for_task(task_id)

        while task_queue and len(events) < max_events:
            events.append(_public_event(task_queue.popleft()))

        if include_global:
            global_queue = _pending_queue_for_task(_GLOBAL_TASK_ID)
            while global_queue and len(events) < max_events:
                events.append(_public_event(global_queue.popleft()))

    return events


def _shorten(value: Any, *, max_chars: int) -> str:
    text = "" if value is None else str(value)
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    suffix = "\n...(truncated)"
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return text[: max_chars - len(suffix)].rstrip() + suffix


def _sanitize_inline(value: Any, *, max_chars: int) -> str:
    text = "" if value is None else str(value)
    sanitized_chars: list[str] = []
    for char in text:
        if char == "\n":
            sanitized_chars.append("\\n")
        elif char == "\r":
            sanitized_chars.append("\\r")
        elif char == "\t":
            sanitized_chars.append("\\t")
        elif ord(char) < 32 or ord(char) == 127:
            sanitized_chars.append(f"\\x{ord(char):02x}")
        else:
            sanitized_chars.append(char)
    return _shorten("".join(sanitized_chars), max_chars=max_chars)


def _indent_untrusted_output(output: str) -> str:
    return "\n".join(f"    {line}" for line in output.splitlines())


def _format_event(event: dict[str, Any], *, max_output_chars: int) -> str:
    raw_event_type = str(event.get("type") or "unknown")
    event_type = _sanitize_inline(raw_event_type, max_chars=_MAX_EVENT_TYPE_CHARS)
    session_id = _sanitize_inline(event.get("session_id") or "", max_chars=_MAX_SESSION_ID_CHARS)
    command = _sanitize_inline(event.get("command") or "", max_chars=_MAX_COMMAND_CHARS)

    parts = [f"- type={event_type}"]
    if session_id:
        parts.append(f"session_id={session_id}")
    if raw_event_type == "completion":
        exit_code = _sanitize_inline(event.get("exit_code"), max_chars=_MAX_SMALL_FIELD_CHARS)
        parts.append(f"exit_code={exit_code}")
    if raw_event_type == "watch_match":
        pattern = _sanitize_inline(event.get("pattern"), max_chars=_MAX_PATTERN_CHARS)
        suppressed = _sanitize_inline(event.get("suppressed", 0), max_chars=_MAX_SMALL_FIELD_CHARS)
        parts.append(f"pattern={pattern}")
        parts.append(f"suppressed={suppressed}")
    if raw_event_type in {"watch_disabled", "watch_overflow_tripped", "watch_overflow_released"}:
        message = event.get("message")
        if message:
            parts.append(f"message={_sanitize_inline(message, max_chars=_MAX_MESSAGE_FIELD_CHARS)}")

    header = " ".join(parts)
    output = _shorten(event.get("output") or "", max_chars=max_output_chars)
    body = f"{header}\n  command: {command}"
    if output:
        body += f"\n  untrusted_output:\n{_indent_untrusted_output(output)}"
    return body


def _build_notification_message(
    body: str,
    *,
    max_message_chars: int,
    preserved_body_suffix: str = "",
) -> str:
    intro = (
        "[IMPORTANT: Background terminal update]\n"
        "One or more Hermes background processes produced notifications for this conversation.\n"
    )
    end_marker = "\n\nEnd background terminal update.\n"
    footer = (
        "Decide whether to inspect logs with process(action='log'|'poll'), continue the task, "
        "report completion to the user, or kill the process if it is no longer needed."
    )
    message = f"{intro}{body}{end_marker}{footer}"
    if max_message_chars <= 0:
        return ""
    if len(message) <= max_message_chars:
        return message

    truncation_notice = "\n... notification message truncated ..."
    body_budget = max_message_chars - len(intro) - len(end_marker) - len(footer) - len(truncation_notice)
    if body_budget <= 0:
        return _shorten(message, max_chars=max_message_chars)

    bounded_body = _shorten(body, max_chars=body_budget)
    if preserved_body_suffix and body.endswith(preserved_body_suffix):
        separator = "\n\n"
        suffix_budget = len(separator) + len(preserved_body_suffix)
        prefix_budget = body_budget - suffix_budget
        if prefix_budget > 0:
            prefix = body[: -len(preserved_body_suffix)].rstrip()
            bounded_body = (
                _shorten(prefix, max_chars=prefix_budget)
                + separator
                + preserved_body_suffix
            )
    return f"{intro}{bounded_body}{truncation_notice}{end_marker}{footer}"


def format_terminal_notification_message(
    events: list[dict[str, Any]],
    *,
    max_output_chars: int = 1200,
    max_events: int = DEFAULT_MAX_NOTIFICATION_EVENTS,
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
) -> str:
    """Format Hermes background events as one model-visible continuation message."""
    if not events:
        return ""

    bounded_max_events = max(0, max_events)
    events_to_format = events[:bounded_max_events]
    formatted_events = "\n\n".join(
        _format_event(event, max_output_chars=max_output_chars)
        for event in events_to_format
    )
    omitted_count = len(events) - len(events_to_format)
    omitted_line = ""
    if omitted_count > 0:
        omitted_line = f"... {omitted_count} additional event(s) omitted ..."
        formatted_events = f"{formatted_events}\n\n{omitted_line}" if formatted_events else omitted_line

    return _build_notification_message(
        formatted_events,
        max_message_chars=max_message_chars,
        preserved_body_suffix=omitted_line,
    )

from __future__ import annotations

from collections import defaultdict, deque
import threading
import time
from typing import Any

SILENT_MARKER = "[SILENT]"

_MAX_PENDING_EVENTS_PER_THREAD = 100
_EVENT_TTL_SECONDS = 3600
_QUEUED_AT_KEY = "_queued_at"

_events_lock = threading.Lock()
_events_by_thread: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
_INLINE_FIELD_MAX_CHARS = 200


def should_notify(final_response: str | None) -> bool:
    return not str(final_response or "").lstrip().startswith(SILENT_MARKER)


def _prune_locked(now: float) -> None:
    expires_before = now - _EVENT_TTL_SECONDS
    empty_thread_ids = []
    for thread_id, queue in list(_events_by_thread.items()):
        while queue and float(queue[0].get(_QUEUED_AT_KEY, 0.0)) < expires_before:
            queue.popleft()
        if not queue:
            empty_thread_ids.append(thread_id)
    for thread_id in empty_thread_ids:
        _events_by_thread.pop(thread_id, None)


def queue_cron_notification(thread_id: str | None, event: dict[str, Any]) -> None:
    if not thread_id:
        return

    now = time.time()
    queued_event = dict(event)
    queued_event[_QUEUED_AT_KEY] = now

    with _events_lock:
        _prune_locked(now)
        queue = _events_by_thread.setdefault(str(thread_id), deque())
        queue.append(queued_event)
        while len(queue) > _MAX_PENDING_EVENTS_PER_THREAD:
            queue.popleft()


def drain_cron_notifications_for_thread_id(
    thread_id: str | None,
    max_events: int = 10,
) -> list[dict[str, Any]]:
    if not thread_id:
        return []

    events: list[dict[str, Any]] = []
    with _events_lock:
        _prune_locked(time.time())
        queue = _events_by_thread.get(str(thread_id))
        if queue is None:
            return []

        while queue and len(events) < max(0, max_events):
            event = dict(queue.popleft())
            event.pop(_QUEUED_AT_KEY, None)
            events.append(event)
        if not queue:
            _events_by_thread.pop(str(thread_id), None)

    return events


def _shorten(text: Any, max_chars: int) -> str:
    if max_chars <= 0:
        return ""

    value = "" if text is None else str(text)
    if len(value) <= max_chars:
        return value

    marker = "\n...(truncated)"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return value[: max_chars - len(marker)].rstrip() + marker


def _shorten_inline(text: Any, max_chars: int) -> str:
    if max_chars <= 0:
        return ""

    value = "" if text is None else str(text)
    if len(value) <= max_chars:
        return value

    marker = "...(truncated)"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return value[: max_chars - len(marker)].rstrip() + marker


def _sanitize_inline_field(value: Any, max_chars: int = _INLINE_FIELD_MAX_CHARS) -> str:
    sanitized = []
    for char in "" if value is None else str(value):
        if char == "\n":
            sanitized.append("\\n")
        elif char == "\r":
            sanitized.append("\\r")
        elif char == "\t":
            sanitized.append("\\t")
        elif ord(char) < 32 or ord(char) == 127:
            sanitized.append(f"\\x{ord(char):02x}")
        else:
            sanitized.append(char)
    return _shorten_inline("".join(sanitized), max_chars)


def format_cron_notification_message(
    events: list[dict[str, Any]],
    max_message_chars: int = 6000,
) -> str:
    if not events:
        return ""

    event_messages = []
    for event in events:
        job_name = _sanitize_inline_field(event.get("job_name") or "(unnamed)")
        job_id = _sanitize_inline_field(event.get("job_id") or "(unknown)")
        status = _sanitize_inline_field(event.get("status") or "unknown")
        output_path = _sanitize_inline_field(event.get("output_path")) if event.get("output_path") else None
        final_response = event.get("final_response")
        if final_response:
            detail_label = "final_response"
            detail = final_response
        else:
            detail_label = "error"
            detail = event.get("error", "")

        header = f"- job_name={job_name} job_id={job_id} status={status}"
        if output_path:
            header += f" output_path={output_path}"

        body = _shorten(detail, 1200).replace("\n", "\n    ")
        event_messages.append(f"{header}\n  {detail_label}:\n    {body}")

    message = (
        "[IMPORTANT: Cron job update]\n"
        "One or more scheduled cron jobs produced results for this thread.\n\n"
        + "\n\n".join(event_messages)
        + "\n\nEnd cron job update."
    )
    return _shorten(message, max_message_chars)

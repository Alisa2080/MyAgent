from __future__ import annotations

import os
from typing import Any

from agent_core.terminal_lifecycle import terminal_execution_scope
from agent_core.terminal_notifications import (
    drain_terminal_notifications_for_thread_id,
    format_terminal_notification_message,
)

DEFAULT_MAX_AUTO_RESUMES = 3
MAX_AUTO_RESUMES_ENV = "HERMES_TERMINAL_MAX_AUTO_RESUMES"


def _thread_id_from_config(config: dict[str, Any] | None) -> str | None:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    value = configurable.get("thread_id")
    return str(value) if value else None


def max_terminal_auto_resumes() -> int:
    raw = os.environ.get(MAX_AUTO_RESUMES_ENV)
    if raw is None:
        return DEFAULT_MAX_AUTO_RESUMES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_AUTO_RESUMES
    return max(0, value)


def invoke_agent_with_terminal_notifications(
    agent: Any,
    input_data: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    max_auto_resumes: int | None = None,
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

    with terminal_execution_scope(thread_id):
        result = agent.invoke(input_data, config)

    if not thread_id:
        return result

    for _ in range(resume_limit):
        events = drain_terminal_notifications_for_thread_id(thread_id)
        if not events:
            break

        message = format_terminal_notification_message(events)
        if not message:
            break

        resume_input = {"messages": [{"role": "user", "content": message}]}
        with terminal_execution_scope(thread_id):
            result = agent.invoke(resume_input, config)

    return result

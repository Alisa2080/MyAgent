from __future__ import annotations

import os
from typing import Any

from agent_core.terminal_lifecycle import (
    cleanup_task_resources_for_thread_id,
    terminal_execution_scope,
)
from agent_core.terminal_notifications import (
    drain_terminal_notifications_for_thread_id,
    format_terminal_notification_message,
)

DEFAULT_MAX_AUTO_RESUMES = 3
MAX_AUTO_RESUMES_ENV = "HERMES_TERMINAL_MAX_AUTO_RESUMES"
PER_TURN_CLEANUP_ENV = "HERMES_TERMINAL_PER_TURN_CLEANUP"


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
) -> Any:
    try:
        with terminal_execution_scope(thread_id):
            return agent.invoke(input_data, config)
    finally:
        if thread_id and per_turn_cleanup_enabled():
            cleanup_task_resources_for_thread_id(thread_id, reason=cleanup_reason)


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

    result = _invoke_agent_turn(
        agent,
        input_data,
        config,
        thread_id,
        cleanup_reason="turn_finished",
    )

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
        result = _invoke_agent_turn(
            agent,
            resume_input,
            config,
            thread_id,
            cleanup_reason="terminal_notification_resume_finished",
        )

    return result

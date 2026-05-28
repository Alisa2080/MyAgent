"""Direct Python wrappers around the standalone terminal toolkit."""

from __future__ import annotations

from typing import List, Optional

from .process_registry import process_tool
from .terminal_tool import terminal_tool


def run_terminal(
    command: str,
    background: bool = False,
    timeout: Optional[int] = None,
    task_id: str = "default",
    workdir: Optional[str] = None,
    pty: bool = False,
    notify_on_complete: bool = False,
    watch_patterns: Optional[List[str]] = None,
    force: bool = False,
    allow_network_once: bool = False,
) -> str:
    return terminal_tool(
        command=command,
        background=background,
        timeout=timeout,
        task_id=task_id,
        workdir=workdir,
        pty=pty,
        notify_on_complete=notify_on_complete,
        watch_patterns=watch_patterns,
        force=force,
        allow_network_once=allow_network_once,
    )


def run_process(
    action: str,
    session_id: str = "",
    data: str = "",
    timeout: Optional[int] = None,
    offset: int = 0,
    limit: int = 200,
    task_id: str = "default",
) -> str:
    return process_tool(
        {
            "action": action,
            "session_id": session_id,
            "data": data,
            "timeout": timeout,
            "offset": offset,
            "limit": limit,
        },
        task_id=task_id,
    )

"""LangChain adapters for the standalone Hermes terminal toolkit."""

from __future__ import annotations

from typing import List, Literal, Optional

try:
    from langchain.tools import tool
except ImportError:  # pragma: no cover - exercised only without LangChain
    tool = None


def _require_langchain():
    if tool is None:
        raise ImportError("Install langchain to use hermes_terminal_toolkit.langchain_tools")
    try:
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise ImportError("Install pydantic to use hermes_terminal_toolkit.langchain_tools") from exc
    return BaseModel, Field


def _load_wrappers():
    from .terminal import run_process, run_terminal
    return run_process, run_terminal


def build_langchain_tools(default_task_id: str = "default", expose_task_id: bool = False):
    """Return LangChain tools for terminal/process."""
    BaseModel, Field = _require_langchain()
    run_process, run_terminal = _load_wrappers()

    if expose_task_id:
        class TerminalInput(BaseModel):
            command: str = Field(description="Shell command to execute")
            background: bool = Field(default=False, description="Run as a tracked background process")
            timeout: Optional[int] = Field(default=None, ge=1, description="Foreground timeout in seconds")
            workdir: Optional[str] = Field(default=None, description="Per-command working directory")
            pty: bool = Field(default=False, description="Use a PTY for interactive CLI tools")
            notify_on_complete: bool = Field(default=False, description="Notify once when a background process exits")
            watch_patterns: Optional[List[str]] = Field(
                default=None,
                description="Rare one-shot output markers to watch in background mode",
            )
            task_id: str = Field(default=default_task_id, description="Toolkit task/session id for environment reuse")

        class ProcessInput(BaseModel):
            action: Literal["list", "poll", "log", "wait", "kill", "write", "submit", "close"] = Field(
                description="Background process action"
            )
            session_id: str = Field(default="", description="Background process session id")
            data: str = Field(default="", description="Stdin payload for write/submit")
            timeout: Optional[int] = Field(default=None, ge=1, description="Wait timeout in seconds")
            offset: int = Field(default=0, ge=0, description="Log line offset")
            limit: int = Field(default=200, ge=1, description="Maximum log lines to return")
            task_id: str = Field(default=default_task_id, description="Toolkit task/session id")
    else:
        class TerminalInput(BaseModel):
            command: str = Field(description="Shell command to execute")
            background: bool = Field(default=False, description="Run as a tracked background process")
            timeout: Optional[int] = Field(default=None, ge=1, description="Foreground timeout in seconds")
            workdir: Optional[str] = Field(default=None, description="Per-command working directory")
            pty: bool = Field(default=False, description="Use a PTY for interactive CLI tools")
            notify_on_complete: bool = Field(default=False, description="Notify once when a background process exits")
            watch_patterns: Optional[List[str]] = Field(
                default=None,
                description="Rare one-shot output markers to watch in background mode",
            )

        class ProcessInput(BaseModel):
            action: Literal["list", "poll", "log", "wait", "kill", "write", "submit", "close"] = Field(
                description="Background process action"
            )
            session_id: str = Field(default="", description="Background process session id")
            data: str = Field(default="", description="Stdin payload for write/submit")
            timeout: Optional[int] = Field(default=None, ge=1, description="Wait timeout in seconds")
            offset: int = Field(default=0, ge=0, description="Log line offset")
            limit: int = Field(default=200, ge=1, description="Maximum log lines to return")

    @tool("terminal", args_schema=TerminalInput)
    def terminal(
        command: str,
        background: bool = False,
        timeout: Optional[int] = None,
        workdir: Optional[str] = None,
        pty: bool = False,
        notify_on_complete: bool = False,
        watch_patterns: Optional[List[str]] = None,
        task_id: str = default_task_id,
    ) -> str:
        """Execute a shell command through the standalone terminal toolkit."""
        return run_terminal(
            command=command,
            background=background,
            timeout=timeout,
            task_id=task_id,
            workdir=workdir,
            pty=pty,
            notify_on_complete=notify_on_complete,
            watch_patterns=watch_patterns,
        )

    @tool("process", args_schema=ProcessInput)
    def process(
        action: str,
        session_id: str = "",
        data: str = "",
        timeout: Optional[int] = None,
        offset: int = 0,
        limit: int = 200,
        task_id: str = default_task_id,
    ) -> str:
        """Manage background processes started with terminal(background=true)."""
        return run_process(
            action=action,
            session_id=session_id,
            data=data,
            timeout=timeout,
            offset=offset,
            limit=limit,
            task_id=task_id,
        )

    return [terminal, process]

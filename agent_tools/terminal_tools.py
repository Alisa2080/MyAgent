import json
from typing import Literal

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from agent_core.session_context import hermes_task_id_from_runtime
from agent_core.workspace import WORKDIR
from agent_tools.hermes_terminal_toolkit.terminal import run_process, run_terminal
from agent_tools.hermes_terminal_toolkit.process_registry import process_registry
from agent_tools.tool_output import tool_error, tool_ok


class TerminalInput(BaseModel):
    command: str = Field(description="Shell command to execute through Hermes terminal.")
    background: bool = Field(default=False, description="Run as a tracked background process.")
    timeout: int | None = Field(default=None, ge=1, description="Timeout in seconds.")
    workdir: str | None = Field(default=None, description="Optional per-command working directory.")
    pty: bool = Field(default=False, description="Use a PTY for interactive commands.")
    notify_on_complete: bool = Field(default=False, description="Queue completion notification for background commands.")
    watch_patterns: list[str] | None = Field(
        default=None,
        description="Output patterns that should trigger background process notifications.",
    )


class ProcessInput(BaseModel):
    action: Literal["list", "poll", "log", "wait", "kill", "write", "submit", "close"] = Field(
        description="Background process action."
    )
    session_id: str = Field(default="", description="Background process session id.")
    data: str = Field(default="", description="Data for write or submit actions.")
    timeout: int | None = Field(default=None, ge=1, description="Wait timeout in seconds.")
    offset: int = Field(default=0, ge=0, description="Log line offset.")
    limit: int = Field(default=200, ge=1, description="Maximum log lines to return.")


def _decode_hermes_payload(raw: str) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "Hermes terminal returned invalid JSON.", "raw": raw}
    if not isinstance(payload, dict):
        return {"error": f"Hermes terminal returned unexpected payload type: {type(payload).__name__}", "raw": raw}
    return payload


def _status_code_from_payload(payload: dict) -> str:
    status = payload.get("status")
    if isinstance(status, str) and status:
        return status
    if payload.get("error"):
        return "terminal_error"
    return "terminal_error"


def _terminal_impl(
    *,
    command: str,
    background: bool = False,
    timeout: int | None = None,
    workdir: str | None = None,
    pty: bool = False,
    notify_on_complete: bool = False,
    watch_patterns: list[str] | None = None,
    runtime: ToolRuntime | None = None,
) -> str:
    task_id = hermes_task_id_from_runtime(runtime)
    raw = run_terminal(
        command=command,
        background=background,
        timeout=timeout,
        task_id=task_id,
        workdir=workdir or str(WORKDIR),
        pty=pty,
        notify_on_complete=notify_on_complete,
        watch_patterns=watch_patterns,
        force=False,
    )
    payload = _decode_hermes_payload(raw)
    if payload.get("error"):
        return tool_error(
            "terminal",
            str(payload["error"]),
            code=_status_code_from_payload(payload),
            data=payload,
            meta={"backend": "hermes_terminal_toolkit"},
        )
    return tool_ok(
        "terminal",
        data=payload,
        message="Terminal command completed." if not background else "Background process started.",
        meta={"backend": "hermes_terminal_toolkit"},
    )


@tool("terminal", args_schema=TerminalInput)
def terminal(
    command: str,
    runtime: ToolRuntime,
    background: bool = False,
    timeout: int | None = None,
    workdir: str | None = None,
    pty: bool = False,
    notify_on_complete: bool = False,
    watch_patterns: list[str] | None = None,
) -> str:
    """Execute shell commands through Hermes terminal with runtime-scoped task isolation."""
    return _terminal_impl(
        command=command,
        background=background,
        timeout=timeout,
        workdir=workdir,
        pty=pty,
        notify_on_complete=notify_on_complete,
        watch_patterns=watch_patterns,
        runtime=runtime,
    )

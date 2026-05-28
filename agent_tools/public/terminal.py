import json
from typing import Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from agent_core.permissions import tool_policy
from agent_core.permissions.approvals import consume_approval
from agent_core.permissions.tool_grants import consume_tool_policy_grant
from agent_core.policy_tool_middleware import process_policy_args, terminal_policy_args
from agent_core.session_context import RuntimeContext
from agent_core.terminal_process_policy import background_quota_available, background_quota_guard
from agent_core.workspace import WORKDIR
from agent_tools.terminal_toolkit.terminal import run_process, run_terminal
from agent_tools.terminal_toolkit.process_registry import process_registry
from agent_tools.shared.tool_result import tool_failure, tool_success


class TerminalInput(BaseModel):
    command: str = Field(description="Shell command to execute through terminal toolkit.")
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


def _decode_terminal_payload(raw: str) -> tuple[dict, str | None]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}, "terminal toolkit returned invalid JSON."
    if not isinstance(payload, dict):
        return {"raw": raw}, f"terminal toolkit returned unexpected payload type: {type(payload).__name__}"
    return payload, None


def _status_code_from_payload(payload: dict) -> str:
    status = payload.get("status")
    if isinstance(status, str) and status:
        return status
    if payload.get("error"):
        return "terminal_error"
    return "terminal_error"


def _exit_code_from_payload(payload: dict) -> int | None:
    try:
        return int(payload["exit_code"])
    except (KeyError, TypeError, ValueError):
        return None


def _terminal_policy_args(
    *,
    command: str,
    background: bool,
    timeout: int | None,
    workdir: str | None,
    pty: bool,
    notify_on_complete: bool,
    watch_patterns: list[str] | None,
) -> dict:
    return terminal_policy_args(
        {
            "command": command,
            "background": background,
            "timeout": timeout,
            "workdir": workdir,
            "pty": pty,
            "notify_on_complete": notify_on_complete,
            "watch_patterns": watch_patterns,
        }
    )


def _process_policy_args(
    *,
    action: str,
    session_id: str,
    data: str,
    timeout: int | None,
    offset: int,
    limit: int,
) -> dict:
    return process_policy_args(
        {
            "action": action,
            "session_id": session_id,
            "data": data,
            "timeout": timeout,
            "offset": offset,
            "limit": limit,
        }
    )


def _consume_tool_grant(tool_name: str, policy_args: dict, runtime_context: RuntimeContext):
    return consume_tool_policy_grant(
        task_id=runtime_context.task_id,
        tool_call_id=runtime_context.tool_call_id,
        tool_name=tool_name,
        args=policy_args,
    )


def _approval_or_grant_for_review(
    *,
    tool_name: str,
    policy_args: dict,
    runtime_context: RuntimeContext,
    required_risk_tags: tuple[str, ...],
):
    grant = consume_tool_policy_grant(
        task_id=runtime_context.task_id,
        tool_call_id=runtime_context.tool_call_id,
        tool_name=tool_name,
        args=policy_args,
        required_risk_tags=required_risk_tags,
    )
    if grant is not None:
        return grant
    return consume_approval(
        task_id=runtime_context.task_id,
        tool_call_id=runtime_context.tool_call_id,
        tool_name=tool_name,
        args=policy_args,
        required_risk_tags=required_risk_tags,
    )


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
) -> ToolMessage:
    runtime_context = RuntimeContext.from_runtime(runtime)
    task_id = runtime_context.task_id
    tool_call_id = runtime_context.tool_call_id
    policy_args = _terminal_policy_args(
        command=command,
        background=background,
        timeout=timeout,
        workdir=workdir,
        pty=pty,
        notify_on_complete=notify_on_complete,
        watch_patterns=watch_patterns,
    )
    grant = _consume_tool_grant("terminal", policy_args, runtime_context)
    allow_network_once = grant.allow_network_once if grant is not None else False
    force = bool(grant.risk_tags) if grant is not None else False
    if grant is None:
        decision = tool_policy.evaluate_tool_call("terminal", policy_args, task_id, tool_call_id=tool_call_id)
        if decision.outcome == "deny":
            return tool_failure(
                "terminal",
                decision.human_message,
                code="policy_denied",
                data=decision.data,
                meta={"backend": "terminal_toolkit"},
                runtime=runtime,
            )
        if decision.outcome == "review":
            approval = _approval_or_grant_for_review(
                tool_name="terminal",
                policy_args=policy_args,
                runtime_context=runtime_context,
                required_risk_tags=decision.risk_tags,
            )
            if approval is None:
                return tool_failure(
                    "terminal",
                    decision.human_message,
                    code="approval_required",
                    data=decision.data,
                    meta={"backend": "terminal_toolkit"},
                    runtime=runtime,
                )
            allow_network_once = approval.allow_network_once
            force = True
    if background:
        with background_quota_guard(task_id):
            available, current, limit = background_quota_available(task_id)
            if not available:
                return tool_failure(
                    "terminal",
                    f"Background process quota exceeded for this session ({current}/{limit}).",
                    code="background_quota_exceeded",
                    data={"current": current, "limit": limit},
                    meta={"backend": "terminal_toolkit"},
                    runtime=runtime,
                )
            raw = run_terminal(
                command=command,
                background=background,
                timeout=timeout,
                task_id=task_id,
                workdir=workdir or str(WORKDIR),
                pty=pty,
                notify_on_complete=notify_on_complete,
                watch_patterns=watch_patterns,
                force=force,
                allow_network_once=allow_network_once,
            )
    else:
        raw = run_terminal(
            command=command,
            background=background,
            timeout=timeout,
            task_id=task_id,
            workdir=workdir or str(WORKDIR),
            pty=pty,
            notify_on_complete=notify_on_complete,
            watch_patterns=watch_patterns,
            force=force,
            allow_network_once=allow_network_once,
        )
    payload, decode_error = _decode_terminal_payload(raw)
    if decode_error:
        return tool_failure(
            "terminal",
            decode_error,
            code="invalid_response",
            data=payload,
            meta={"backend": "terminal_toolkit"},
            runtime=runtime,
            content="Tool returned invalid response.",
        )
    if payload.get("error"):
        return tool_failure(
            "terminal",
            str(payload["error"]),
            code=_status_code_from_payload(payload),
            data=payload,
            meta={"backend": "terminal_toolkit"},
            runtime=runtime,
        )
    exit_code = _exit_code_from_payload(payload)
    if not background and exit_code not in (None, 0):
        code = "timeout" if exit_code == 124 else "command_failed"
        return tool_failure(
            "terminal",
            f"Command exited with code {exit_code}.",
            code=code,
            data=payload,
            meta={"backend": "terminal_toolkit"},
            runtime=runtime,
        )
    message = "Terminal command completed." if not background else "Background process started."
    content = (
        f"Background process started: {payload.get('session_id')}."
        if background and payload.get("session_id")
        else f"Command completed with exit code {exit_code}."
        if not background and exit_code is not None
        else message
    )
    return tool_success(
        "terminal",
        data=payload,
        message=message,
        meta={"backend": "terminal_toolkit"},
        runtime=runtime,
        content=content,
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
) -> ToolMessage:
    """Execute shell commands through terminal toolkit with runtime-scoped task isolation."""
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


_PROCESS_ACTIONS_REQUIRING_SESSION = {"poll", "log", "wait", "kill", "write", "submit", "close"}


def _session_belongs_to_task(session_id: str, task_id: str) -> bool:
    session = process_registry.get(session_id)
    return bool(session is not None and session.task_id == task_id)


def _process_impl(
    *,
    action: str,
    session_id: str = "",
    data: str = "",
    timeout: int | None = None,
    offset: int = 0,
    limit: int = 200,
    runtime: ToolRuntime | None = None,
) -> ToolMessage:
    runtime_context = RuntimeContext.from_runtime(runtime)
    task_id = runtime_context.task_id
    tool_call_id = runtime_context.tool_call_id
    if action in _PROCESS_ACTIONS_REQUIRING_SESSION:
        if not session_id:
            return tool_failure("process", f"session_id is required for {action}", code="invalid_input", runtime=runtime)
        if not _session_belongs_to_task(session_id, task_id):
            return tool_failure(
                "process",
                "Process session does not belong to the current runtime task id.",
                code="access_denied",
                data={"session_id": session_id},
                meta={"backend": "terminal_toolkit"},
                runtime=runtime,
            )

    policy_args = _process_policy_args(
        action=action,
        session_id=session_id,
        data=data,
        timeout=timeout,
        offset=offset,
        limit=limit,
    )
    grant = _consume_tool_grant("process", policy_args, runtime_context)
    if grant is None:
        decision = tool_policy.evaluate_tool_call("process", policy_args, task_id, tool_call_id=tool_call_id)
        if decision.outcome == "deny":
            return tool_failure(
                "process",
                decision.human_message,
                code="policy_denied",
                data=decision.data,
                meta={"backend": "terminal_toolkit"},
                runtime=runtime,
            )
        if decision.outcome == "review":
            approval = _approval_or_grant_for_review(
                tool_name="process",
                policy_args=policy_args,
                runtime_context=runtime_context,
                required_risk_tags=decision.risk_tags,
            )
            if approval is None:
                return tool_failure(
                    "process",
                    decision.human_message,
                    code="approval_required",
                    data=decision.data,
                    meta={"backend": "terminal_toolkit"},
                    runtime=runtime,
                )

    raw = run_process(
        action=action,
        session_id=session_id,
        data=data,
        timeout=timeout,
        offset=offset,
        limit=limit,
        task_id=task_id,
    )
    payload, decode_error = _decode_terminal_payload(raw)
    if decode_error:
        return tool_failure(
            "process",
            decode_error,
            code="invalid_response",
            data=payload,
            meta={"backend": "terminal_toolkit"},
            runtime=runtime,
            content="Tool returned invalid response.",
        )
    if payload.get("error"):
        return tool_failure(
            "process",
            str(payload["error"]),
            code=_status_code_from_payload(payload),
            data=payload,
            meta={"backend": "terminal_toolkit"},
            runtime=runtime,
        )
    return tool_success(
        "process",
        data=payload,
        message="Process action completed.",
        meta={"backend": "terminal_toolkit"},
        runtime=runtime,
        content=f"Process action completed: {action}.",
    )


@tool("process", args_schema=ProcessInput)
def process(
    action: str,
    runtime: ToolRuntime,
    session_id: str = "",
    data: str = "",
    timeout: int | None = None,
    offset: int = 0,
    limit: int = 200,
) -> ToolMessage:
    """Manage terminal toolkit background processes scoped to the current runtime task id."""
    return _process_impl(
        action=action,
        session_id=session_id,
        data=data,
        timeout=timeout,
        offset=offset,
        limit=limit,
        runtime=runtime,
    )

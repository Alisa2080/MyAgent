from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from agent_core.session_context import RuntimeContext, origin_identity_from_runtime
from agent_tools.shared.tool_result import tool_failure, tool_success
from cron.jobs import (
    create_job,
    get_job,
    list_jobs,
    pause_job,
    remove_job,
    resume_job,
    trigger_job,
    update_job,
)
from cron.paths import get_scripts_dir


class CronJobInput(BaseModel):
    action: Literal["create", "list", "update", "pause", "resume", "remove", "run"] = Field(
        description="Cron job action."
    )
    job_id: str | None = Field(default=None, description="Required for update/pause/resume/remove/run.")
    prompt: str | None = Field(default=None, description="Self-contained cron prompt.")
    schedule: str | None = Field(default=None, description="Schedule such as 30m, every 2h, cron, or ISO timestamp.")
    name: str | None = Field(default=None, description="Optional job name.")
    repeat: int | None = Field(default=None, description="Optional repeat count; <=0 means forever.")
    deliver: str | None = Field(
        default=None,
        description="Delivery target(s), comma-separated: local, origin, webhook:<url>, or platform:chat_id[:thread_id].",
    )
    include_disabled: bool = Field(default=False, description="Include disabled jobs when listing.")
    skills: list[str] | None = Field(default=None, description="Ordered skill names to load before prompt.")
    model: str | None = Field(default=None, description="Stored for compatibility; ignored by first runner.")
    provider: str | None = Field(default=None, description="Stored for compatibility; ignored by first runner.")
    base_url: str | None = Field(default=None, description="Stored for compatibility; ignored by first runner.")
    reason: str | None = Field(default=None, description="Pause reason.")
    script: str | None = Field(default=None, description="Relative script under cron scripts dir.")
    context_from: list[str] | None = Field(default=None, description="Job ids whose latest output is injected.")
    enabled_toolsets: list[str] | None = Field(default=None, description="Explicit unattended toolset grants.")
    workdir: str | None = Field(default=None, description="Absolute project directory for this job.")
    idle_timeout_seconds: int | None = Field(
        default=None,
        description="Idle timeout in seconds; 0 disables idle timeout. Defaults to AGENT_CRON_TIMEOUT.",
    )
    max_runtime_seconds: int | None = Field(
        default=None,
        description="Optional hard runtime cap in seconds; 0 or omitted disables the cap.",
    )
    concurrency_key: str | None = Field(
        default=None,
        description="Optional key used to serialize related cron runs. Defaults to job:<job_id>.",
    )
    concurrency_policy: Literal["queue_one", "queue_all", "replace_running", "skip_if_running"] | None = Field(
        default=None,
        description="How to handle a due run when another run with the same concurrency key is active. Defaults to queue_one.",
    )


_INVISIBLE_CHARS = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\ufeff",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
}
_THREAT_PATTERNS = [
    (r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions", "prompt_injection"),
    (r"curl\s+[^\n]*(?:\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)|/etc/passwd)", "exfil_curl"),
    (r"wget\s+[^\n]*(?:\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)|/etc/passwd)", "exfil_wget"),
    (r"(?:cat|printenv|env)\s+[^\n]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "secret_exfil"),
    (r"authorized_keys|ssh-rsa\s+[A-Za-z0-9+/=]+", "ssh_backdoor"),
    (r"\brm\s+-rf\s+/(?:\s|$)", "destructive_root_rm"),
    (r"\bmkfs(?:\.\w+)?\s+", "destructive_mkfs"),
]


def _runtime_thread_id(runtime: ToolRuntime | None) -> str | None:
    config = getattr(runtime, "config", None)
    return RuntimeContext.from_config(config).thread_id


def run_cronjob_action(
    action: str,
    *,
    runtime: ToolRuntime | None = None,
    origin_thread_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return _cronjob_impl(
        action=action,
        runtime=runtime,
        origin_thread_id=origin_thread_id,
        **kwargs,
    )


def _scan_prompt(prompt: str) -> str | None:
    for char in _INVISIBLE_CHARS:
        if char in prompt:
            return f"prompt contains invisible unicode U+{ord(char):04X}"

    for pattern, code in _THREAT_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE):
            return f"prompt matches blocked pattern {code}"
    return None


def _normalize_context_from(context_from: Any) -> list[str] | None:
    if context_from is None:
        return None
    if isinstance(context_from, str):
        items = [context_from]
    else:
        items = list(context_from)
    normalized = [str(item).strip() for item in items if str(item).strip()]
    return normalized or None


def _validate_context_from(context_from: Any) -> tuple[list[str] | None, dict[str, Any] | None]:
    normalized = _normalize_context_from(context_from)
    if not normalized:
        return None, None

    for job_id in normalized:
        if get_job(job_id) is None:
            return None, {
                "success": False,
                "code": "missing_context_job",
                "error": f"context_from references unknown cron job {job_id!r}.",
            }
    return normalized, None


def _validate_script_path(script: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if script is None:
        return None, None

    raw = str(script).strip()
    if not raw:
        return None, None

    if raw.startswith("~") or (len(raw) >= 2 and raw[1] == ":") or Path(raw).is_absolute():
        return None, {
            "success": False,
            "code": "invalid_script_path",
            "error": "script must be a relative path under the cron scripts directory.",
        }

    scripts_dir = get_scripts_dir().resolve()
    candidate = (scripts_dir / raw).resolve()
    try:
        candidate.relative_to(scripts_dir)
    except ValueError:
        return None, {
            "success": False,
            "code": "invalid_script_path",
            "error": "script path escapes the cron scripts directory.",
        }
    return raw, None


def _validate_timeout_field(name: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return {"success": False, "code": "invalid_timeout", "error": f"{name} must be an integer number of seconds."}
    if seconds < 0:
        return {"success": False, "code": "invalid_timeout", "error": f"{name} must be >= 0."}
    return None


def _format_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("id"),
        "name": job.get("name"),
        "prompt_preview": (job.get("prompt") or "")[:100],
        "skills": job.get("skills") or [],
        "schedule": job.get("schedule_display"),
        "repeat": job.get("repeat"),
        "deliver": job.get("deliver", "local"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "enabled": job.get("enabled", True),
        "state": job.get("state"),
        "workdir": job.get("workdir"),
        "concurrency_key": job.get("concurrency_key"),
        "concurrency_policy": job.get("concurrency_policy"),
    }


def _deliver_mentions_origin(deliver: Any) -> bool:
    if deliver is None:
        return False
    return any(part.strip().lower() == "origin" for part in str(deliver).split(","))


def _validate_delivery(deliver: Any, *, origin: dict[str, Any] | None) -> dict[str, Any] | None:
    if deliver is None:
        return None
    from cron.delivery_registry import default_delivery_registry, known_adapter_error
    from cron.delivery_targets import DeliveryIdentity

    registry = default_delivery_registry()
    validation = registry.validate_targets(
        str(deliver),
        origin=DeliveryIdentity.from_job_origin(origin),
        job={},
    )
    if validation.ok:
        return None
    code = "invalid_webhook" if validation.error and "webhook URL" in validation.error else "unsupported_delivery"
    error = validation.error or f"Unsupported delivery target: {deliver}"
    if code == "unsupported_delivery":
        error = known_adapter_error(registry, error)
    return {
        "success": False,
        "code": code,
        "error": error,
    }


def _prompt_scan_error(prompt: str | None) -> dict[str, Any] | None:
    if not prompt:
        return None
    scan_error = _scan_prompt(prompt)
    if scan_error:
        return {"success": False, "code": "blocked_prompt", "error": scan_error}
    return None


def _normalize_repeat(repeat: Any) -> Any:
    if repeat is None:
        return None
    try:
        return None if int(repeat) <= 0 else repeat
    except (TypeError, ValueError):
        return repeat


def _origin_identity_from_thread(thread_id: str | None) -> dict[str, str] | None:
    if not thread_id:
        return None
    return {
        "source_type": "cli",
        "session_id": str(thread_id),
        "thread_id": str(thread_id),
    }


def _runtime_origin_identity(runtime: ToolRuntime | None, origin_thread_id: str | None = None) -> dict[str, str] | None:
    identity = origin_identity_from_runtime(runtime)
    if identity is not None:
        return identity
    if origin_thread_id:
        return {
            "source_type": "cli",
            "session_id": str(origin_thread_id),
            "thread_id": str(origin_thread_id),
        }
    return None


def _cronjob_impl(
    action: str,
    runtime: ToolRuntime | None = None,
    origin_thread_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    normalized = (action or "").strip().lower()
    deliver = kwargs.get("deliver")

    try:
        if normalized == "create":
            schedule = kwargs.get("schedule")
            prompt = kwargs.get("prompt") or ""
            skills = kwargs.get("skills") or []
            if not schedule:
                return {"success": False, "code": "missing_schedule", "error": "schedule is required for create."}
            if not prompt and not skills:
                return {"success": False, "code": "missing_task", "error": "create requires prompt or skills."}

            scan_error = _prompt_scan_error(prompt)
            if scan_error:
                return scan_error

            context_from, context_error = _validate_context_from(kwargs.get("context_from"))
            if context_error:
                return context_error

            script, script_error = _validate_script_path(kwargs.get("script"))
            if script_error:
                return script_error

            origin = _runtime_origin_identity(runtime, origin_thread_id)
            if _deliver_mentions_origin(deliver) and origin is None:
                return {
                    "success": False,
                    "code": "missing_origin_thread",
                    "error": "deliver='origin' requires an active origin identity.",
                }
            if deliver is not None and not _deliver_mentions_origin(deliver):
                origin = None
            delivery_error = _validate_delivery(deliver, origin=origin)
            if delivery_error:
                return delivery_error

            idle_timeout_error = _validate_timeout_field("idle_timeout_seconds", kwargs.get("idle_timeout_seconds"))
            if idle_timeout_error:
                return idle_timeout_error
            max_runtime_error = _validate_timeout_field("max_runtime_seconds", kwargs.get("max_runtime_seconds"))
            if max_runtime_error:
                return max_runtime_error

            job = create_job(
                prompt=prompt,
                schedule=schedule,
                name=kwargs.get("name"),
                repeat=_normalize_repeat(kwargs.get("repeat")),
                deliver=deliver,
                origin=origin,
                skills=skills,
                model=kwargs.get("model"),
                provider=kwargs.get("provider"),
                base_url=kwargs.get("base_url"),
                script=script,
                context_from=context_from,
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                workdir=kwargs.get("workdir"),
                idle_timeout_seconds=kwargs.get("idle_timeout_seconds"),
                max_runtime_seconds=kwargs.get("max_runtime_seconds"),
                concurrency_key=kwargs.get("concurrency_key"),
                concurrency_policy=kwargs.get("concurrency_policy"),
            )
            return {
                "success": True,
                "job_id": job.get("id"),
                "job": _format_job(job),
                "message": f"Cron job '{job.get('name')}' created.",
            }

        if normalized == "list":
            jobs = list_jobs(include_disabled=bool(kwargs.get("include_disabled")))
            return {"success": True, "jobs": [_format_job(job) for job in jobs]}

        job_id = kwargs.get("job_id")
        if not job_id:
            return {
                "success": False,
                "code": "missing_job_id",
                "error": f"job_id is required for {normalized}.",
            }

        if normalized == "pause":
            return {"success": True, "job": _format_job(pause_job(job_id, reason=kwargs.get("reason")))}
        if normalized == "resume":
            return {"success": True, "job": _format_job(resume_job(job_id))}
        if normalized == "remove":
            return {"success": True, "removed": bool(remove_job(job_id))}
        if normalized == "run":
            return {"success": True, "job": _format_job(trigger_job(job_id))}
        if normalized == "update":
            prompt = kwargs.get("prompt")
            scan_error = _prompt_scan_error(prompt)
            if scan_error:
                return scan_error

            updates = {
                key: value
                for key, value in kwargs.items()
                if key not in {"job_id", "include_disabled", "reason"} and value is not None
            }

            if "context_from" in updates:
                context_from, context_error = _validate_context_from(updates["context_from"])
                if context_error:
                    return context_error
                updates["context_from"] = context_from

            if "script" in updates:
                script, script_error = _validate_script_path(updates["script"])
                if script_error:
                    return script_error
                updates["script"] = script

            if "repeat" in updates:
                updates["repeat"] = _normalize_repeat(updates["repeat"])

            idle_timeout_error = _validate_timeout_field("idle_timeout_seconds", updates.get("idle_timeout_seconds"))
            if idle_timeout_error:
                return idle_timeout_error
            max_runtime_error = _validate_timeout_field("max_runtime_seconds", updates.get("max_runtime_seconds"))
            if max_runtime_error:
                return max_runtime_error

            if _deliver_mentions_origin(updates.get("deliver")):
                origin = _runtime_origin_identity(runtime, origin_thread_id)
                if origin is None:
                    return {
                        "success": False,
                        "code": "missing_origin_thread",
                        "error": "deliver='origin' requires an active origin identity.",
                    }
                updates["origin"] = origin
            elif "deliver" in updates:
                updates["origin"] = None
            delivery_error = _validate_delivery(updates.get("deliver"), origin=updates.get("origin"))
            if delivery_error:
                return delivery_error

            return {"success": True, "job": _format_job(update_job(job_id, updates))}

        return {"success": False, "code": "unknown_action", "error": f"Unknown cron action {action!r}."}
    except Exception as exc:
        return {"success": False, "code": "cron_error", "error": str(exc)}


@tool("cronjob", args_schema=CronJobInput)
def cronjob(
    action: str,
    runtime: ToolRuntime,
    job_id: str | None = None,
    prompt: str | None = None,
    schedule: str | None = None,
    name: str | None = None,
    repeat: int | None = None,
    deliver: str | None = None,
    include_disabled: bool = False,
    skills: list[str] | None = None,
    model: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    reason: str | None = None,
    script: str | None = None,
    context_from: list[str] | None = None,
    enabled_toolsets: list[str] | None = None,
    workdir: str | None = None,
    idle_timeout_seconds: int | None = None,
    max_runtime_seconds: int | None = None,
    concurrency_key: str | None = None,
    concurrency_policy: Literal["queue_one", "queue_all", "replace_running", "skip_if_running"] | None = None,
) -> ToolMessage:
    """Manage unattended scheduled cron jobs with local, origin, or webhook delivery."""
    result = run_cronjob_action(
        action=action,
        runtime=runtime,
        origin_thread_id=_runtime_thread_id(runtime),
        job_id=job_id,
        prompt=prompt,
        schedule=schedule,
        name=name,
        repeat=repeat,
        deliver=deliver,
        include_disabled=include_disabled,
        skills=skills,
        model=model,
        provider=provider,
        base_url=base_url,
        reason=reason,
        script=script,
        context_from=context_from,
        enabled_toolsets=enabled_toolsets,
        workdir=workdir,
        idle_timeout_seconds=idle_timeout_seconds,
        max_runtime_seconds=max_runtime_seconds,
        concurrency_key=concurrency_key,
        concurrency_policy=concurrency_policy,
    )
    if result.get("success"):
        return tool_success(
            "cronjob",
            data=result,
            message=result.get("message", "Cron job action completed."),
            runtime=runtime,
            content=result.get("message", f"Cron job action completed: {action}."),
        )
    return tool_failure(
        "cronjob",
        result.get("error", "Cron job action failed."),
        code=result.get("code", "cron_error"),
        data=result,
        runtime=runtime,
    )

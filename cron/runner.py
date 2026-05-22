from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents import create_agent

from agent_core.message_utils import extract_text_from_agent_response
from agent_core.model_config import MAIN_MODEL
from agent_core.system_prompt import (
    SystemPromptBuilder,
    build_prompt_context,
    load_project_instruction_blocks,
    model_display_name,
)
from agent_core.tool_limits import build_tool_call_limit_middleware
from agent_core.workspace import WORKDIR
from cron.jobs import latest_job_output, now
from cron.notifications import SILENT_MARKER
from cron.paths import get_scripts_dir


SCRIPT_OUTPUT_MAX_CHARS = 12_000
SKILL_CONTENT_MAX_CHARS = 12_000
AGENT_RECURSION_LIMIT = 90


class _ScriptTimeoutError(TimeoutError):
    def __init__(self, message: str, output: str):
        super().__init__(message)
        self.output = output


@dataclass
class JobRunResult:
    success: bool
    output_doc: str
    final_response: str
    error: str | None = None


def _bounded(text: str, max_chars: int | None = None) -> str:
    if max_chars is None:
        max_chars = SCRIPT_OUTPUT_MAX_CHARS
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def validate_script_path(script: str | None) -> str | None:
    if not script or not str(script).strip():
        return None

    raw = str(script).strip()
    if (
        raw.startswith(("/", "\\", "~"))
        or re.match(r"^[A-Za-z]:", raw) is not None
    ):
        return f"Script path must be relative to cron scripts directory: {raw!r}."

    scripts_dir = get_scripts_dir().resolve()
    target = (scripts_dir / raw).resolve()
    try:
        target.relative_to(scripts_dir)
    except ValueError:
        return f"Script path escapes the cron scripts directory: {raw!r}."
    return None


def _resolve_script_path(script: str) -> Path:
    error = validate_script_path(script)
    if error:
        raise ValueError(error)
    return (get_scripts_dir() / str(script).strip()).resolve()


def _script_timeout() -> int:
    try:
        return max(1, int(os.getenv("HERMES_CRON_SCRIPT_TIMEOUT", "120")))
    except ValueError:
        return 120


def _format_script_output(stdout: str | None, stderr: str | None) -> str:
    parts = []
    if stderr:
        parts.extend(["[stderr]", stderr.rstrip()])
    if stdout:
        parts.extend(["[stdout]", stdout.rstrip()])
    return _bounded("\n".join(parts))


def _run_script(script: str) -> tuple[bool, str]:
    path = _resolve_script_path(script)
    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=_script_timeout(),
            cwd=str(path.parent),
        )
    except subprocess.TimeoutExpired as exc:
        output = _format_script_output(
            _decode_timeout_output(exc.stdout),
            _decode_timeout_output(exc.stderr),
        )
        raise _ScriptTimeoutError(
            f"Pre-run script timed out after {_script_timeout()} seconds.",
            output,
        ) from exc

    output = _format_script_output(result.stdout, result.stderr)
    return result.returncode == 0, output


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _wake_agent(script_output: str | None) -> bool:
    for line in reversed(str(script_output or "").splitlines()):
        text = line.strip()
        if not text or text in {"[stdout]", "[stderr]"}:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return True
        return not (isinstance(payload, dict) and payload.get("wakeAgent") is False)
    return True


def _load_skill_content(skill_name: str) -> str:
    from agent_tools.public.skills import _find_skill

    skill = _find_skill(str(skill_name))
    if not skill:
        return f"[Skill not found: {skill_name}]"

    try:
        content = Path(skill["path"]).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[Skill could not be loaded: {skill_name}: {exc}]"
    return _bounded(content, SKILL_CONTENT_MAX_CHARS)


def _job_skills(job: dict[str, Any]) -> list[str]:
    skills: list[str] = []
    for item in job.get("skills") or []:
        text = str(item).strip()
        if text and text not in skills:
            skills.append(text)
    skill = str(job.get("skill") or "").strip()
    if skill and skill not in skills:
        skills.append(skill)
    return skills


def build_job_prompt(
    job: dict[str, Any],
    script_output: str | None = None,
) -> str:
    parts = [
        "You are running an unattended cron job.",
        "Do not ask clarifying questions. Make a best effort with available context.",
        "Do not create, update, pause, resume, remove, or otherwise modify cron jobs.",
        (
            "Your final response is used for delivery. Start with "
            f"{SILENT_MARKER} only when there is nothing useful to notify."
        ),
    ]

    if script_output:
        parts.extend(["", "## Pre-run Script Output", script_output])

    for upstream_id in job.get("context_from") or []:
        output = latest_job_output(str(upstream_id))
        if output:
            parts.extend(["", f"## Context From Job {upstream_id}", output])

    for skill in _job_skills(job):
        parts.extend(["", f"## Skill: {skill}", _load_skill_content(skill)])

    parts.extend(["", "## Job Prompt", str(job.get("prompt") or "")])
    return "\n".join(parts)


def build_cron_tools(enabled_toolsets: list[str] | None = None) -> list[Any]:
    from agent_core.delegation import READ_ONLY_TOOLS, task
    from agent_tools.public.files import patch, write_file
    from agent_tools.public.terminal import process, terminal

    requested = set(enabled_toolsets or [])
    tools = list(READ_ONLY_TOOLS)
    if "file_write" in requested:
        tools.extend([write_file, patch])
    if "terminal" in requested:
        tools.extend([terminal, process])
    if "delegation" in requested:
        tools.append(task)
    return [tool for tool in tools if getattr(tool, "name", "") != "cronjob"]


def _job_workdir(job: dict[str, Any]) -> Path:
    return Path(job.get("workdir") or WORKDIR).expanduser().resolve()


def _build_cron_agent(job: dict[str, Any]):
    workdir = _job_workdir(job)
    prompt_context = build_prompt_context(
        workdir=workdir,
        model_name=model_display_name(MAIN_MODEL),
        project_instruction_blocks=load_project_instruction_blocks(workdir),
    )
    enabled_toolsets = set(job.get("enabled_toolsets") or [])
    return create_agent(
        model=MAIN_MODEL,
        system_prompt=SystemPromptBuilder().build_parent(prompt_context),
        middleware=build_tool_call_limit_middleware(
            include_task="delegation" in enabled_toolsets
        ),
        tools=build_cron_tools(job.get("enabled_toolsets")),
    )


def _cron_timeout() -> int | None:
    try:
        value = int(os.getenv("HERMES_CRON_TIMEOUT", "600"))
    except ValueError:
        value = 600
    return value if value > 0 else None


def _invoke_cron_agent(job: dict[str, Any], prompt: str) -> str:
    agent = _build_cron_agent(job)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        agent.invoke,
        {"messages": [{"role": "user", "content": prompt}]},
        {"recursion_limit": AGENT_RECURSION_LIMIT},
    )
    try:
        response = future.result(timeout=_cron_timeout())
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return extract_text_from_agent_response(response)


def _output_doc(
    job: dict[str, Any],
    final_response: str,
    script_output: str | None = None,
    error: str | None = None,
) -> str:
    lines = [
        f"# Cron Job: {job.get('name') or job.get('id') or '(unnamed)'}",
        "",
        f"**Job ID:** {job.get('id')}",
        f"**Run Time:** {now().isoformat()}",
        "",
    ]
    if script_output:
        lines.extend(["## Script Output", "", script_output, ""])
    if error:
        lines.extend(["## Error", "", error, ""])
    lines.extend(["## Final Response", "", final_response or ""])
    return "\n".join(lines)


def run_job(job: dict[str, Any]) -> JobRunResult:
    script_output = None
    try:
        script = job.get("script")
        if script:
            script_ok, script_output = _run_script(str(script))
            if not script_ok:
                error = "Pre-run script failed."
                return JobRunResult(
                    success=False,
                    output_doc=_output_doc(job, "", script_output, error),
                    final_response="",
                    error=error,
                )
            if not _wake_agent(script_output):
                final_response = SILENT_MARKER
                return JobRunResult(
                    success=True,
                    output_doc=_output_doc(
                        job,
                        final_response,
                        script_output,
                        "Script gate returned wakeAgent=false; agent skipped.",
                    ),
                    final_response=final_response,
                )

        prompt = build_job_prompt(job, script_output=script_output)
        final_response = _invoke_cron_agent(job, prompt)
        return JobRunResult(
            success=True,
            output_doc=_output_doc(job, final_response, script_output),
            final_response=final_response,
        )
    except _ScriptTimeoutError as exc:
        error = str(exc)
        return JobRunResult(
            success=False,
            output_doc=_output_doc(job, "", exc.output or script_output, error),
            final_response="",
            error=error,
        )
    except concurrent.futures.TimeoutError:
        error = "Cron job timed out."
        return JobRunResult(
            success=False,
            output_doc=_output_doc(job, "", script_output, error),
            final_response="",
            error=error,
        )
    except TimeoutError as exc:
        error = str(exc)
        return JobRunResult(
            success=False,
            output_doc=_output_doc(job, "", script_output, error),
            final_response="",
            error=error,
        )
    except Exception as exc:
        error = str(exc)
        return JobRunResult(
            success=False,
            output_doc=_output_doc(job, "", script_output, error),
            final_response="",
            error=error,
        )

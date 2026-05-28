from __future__ import annotations

from dataclasses import dataclass
import json
import sys
from typing import Any

from agent_core.cron_lifecycle import is_cron_scheduler_running, tick as cron_tick
from cron.jobs import get_job, list_jobs
from cron.paths import display_cron_home, get_jobs_file, get_output_dir, get_scripts_dir

run_cronjob_action = None


def _get_run_cronjob_action():
    global run_cronjob_action
    if run_cronjob_action is None:
        from agent_tools.public.cronjob import run_cronjob_action as _fn
        run_cronjob_action = _fn
    return run_cronjob_action


@dataclass(frozen=True)
class CronCommandResult:
    text: str
    exit_code: int = 0


def _format_error(result: dict[str, Any]) -> CronCommandResult:
    return CronCommandResult(
        result.get("error", "Cron command failed."),
        exit_code=2,
    )


def _format_job_line(job: dict[str, Any]) -> str:
    return (
        f"{job.get('job_id') or job.get('id')}  "
        f"{job.get('state', '-'):<10}  "
        f"{job.get('schedule', job.get('schedule_display', '-'))}  "
        f"{job.get('name', '-')}"
    )


def list_cron_jobs(*, include_disabled: bool = False) -> CronCommandResult:
    result = _get_run_cronjob_action()("list", include_disabled=include_disabled)
    if not result.get("success"):
        return _format_error(result)
    jobs = result.get("jobs") or []
    if not jobs:
        return CronCommandResult("No cron jobs.")
    lines = ["Cron Jobs:"]
    lines.extend(f"  {_format_job_line(job)}" for job in jobs)
    return CronCommandResult("\n".join(lines))


def create_cron_job(
    *,
    schedule: str,
    prompt: str | None = None,
    session_id: str | None = None,
    top_level: bool = False,
    **kwargs: Any,
) -> CronCommandResult:
    deliver = kwargs.pop("deliver", None)
    if deliver == "origin" and not session_id:
        return CronCommandResult(
            "origin delivery requires an active CLI session",
            exit_code=2,
        )
    if deliver is None:
        deliver = "local" if top_level else "origin"
    if deliver == "origin" and top_level:
        return CronCommandResult(
            "origin delivery requires an active CLI session",
            exit_code=2,
        )
    origin_thread_id = session_id if deliver == "origin" else None
    result = _get_run_cronjob_action()(
        "create",
        origin_thread_id=origin_thread_id,
        schedule=schedule,
        prompt=prompt,
        deliver=deliver,
        **kwargs,
    )
    if not result.get("success"):
        return _format_error(result)
    job_id = result.get("job_id") or (result.get("job") or {}).get("job_id")
    job = result.get("job") or {}
    lines = [f"Created cron job {job_id}."]
    if job.get("name"):
        lines.append(f"Name: {job['name']}")
    if job.get("schedule"):
        lines.append(f"Schedule: {job['schedule']}")
    if job.get("next_run_at"):
        lines.append(f"Next run: {job['next_run_at']}")
    return CronCommandResult("\n".join(lines))


def update_cron_job(*, job_id: str, **kwargs: Any) -> CronCommandResult:
    session_id = kwargs.pop("session_id", None)
    top_level = bool(kwargs.pop("top_level", False))
    deliver = kwargs.get("deliver")
    if deliver == "origin" and (top_level or not session_id):
        return CronCommandResult(
            "origin delivery requires an active CLI session",
            exit_code=2,
        )
    if deliver == "origin":
        kwargs["origin"] = {"thread_id": session_id}
    result = _get_run_cronjob_action()(
        "update",
        origin_thread_id=session_id if deliver == "origin" else None,
        job_id=job_id,
        **kwargs,
    )
    if not result.get("success"):
        return _format_error(result)
    job = result.get("job") or {}
    return CronCommandResult(f"Updated cron job {job.get('job_id', job_id)}.")


def simple_job_action(action: str, *, job_id: str, reason: str | None = None) -> CronCommandResult:
    kwargs: dict[str, Any] = {"job_id": job_id}
    if reason is not None:
        kwargs["reason"] = reason
    result = _get_run_cronjob_action()(action, **kwargs)
    if not result.get("success"):
        return _format_error(result)
    if action == "remove":
        if not result.get("removed", False):
            return CronCommandResult(
                f"Cron job not found: {job_id}.",
                exit_code=2,
            )
        return CronCommandResult(f"Removed cron job {job_id}.")
    job = result.get("job") or {}
    labels = {
        "pause": "Paused",
        "resume": "Resumed",
        "run": "Ran",
    }
    label = labels.get(action, f"{action.capitalize()}d")
    return CronCommandResult(f"{label} cron job {job.get('job_id', job_id)}.")


def cron_status() -> CronCommandResult:
    from cron.runner_client import runner_mode_diagnostic

    jobs = list_jobs(include_disabled=True)
    mode, mode_ok = runner_mode_diagnostic()
    mode_label = f"{'ok' if mode_ok else 'UNSUPPORTED'} ({mode})"
    lines = [
        f"Scheduler: {'running' if is_cron_scheduler_running() else 'stopped'}",
        f"Cron home: {display_cron_home()}",
        f"Runner mode: {mode_label}",
        f"Jobs file: {get_jobs_file()}",
        f"Output dir: {get_output_dir()}",
        f"Scripts dir: {get_scripts_dir()}",
        f"Jobs: {len(jobs)}",
    ]
    if mode == "subprocess":
        from cron.runner_subprocess import subprocess_timeout_diagnostic

        timeout_val, timeout_ok = subprocess_timeout_diagnostic()
        if timeout_ok:
            lines.append(f"Subprocess timeout: {timeout_val}s")
        else:
            lines.append("Subprocess timeout: invalid")
    lines.extend(_delivery_stats_lines())
    return CronCommandResult("\n".join(lines))


def _delivery_stats_lines() -> list[str]:
    from cron.delivery_store import DeliveryStore

    store = DeliveryStore()
    stats = store.stats()
    lines = [
        "Delivery queue: "
        f"pending={stats.get('pending', 0)} "
        f"failed={stats.get('failed', 0)} "
        f"dead={stats.get('dead', 0)} "
        f"delivered={stats.get('delivered', 0)}"
    ]
    errors = store.recent_errors(limit=1)
    if errors:
        error = errors[0]
        lines.append(
            "Last delivery error: "
            f"job={error.get('job_id') or '-'} "
            f"target={error.get('target') or '-'} "
            f"status={error.get('status') or '-'} "
            f"error={error.get('last_error') or '-'}"
        )
    return lines


def _check_line(status: str, message: str) -> str:
    return f"[{status}] {message}"


def cron_doctor() -> CronCommandResult:
    from cron.delivery import parse_target, validate_webhook_url
    from cron.delivery_store import DeliveryStore
    from cron.paths import get_runner_tmp_dir
    from cron.runner_client import runner_mode_diagnostic
    from cron.runner_subprocess import subprocess_timeout_diagnostic

    lines: list[str] = []
    warnings = 0
    failures = 0

    def add(status: str, message: str) -> None:
        nonlocal warnings, failures
        normalized = status.lower()
        if normalized == "warn":
            warnings += 1
        elif normalized == "fail":
            failures += 1
        lines.append(_check_line(normalized, message))

    # Runner mode
    mode, mode_ok = runner_mode_diagnostic()
    if mode_ok:
        add("ok", f"runner mode: {mode}")
    else:
        add("fail", f"runner mode: {mode!r} (unsupported)")
    if mode == "subprocess":
        timeout_val, timeout_ok = subprocess_timeout_diagnostic()
        if timeout_ok:
            add("ok", f"subprocess timeout: {timeout_val}s")
        else:
            add("fail", f"subprocess timeout: invalid (set AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT to a positive integer)")
        # Worker entrypoint resolvable
        import subprocess as _subprocess
        try:
            result = _subprocess.run(
                [sys.executable, "-m", "cron.runner_worker", "--help"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                add("ok", "runner_worker entrypoint: resolvable")
            else:
                details = (result.stderr or result.stdout or b"").decode(
                    "utf-8", errors="replace"
                ).strip()
                suffix = f": {details}" if details else ""
                add("fail", f"runner_worker entrypoint failed with code {result.returncode}{suffix}")
        except _subprocess.TimeoutExpired:
            add("warn", "runner_worker entrypoint: timed out during check")
        except Exception as exc:
            add("fail", f"runner_worker entrypoint: {exc}")
        # Temp directory writable
        try:
            runner_tmp = get_runner_tmp_dir()
            runner_tmp.mkdir(parents=True, exist_ok=True)
            probe = runner_tmp / ".doctor-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            add("ok", f"runner tmp dir writable: {runner_tmp}")
        except Exception as exc:
            add("fail", f"runner tmp dir not writable: {exc}")

    try:
        cron_home = display_cron_home()
        add("ok", f"cron home: {cron_home}")
        get_jobs_file().parent.mkdir(parents=True, exist_ok=True)
        get_output_dir().mkdir(parents=True, exist_ok=True)
        get_scripts_dir().mkdir(parents=True, exist_ok=True)
        add("ok", f"jobs file path: {get_jobs_file()}")
        add("ok", f"output dir writable: {get_output_dir()}")
        add("ok", f"scripts dir writable: {get_scripts_dir()}")
        for directory in (get_output_dir(), get_scripts_dir()):
            probe = directory / ".doctor-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
    except Exception as exc:
        add("fail", f"cron paths are not writable: {exc}")

    if get_jobs_file().exists():
        try:
            json.loads(get_jobs_file().read_text(encoding="utf-8"))
            add("ok", "jobs file JSON is valid")
        except Exception as exc:
            add("fail", f"jobs file JSON is invalid or unreadable: {exc}")

    try:
        store = DeliveryStore()
        stats = store.stats()
        add("ok", f"delivery db readable/writable: {store.path}")
        if stats.get("dead", 0):
            add("fail", f"dead delivery events: {stats['dead']}")
        if stats.get("failed", 0):
            add("warn", f"failed delivery events pending retry: {stats['failed']}")
        stale = store.stale_delivering(max_age_seconds=600)
        if stale:
            add("warn", f"stale delivering events: {len(stale)}")
    except Exception as exc:
        add("fail", f"delivery db error: {exc}")

    if is_cron_scheduler_running():
        add("ok", "scheduler is running")
    else:
        add("warn", "scheduler is stopped; jobs will only run via manual cron tick")

    try:
        active_jobs = list_jobs(include_disabled=False)
    except Exception as exc:
        active_jobs = []
        add("fail", f"jobs could not be loaded: {exc}")

    for job in active_jobs:
        if not job.get("next_run_at"):
            add("warn", f"active job {job.get('id')} has no next_run_at")
        target = parse_target(job)
        if target.target_type == "origin" and not target.target_id:
            add("fail", f"active job {job.get('id')} origin delivery has no thread id")
        elif target.target_type == "webhook":
            webhook_error = validate_webhook_url(target.target_id)
            if webhook_error:
                add("fail", f"active job {job.get('id')} webhook delivery invalid: {webhook_error}")
        elif target.target_type == "unsupported":
            add("fail", f"active job {job.get('id')} has unsupported delivery target: {target.raw}")

    exit_code = 2 if failures else (1 if warnings else 0)
    return CronCommandResult("\n".join(lines), exit_code=exit_code)


def run_tick() -> CronCommandResult:
    result = cron_tick()
    lines = [
        (
            "Tick: "
            f"due={result.due} ran={result.ran} "
            f"succeeded={result.succeeded} failed={result.failed} skipped={result.skipped}"
        )
    ]
    for item in result.results:
        if not item.success:
            lines.append(f"  failed {item.job_id}: {item.error or '-'}")
    return CronCommandResult("\n".join(lines), exit_code=1 if result.failed else 0)


def existing_job_skills(job_id: str) -> list[str]:
    job = get_job(job_id)
    if not job:
        return []
    skills = list(job.get("skills") or [])
    skill = str(job.get("skill") or "").strip()
    if skill and skill not in skills:
        skills.append(skill)
    return skills


def test_delivery(
    *, target: str, session_id: str | None = None
) -> CronCommandResult:
    from cron.delivery import JobRunResult, enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.jobs import now

    job: dict[str, Any] = {
        "id": "test-delivery",
        "name": "test-delivery",
        "deliver": target,
    }
    if target == "origin":
        if not session_id:
            return CronCommandResult(
                "origin test-delivery requires --session-id", exit_code=2
            )
        job["origin"] = {"thread_id": session_id}

    result = enqueue_result(
        job,
        JobRunResult(
            success=True,
            output_doc="# Test Delivery\n\nThis is a cron delivery test.",
            final_response="This is a cron delivery test.",
        ),
        output_path="",
        run_at=now(),
    )
    process_due(limit=20)
    if result is None:
        return CronCommandResult("No delivery event created.", exit_code=2)
    stored = DeliveryStore().get(result["id"])
    text = (
        f"test-delivery event={stored['id']} "
        f"target={stored['target']} status={stored['status']} "
        f"error={stored['last_error'] or '-'}"
    )
    if stored["status"] in {"delivered", "pending"}:
        return CronCommandResult(text, exit_code=0)
    if stored["status"] == "dead":
        return CronCommandResult(text, exit_code=2)
    return CronCommandResult(text, exit_code=1)

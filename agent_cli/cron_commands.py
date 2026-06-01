from __future__ import annotations

from dataclasses import dataclass
import json
import sys
from typing import Any

from cron.jobs import get_job, list_jobs
from cron.paths import display_cron_home, get_jobs_file, get_output_dir, get_scripts_dir


def _short(value: Any, length: int = 8) -> str:
    text = str(value or "-")
    return text[:length] if text != "-" else "-"


def _preview(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text or "-"
    return text[: limit - 3] + "..."

run_cronjob_action = None
cron_tick = None


def _get_run_cronjob_action():
    global run_cronjob_action
    if run_cronjob_action is None:
        from agent_tools.public.cronjob import run_cronjob_action as _fn
        run_cronjob_action = _fn
    return run_cronjob_action


def _get_cron_tick():
    global cron_tick
    if cron_tick is None:
        from agent_core.cron_lifecycle import tick as _tick
        cron_tick = _tick
    return cron_tick


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


def _deliver_mentions_origin(deliver: str | None) -> bool:
    return any(part.strip().lower() == "origin" for part in str(deliver or "").split(","))


def _cli_origin(session_id: str) -> dict[str, str]:
    return {
        "source_type": "cli",
        "session_id": session_id,
        "thread_id": session_id,
    }


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
    if _deliver_mentions_origin(deliver) and not session_id:
        return CronCommandResult(
            "origin delivery requires an active CLI session",
            exit_code=2,
        )
    if deliver is None:
        deliver = "local" if top_level else "origin"
    if _deliver_mentions_origin(deliver) and top_level:
        return CronCommandResult(
            "origin delivery requires an active CLI session",
            exit_code=2,
        )
    origin_thread_id = session_id if _deliver_mentions_origin(deliver) else None
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
    if _deliver_mentions_origin(deliver) and (top_level or not session_id):
        return CronCommandResult(
            "origin delivery requires an active CLI session",
            exit_code=2,
        )
    if _deliver_mentions_origin(deliver):
        kwargs["origin"] = _cli_origin(str(session_id))
    result = _get_run_cronjob_action()(
        "update",
        origin_thread_id=session_id if _deliver_mentions_origin(deliver) else None,
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


def run_cron_job(
    *,
    job_id: str,
    dry_run: bool = False,
    now_text: str | None = None,
) -> CronCommandResult:
    if not dry_run:
        return simple_job_action("run", job_id=job_id)
    from cron.state_store import StateStore, utc_now

    store = StateStore()
    try:
        plan = store.plan_job_claim(job_id, now_text=now_text or utc_now().isoformat())
    except KeyError:
        return CronCommandResult(f"Cron job not found: {job_id}.", exit_code=2)

    job = plan.get("job") or {}
    timeouts = plan.get("timeouts") or {}
    idle_timeout = timeouts.get("idle_timeout_seconds")
    max_runtime = timeouts.get("max_runtime_seconds")
    targets = plan.get("delivery_targets") or []
    target_labels = []
    for target in targets:
        if isinstance(target, dict):
            target_labels.append(str(target.get("target") or target.get("target_type") or target))
        else:
            target_labels.append(str(target))
    lines = [
        f"Dry run for cron job {job_id}",
        f"Decision: {plan['decision']}",
        f"Due: {'yes' if plan.get('due') else 'no'}",
        f"Enabled: {job.get('enabled', '-')}",
        f"Next run: {plan.get('next_run_at') or '-'}",
        f"Next scheduled: {plan.get('next_scheduled_at') or '-'}",
        f"Concurrency: {plan.get('concurrency_key') or '-'} ({plan.get('concurrency_policy') or '-'})",
        "Timeouts: "
        f"idle={str(idle_timeout) + 's' if idle_timeout is not None else '-'} "
        f"max_runtime={str(max_runtime) + 's' if max_runtime is not None else '-'}",
        f"Delivery targets: {', '.join(target_labels) if target_labels else '-'}",
        f"Active same-key runs: {plan.get('active_run_count', 0)}",
    ]
    return CronCommandResult("\n".join(lines))


def serve_cron(
    *,
    interval_seconds: float = 60,
    lease_seconds: int = 180,
    once: bool = False,
) -> CronCommandResult:
    from cron.service import serve

    exit_code = serve(
        interval_seconds=interval_seconds,
        lease_seconds=lease_seconds,
        once=once,
    )
    return CronCommandResult("Cron service exited.", exit_code=exit_code)


def _cron_service_result(result) -> CronCommandResult:
    return CronCommandResult(result.message, exit_code=result.exit_code)


def install_cron_service(
    *,
    interval_seconds: float,
    lease_seconds: int,
    force: bool = False,
) -> CronCommandResult:
    from cron.service_manager import install_service

    return _cron_service_result(
        install_service(
            interval_seconds=interval_seconds,
            lease_seconds=lease_seconds,
            force=force,
        )
    )


def uninstall_cron_service() -> CronCommandResult:
    from cron.service_manager import uninstall_service

    return _cron_service_result(uninstall_service())


def start_cron_service() -> CronCommandResult:
    from cron.service_manager import start_service

    return _cron_service_result(start_service())


def stop_cron_service() -> CronCommandResult:
    from cron.service_manager import stop_service

    return _cron_service_result(stop_service())


def restart_cron_service() -> CronCommandResult:
    from cron.service_manager import restart_service

    return _cron_service_result(restart_service())


def cron_service_logs(*, lines: int = 100) -> CronCommandResult:
    from cron.service_manager import service_logs

    return _cron_service_result(service_logs(lines=lines))


def _service_manager_summary_line(status) -> str:
    if not status.supported:
        return "Service manager: unsupported"
    installed = "installed" if status.installed else "not-installed"
    active = "active" if status.active else "inactive"
    enabled = "enabled" if status.enabled else "disabled"
    return f"Service manager: {status.platform} {installed} {active} {enabled}"


def _last_tick_line(last_tick: dict[str, Any]) -> str:
    return (
        "Last tick: "
        f"due={last_tick.get('due', 0)} "
        f"ran={last_tick.get('ran', 0)} "
        f"succeeded={last_tick.get('succeeded', 0)} "
        f"failed={last_tick.get('failed', 0)} "
        f"skipped={last_tick.get('skipped', 0)}"
    )


def cron_service_status() -> CronCommandResult:
    from cron.service_manager import compose_service_status

    status = compose_service_status()
    ready = (
        status.supported
        and status.installed
        and status.enabled
        and status.active
        and status.heartbeat_fresh
    )
    lines = [
        _service_manager_summary_line(status),
        f"Automatic scheduling: {'enabled' if ready else 'not-ready'}",
    ]
    if status.pid:
        lines.append(f"PID: {status.pid}")
    lines.append(f"Heartbeat: {'fresh' if status.heartbeat_fresh else 'stale-or-missing'}")
    if status.process_state:
        lines.append(f"Process state: {status.process_state}")
    if status.leader_state:
        lines.append(f"Leader state: {status.leader_state}")
    if status.last_heartbeat_at:
        lines.append(f"Last heartbeat: {status.last_heartbeat_at}")
    if status.last_tick:
        lines.append(_last_tick_line(status.last_tick))
    if status.last_error:
        lines.append(f"Last service error: {status.last_error}")
    if status.exit_reason:
        lines.append(f"Exit reason: {status.exit_reason}")
    if status.error:
        lines.append(f"Platform error: {status.error}")
    return CronCommandResult("\n".join(lines), exit_code=0 if status.supported else 2)


def _service_status_lines() -> list[str]:
    from cron.leader import SchedulerLeaderLease
    from cron.service_state import read_service_status, service_status_path

    path = service_status_path()
    status = read_service_status(path=path) or {}
    lines = [
        f"Service status: {path}",
        f"Scheduler service: {status.get('process_state') or 'unknown'}",
        f"Service PID: {status.get('pid') or '-'}",
        f"Leader state: {status.get('leader_state') or 'unknown'}",
        f"Last heartbeat: {status.get('last_heartbeat_at') or '-'}",
    ]

    last_tick = status.get("last_tick")
    if isinstance(last_tick, dict):
        lines.append(
            "Last tick: "
            f"due={last_tick.get('due', 0)} "
            f"ran={last_tick.get('ran', 0)} "
            f"succeeded={last_tick.get('succeeded', 0)} "
            f"failed={last_tick.get('failed', 0)} "
            f"skipped={last_tick.get('skipped', 0)}"
        )
    if status.get("last_error"):
        lines.append(f"Last service error: {status['last_error']}")
    if status.get("exit_reason"):
        lines.append(f"Exit reason: {status['exit_reason']}")

    lease = SchedulerLeaderLease().current()
    if lease is not None:
        lines.append(f"Lease owner: {lease.owner_id}")
        lines.append(f"Lease expires: {lease.expires_at}")
    return lines


def cron_status() -> CronCommandResult:
    from cron.delivery_registry import default_delivery_registry
    from cron.runner_client import runner_mode_diagnostic
    from cron.state_store import StateStore

    jobs = list_jobs(include_disabled=True)
    state_store = StateStore()
    mode, mode_ok = runner_mode_diagnostic()
    mode_label = f"{'ok' if mode_ok else 'UNSUPPORTED'} ({mode})"
    counts = state_store.job_counts_by_state()
    count_text = ", ".join(f"{state}={count}" for state, count in sorted(counts.items())) or "-"
    lines = [
        *_service_status_lines(),
        f"Cron home: {display_cron_home()}",
        f"Cron sqlite: {state_store.path}",
        f"Runner mode: {mode_label}",
        f"Jobs file: {get_jobs_file()}",
        f"Output dir: {get_output_dir()}",
        f"Scripts dir: {get_scripts_dir()}",
        f"Jobs: {len(jobs)}",
        f"Job states: {count_text}",
        f"Delivery adapters: {', '.join(default_delivery_registry().adapter_keys())}",
    ]

    from cron.state_store import StateStore

    summary = StateStore().cron_status_summary(top_n=5)
    next_due = summary.get("next_due")
    lines.append(f"Next due: {next_due['id']} {next_due.get('name') or ''} at {next_due['next_run_at']}" if next_due else "Next due: -")
    lines.append(f"Running: {len(summary['running'])}")
    for run in summary["running"]:
        lines.append(
            f"  {_short(run['id'])} job={_short(run['job_id'])} "
            f"activity={run.get('last_activity_desc') or '-'}"
        )
    lines.append(f"Queued: {len(summary['queued'])}")
    for run in summary["queued"]:
        lines.append(f"  {_short(run['id'])} job={_short(run['job_id'])} scheduled={run.get('scheduled_for') or '-'}")
    lines.append(f"Stale: {len(summary['stale'])}")
    if summary.get("latest_failed_run"):
        failed = summary["latest_failed_run"]
        lines.append(f"Latest failed run: {_short(failed['id'])} exit={failed.get('exit_reason') or '-'} error={_preview(failed.get('error'))}")
    
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
    origin_pending = store.origin_pending_count()
    if origin_pending:
        lines.append(f"Origin poll pending: {origin_pending}")
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


def _add_service_heartbeat_check(add) -> None:
    from cron.jobs import now
    from cron.service_state import (
        is_status_fresh,
        read_service_status,
        service_status_path,
    )

    try:
        path = service_status_path()
        status = read_service_status(path=path)
    except Exception as exc:
        add("warn", f"cron service heartbeat: unreadable ({exc})")
        return

    if status is None:
        if path.exists():
            add("warn", "cron service heartbeat: unreadable")
        else:
            add(
                "warn",
                "cron service heartbeat: missing; start automatic scheduling "
                "with `agent cron serve`",
            )
        return

    if is_status_fresh(
        status,
        now_text=now().isoformat(),
        stale_after_seconds=180,
    ):
        leader_state = status.get("leader_state") or "unknown"
        add("ok", f"cron service heartbeat: fresh ({leader_state})")
    else:
        add("warn", "cron service heartbeat: stale; automatic scheduling may be stopped")


def cron_doctor() -> CronCommandResult:
    from cron.delivery import validate_webhook_url
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_targets import DeliveryIdentity
    from cron.delivery_store import DeliveryStore
    from cron.paths import get_runner_tmp_dir
    from cron.runner_client import runner_mode_diagnostic
    from cron.runner_subprocess import subprocess_timeout_diagnostic
    from cron.state_store import StateStore

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
        state_store = StateStore()
        add("ok", f"cron sqlite: {state_store.path}")
        counts = state_store.job_counts_by_state()
        count_text = ", ".join(f"{state}={count}" for state, count in sorted(counts.items())) or "-"
        add("ok", f"job states: {count_text}")
        registry = default_delivery_registry()
        add("ok", f"delivery adapters: {', '.join(registry.adapter_keys())}")
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

    _add_service_heartbeat_check(add)

    try:
        active_jobs = list_jobs(include_disabled=False)
    except Exception as exc:
        active_jobs = []
        add("fail", f"jobs could not be loaded: {exc}")

    for job in active_jobs:
        if not job.get("next_run_at"):
            add("warn", f"active job {job.get('id')} has no next_run_at")
        origin = DeliveryIdentity.from_job_origin(job.get("origin"))
        validation = default_delivery_registry().validate_targets(
            job.get("deliver"),
            origin=origin,
            job=job,
        )
        if not validation.ok:
            add("fail", f"active job {job.get('id')} delivery invalid: {validation.error}")
            continue
        for target in validation.targets:
            if target.target_type == "webhook":
                webhook_error = validate_webhook_url(target.address)
                if webhook_error:
                    add("fail", f"active job {job.get('id')} webhook delivery invalid: {webhook_error}")

    exit_code = 2 if failures else (1 if warnings else 0)
    return CronCommandResult("\n".join(lines), exit_code=exit_code)


def run_tick() -> CronCommandResult:
    from cron.leader import SchedulerLeaderLease

    result = _get_cron_tick()()
    lines = [
        (
            "Tick: "
            f"due={result.due} ran={result.ran} "
            f"succeeded={result.succeeded} failed={result.failed} skipped={result.skipped}"
        )
    ]
    try:
        lease = SchedulerLeaderLease().current()
    except Exception:
        lease = None
    if lease is not None:
        lines.append(
            f"Scheduler lease owner: {lease.owner_id} expires={lease.expires_at}"
        )
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


def list_cron_runs(job_id: str | None = None, *, limit: int = 20) -> CronCommandResult:
    from cron.state_store import StateStore

    store = StateStore()
    rows = store.list_runs(job_id=job_id, limit=limit)
    if not rows:
        return CronCommandResult("No cron runs.")
    lines = ["Cron Runs:"]
    for run in rows:
        lines.append(
            "  "
            f"{_short(run['id'])} "
            f"job={_short(run['job_id'])} "
            f"status={run['status']} "
            f"scheduled={run.get('scheduled_for') or '-'} "
            f"exit={run.get('exit_reason') or '-'} "
            f"delivery={run.get('delivery_status') or '-'}"
        )
        activity = run.get("last_activity_desc") or run.get("current_tool")
        if run["status"] in {"queued", "claimed", "running"} and activity:
            lines.append(f"    activity={activity} heartbeat={run.get('heartbeat_at') or '-'}")
    return CronCommandResult("\n".join(lines))


def cron_logs(job_id: str, *, limit: int = 10) -> CronCommandResult:
    from cron.state_store import StateStore

    store = StateStore()
    try:
        job = store.get_job(job_id)
    except KeyError:
        return CronCommandResult(f"Cron job not found: {job_id}.", exit_code=2)
    runs = store.list_runs(job_id=job_id, limit=limit)
    lines = [
        f"Cron Logs: {job.get('name') or job_id}",
        f"Job: {job_id}",
        f"Next run: {job.get('next_run_at') or '-'}",
    ]
    if not runs:
        lines.append("No runs.")
        return CronCommandResult("\n".join(lines))
    lines.append("Recent runs:")
    for run in runs:
        lines.append(
            f"  {_short(run['id'])} status={run['status']} "
            f"scheduled={run.get('scheduled_for') or '-'} "
            f"output={run.get('output_path') or '-'}"
        )
        if run.get("error"):
            lines.append(f"    error={_preview(run['error'])}")
        elif run.get("final_response"):
            lines.append(f"    final={_preview(run['final_response'])}")
    return CronCommandResult("\n".join(lines))


def list_deliveries(selector: str | None = None, *, limit: int = 20) -> CronCommandResult:
    from cron.state_store import StateStore

    store = StateStore()
    job_id = None
    run_id = None
    filter_label = "all"
    if selector:
        try:
            store.get_job(selector)
            job_id = selector
            filter_label = f"job={selector}"
        except KeyError:
            try:
                store.get_run(selector)
                run_id = selector
                filter_label = f"run={selector}"
            except KeyError:
                return CronCommandResult(f"No cron job or run found for: {selector}.", exit_code=2)
    events = store.list_delivery_events(job_id=job_id, run_id=run_id, limit=limit)
    if not events:
        return CronCommandResult(f"No delivery events ({filter_label}).")
    lines = [f"Delivery Events ({filter_label}):"]
    for event in events:
        lines.append(
            "  "
            f"{_short(event['id'])} "
            f"job={_short(event.get('job_id'))} "
            f"run={_short(event.get('run_id'))} "
            f"target={event.get('target') or '-'} "
            f"adapter={event.get('adapter_key') or '-'} "
            f"status={event.get('status') or '-'} "
            f"attempts={event.get('attempt_count') or 0} "
            f"next={event.get('next_attempt_at') or '-'}"
        )
        if event.get("last_error"):
            lines.append(f"    error={_preview(event['last_error'])}")
    return CronCommandResult("\n".join(lines))


def retry_delivery(event_id: str) -> CronCommandResult:
    from cron.delivery_store import DeliveryStore

    store = DeliveryStore()
    try:
        before = store.get(event_id)
    except KeyError:
        return CronCommandResult(f"Delivery event not found: {event_id}.", exit_code=2)
    try:
        after = store.retry(event_id)
    except ValueError as exc:
        return CronCommandResult(str(exc), exit_code=2)
    except Exception as exc:
        return CronCommandResult(f"Failed to retry delivery event: {exc}", exit_code=1)
    return CronCommandResult(
        f"Delivery event {event_id} reset: {before['status']} -> {after['status']}."
    )


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
    target_parts = {part.strip().lower() for part in str(target).split(",") if part.strip()}
    if "origin" in target_parts:
        if not session_id:
            return CronCommandResult(
                "origin test-delivery requires --session-id", exit_code=2
            )
        job["origin"] = _cli_origin(session_id)

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
    events = result if isinstance(result, list) else [result]
    stored_events = [DeliveryStore().get(event["id"]) for event in events]
    lines = [
        (
            f"test-delivery event={stored['id']} "
            f"target={stored['target']} status={stored['status']} "
            f"error={stored['last_error'] or '-'}"
        )
        for stored in stored_events
    ]
    text = "\n".join(lines)
    statuses = {stored["status"] for stored in stored_events}
    if statuses <= {"delivered", "pending"}:
        return CronCommandResult(text, exit_code=0)
    if "dead" in statuses:
        return CronCommandResult(text, exit_code=2)
    return CronCommandResult(text, exit_code=1)

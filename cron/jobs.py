from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cron.paths import (
    atomic_write_json,
    ensure_cron_dirs,
    get_jobs_file,
    get_output_dir,
    secure_file,
)

try:
    from croniter import croniter
except ImportError:  # pragma: no cover - exercised only without optional dep
    croniter = None


ONESHOT_GRACE_SECONDS = 120
MIN_RECURRING_GRACE_SECONDS = 120
MAX_RECURRING_GRACE_SECONDS = 7200
JOBS_FILE = get_jobs_file()

_jobs_file_lock = threading.Lock()


def now() -> datetime:
    return datetime.now().astimezone()


def parse_duration(value: str) -> int:
    text = str(value or "").strip().lower()
    match = re.fullmatch(
        r"(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)",
        text,
    )
    if not match:
        raise ValueError(
            f"Invalid duration: {value!r}. Use format like '30m', '2h', or '1d'."
        )

    amount = int(match.group(1))
    unit = match.group(2)[0]
    return amount * {"m": 1, "h": 60, "d": 1440}[unit]


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _ensure_aware(dt)


def parse_schedule(schedule: str) -> dict[str, Any]:
    raw = str(schedule or "").strip()
    lowered = raw.lower()
    if not raw:
        raise ValueError("schedule is required")

    if lowered.startswith("every "):
        minutes = parse_duration(raw[6:])
        return {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"}

    parts = raw.split()
    if len(parts) >= 5:
        if croniter is None:
            raise ValueError("Cron expressions require the croniter package.")
        try:
            croniter(raw)
        except Exception as exc:
            raise ValueError(f"Invalid cron expression {raw!r}: {exc}") from exc
        return {"kind": "cron", "expr": raw, "display": raw}

    if "T" in raw or re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        try:
            dt = _parse_datetime(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid timestamp {raw!r}: {exc}") from exc
        return {
            "kind": "once",
            "run_at": dt.isoformat(),
            "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}",
        }

    minutes = parse_duration(raw)
    run_at = now() + timedelta(minutes=minutes)
    return {
        "kind": "once",
        "run_at": run_at.isoformat(),
        "display": f"once in {raw}",
    }


def _normalize_workdir(workdir: str | None) -> str | None:
    if workdir is None or not str(workdir).strip():
        return None

    path = Path(str(workdir).strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Cron workdir must be an absolute path: {workdir!r}")

    resolved = path.resolve()
    if not resolved.exists():
        raise ValueError(f"Cron workdir does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Cron workdir is not a directory: {resolved}")
    return str(resolved)


def _normalize_skills(skill: str | None, skills: Any) -> list[str]:
    if skills is None:
        raw_items = [skill] if skill else []
    elif isinstance(skills, str):
        raw_items = [skills]
    else:
        raw_items = list(skills)

    normalized: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _normalize_context_from(value: str | list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [value]
    else:
        items = value
    normalized = [str(item).strip() for item in items if str(item).strip()]
    return normalized or None


def _normalize_enabled_toolsets(value: Any) -> list[str] | None:
    if value is None:
        return None
    items = [value] if isinstance(value, str) else list(value)
    normalized = [str(item).strip() for item in items if str(item).strip()]
    return normalized or None


def load_jobs() -> list[dict[str, Any]]:
    ensure_cron_dirs()
    path = get_jobs_file()
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cron jobs file is invalid JSON: {path}") from exc

    jobs = payload.get("jobs", [])
    if not isinstance(jobs, list):
        raise RuntimeError("Cron jobs file is invalid: 'jobs' must be a list.")
    return jobs


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    atomic_write_json(get_jobs_file(), {"jobs": jobs, "updated_at": now().isoformat()})


def list_jobs(include_disabled: bool = False) -> list[dict[str, Any]]:
    jobs = load_jobs()
    if not include_disabled:
        jobs = [job for job in jobs if job.get("enabled", True)]
    return copy.deepcopy(jobs)


def get_job(job_id: str) -> dict[str, Any] | None:
    for job in load_jobs():
        if job.get("id") == job_id:
            return copy.deepcopy(job)
    return None


def compute_next_run(
    schedule: dict[str, Any],
    last_run_at: str | None = None,
    base: datetime | None = None,
) -> str | None:
    current = _ensure_aware(base or now())
    kind = schedule.get("kind")

    if kind == "once":
        if last_run_at:
            return None
        return schedule.get("run_at")

    if kind == "interval":
        minutes = int(schedule["minutes"])
        anchor = _parse_datetime(last_run_at) if last_run_at else current
        return (anchor + timedelta(minutes=minutes)).isoformat()

    if kind == "cron":
        if croniter is None:
            raise ValueError("Cron expressions require the croniter package.")
        return croniter(schedule["expr"], current).get_next(datetime).isoformat()

    raise ValueError(f"Unsupported schedule kind: {kind!r}")


def create_job(
    prompt: str,
    schedule: str,
    name: str | None = None,
    repeat: int | None = None,
    deliver: str | None = None,
    origin: dict[str, Any] | None = None,
    skill: str | None = None,
    skills: list[str] | str | None = None,
    model: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    script: str | None = None,
    context_from: str | list[str] | None = None,
    enabled_toolsets: list[str] | str | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    parsed_schedule = parse_schedule(schedule)
    repeat_times = repeat
    if parsed_schedule["kind"] == "once" and repeat_times is None:
        repeat_times = 1

    normalized_skills = _normalize_skills(skill, skills)
    current = now()
    job = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or prompt or "cron job")[:50].strip() or "cron job",
        "prompt": prompt or "",
        "schedule": parsed_schedule,
        "schedule_display": parsed_schedule.get("display", schedule),
        "enabled": True,
        "state": "scheduled",
        "next_run_at": compute_next_run(parsed_schedule, base=current),
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "repeat": {"times": repeat_times, "completed": 0},
        "deliver": deliver or ("origin" if origin else "local"),
        "origin": copy.deepcopy(origin) if origin else None,
        "workdir": _normalize_workdir(workdir),
        "script": script or None,
        "context_from": _normalize_context_from(context_from),
        "skills": normalized_skills,
        "skill": normalized_skills[0] if normalized_skills else None,
        "enabled_toolsets": _normalize_enabled_toolsets(enabled_toolsets),
        "model": model or None,
        "provider": provider or None,
        "base_url": base_url or None,
        "created_at": current.isoformat(),
    }

    with _jobs_file_lock:
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)
    return copy.deepcopy(job)


def update_job(job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    with _jobs_file_lock:
        jobs = load_jobs()
        for index, job in enumerate(jobs):
            if job.get("id") != job_id:
                continue

            normalized_updates = dict(updates)
            if "workdir" in normalized_updates:
                normalized_updates["workdir"] = _normalize_workdir(
                    normalized_updates["workdir"]
                )
            if "schedule" in normalized_updates and isinstance(
                normalized_updates["schedule"], str
            ):
                parsed_schedule = parse_schedule(normalized_updates["schedule"])
                normalized_updates["schedule"] = parsed_schedule
                normalized_updates["schedule_display"] = parsed_schedule.get("display")
                normalized_updates["next_run_at"] = compute_next_run(parsed_schedule)

            merged = dict(job)
            merged.update(normalized_updates)
            jobs[index] = merged
            save_jobs(jobs)
            return copy.deepcopy(merged)

    raise KeyError(f"Cron job not found: {job_id}")


def remove_job(job_id: str) -> bool:
    with _jobs_file_lock:
        jobs = load_jobs()
        kept = [job for job in jobs if job.get("id") != job_id]
        if len(kept) == len(jobs):
            return False
        save_jobs(kept)
        return True


def pause_job(job_id: str, reason: str | None = None) -> dict[str, Any]:
    return update_job(
        job_id,
        {
            "enabled": False,
            "state": "paused",
            "paused_reason": reason,
            "paused_at": now().isoformat(),
        },
    )


def resume_job(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise KeyError(f"Cron job not found: {job_id}")
    return update_job(
        job_id,
        {
            "enabled": True,
            "state": "scheduled",
            "next_run_at": compute_next_run(job["schedule"]),
        },
    )


def trigger_job(job_id: str) -> dict[str, Any]:
    return update_job(
        job_id,
        {"enabled": True, "state": "scheduled", "next_run_at": now().isoformat()},
    )


def _recurring_grace_seconds(schedule: dict[str, Any], run_at: datetime) -> int:
    if schedule.get("kind") == "interval":
        half_period = int(schedule.get("minutes", 1)) * 30
    elif schedule.get("kind") == "cron":
        if croniter is None:
            half_period = MIN_RECURRING_GRACE_SECONDS
        else:
            try:
                previous_run = croniter(schedule["expr"], run_at).get_prev(datetime)
                next_run = croniter(schedule["expr"], run_at).get_next(datetime)
                half_period = int((next_run - previous_run).total_seconds() / 2)
            except Exception:
                half_period = MIN_RECURRING_GRACE_SECONDS
    else:
        half_period = MIN_RECURRING_GRACE_SECONDS

    return max(
        MIN_RECURRING_GRACE_SECONDS,
        min(half_period, MAX_RECURRING_GRACE_SECONDS),
    )


def get_due_jobs(now_dt: datetime | None = None) -> list[dict[str, Any]]:
    current = _ensure_aware(now_dt or now())
    due_jobs: list[dict[str, Any]] = []

    for job in load_jobs():
        if not job.get("enabled", True):
            continue
        if job.get("state") not in (None, "scheduled"):
            continue

        next_run_at = job.get("next_run_at")
        if not next_run_at:
            continue

        run_at = _parse_datetime(next_run_at)
        if run_at > current:
            continue

        schedule = job.get("schedule") or {}
        if schedule.get("kind") == "once":
            if current - run_at <= timedelta(seconds=ONESHOT_GRACE_SECONDS):
                due_jobs.append(copy.deepcopy(job))
            continue

        grace_seconds = _recurring_grace_seconds(schedule, run_at)
        if current - run_at > timedelta(seconds=grace_seconds):
            update_job(job["id"], {"next_run_at": compute_next_run(schedule, base=current)})
            continue

        due_jobs.append(copy.deepcopy(job))

    return due_jobs


def advance_next_run(job_id: str, run_at: datetime | None = None) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise KeyError(f"Cron job not found: {job_id}")

    schedule = job.get("schedule") or {}
    effective_run_at = _ensure_aware(run_at or now())
    kind = schedule.get("kind")
    if kind == "once":
        next_run_at = None
    elif kind == "interval":
        next_run_at = (
            effective_run_at + timedelta(minutes=int(schedule["minutes"]))
        ).isoformat()
    elif kind == "cron":
        next_run_at = compute_next_run(schedule, base=effective_run_at)
    else:
        raise ValueError(f"Unsupported schedule kind: {kind!r}")

    return update_job(job_id, {"next_run_at": next_run_at})


def mark_job_run(
    job_id: str,
    success: bool,
    error: str | None = None,
    run_at: datetime | None = None,
) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise KeyError(f"Cron job not found: {job_id}")

    repeat = dict(job.get("repeat") or {"times": None, "completed": 0})
    repeat["completed"] = int(repeat.get("completed") or 0) + 1
    repeat_times = repeat.get("times")
    completed = repeat_times is not None and repeat["completed"] >= int(repeat_times)

    updates = {
        "last_run_at": _ensure_aware(run_at or now()).isoformat(),
        "last_status": "ok" if success else "error",
        "last_error": None if success else error,
        "repeat": repeat,
        "state": "completed" if completed else "scheduled",
        "enabled": False if completed else job.get("enabled", True),
    }
    return update_job(job_id, updates)


def save_job_output(
    job_id: str,
    output_doc: str,
    run_at: datetime | None = None,
) -> str:
    ensure_cron_dirs()
    timestamp = _ensure_aware(run_at or now()).strftime("%Y%m%d_%H%M%S")
    output_dir = get_output_dir() / str(job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{timestamp}.md"
    path.write_text(output_doc, encoding="utf-8")
    secure_file(path)
    return str(path)


def latest_job_output(job_id: str) -> str | None:
    output_dir = get_output_dir() / str(job_id)
    if not output_dir.exists():
        return None

    files = sorted(output_dir.glob("*.md"), key=lambda path: path.stat().st_mtime)
    if not files:
        return None
    return files[-1].read_text(encoding="utf-8")

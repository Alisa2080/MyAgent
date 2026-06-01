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

CONCURRENCY_POLICIES = {
    "queue_one",
    "queue_all",
    "replace_running",
    "skip_if_running",
}
DEFAULT_CONCURRENCY_POLICY = "queue_one"


def normalize_concurrency_policy(value: Any) -> str:
    """Normalize and validate concurrency policy."""
    if value is None or str(value).strip() == "":
        return DEFAULT_CONCURRENCY_POLICY
    normalized = str(value).strip().lower()
    if normalized not in CONCURRENCY_POLICIES:
        raise ValueError(f"Unsupported concurrency_policy: {normalized}")
    return normalized


def normalize_concurrency_key(value: Any, job_id: str) -> str:
    """Normalize concurrency key, defaulting to job:<job_id>."""
    normalized = str(value).strip() if value is not None else ""
    return normalized or f"job:{job_id}"

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


def _normalize_timeout_seconds(value: Any, *, allow_zero: bool = True) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout seconds must be an integer") from exc
    if seconds < 0 or (seconds == 0 and not allow_zero):
        raise ValueError("timeout seconds must be >= 0")
    return seconds


def _normalize_delivery_config(job: dict[str, Any]) -> dict[str, Any]:
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_targets import DeliveryIdentity

    normalized = dict(job)
    origin = DeliveryIdentity.from_job_origin(normalized.get("origin"))
    deliver = normalized.get("deliver") or ("origin" if origin else "local")
    validation = default_delivery_registry().validate_targets(
        deliver,
        origin=origin,
        job=normalized,
    )
    if not validation.ok:
        raise ValueError(validation.error or f"unsupported delivery target: {deliver}")
    normalized["deliver"] = deliver
    normalized["delivery_targets"] = [target.to_json() for target in validation.targets]
    if all(target.target_type != "origin" for target in validation.targets):
        normalized["origin"] = None
    return normalized


def _store():
    from cron.state_store import StateStore

    return StateStore()


def load_jobs() -> list[dict[str, Any]]:
    return _store().list_jobs(include_disabled=True)


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    _store().replace_jobs(jobs)


def list_jobs(include_disabled: bool = False) -> list[dict[str, Any]]:
    return copy.deepcopy(_store().list_jobs(include_disabled=include_disabled))


def get_job(job_id: str) -> dict[str, Any] | None:
    job = _store().get_job(job_id)
    return copy.deepcopy(job) if job is not None else None


def _find_job_index(jobs: list[dict[str, Any]], job_id: str) -> int:
    for index, job in enumerate(jobs):
        if job.get("id") == job_id:
            return index
    raise KeyError(f"Cron job not found: {job_id}")


def _normalize_repeat_update(value: Any, existing: Any = None) -> Any:
    existing_completed = 0
    if isinstance(existing, dict):
        existing_completed = int(existing.get("completed") or 0)

    if isinstance(value, dict):
        normalized = dict(value)
        normalized.setdefault("completed", existing_completed)
        if normalized.get("times") is not None:
            try:
                times = int(normalized["times"])
            except (TypeError, ValueError):
                return normalized
            normalized["times"] = times if times > 0 else None
        return normalized

    try:
        times = int(value)
    except (TypeError, ValueError):
        return value
    return {"times": times if times > 0 else None, "completed": existing_completed}


def _coerce_repeat_state(value: Any) -> dict[str, Any]:
    if value is None:
        return {"times": None, "completed": 0}
    if isinstance(value, dict):
        repeat = dict(value)
        repeat.setdefault("times", None)
        repeat["completed"] = int(repeat.get("completed") or 0)
        if repeat.get("times") is not None:
            repeat["times"] = int(repeat["times"])
        return repeat
    normalized = _normalize_repeat_update(value)
    if isinstance(normalized, dict):
        return normalized
    return {"times": None, "completed": 0}


def _normalize_timeout_field(value: Any) -> int | None:
    """Coerce a timeout field value to int (>=0) or None.

    - None / empty string / whitespace → None
    - Numeric string or int → int; 0 is allowed (means "disabled / no timeout")
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _normalize_updates(
    updates: dict[str, Any],
    existing_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_updates = dict(updates)
    if "workdir" in normalized_updates:
        normalized_updates["workdir"] = _normalize_workdir(
            normalized_updates["workdir"]
        )
    if "repeat" in normalized_updates:
        existing_repeat = (existing_job or {}).get("repeat")
        normalized_updates["repeat"] = _normalize_repeat_update(
            normalized_updates["repeat"],
            existing_repeat,
        )
    if "schedule" in normalized_updates and isinstance(
        normalized_updates["schedule"], str
    ):
        parsed_schedule = parse_schedule(normalized_updates["schedule"])
        normalized_updates["schedule"] = parsed_schedule
        normalized_updates["schedule_display"] = parsed_schedule.get("display")
        normalized_updates["next_run_at"] = compute_next_run(parsed_schedule)
    for key in ("idle_timeout_seconds", "max_runtime_seconds"):
        if key in normalized_updates:
            normalized_updates[key] = _normalize_timeout_seconds(normalized_updates[key])
    if "concurrency_policy" in normalized_updates:
        normalized_updates["concurrency_policy"] = normalize_concurrency_policy(
            normalized_updates["concurrency_policy"]
        )
    if "concurrency_key" in normalized_updates:
        job_id = str((existing_job or {}).get("id") or "")
        if not job_id:
            raise ValueError("concurrency_key update requires an existing job id")
        normalized_updates["concurrency_key"] = normalize_concurrency_key(
            normalized_updates["concurrency_key"],
            job_id,
        )
    return normalized_updates


def _update_job_locked(
    jobs: list[dict[str, Any]],
    index: int,
    updates: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(jobs[index])
    merged.update(_normalize_updates(updates, merged))
    jobs[index] = merged
    save_jobs(jobs)
    return copy.deepcopy(merged)


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
    idle_timeout_seconds: int | str | None = None,
    max_runtime_seconds: int | str | None = None,
    concurrency_key: str | None = None,
    concurrency_policy: str | None = None,
) -> dict[str, Any]:
    parsed_schedule = parse_schedule(schedule)
    repeat_times = repeat
    if parsed_schedule["kind"] == "once" and repeat_times is None:
        repeat_times = 1

    normalized_skills = _normalize_skills(skill, skills)
    current = now()
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
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
        "idle_timeout_seconds": _normalize_timeout_seconds(idle_timeout_seconds),
        "max_runtime_seconds": _normalize_timeout_seconds(max_runtime_seconds),
        "concurrency_key": normalize_concurrency_key(concurrency_key, job_id),
        "concurrency_policy": normalize_concurrency_policy(concurrency_policy),
        "created_at": current.isoformat(),
    }
    job = _normalize_delivery_config(job)

    return copy.deepcopy(_store().create_job(job))


def update_job(job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    existing = get_job(job_id)
    if existing is None:
        raise KeyError(f"Cron job not found: {job_id}")
    normalized = _normalize_updates(updates, existing)
    if "deliver" in normalized or "origin" in normalized:
        merged = dict(existing)
        merged.update(normalized)
        normalized = _normalize_delivery_config(merged)
    return copy.deepcopy(_store().update_job(job_id, normalized))


def remove_job(job_id: str) -> bool:
    return _store().remove_job(job_id)


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
    schedule = job.get("schedule")
    return update_job(
        job_id,
        {
            "enabled": True,
            "state": "scheduled",
            "next_run_at": compute_next_run(schedule) if schedule else None,
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
    """Compatibility helper for manual inspection.

    Automatic scheduling must use StateStore.claim_due_jobs() so a run record
    exists before execution and next_run_at is not advanced at claim time.
    """
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
            update_job(
                job["id"],
                {"next_run_at": compute_next_run(schedule, base=current)},
            )
            continue

        due_jobs.append(copy.deepcopy(job))

    return due_jobs


def advance_next_run(job_id: str, run_at: datetime | None = None) -> dict[str, Any]:
    """Compatibility helper for manual schedule edits.

    Automatic scheduling must advance next_run_at through StateStore.complete_run().
    """
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
    delivery_error: str | None = None,
) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise KeyError(f"Cron job not found: {job_id}")

    repeat = _coerce_repeat_state(job.get("repeat"))
    repeat["completed"] = int(repeat.get("completed") or 0) + 1
    repeat_times = repeat.get("times")
    completed = repeat_times is not None and repeat["completed"] >= int(
        repeat_times
    )

    schedule_kind = (job.get("schedule") or {}).get("kind")
    if completed:
        state = "completed"
    elif success or schedule_kind in {"interval", "cron"}:
        state = "scheduled"
    else:
        state = "error"

    updates = {
        "last_run_at": _ensure_aware(run_at or now()).isoformat(),
        "last_status": "ok" if success else "error",
        "last_error": None if success else error,
        "last_delivery_error": delivery_error,
        "repeat": repeat,
        "state": state,
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

    files = sorted(output_dir.glob("*.md"), key=lambda path: path.name)
    if not files:
        return None
    return files[-1].read_text(encoding="utf-8")

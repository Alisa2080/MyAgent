# Cron System Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the cron reference system into this LangChain project with explicit lifecycle startup, optional `cronjob` tool exposure, conservative unattended execution, local persistence, and thread-scoped notifications.

**Architecture:** Replace Gateway Reference/AIAgent/platform-adapter dependencies with focused project-native modules. Storage and scheduling live under `cron/`; lifecycle integration lives in `agent_core/`; the LangChain tool surface lives in `agent_tools/public/`.

**Tech Stack:** Python, LangChain `create_agent`, LangChain `@tool`, Pydantic schemas, `croniter`, JSON file persistence, `pytest`, monkeypatch-based tests.

---

## File Structure

- Create `cron/paths.py`: resolve cron home, data files, output directory, scripts directory, secure chmod helpers, and atomic replace.
- Replace/adapt `cron/jobs.py`: schedule parsing, JSON persistence, job CRUD, due-job computation, next-run advancement, output save/load.
- Create `cron/notifications.py`: in-memory bounded per-thread cron event queues and message formatter.
- Create `agent_tools/public/cronjob.py`: LangChain `cronjob` tool using current public-tool patterns.
- Modify `agent_tools/public/__init__.py`: export `cronjob`.
- Modify `agent_core/builders.py`: add `include_cron_tools=False` and include `cronjob` only when requested.
- Create `cron/runner.py`: script wake gate, prompt construction, cron agent construction, timeout handling, final response extraction.
- Replace/adapt `cron/scheduler.py`: tick lock, due-job orchestration, workdir partitioning, result save/mark/delivery.
- Create `agent_core/cron_lifecycle.py`: explicit daemon ticker lifecycle.
- Create tests:
  - `tests/test_cron_paths.py`
  - `tests/test_cron_jobs.py`
  - `tests/test_cron_notifications.py`
  - `tests/test_cronjob_tool.py`
  - `tests/test_cron_runner.py`
  - `tests/test_cron_scheduler.py`
  - `tests/test_cron_lifecycle.py`

Do not implement CLI, REST API, platform delivery adapters, reference implementation `AIAgent`, or provider routing.

---

### Task 1: Cron Path Helpers

**Files:**
- Create: `cron/paths.py`
- Test: `tests/test_cron_paths.py`

- [ ] **Step 1: Write failing path tests**

Add `tests/test_cron_paths.py`:

```python
from pathlib import Path


def test_get_cron_home_prefers_toolkit_home(monkeypatch, tmp_path):
    import cron.paths as paths

    toolkit_home = tmp_path / "toolkit-home"
    monkeypatch.setenv("TOOLKIT_HOME", str(toolkit_home))

    assert paths.get_cron_home() == toolkit_home
    assert paths.get_cron_dir() == toolkit_home / "cron"
    assert paths.get_jobs_file() == toolkit_home / "cron" / "jobs.json"
    assert paths.get_output_dir() == toolkit_home / "cron" / "output"
    assert paths.get_scripts_dir() == toolkit_home / "scripts"


def test_get_cron_home_uses_toolkit_parent_when_no_toolkit_home(monkeypatch, tmp_path):
    import cron.paths as paths

    toolkit_home = tmp_path / "toolkit"
    monkeypatch.delenv("TOOLKIT_HOME", raising=False)
    monkeypatch.setattr(paths, "get_toolkit_home", lambda: toolkit_home)

    assert paths.get_cron_home() == toolkit_home


def test_ensure_cron_dirs_creates_secure_dirs(monkeypatch, tmp_path):
    import cron.paths as paths

    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))

    paths.ensure_cron_dirs()

    assert (tmp_path / "cron").is_dir()
    assert (tmp_path / "cron" / "output").is_dir()
    assert (tmp_path / "scripts").is_dir()


def test_atomic_write_json_sets_owner_only_permissions(monkeypatch, tmp_path):
    import cron.paths as paths

    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))
    target = paths.get_jobs_file()

    paths.atomic_write_json(target, {"jobs": []})

    assert target.exists()
    assert '"jobs": []' in target.read_text()
    if hasattr(target, "stat"):
        assert oct(target.stat().st_mode & 0o777) == "0o600"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_cron_paths.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'cron.paths'`.

- [ ] **Step 3: Implement `cron/paths.py`**

Create `cron/paths.py`:

```python
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from agent_tools.terminal_toolkit.paths import get_toolkit_home


def get_cron_home() -> Path:
    toolkit_home = os.getenv("TOOLKIT_HOME")
    if toolkit_home:
        return Path(os.path.expanduser(toolkit_home)).resolve()
    return get_toolkit_home().resolve()


def display_cron_home() -> str:
    home = get_cron_home()
    try:
        return str(home).replace(str(Path.home()), "~", 1)
    except Exception:
        return str(home)


def get_cron_dir() -> Path:
    return get_cron_home() / "cron"


def get_jobs_file() -> Path:
    return get_cron_dir() / "jobs.json"


def get_output_dir() -> Path:
    return get_cron_dir() / "output"


def get_scripts_dir() -> Path:
    return get_cron_home() / "scripts"


def secure_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except (OSError, NotImplementedError):
        pass


def secure_file(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def ensure_cron_dirs() -> None:
    for path in (get_cron_dir(), get_output_dir(), get_scripts_dir()):
        path.mkdir(parents=True, exist_ok=True)
        secure_dir(path)


def atomic_replace(src: str | Path, dst: str | Path) -> None:
    os.replace(str(src), str(dst))


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_cron_dirs()
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        suffix=".tmp",
        prefix=f".{path.name}_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, path)
        secure_file(path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

- [ ] **Step 4: Run path tests**

Run: `pytest tests/test_cron_paths.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/paths.py tests/test_cron_paths.py
git commit -m "feat: add cron path helpers"
```

---

### Task 2: Job Storage and Schedule Semantics

**Files:**
- Replace/adapt: `cron/jobs.py`
- Test: `tests/test_cron_jobs.py`

- [ ] **Step 1: Write failing job tests**

Add `tests/test_cron_jobs.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest


def test_parse_duration_schedule_creates_oneshot(monkeypatch, tmp_path):
    import cron.jobs as jobs

    now = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "now", lambda: now)

    parsed = jobs.parse_schedule("30m")

    assert parsed["kind"] == "once"
    assert parsed["run_at"] == (now + timedelta(minutes=30)).isoformat()


def test_create_job_defaults_origin_delivery(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "now", lambda: datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc))

    job = jobs.create_job(
        prompt="write a report",
        schedule="30m",
        origin={"thread_id": "thread-1"},
    )

    assert job["deliver"] == "origin"
    assert job["repeat"] == {"times": 1, "completed": 0}
    assert jobs.get_job(job["id"])["name"] == "write a report"


def test_workdir_must_be_absolute(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="absolute path"):
        jobs.create_job(prompt="x", schedule="30m", workdir="relative/path")


def test_get_due_jobs_fast_forwards_stale_interval(monkeypatch, tmp_path):
    import cron.jobs as jobs

    base = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "now", lambda: base)
    job = jobs.create_job(prompt="x", schedule="every 10m", deliver="local")
    jobs.update_job(job["id"], {"next_run_at": (base - timedelta(hours=3)).isoformat()})

    due = jobs.get_due_jobs(now_dt=base)

    assert due == []
    refreshed = jobs.get_job(job["id"])
    assert datetime.fromisoformat(refreshed["next_run_at"]) > base


def test_advance_next_run_before_mark_run(monkeypatch, tmp_path):
    import cron.jobs as jobs

    base = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "now", lambda: base)
    job = jobs.create_job(prompt="x", schedule="every 30m", deliver="local")
    jobs.update_job(job["id"], {"next_run_at": base.isoformat()})

    advanced = jobs.advance_next_run(job["id"], base)
    jobs.mark_job_run(job["id"], success=True, error=None, run_at=base)

    assert advanced["next_run_at"] == (base + timedelta(minutes=30)).isoformat()
    final = jobs.get_job(job["id"])
    assert final["last_status"] == "ok"
    assert final["repeat"]["completed"] == 1
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_cron_jobs.py -v`

Expected: FAIL from reference-only imports in the existing copied `cron/jobs.py`.

- [ ] **Step 3: Implement `cron/jobs.py`**

Replace `cron/jobs.py` with focused storage/schedule code that imports only
standard library, `croniter`, and `cron.paths`. The public functions required
by Tasks 3 through 9 are:

```python
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cron.paths import atomic_write_json, ensure_cron_dirs, get_jobs_file, get_output_dir, secure_file

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    croniter = None
    HAS_CRONITER = False

logger = logging.getLogger(__name__)
ONESHOT_GRACE_SECONDS = 120
_jobs_file_lock = threading.Lock()


def now() -> datetime:
    return datetime.now().astimezone()


def parse_duration(value: str) -> int:
    match = re.match(r"^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", value.strip().lower())
    if not match:
        raise ValueError(f"Invalid duration: {value!r}. Use format like '30m', '2h', or '1d'.")
    number = int(match.group(1))
    unit = match.group(2)[0]
    return number * {"m": 1, "h": 60, "d": 1440}[unit]


def parse_schedule(schedule: str) -> dict[str, Any]:
    raw = str(schedule or "").strip()
    lowered = raw.lower()
    if not raw:
        raise ValueError("schedule is required")
    if lowered.startswith("every "):
        minutes = parse_duration(raw[6:].strip())
        return {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"}
    parts = raw.split()
    if len(parts) >= 5 and all(re.match(r"^[\d\*\-,/]+$", part) for part in parts[:5]):
        if not HAS_CRONITER:
            raise ValueError("Cron expressions require the croniter package.")
        croniter(raw)
        return {"kind": "cron", "expr": raw, "display": raw}
    if "T" in raw or re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return {"kind": "once", "run_at": dt.isoformat(), "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}"}
    minutes = parse_duration(raw)
    return {"kind": "once", "run_at": (now() + timedelta(minutes=minutes)).isoformat(), "display": f"once in {raw}"}


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _normalize_workdir(workdir: str | None) -> str | None:
    if workdir is None or not str(workdir).strip():
        return None
    path = Path(str(workdir).strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Cron workdir must be an absolute path (got {workdir!r}).")
    resolved = path.resolve()
    if not resolved.exists():
        raise ValueError(f"Cron workdir does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Cron workdir is not a directory: {resolved}")
    return str(resolved)


def _normalize_skills(skill: str | None = None, skills: Any = None) -> list[str]:
    items = [skill] if skills is None and skill else ([] if skills is None else ([skills] if isinstance(skills, str) else list(skills)))
    result = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def load_jobs() -> list[dict[str, Any]]:
    ensure_cron_dirs()
    path = get_jobs_file()
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        raise RuntimeError("Cron database is invalid: jobs must be a list.")
    return jobs


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    atomic_write_json(get_jobs_file(), {"jobs": jobs, "updated_at": now().isoformat()})


def list_jobs(include_disabled: bool = False) -> list[dict[str, Any]]:
    loaded = load_jobs()
    if include_disabled:
        return copy.deepcopy(loaded)
    return copy.deepcopy([job for job in loaded if job.get("enabled", True)])


def get_job(job_id: str) -> dict[str, Any] | None:
    for job in load_jobs():
        if job.get("id") == job_id:
            return copy.deepcopy(job)
    return None


def compute_next_run(schedule: dict[str, Any], last_run_at: str | None = None, base: datetime | None = None) -> str | None:
    current = _ensure_aware(base or now())
    kind = schedule.get("kind")
    if kind == "once":
        if last_run_at:
            return None
        run_at = schedule.get("run_at")
        if not run_at:
            return None
        run_at_dt = _ensure_aware(datetime.fromisoformat(run_at))
        return run_at if run_at_dt >= current - timedelta(seconds=ONESHOT_GRACE_SECONDS) else None
    if kind == "interval":
        anchor = _ensure_aware(datetime.fromisoformat(last_run_at)) if last_run_at else current
        return (anchor + timedelta(minutes=int(schedule["minutes"]))).isoformat()
    if kind == "cron":
        if not HAS_CRONITER:
            return None
        return croniter(schedule["expr"], current).get_next(datetime).isoformat()
    return None


def create_job(prompt: str, schedule: str, name: str | None = None, repeat: int | None = None, deliver: str | None = None, origin: dict[str, Any] | None = None, skill: str | None = None, skills: list[str] | None = None, model: str | None = None, provider: str | None = None, base_url: str | None = None, script: str | None = None, context_from: str | list[str] | None = None, enabled_toolsets: list[str] | None = None, workdir: str | None = None) -> dict[str, Any]:
    parsed = parse_schedule(schedule)
    if repeat is not None and repeat <= 0:
        repeat = None
    if parsed["kind"] == "once" and repeat is None:
        repeat = 1
    normalized_skills = _normalize_skills(skill, skills)
    if isinstance(context_from, str):
        context_from = [context_from.strip()] if context_from.strip() else None
    elif isinstance(context_from, list):
        context_from = [str(item).strip() for item in context_from if str(item).strip()] or None
    else:
        context_from = None
    current = now()
    job = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or prompt or (normalized_skills[0] if normalized_skills else "cron job"))[:50].strip(),
        "prompt": prompt or "",
        "skills": normalized_skills,
        "skill": normalized_skills[0] if normalized_skills else None,
        "model": (model or None),
        "provider": (provider or None),
        "base_url": (base_url or None),
        "script": (script or None),
        "context_from": context_from,
        "schedule": parsed,
        "schedule_display": parsed.get("display", schedule),
        "repeat": {"times": repeat, "completed": 0},
        "deliver": deliver or ("origin" if origin else "local"),
        "origin": origin or None,
        "enabled": True,
        "state": "scheduled",
        "next_run_at": compute_next_run(parsed, base=current),
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "enabled_toolsets": [str(item).strip() for item in enabled_toolsets if str(item).strip()] if enabled_toolsets else None,
        "workdir": _normalize_workdir(workdir),
        "created_at": current.isoformat(),
    }
    with _jobs_file_lock:
        all_jobs = load_jobs()
        all_jobs.append(job)
        save_jobs(all_jobs)
    return copy.deepcopy(job)
```

Add these remaining public helpers below `create_job`:

```python
def update_job(job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    with _jobs_file_lock:
        all_jobs = load_jobs()
        for index, job in enumerate(all_jobs):
            if job.get("id") != job_id:
                continue
            merged = dict(job)
            if "workdir" in updates:
                updates = dict(updates)
                updates["workdir"] = _normalize_workdir(updates["workdir"])
            if "schedule" in updates and isinstance(updates["schedule"], str):
                updates = dict(updates)
                parsed = parse_schedule(updates["schedule"])
                updates["schedule"] = parsed
                updates["schedule_display"] = parsed.get("display")
                updates["next_run_at"] = compute_next_run(parsed)
            merged.update(updates)
            all_jobs[index] = merged
            save_jobs(all_jobs)
            return copy.deepcopy(merged)
    raise KeyError(f"Cron job not found: {job_id}")


def remove_job(job_id: str) -> bool:
    with _jobs_file_lock:
        all_jobs = load_jobs()
        kept = [job for job in all_jobs if job.get("id") != job_id]
        if len(kept) == len(all_jobs):
            return False
        save_jobs(kept)
        return True


def pause_job(job_id: str, reason: str | None = None) -> dict[str, Any]:
    return update_job(job_id, {"enabled": False, "state": "paused", "paused_reason": reason, "paused_at": now().isoformat()})


def resume_job(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if not job:
        raise KeyError(f"Cron job not found: {job_id}")
    return update_job(job_id, {"enabled": True, "state": "scheduled", "next_run_at": compute_next_run(job["schedule"])})


def trigger_job(job_id: str) -> dict[str, Any]:
    return update_job(job_id, {"enabled": True, "state": "scheduled", "next_run_at": now().isoformat()})


def _grace_seconds(schedule: dict[str, Any]) -> int:
    minimum = 120
    maximum = 7200
    if schedule.get("kind") == "interval":
        return max(minimum, min(int(schedule.get("minutes", 1)) * 30, maximum))
    if schedule.get("kind") == "cron" and HAS_CRONITER:
        try:
            base = now()
            iterator = croniter(schedule["expr"], base)
            first = iterator.get_next(datetime)
            second = iterator.get_next(datetime)
            return max(minimum, min(int((second - first).total_seconds() // 2), maximum))
        except Exception:
            return minimum
    return minimum


def get_due_jobs(now_dt: datetime | None = None) -> list[dict[str, Any]]:
    current = _ensure_aware(now_dt or now())
    due: list[dict[str, Any]] = []
    for job in load_jobs():
        if not job.get("enabled", True) or job.get("state") not in {None, "scheduled"}:
            continue
        next_run_at = job.get("next_run_at")
        if not next_run_at:
            continue
        run_at = _ensure_aware(datetime.fromisoformat(next_run_at))
        if run_at > current:
            continue
        schedule = job.get("schedule") or {}
        if schedule.get("kind") == "once":
            if run_at >= current - timedelta(seconds=ONESHOT_GRACE_SECONDS):
                due.append(copy.deepcopy(job))
            continue
        if run_at < current - timedelta(seconds=_grace_seconds(schedule)):
            update_job(job["id"], {"next_run_at": compute_next_run(schedule, base=current)})
            continue
        due.append(copy.deepcopy(job))
    return due


def advance_next_run(job_id: str, run_at: datetime | None = None) -> dict[str, Any]:
    job = get_job(job_id)
    if not job:
        raise KeyError(f"Cron job not found: {job_id}")
    schedule = job["schedule"]
    if schedule.get("kind") == "once":
        next_run_at = None
    elif schedule.get("kind") == "interval":
        next_run_at = ((run_at or now()) + timedelta(minutes=int(schedule["minutes"]))).isoformat()
    else:
        next_run_at = compute_next_run(schedule, base=run_at or now())
    return update_job(job_id, {"next_run_at": next_run_at})


def mark_job_run(job_id: str, success: bool, error: str | None = None, run_at: datetime | None = None) -> dict[str, Any]:
    job = get_job(job_id)
    if not job:
        raise KeyError(f"Cron job not found: {job_id}")
    repeat = dict(job.get("repeat") or {"times": None, "completed": 0})
    repeat["completed"] = int(repeat.get("completed") or 0) + 1
    completed = repeat.get("times") is not None and repeat["completed"] >= int(repeat["times"])
    updates = {
        "last_run_at": (run_at or now()).isoformat(),
        "last_status": "ok" if success else "error",
        "last_error": error,
        "repeat": repeat,
        "state": "completed" if completed else ("scheduled" if success else "error"),
        "enabled": False if completed else job.get("enabled", True),
    }
    return update_job(job_id, updates)


def save_job_output(job_id: str, output_doc: str, run_at: datetime | None = None) -> str:
    timestamp = (run_at or now()).strftime("%Y%m%d_%H%M%S")
    output_dir = get_output_dir() / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{timestamp}.md"
    path.write_text(output_doc, encoding="utf-8")
    secure_file(path)
    return str(path)


def latest_job_output(job_id: str) -> str | None:
    output_dir = get_output_dir() / job_id
    if not output_dir.exists():
        return None
    files = sorted(output_dir.glob("*.md"))
    if not files:
        return None
    return files[-1].read_text(encoding="utf-8")
```

- [ ] **Step 4: Run job tests**

Run: `pytest tests/test_cron_jobs.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/jobs.py tests/test_cron_jobs.py
git commit -m "feat: add cron job storage"
```

---

### Task 3: Cron Notifications

**Files:**
- Create: `cron/notifications.py`
- Test: `tests/test_cron_notifications.py`

- [ ] **Step 1: Write failing notification tests**

Add `tests/test_cron_notifications.py`:

```python
def test_queue_and_drain_thread_notifications(monkeypatch):
    import cron.notifications as notifications

    monkeypatch.setattr(notifications, "_events_by_thread", {})

    notifications.queue_cron_notification(
        "thread-1",
        {"type": "cron_result", "job_id": "job-1", "final_response": "done"},
    )

    assert notifications.drain_cron_notifications_for_thread_id("thread-2") == []
    assert notifications.drain_cron_notifications_for_thread_id("thread-1") == [
        {"type": "cron_result", "job_id": "job-1", "final_response": "done"}
    ]
    assert notifications.drain_cron_notifications_for_thread_id("thread-1") == []


def test_silent_response_is_not_queued(monkeypatch):
    import cron.notifications as notifications

    monkeypatch.setattr(notifications, "_events_by_thread", {})

    assert notifications.should_notify("[SILENT] nothing changed") is False
    assert notifications.should_notify("report ready") is True


def test_format_cron_notification_message():
    import cron.notifications as notifications

    message = notifications.format_cron_notification_message(
        [
            {
                "type": "cron_result",
                "job_id": "abc",
                "job_name": "daily",
                "status": "ok",
                "final_response": "Report ready",
                "output_path": "/tmp/out.md",
            }
        ]
    )

    assert "[IMPORTANT: Cron job update]" in message
    assert "daily" in message
    assert "Report ready" in message
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_cron_notifications.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'cron.notifications'`.

- [ ] **Step 3: Implement notifications**

Create `cron/notifications.py`:

```python
from __future__ import annotations

from collections import defaultdict, deque
import threading
import time
from typing import Any

SILENT_MARKER = "[SILENT]"
_MAX_PENDING_EVENTS_PER_THREAD = 100
_EVENT_TTL_SECONDS = 3600
_QUEUED_AT_KEY = "_queued_at"
_events_lock = threading.Lock()
_events_by_thread: dict[str, deque[dict[str, Any]]] = defaultdict(deque)


def should_notify(final_response: str | None) -> bool:
    return not str(final_response or "").lstrip().startswith(SILENT_MARKER)


def _prune_locked(now: float) -> None:
    expires_before = now - _EVENT_TTL_SECONDS
    empty = []
    for thread_id, queue in _events_by_thread.items():
        while queue and float(queue[0].get(_QUEUED_AT_KEY, 0.0)) < expires_before:
            queue.popleft()
        if not queue:
            empty.append(thread_id)
    for thread_id in empty:
        _events_by_thread.pop(thread_id, None)


def queue_cron_notification(thread_id: str | None, event: dict[str, Any]) -> None:
    if not thread_id:
        return
    now = time.time()
    buffered = dict(event)
    buffered[_QUEUED_AT_KEY] = now
    with _events_lock:
        _prune_locked(now)
        queue = _events_by_thread[str(thread_id)]
        queue.append(buffered)
        while len(queue) > _MAX_PENDING_EVENTS_PER_THREAD:
            queue.popleft()


def drain_cron_notifications_for_thread_id(thread_id: str | None, max_events: int = 10) -> list[dict[str, Any]]:
    if not thread_id:
        return []
    events = []
    with _events_lock:
        _prune_locked(time.time())
        queue = _events_by_thread[str(thread_id)]
        while queue and len(events) < max(0, max_events):
            event = dict(queue.popleft())
            event.pop(_QUEUED_AT_KEY, None)
            events.append(event)
    return events


def _shorten(text: Any, max_chars: int = 1200) -> str:
    value = "" if text is None else str(text)
    if len(value) <= max_chars:
        return value
    return value[: max_chars - len("\n...(truncated)")].rstrip() + "\n...(truncated)"


def format_cron_notification_message(events: list[dict[str, Any]], max_message_chars: int = 6000) -> str:
    if not events:
        return ""
    parts = []
    for event in events:
        header = f"- job={event.get('job_name') or event.get('job_id')} status={event.get('status')}"
        output_path = event.get("output_path")
        body = _shorten(event.get("final_response") or event.get("error") or "")
        if output_path:
            header += f" output_path={output_path}"
        parts.append(f"{header}\n  result:\n    " + body.replace("\n", "\n    "))
    message = (
        "[IMPORTANT: Cron job update]\n"
        "One or more scheduled cron jobs produced results for this thread.\n\n"
        + "\n\n".join(parts)
        + "\n\nEnd cron job update."
    )
    return _shorten(message, max_message_chars)
```

- [ ] **Step 4: Run notification tests**

Run: `pytest tests/test_cron_notifications.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/notifications.py tests/test_cron_notifications.py
git commit -m "feat: add cron notifications"
```

---

### Task 4: Optional `cronjob` Tool and Builder Wiring

**Files:**
- Create: `agent_tools/public/cronjob.py`
- Modify: `agent_tools/public/__init__.py`
- Modify: `agent_core/builders.py`
- Test: `tests/test_cronjob_tool.py`

- [ ] **Step 1: Write failing tool tests**

Add `tests/test_cronjob_tool.py`:

```python
from types import SimpleNamespace


def test_cronjob_create_captures_runtime_thread(monkeypatch, tmp_path):
    import agent_tools.public.cronjob as cronjob_tool

    created = {}

    def fake_create_job(**kwargs):
        created.update(kwargs)
        return {
            "id": "job-1",
            "name": "daily",
            "skill": None,
            "skills": [],
            "schedule_display": "once in 30m",
            "repeat": {"times": 1, "completed": 0},
            "deliver": "origin",
            "next_run_at": "2026-05-22T09:30:00+00:00",
            "enabled": True,
            "state": "scheduled",
            "prompt": kwargs["prompt"],
        }

    runtime = SimpleNamespace(config={"configurable": {"thread_id": "thread-1"}})
    monkeypatch.setattr(cronjob_tool, "create_job", fake_create_job)

    result = cronjob_tool._cronjob_impl(
        action="create",
        prompt="write report",
        schedule="30m",
        runtime=runtime,
    )

    assert result["success"] is True
    assert created["origin"] == {"thread_id": "thread-1"}
    assert created["deliver"] is None


def test_cronjob_rejects_platform_delivery():
    import agent_tools.public.cronjob as cronjob_tool

    result = cronjob_tool._cronjob_impl(
        action="create",
        prompt="write report",
        schedule="30m",
        deliver="telegram:123",
        runtime=None,
    )

    assert result["success"] is False
    assert result["code"] == "unsupported_delivery"


def test_build_agent_excludes_cronjob_by_default(monkeypatch):
    import agent_core.builders as builders

    captured = {}
    monkeypatch.setattr(builders, "create_agent", lambda **kwargs: captured.update(kwargs) or kwargs)
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda name: "")
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: 0)
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)

    builders.build_agent()

    names = [getattr(tool, "name", "") for tool in captured["tools"]]
    assert "cronjob" not in names


def test_build_agent_can_include_cronjob(monkeypatch):
    import agent_core.builders as builders

    captured = {}
    monkeypatch.setattr(builders, "create_agent", lambda **kwargs: captured.update(kwargs) or kwargs)
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda name: "")
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: 0)
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)

    builders.build_agent(include_cron_tools=True)

    names = [getattr(tool, "name", "") for tool in captured["tools"]]
    assert "cronjob" in names
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_cronjob_tool.py -v`

Expected: FAIL with missing `agent_tools.public.cronjob` and unexpected `include_cron_tools` argument.

- [ ] **Step 3: Implement `agent_tools/public/cronjob.py`**

Create a tool module using the existing public tool style:

```python
from __future__ import annotations

import json
import re
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from agent_tools.shared.tool_output import tool_error, tool_ok
from cron.jobs import create_job, get_job, list_jobs, pause_job, remove_job, resume_job, trigger_job, update_job


class CronJobInput(BaseModel):
    action: Literal["create", "list", "update", "pause", "resume", "remove", "run"] = Field(description="Cron job action.")
    job_id: str | None = Field(default=None, description="Required for update/pause/resume/remove/run.")
    prompt: str | None = Field(default=None, description="Self-contained cron prompt.")
    schedule: str | None = Field(default=None, description="Schedule such as 30m, every 2h, cron, or ISO timestamp.")
    name: str | None = Field(default=None, description="Optional job name.")
    repeat: int | None = Field(default=None, description="Optional repeat count; <=0 means forever.")
    deliver: str | None = Field(default=None, description="local or origin only in this project.")
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


_THREAT_PATTERNS = [
    (r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions", "prompt_injection"),
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"authorized_keys", "ssh_backdoor"),
    (r"rm\s+-rf\s+/", "destructive_root_rm"),
]
_INVISIBLE_CHARS = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e"}


def _runtime_thread_id(runtime: ToolRuntime | None) -> str | None:
    config = getattr(runtime, "config", None)
    configurable = config.get("configurable") if isinstance(config, dict) else None
    value = configurable.get("thread_id") if isinstance(configurable, dict) else None
    return str(value) if value else None


def _scan_prompt(prompt: str) -> str | None:
    for char in _INVISIBLE_CHARS:
        if char in prompt:
            return f"prompt contains invisible unicode U+{ord(char):04X}"
    for pattern, code in _THREAT_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE):
            return f"prompt matches blocked pattern {code}"
    return None


def _format_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["id"],
        "name": job["name"],
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
    }


def _plain_result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


def _cronjob_impl(action: str, runtime: ToolRuntime | None = None, **kwargs: Any) -> dict[str, Any]:
    normalized = (action or "").strip().lower()
    deliver = kwargs.get("deliver")
    if deliver not in {None, "local", "origin"}:
        return {"success": False, "code": "unsupported_delivery", "error": "Only deliver='local' and deliver='origin' are supported."}
    try:
        if normalized == "create":
            prompt = kwargs.get("prompt") or ""
            skills = kwargs.get("skills") or []
            schedule = kwargs.get("schedule")
            if not schedule:
                return {"success": False, "code": "missing_schedule", "error": "schedule is required for create."}
            if not prompt and not skills:
                return {"success": False, "code": "missing_task", "error": "create requires prompt or skills."}
            scan_error = _scan_prompt(prompt) if prompt else None
            if scan_error:
                return {"success": False, "code": "blocked_prompt", "error": scan_error}
            thread_id = _runtime_thread_id(runtime)
            origin = {"thread_id": thread_id} if thread_id else None
            job = create_job(origin=origin, **kwargs)
            return {"success": True, "job_id": job["id"], "job": _format_job(job), "message": f"Cron job '{job['name']}' created."}
        if normalized == "list":
            return {"success": True, "jobs": [_format_job(job) for job in list_jobs(include_disabled=bool(kwargs.get("include_disabled")))]}
        job_id = kwargs.get("job_id")
        if not job_id:
            return {"success": False, "code": "missing_job_id", "error": f"job_id is required for {normalized}."}
        if normalized == "pause":
            return {"success": True, "job": _format_job(pause_job(job_id, reason=kwargs.get("reason")))}
        if normalized == "resume":
            return {"success": True, "job": _format_job(resume_job(job_id))}
        if normalized == "remove":
            return {"success": True, "removed": bool(remove_job(job_id))}
        if normalized == "run":
            return {"success": True, "job": _format_job(trigger_job(job_id))}
        if normalized == "update":
            updates = {key: value for key, value in kwargs.items() if key not in {"job_id", "include_disabled"} and value is not None}
            return {"success": True, "job": _format_job(update_job(job_id, updates))}
        return {"success": False, "code": "unknown_action", "error": f"Unknown cron action {action!r}."}
    except Exception as exc:
        return {"success": False, "code": "cron_error", "error": str(exc)}


@tool("cronjob", args_schema=CronJobInput)
def cronjob(action: str, runtime: ToolRuntime | None = None, **kwargs: Any) -> str:
    """Manage unattended scheduled cron jobs. Only local and origin delivery are supported."""
    result = _cronjob_impl(action=action, runtime=runtime, **kwargs)
    if result.get("success"):
        return tool_ok("cronjob", data=result, message=result.get("message", "Cron job action completed."))
    return tool_error("cronjob", result.get("error", "Cron job action failed."), code=result.get("code", "cron_error"), data=result)
```

- [ ] **Step 4: Export and wire builder**

Modify `agent_tools/public/__init__.py` to import and export `cronjob`.

Modify `agent_core/builders.py`:

```python
def build_agent(*, include_cron_tools: bool = False):
    ...
    tools = [*BASE_TOOLS, memory_manage, task]
    if include_cron_tools:
        from agent_tools.public.cronjob import cronjob
        tools.append(cronjob)
    return create_agent(..., tools=tools)
```

- [ ] **Step 5: Run tool tests**

Run: `pytest tests/test_cronjob_tool.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/public/cronjob.py agent_tools/public/__init__.py agent_core/builders.py tests/test_cronjob_tool.py
git commit -m "feat: expose optional cronjob tool"
```

---

### Task 5: Cron Runner

**Files:**
- Create: `cron/runner.py`
- Test: `tests/test_cron_runner.py`

- [ ] **Step 1: Write failing runner tests**

Add `tests/test_cron_runner.py`:

```python
from pathlib import Path


def test_resolve_script_path_rejects_escape(monkeypatch, tmp_path):
    import cron.runner as runner

    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))

    assert runner.validate_script_path("../escape.py").startswith("Script path escapes")
    assert runner.validate_script_path("/tmp/script.py").startswith("Script path must be relative")


def test_wake_gate_false_skips_agent(monkeypatch, tmp_path):
    import cron.runner as runner

    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "check.py"
    script.write_text('print("nothing")\nprint(\'{"wakeAgent": false}\')\n')

    def fail_agent(job, prompt):
        raise AssertionError("agent should not run")

    monkeypatch.setattr(runner, "_invoke_cron_agent", fail_agent)
    result = runner.run_job({"id": "job-1", "name": "gate", "prompt": "x", "script": "check.py"})

    assert result.success is True
    assert result.final_response == "[SILENT]"
    assert "agent skipped" in result.output_doc


def test_build_cron_tools_excludes_cronjob():
    import cron.runner as runner

    names = [getattr(tool, "name", "") for tool in runner.build_cron_tools(["terminal", "file_write", "delegation"])]

    assert "cronjob" not in names
    assert "terminal" in names
    assert "write_file" in names
    assert "task" in names


def test_build_job_prompt_includes_context_and_skills(monkeypatch, tmp_path):
    import cron.runner as runner

    monkeypatch.setattr(runner, "latest_job_output", lambda job_id: f"output for {job_id}")
    monkeypatch.setattr(runner, "_load_skill_content", lambda skill: f"skill content {skill}")

    prompt = runner.build_job_prompt(
        {
            "id": "job-1",
            "name": "daily",
            "prompt": "write report",
            "context_from": ["upstream"],
            "skills": ["reporting"],
        },
        script_output="script output",
    )

    assert "unattended cron job" in prompt
    assert "script output" in prompt
    assert "output for upstream" in prompt
    assert "skill content reporting" in prompt
    assert "write report" in prompt
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_cron_runner.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'cron.runner'`.

- [ ] **Step 3: Implement runner dataclass and script gate**

Create `cron/runner.py` with:

```python
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents import create_agent

from agent_core.message_utils import extract_text_from_agent_response
from agent_core.model_config import MAIN_MODEL, SMALL_MODEL
from agent_core.system_prompt import SystemPromptBuilder, build_prompt_context, load_project_instruction_blocks, model_display_name
from agent_core.tool_limits import build_tool_call_limit_middleware
from agent_core.workspace import WORKDIR
from agent_core.delegation import READ_ONLY_TOOLS, task
from agent_tools.public.files import patch, write_file
from agent_tools.public.terminal import process, terminal
from cron.jobs import latest_job_output, now
from cron.notifications import SILENT_MARKER
from cron.paths import get_scripts_dir


@dataclass
class JobRunResult:
    success: bool
    output_doc: str
    final_response: str
    error: str | None = None


def validate_script_path(script: str | None) -> str | None:
    if not script or not str(script).strip():
        return None
    raw = str(script).strip()
    if raw.startswith(("/", "~")) or (len(raw) >= 2 and raw[1] == ":"):
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
    return (get_scripts_dir() / script).resolve()


def _script_timeout() -> int:
    try:
        return max(1, int(os.getenv("AGENT_CRON_SCRIPT_TIMEOUT", "120")))
    except ValueError:
        return 120


def _run_script(script: str) -> tuple[bool, str]:
    path = _resolve_script_path(script)
    result = subprocess.run(
        [os.environ.get("PYTHON", "python"), str(path)],
        capture_output=True,
        text=True,
        timeout=_script_timeout(),
        cwd=str(path.parent),
    )
    output = (result.stdout or "") + (("\n[stderr]\n" + result.stderr) if result.stderr else "")
    return result.returncode == 0, output[-12000:]


def _wake_agent(script_output: str) -> bool:
    for line in reversed(script_output.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return True
        if isinstance(payload, dict) and payload.get("wakeAgent") is False:
            return False
        return True
    return True
```

- [ ] **Step 4: Implement prompt/tool/agent invocation**

Continue `cron/runner.py`:

```python
def _load_skill_content(skill_name: str) -> str:
    from agent_tools.public.skills import SKILLS_DIR
    path = Path(SKILLS_DIR) / skill_name / "SKILL.md"
    if not path.exists():
        return f"[Skill not found: {skill_name}]"
    return path.read_text(encoding="utf-8")[-12000:]


def build_job_prompt(job: dict[str, Any], script_output: str | None = None) -> str:
    parts = [
        "You are running an unattended cron job. Do not ask clarifying questions.",
        "Make a best effort with available context. Do not create or modify cron jobs.",
        "Your final response is used for delivery. Start with [SILENT] only when there is nothing useful to notify.",
    ]
    if script_output:
        parts.extend(["", "Pre-run script output:", script_output])
    for upstream_id in job.get("context_from") or []:
        output = latest_job_output(upstream_id)
        if output:
            parts.extend(["", f"Context from job {upstream_id}:", output])
    for skill in job.get("skills") or []:
        parts.extend(["", f"Skill {skill}:", _load_skill_content(skill)])
    if job.get("prompt"):
        parts.extend(["", "Job instruction:", job["prompt"]])
    return "\n".join(parts)


def build_cron_tools(enabled_toolsets: list[str] | None = None) -> list[Any]:
    requested = set(enabled_toolsets or [])
    tools = list(READ_ONLY_TOOLS)
    if "file_write" in requested:
        tools.extend([write_file, patch])
    if "terminal" in requested:
        tools.extend([terminal, process])
    if "delegation" in requested:
        tools.append(task)
    return tools


def _build_cron_agent(job: dict[str, Any]):
    workdir = Path(job.get("workdir") or WORKDIR)
    prompt_context = build_prompt_context(
        workdir=str(workdir),
        model_name=model_display_name(MAIN_MODEL),
        project_instruction_blocks=load_project_instruction_blocks(str(workdir)),
    )
    return create_agent(
        model=MAIN_MODEL,
        system_prompt=SystemPromptBuilder().build_parent(prompt_context),
        middleware=build_tool_call_limit_middleware(include_task="delegation" in set(job.get("enabled_toolsets") or [])),
        tools=build_cron_tools(job.get("enabled_toolsets")),
    )


def _cron_timeout() -> int | None:
    try:
        value = int(os.getenv("AGENT_CRON_TIMEOUT", "600"))
    except ValueError:
        value = 600
    return value if value > 0 else None


def _invoke_cron_agent(job: dict[str, Any], prompt: str) -> str:
    agent = _build_cron_agent(job)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(agent.invoke, {"messages": [{"role": "user", "content": prompt}]}, {"recursion_limit": 90})
        response = future.result(timeout=_cron_timeout())
    return extract_text_from_agent_response(response)
```

- [ ] **Step 5: Implement `run_job`**

Finish `cron/runner.py`:

```python
def _output_doc(job: dict[str, Any], final_response: str, script_output: str | None = None, error: str | None = None) -> str:
    lines = [
        f"# Cron Job: {job.get('name') or job.get('id')}",
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
            script_ok, script_output = _run_script(script)
            if not script_ok:
                error = "Pre-run script failed."
                return JobRunResult(False, _output_doc(job, "", script_output, error), "", error)
            if not _wake_agent(script_output):
                final = SILENT_MARKER
                return JobRunResult(True, _output_doc(job, final, script_output, "Script gate returned wakeAgent=false; agent skipped."), final, None)
        prompt = build_job_prompt(job, script_output=script_output)
        final = _invoke_cron_agent(job, prompt)
        return JobRunResult(True, _output_doc(job, final, script_output), final, None)
    except concurrent.futures.TimeoutError:
        error = "Cron job timed out."
        return JobRunResult(False, _output_doc(job, "", script_output, error), "", error)
    except Exception as exc:
        error = str(exc)
        return JobRunResult(False, _output_doc(job, "", script_output, error), "", error)
```

- [ ] **Step 6: Run runner tests**

Run: `pytest tests/test_cron_runner.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cron/runner.py tests/test_cron_runner.py
git commit -m "feat: add cron job runner"
```

---

### Task 6: Scheduler Tick

**Files:**
- Replace/adapt: `cron/scheduler.py`
- Test: `tests/test_cron_scheduler.py`

- [ ] **Step 1: Write failing scheduler tests**

Add `tests/test_cron_scheduler.py`:

```python
from dataclasses import dataclass
from datetime import datetime, timezone


def test_tick_advances_before_running(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    calls = []
    job = {"id": "job-1", "name": "daily", "workdir": None, "deliver": "local"}

    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: [job])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda job_id, run_at: calls.append(("advance", job_id)) or job)
    monkeypatch.setattr(scheduler, "run_job", lambda advanced: calls.append(("run", advanced["id"])) or scheduler.JobRunResult(True, "doc", "final", None))
    monkeypatch.setattr(scheduler, "save_job_output", lambda job_id, doc, run_at=None: "/tmp/out.md")
    monkeypatch.setattr(scheduler, "mark_job_run", lambda job_id, success, error=None, run_at=None: calls.append(("mark", job_id, success)))

    result = scheduler.tick(now_dt=datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc))

    assert result.ran == 1
    assert calls == [("advance", "job-1"), ("run", "job-1"), ("mark", "job-1", True)]


def test_tick_queues_origin_notification(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    job = {"id": "job-1", "name": "daily", "workdir": None, "deliver": "origin", "origin": {"thread_id": "thread-1"}}
    queued = []

    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: [job])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda job_id, run_at: job)
    monkeypatch.setattr(scheduler, "run_job", lambda advanced: scheduler.JobRunResult(True, "doc", "final", None))
    monkeypatch.setattr(scheduler, "save_job_output", lambda job_id, doc, run_at=None: "/tmp/out.md")
    monkeypatch.setattr(scheduler, "mark_job_run", lambda job_id, success, error=None, run_at=None: None)
    monkeypatch.setattr(scheduler, "queue_cron_notification", lambda thread_id, event: queued.append((thread_id, event)))

    scheduler.tick()

    assert queued[0][0] == "thread-1"
    assert queued[0][1]["job_id"] == "job-1"


def test_tick_runs_workdir_jobs_sequentially(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    order = []
    jobs = [
        {"id": "a", "name": "a", "workdir": "/tmp/a", "deliver": "local"},
        {"id": "b", "name": "b", "workdir": "/tmp/b", "deliver": "local"},
    ]

    monkeypatch.setenv("TOOLKIT_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: jobs)
    monkeypatch.setattr(scheduler, "advance_next_run", lambda job_id, run_at: next(job for job in jobs if job["id"] == job_id))
    monkeypatch.setattr(scheduler, "run_job", lambda job: order.append(job["id"]) or scheduler.JobRunResult(True, "doc", "final", None))
    monkeypatch.setattr(scheduler, "save_job_output", lambda job_id, doc, run_at=None: "/tmp/out.md")
    monkeypatch.setattr(scheduler, "mark_job_run", lambda job_id, success, error=None, run_at=None: None)

    scheduler.tick()

    assert order == ["a", "b"]
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_cron_scheduler.py -v`

Expected: FAIL from reference-only imports in copied `cron/scheduler.py`.

- [ ] **Step 3: Implement scheduler dataclasses and lock**

Replace `cron/scheduler.py` with:

```python
from __future__ import annotations

import concurrent.futures
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:
    fcntl = None

from cron.jobs import advance_next_run, get_due_jobs, mark_job_run, now, save_job_output
from cron.notifications import queue_cron_notification, should_notify
from cron.paths import ensure_cron_dirs, get_cron_dir
from cron.runner import JobRunResult, run_job

logger = logging.getLogger(__name__)


@dataclass
class JobTickResult:
    job_id: str
    success: bool
    output_path: str | None = None
    error: str | None = None


@dataclass
class TickResult:
    due: int = 0
    ran: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    results: list[JobTickResult] = field(default_factory=list)


class _TickLock:
    def __init__(self):
        self.path = get_cron_dir() / ".tick.lock"
        self.handle = None

    def __enter__(self):
        ensure_cron_dirs()
        self.handle = self.path.open("a+")
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is not None:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
```

- [ ] **Step 4: Implement job processing and delivery**

Continue `cron/scheduler.py`:

```python
def _max_parallel() -> int | None:
    raw = os.getenv("AGENT_CRON_MAX_PARALLEL")
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


def _deliver_result(job: dict[str, Any], result: JobRunResult, output_path: str) -> None:
    if job.get("deliver", "local") != "origin":
        return
    if not result.success or not should_notify(result.final_response):
        return
    origin = job.get("origin") or {}
    thread_id = origin.get("thread_id")
    if not thread_id:
        return
    queue_cron_notification(
        str(thread_id),
        {
            "type": "cron_result",
            "job_id": job.get("id"),
            "job_name": job.get("name"),
            "status": "ok",
            "final_response": result.final_response,
            "output_path": output_path,
            "error": None,
            "run_at": now().isoformat(),
        },
    )


def _process_job(job: dict[str, Any], run_at) -> JobTickResult:
    job_id = job["id"]
    try:
        advanced = advance_next_run(job_id, run_at)
        result = run_job(advanced)
        output_path = save_job_output(job_id, result.output_doc, run_at=run_at)
        mark_job_run(job_id, success=result.success, error=result.error, run_at=run_at)
        _deliver_result(advanced, result, output_path)
        return JobTickResult(job_id=job_id, success=result.success, output_path=output_path, error=result.error)
    except Exception as exc:
        logger.exception("Cron job %s failed during tick.", job_id)
        try:
            mark_job_run(job_id, success=False, error=str(exc), run_at=run_at)
        except Exception:
            logger.exception("Failed to mark cron job %s error state.", job_id)
        return JobTickResult(job_id=job_id, success=False, error=str(exc))


def _run_parallel(jobs: list[dict[str, Any]], run_at) -> list[JobTickResult]:
    if not jobs:
        return []
    max_workers = _max_parallel() or len(jobs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_process_job, job, run_at) for job in jobs]
        return [future.result() for future in futures]


def tick(now_dt=None) -> TickResult:
    run_at = now_dt or now()
    try:
        with _TickLock():
            due_jobs = get_due_jobs(now_dt=run_at)
            result = TickResult(due=len(due_jobs))
            workdir_jobs = [job for job in due_jobs if job.get("workdir")]
            parallel_jobs = [job for job in due_jobs if not job.get("workdir")]
            for job in workdir_jobs:
                result.results.append(_process_job(job, run_at))
            result.results.extend(_run_parallel(parallel_jobs, run_at))
            result.ran = len(result.results)
            result.succeeded = sum(1 for item in result.results if item.success)
            result.failed = sum(1 for item in result.results if not item.success)
            return result
    except BlockingIOError:
        return TickResult(skipped=1)
```

- [ ] **Step 5: Run scheduler tests**

Run: `pytest tests/test_cron_scheduler.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cron/scheduler.py tests/test_cron_scheduler.py
git commit -m "feat: add cron scheduler tick"
```

---

### Task 7: Scheduler Lifecycle

**Files:**
- Create: `agent_core/cron_lifecycle.py`
- Test: `tests/test_cron_lifecycle.py`

- [ ] **Step 1: Write failing lifecycle tests**

Add `tests/test_cron_lifecycle.py`:

```python
def test_start_cron_scheduler_is_idempotent(monkeypatch):
    import agent_core.cron_lifecycle as lifecycle

    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.daemon = kwargs.get("daemon")

        def start(self):
            started.append(self)

        def is_alive(self):
            return True

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(lifecycle, "_thread", None)
    monkeypatch.setattr(lifecycle.threading, "Thread", lambda **kwargs: FakeThread(**kwargs))

    assert lifecycle.start_cron_scheduler(interval_seconds=1) is True
    assert lifecycle.start_cron_scheduler(interval_seconds=1) is False
    assert len(started) == 1


def test_stop_cron_scheduler_sets_event(monkeypatch):
    import agent_core.cron_lifecycle as lifecycle

    stopped = []

    class FakeThread:
        def is_alive(self):
            return False

        def join(self, timeout=None):
            stopped.append(timeout)

    monkeypatch.setattr(lifecycle, "_thread", FakeThread())
    lifecycle._stop_event.clear()

    assert lifecycle.stop_cron_scheduler(timeout=0.1) is True
    assert lifecycle._stop_event.is_set()
    assert stopped == [0.1]
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_cron_lifecycle.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.cron_lifecycle'`.

- [ ] **Step 3: Implement lifecycle**

Create `agent_core/cron_lifecycle.py`:

```python
from __future__ import annotations

import logging
import threading
import time

from cron.scheduler import tick

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _ticker_loop(interval_seconds: float) -> None:
    while not _stop_event.is_set():
        try:
            tick()
        except Exception:
            logger.exception("Cron scheduler tick failed.")
        _stop_event.wait(interval_seconds)


def start_cron_scheduler(interval_seconds: float = 60) -> bool:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        _thread = threading.Thread(
            target=_ticker_loop,
            args=(max(1.0, float(interval_seconds)),),
            name="cron-ticker",
            daemon=True,
        )
        _thread.start()
        return True


def stop_cron_scheduler(timeout: float | None = None) -> bool:
    global _thread
    with _lock:
        thread = _thread
        if thread is None:
            _stop_event.set()
            return False
        _stop_event.set()
    thread.join(timeout=timeout)
    with _lock:
        stopped = not thread.is_alive()
        if stopped:
            _thread = None
        return stopped


def is_cron_scheduler_running() -> bool:
    thread = _thread
    return bool(thread is not None and thread.is_alive())
```

- [ ] **Step 4: Run lifecycle tests**

Run: `pytest tests/test_cron_lifecycle.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/cron_lifecycle.py tests/test_cron_lifecycle.py
git commit -m "feat: add cron scheduler lifecycle"
```

---

### Task 8: End-to-End Cron Port Verification

**Files:**
- Modify: `cron/__init__.py`
- Modify: `README.md`
- Test: existing and new cron tests

- [ ] **Step 1: Add public package exports**

Update `cron/__init__.py`:

```python
"""Project-native cron scheduling support."""

from cron.jobs import create_job, get_job, list_jobs
from cron.notifications import drain_cron_notifications_for_thread_id
from cron.scheduler import tick

__all__ = [
    "create_job",
    "drain_cron_notifications_for_thread_id",
    "get_job",
    "list_jobs",
    "tick",
]
```

- [ ] **Step 2: Document embedding API**

Add a short README section:

```markdown
## Cron Scheduling

Cron is not started automatically. Embedding applications that want scheduled
jobs should call:

```python
from agent_core.cron_lifecycle import start_cron_scheduler, stop_cron_scheduler

start_cron_scheduler(interval_seconds=60)
```

To let the parent agent create and manage jobs, build it explicitly with:

```python
from agent_core.builders import build_agent

agent = build_agent(include_cron_tools=True)
```

Cron output is saved under the configured toolkit home. `deliver="origin"`
queues thread-scoped notifications that embedding applications can drain with
`cron.notifications.drain_cron_notifications_for_thread_id(thread_id)`.
```

- [ ] **Step 3: Run focused cron test suite**

Run:

```bash
pytest \
  tests/test_cron_paths.py \
  tests/test_cron_jobs.py \
  tests/test_cron_notifications.py \
  tests/test_cronjob_tool.py \
  tests/test_cron_runner.py \
  tests/test_cron_scheduler.py \
  tests/test_cron_lifecycle.py \
  -v
```

Expected: PASS.

- [ ] **Step 4: Run adjacent integration tests**

Run:

```bash
pytest \
  tests/test_agent_tools_structure.py \
  tests/test_terminal_lifecycle.py \
  tests/test_agent_runner.py \
  -v
```

Expected: PASS.

- [ ] **Step 5: Run import smoke checks**

Run:

```bash
python - <<'PY'
from agent_core.builders import build_agent
from agent_core.cron_lifecycle import is_cron_scheduler_running
from agent_tools.public.cronjob import cronjob
from cron.scheduler import tick

assert callable(build_agent)
assert callable(is_cron_scheduler_running)
assert getattr(cronjob, "name", "") == "cronjob"
assert callable(tick)
print("cron imports ok")
PY
```

Expected: prints `cron imports ok`.

- [ ] **Step 6: Commit**

```bash
git add cron/__init__.py README.md
git commit -m "docs: document cron embedding api"
```

---

### Task 9: Final Verification and Cleanup

**Files:**
- Inspect: `cron/`, `agent_core/`, `agent_tools/public/`, `tests/`

- [ ] **Step 1: Run all tests**

Run: `pytest -v`

Expected: PASS.

- [ ] **Step 2: Search for forbidden reference-only imports**

Run:

```bash
rg -n "cron_constants|cron_cli|gateway\\.|tools\\.registry|run_agent|AIAgent|send_message_tool" cron agent_core agent_tools tests
```

Expected: no matches in new cron integration code. Existing terminal-toolkit references are acceptable only outside the new cron modules.

- [ ] **Step 3: Verify no recursive cron tool in runner**

Run:

```bash
python - <<'PY'
from cron.runner import build_cron_tools

for toolsets in (None, [], ["terminal", "file_write", "delegation"]):
    names = [getattr(tool, "name", "") for tool in build_cron_tools(toolsets)]
    assert "cronjob" not in names, names
print("cron runner excludes cronjob")
PY
```

Expected: prints `cron runner excludes cronjob`.

- [ ] **Step 4: Check git status**

Run: `git status --short`

Expected: only intentional files are modified or untracked. Do not revert pre-existing user changes.

- [ ] **Step 5: Final commit if verification required fixes**

If fixes were needed after Task 8, commit only those fixes:

```bash
git add <fixed-files>
git commit -m "fix: complete cron port verification"
```

If no fixes were needed, skip this commit.

---

## Self-Review

Spec coverage:

- Explicit lifecycle: Task 7.
- Home/path behavior and permissions: Task 1.
- Job storage, schedules, due jobs, repeat, at-most-once state: Task 2 and Task 6.
- Thread notification delivery: Task 3 and Task 6.
- Optional parent-agent tool exposure: Task 4.
- Conservative cron tool permissions: Task 5.
- Script wake gate: Task 5.
- Workdir support and sequential execution: Task 2 and Task 6.
- Model/provider/base_url compatibility without execution override: Task 2 and Task 5.
- Tests and non-goals: Task 8 and Task 9.

Placeholder scan:

- No open-ended implementation gaps are intentionally left.
- Commands include expected outcomes.
- New function names are defined before subsequent tasks depend on them.

Risk notes:

- Task 2 has the broadest surface and may need small follow-up tests while implementing `get_due_jobs` and output helpers.
- Task 5 uses whole-agent timeout, matching the approved first-version compromise.
- Existing untracked copied `cron/` files should be replaced/adapted carefully without reverting unrelated user changes.

# Cron Run Observability and Timeouts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persisted cron run activity, job-level idle/max runtime timeout settings, timeout exit reasons, and richer `cron status` diagnostics.

**Architecture:** Extend the existing SQLite-backed `StateStore` as the source of truth for job timeout settings and run activity. Keep scheduler-owned run state transitions, add a lightweight activity reporter boundary for runner code, and make status output consume focused StateStore query helpers instead of ad hoc SQL in CLI code.

**Tech Stack:** Python, SQLite via `sqlite3`, pytest, existing `cron` modules, existing `agent_cli/cron_commands.py`.

---

## File Map

- `cron/state_store.py`: Add schema columns, row mapping, timeout resolution, activity updates, status query helpers, timeout completion helper.
- `cron/jobs.py`: Add create/update normalization for `idle_timeout_seconds` and `max_runtime_seconds`.
- `agent_tools/public/cronjob.py`: Add tool schema fields and boundary validation.
- `cron/contracts.py`: Add optional `exit_reason` to `JobRunResult` so runner timeouts can report stable reasons.
- `cron/scheduler.py`: Add activity reporter wiring, phase activity updates, and pass resolved timeout settings to runners.
- `cron/runner.py`: Enforce idle timeout by activity time and optional max runtime in inprocess mode; report script/agent activity.
- `cron/runner_subprocess.py`: Preserve subprocess hard timeout, add parent-side heartbeat and optional max runtime handling.
- `agent_cli/cron_commands.py`: Extend `cron status` with next due, running, stale running, and latest failed run Top N sections.
- Tests:
  - `tests/test_cron_state_store.py`
  - `tests/test_cron_jobs.py`
  - `tests/test_cronjob_tool.py`
  - `tests/test_cron_scheduler.py`
  - `tests/test_cron_runner.py`
  - `tests/test_cron_runner_subprocess.py`
  - `tests/test_agent_cli_cron_commands.py`

---

### Task 1: Extend SQLite Schema and StateStore Run Activity Helpers

**Files:**
- Modify: `cron/state_store.py`
- Test: `tests/test_cron_state_store.py`

- [ ] **Step 1: Write failing schema and activity tests**

Add tests that prove new columns are created, claim/start write activity, heartbeat can update without activity, and terminal runs ignore late activity.

```python
def test_run_activity_columns_and_claim_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()

    claimed = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]
    run = store.get_run(claimed["run"]["id"])

    assert run["heartbeat_at"] is not None
    assert run["last_activity_at"] is not None
    assert run["last_activity_desc"] == "claimed"
    assert run["current_tool"] is None


def test_update_run_activity_can_heartbeat_without_activity(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()
    run_id = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]["run"]["id"]
    started = store.mark_run_started(run_id)
    original_activity = started["last_activity_at"]

    updated = store.update_run_activity(run_id, heartbeat=True, activity=False)

    assert updated["heartbeat_at"] is not None
    assert updated["last_activity_at"] == original_activity
    assert updated["last_activity_desc"] == "started"


def test_terminal_run_ignores_late_activity(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()
    run_id = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]["run"]["id"]
    store.mark_run_started(run_id)
    store.complete_run(
        run_id,
        success=True,
        output_path="/tmp/out.md",
        final_response="done",
        error=None,
        next_run_at=None,
        completed=True,
    )

    assert store.update_run_activity(run_id, activity=True, last_activity_desc="agent_running") is None
    assert store.get_run(run_id)["last_activity_desc"] == "completed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cron_state_store.py::test_run_activity_columns_and_claim_defaults tests/test_cron_state_store.py::test_update_run_activity_can_heartbeat_without_activity tests/test_cron_state_store.py::test_terminal_run_ignores_late_activity -v`

Expected: FAIL because activity columns and `update_run_activity` do not exist yet.

- [ ] **Step 3: Implement schema columns and row mapping**

In `cron/state_store.py`, add run columns to `CREATE TABLE runs` and `_ensure_columns` handling:

```python
RUN_STATUSES = {"claimed", "running", "succeeded", "failed", "skipped", "abandoned"}

# In CREATE TABLE runs:
heartbeat_at TEXT,
last_activity_at TEXT,
last_activity_desc TEXT,
current_tool TEXT,

# In _ensure_columns call for runs:
"heartbeat_at": "TEXT",
"last_activity_at": "TEXT",
"last_activity_desc": "TEXT",
"current_tool": "TEXT",
```

Keep `_row_to_run()` returning `dict(row)` so new columns are automatically exposed.

- [ ] **Step 4: Implement activity writes in claim/start/complete**

Update `claim_due_jobs()` run insert column list and values so new runs start with `claimed`:

```python
INSERT INTO runs (
    id, job_id, scheduled_for, claimed_at, lease_expires_at,
    started_at, finished_at, attempt, status, exit_reason,
    output_path, final_response, error, delivery_status,
    heartbeat_at, last_activity_at, last_activity_desc, current_tool,
    created_at, updated_at
) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'claimed', NULL,
          NULL, NULL, NULL, NULL,
          ?, ?, 'claimed', NULL,
          ?, ?)
```

Use `now_actual` for `heartbeat_at` and `last_activity_at`.

Update `_skip_missed_job_if_needed()` skipped run insert so skipped records have activity `missed_run`.

Update `mark_run_started()`:

```python
UPDATE runs
SET status = 'running',
    started_at = ?,
    heartbeat_at = ?,
    last_activity_at = ?,
    last_activity_desc = 'started',
    current_tool = NULL,
    updated_at = ?
WHERE id = ? AND status = 'claimed'
```

Update `complete_run()` successful terminal update to set:

```python
heartbeat_at = ?,
last_activity_at = ?,
last_activity_desc = 'completed',
current_tool = NULL
```

- [ ] **Step 5: Add `update_run_activity()`**

Add this method to `StateStore`:

```python
def update_run_activity(
    self,
    run_id: str,
    *,
    heartbeat: bool = True,
    activity: bool = False,
    last_activity_desc: str | None = None,
    current_tool: str | None = None,
) -> dict[str, Any] | None:
    now_text = utc_now().isoformat()
    assignments: list[str] = []
    values: list[Any] = []
    if heartbeat or activity:
        assignments.append("heartbeat_at = ?")
        values.append(now_text)
    if activity:
        assignments.append("last_activity_at = ?")
        values.append(now_text)
    if last_activity_desc is not None:
        assignments.append("last_activity_desc = ?")
        values.append(last_activity_desc)
    if current_tool is not None or activity:
        assignments.append("current_tool = ?")
        values.append(current_tool)
    if not assignments:
        return self.get_run(run_id)
    assignments.append("updated_at = ?")
    values.append(now_text)
    values.append(run_id)
    with self._connect() as conn:
        cursor = conn.execute(
            f"""
            UPDATE runs
            SET {", ".join(assignments)}
            WHERE id = ?
              AND status IN ('claimed', 'running')
            """,
            values,
        )
    if not cursor.rowcount:
        return None
    return self.get_run(run_id)
```

- [ ] **Step 6: Run state store tests**

Run: `pytest tests/test_cron_state_store.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cron/state_store.py tests/test_cron_state_store.py
git commit -m "feat: persist cron run activity"
```

---

### Task 2: Add Job Timeout Schema and Boundary Validation

**Files:**
- Modify: `cron/state_store.py`
- Modify: `cron/jobs.py`
- Modify: `agent_tools/public/cronjob.py`
- Test: `tests/test_cron_jobs.py`
- Test: `tests/test_cronjob_tool.py`

- [ ] **Step 1: Write failing persistence and validation tests**

Add job persistence tests:

```python
def test_create_job_persists_timeout_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, get_job

    job = create_job(
        prompt="write report",
        schedule="30m",
        idle_timeout_seconds=120,
        max_runtime_seconds=900,
    )

    stored = get_job(job["id"])
    assert stored["idle_timeout_seconds"] == 120
    assert stored["max_runtime_seconds"] == 900


def test_update_job_normalizes_timeout_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job

    job = create_job(prompt="write report", schedule="30m")
    updated = update_job(
        job["id"],
        {"idle_timeout_seconds": "0", "max_runtime_seconds": ""},
    )

    assert updated["idle_timeout_seconds"] == 0
    assert updated["max_runtime_seconds"] is None
```

Add tool validation tests:

```python
def test_cronjob_rejects_negative_max_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_tools.public.cronjob import run_cronjob_action

    result = run_cronjob_action(
        "create",
        prompt="write report",
        schedule="30m",
        max_runtime_seconds=-1,
    )

    assert result["success"] is False
    assert result["code"] == "invalid_timeout"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cron_jobs.py::test_create_job_persists_timeout_settings tests/test_cron_jobs.py::test_update_job_normalizes_timeout_settings tests/test_cronjob_tool.py::test_cronjob_rejects_negative_max_runtime -v`

Expected: FAIL because job APIs and tool schema do not accept timeout fields yet.

- [ ] **Step 3: Add columns and row mapping**

In `cron/state_store.py`, add columns to `jobs`:

```python
idle_timeout_seconds INTEGER,
max_runtime_seconds INTEGER,
```

Add `_ensure_columns` entries:

```python
"idle_timeout_seconds": "INTEGER",
"max_runtime_seconds": "INTEGER",
```

Add to `_job_to_row_values()`:

```python
"idle_timeout_seconds": job.get("idle_timeout_seconds"),
"max_runtime_seconds": job.get("max_runtime_seconds"),
```

Add to `_row_to_job()`:

```python
"idle_timeout_seconds": row["idle_timeout_seconds"],
"max_runtime_seconds": row["max_runtime_seconds"],
```

- [ ] **Step 4: Add timeout normalization in `cron/jobs.py`**

Add a helper:

```python
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
```

Extend `create_job()` signature:

```python
idle_timeout_seconds: int | str | None = None,
max_runtime_seconds: int | str | None = None,
```

Add to the job dict:

```python
"idle_timeout_seconds": _normalize_timeout_seconds(idle_timeout_seconds),
"max_runtime_seconds": _normalize_timeout_seconds(max_runtime_seconds),
```

Update `_normalize_updates()`:

```python
for key in ("idle_timeout_seconds", "max_runtime_seconds"):
    if key in normalized_updates:
        normalized_updates[key] = _normalize_timeout_seconds(normalized_updates[key])
```

- [ ] **Step 5: Add tool schema fields and validation**

In `agent_tools/public/cronjob.py`, add `CronJobInput` fields:

```python
idle_timeout_seconds: int | None = Field(
    default=None,
    description="Idle timeout in seconds; 0 disables idle timeout. Defaults to AGENT_CRON_TIMEOUT.",
)
max_runtime_seconds: int | None = Field(
    default=None,
    description="Optional hard runtime cap in seconds; 0 or omitted disables the cap.",
)
```

Add validation helper:

```python
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
```

Call it in create/update for both fields before `create_job()` or `update_job()`. Pass both values through in `run_cronjob_action()` and `cronjob()`.

- [ ] **Step 6: Run timeout schema tests**

Run: `pytest tests/test_cron_jobs.py tests/test_cronjob_tool.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cron/state_store.py cron/jobs.py agent_tools/public/cronjob.py tests/test_cron_jobs.py tests/test_cronjob_tool.py
git commit -m "feat: add cron job timeout settings"
```

---

### Task 3: Add Timeout Resolution and Run Status Query Helpers

**Files:**
- Modify: `cron/state_store.py`
- Test: `tests/test_cron_state_store.py`

- [ ] **Step 1: Write failing tests for timeout resolution and status helpers**

```python
def test_resolve_job_timeouts_uses_job_then_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_TIMEOUT", "77")

    from cron.state_store import StateStore

    store = StateStore()
    assert store.resolve_job_timeouts({}) == {
        "idle_timeout_seconds": 77,
        "max_runtime_seconds": None,
    }
    assert store.resolve_job_timeouts({"idle_timeout_seconds": 12, "max_runtime_seconds": 30}) == {
        "idle_timeout_seconds": 12,
        "max_runtime_seconds": 30,
    }


def test_status_query_helpers_return_next_running_and_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()
    assert store.list_next_due_jobs(limit=1)[0]["id"] == job["id"]

    run_id = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]["run"]["id"]
    store.mark_run_started(run_id)
    assert store.list_running_runs(limit=1)[0]["run_id"] == run_id

    store.complete_run(
        run_id,
        success=False,
        output_path=None,
        final_response=None,
        error="boom",
        next_run_at=None,
        completed=True,
    )
    assert store.latest_failed_run()["run_id"] == run_id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cron_state_store.py::test_resolve_job_timeouts_uses_job_then_env tests/test_cron_state_store.py::test_status_query_helpers_return_next_running_and_failed -v`

Expected: FAIL because helper methods do not exist.

- [ ] **Step 3: Implement timeout resolution**

Add to `StateStore`:

```python
def resolve_job_timeouts(self, job: dict[str, Any]) -> dict[str, int | None]:
    def positive_or_zero(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    idle = positive_or_zero(job.get("idle_timeout_seconds"))
    if idle is None:
        raw = os.getenv("AGENT_CRON_TIMEOUT", "600")
        try:
            idle = int(raw)
        except ValueError:
            idle = 600
    max_runtime = positive_or_zero(job.get("max_runtime_seconds"))
    return {
        "idle_timeout_seconds": idle if idle is not None and idle > 0 else None,
        "max_runtime_seconds": max_runtime if max_runtime is not None and max_runtime > 0 else None,
    }
```

Add `import os` at the top of `cron/state_store.py`.

- [ ] **Step 4: Implement status query helpers**

Add helpers that join jobs and runs:

```python
def list_next_due_jobs(self, *, limit: int = 5) -> list[dict[str, Any]]:
    with self._connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE enabled = 1
              AND state = 'scheduled'
              AND next_run_at IS NOT NULL
            ORDER BY next_run_at, created_at, id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [self._row_to_job(row) for row in rows]


def list_running_runs(self, *, limit: int = 5) -> list[dict[str, Any]]:
    with self._connect() as conn:
        rows = conn.execute(
            """
            SELECT runs.id AS run_id, runs.*, jobs.name AS job_name, jobs.next_run_at,
                   jobs.id AS job_id, jobs.idle_timeout_seconds, jobs.max_runtime_seconds
            FROM runs
            JOIN jobs ON jobs.id = runs.job_id
            WHERE runs.status IN ('claimed', 'running')
            ORDER BY COALESCE(runs.started_at, runs.claimed_at), runs.id
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_failed_run(self) -> dict[str, Any] | None:
    with self._connect() as conn:
        row = conn.execute(
            """
            SELECT runs.id AS run_id, runs.*, jobs.name AS job_name, jobs.id AS job_id
            FROM runs
            JOIN jobs ON jobs.id = runs.job_id
            WHERE runs.status IN ('failed', 'abandoned')
            ORDER BY COALESCE(runs.finished_at, runs.updated_at) DESC
            LIMIT 1
            """
        ).fetchone()
    return None if row is None else dict(row)
```

Add `list_stale_running_runs()` using `_parse_time()` and `resolve_job_timeouts()`:

```python
def list_stale_running_runs(self, *, now_text: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    now_dt = _parse_time(now_text or utc_now().isoformat())
    if now_dt is None:
        return []
    stale: list[dict[str, Any]] = []
    for row in self.list_running_runs(limit=100):
        job_timeout = self.resolve_job_timeouts(row)
        reason = None
        heartbeat_at = _parse_time(row.get("heartbeat_at"))
        activity_at = _parse_time(row.get("last_activity_at") or row.get("started_at") or row.get("claimed_at"))
        lease_expires_at = _parse_time(row.get("lease_expires_at"))
        if lease_expires_at is not None and lease_expires_at <= now_dt:
            reason = "lease_expired"
        elif heartbeat_at is not None and (now_dt - heartbeat_at).total_seconds() > 300:
            reason = "heartbeat_stale"
        elif job_timeout["idle_timeout_seconds"] and activity_at is not None:
            if (now_dt - activity_at).total_seconds() > job_timeout["idle_timeout_seconds"]:
                reason = "idle_timeout_exceeded"
        if reason:
            enriched = dict(row)
            enriched["stale_reason"] = reason
            stale.append(enriched)
        if len(stale) >= max(1, int(limit)):
            break
    return stale
```

- [ ] **Step 5: Run state store helper tests**

Run: `pytest tests/test_cron_state_store.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cron/state_store.py tests/test_cron_state_store.py
git commit -m "feat: add cron run status queries"
```

---

### Task 4: Add Activity Reporter and Scheduler Phase Updates

**Files:**
- Modify: `cron/scheduler.py`
- Modify: `cron/runner.py`
- Modify: `cron/runner_subprocess.py`
- Test: `tests/test_cron_scheduler.py`

- [ ] **Step 1: Write failing scheduler activity tests**

```python
def test_scheduler_records_agent_and_delivery_activity(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import create_job
    from cron.scheduler import tick
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")

    def fake_runner(running_job):
        assert running_job["run_id"]
        reporter = running_job["_activity_reporter"]
        reporter(activity=True, last_activity_desc="agent_running")
        return JobRunResult(success=True, output_doc="ok", final_response="ok")

    result = tick(job_runner=fake_runner)

    assert result.succeeded == 1
    store = StateStore()
    with store._connect() as conn:
        runs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM runs WHERE job_id = ? ORDER BY created_at DESC",
                (job["id"],),
            ).fetchall()
        ]
    assert runs[0]["last_activity_desc"] == "completed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cron_scheduler.py::test_scheduler_records_agent_and_delivery_activity -v`

Expected: FAIL because `_activity_reporter` is not attached.

- [ ] **Step 3: Add scheduler reporter factory**

In `cron/scheduler.py`, add:

```python
def _activity_reporter(store: StateStore, run_id: str):
    def report(
        *,
        heartbeat: bool = True,
        activity: bool = False,
        last_activity_desc: str | None = None,
        current_tool: str | None = None,
    ) -> None:
        try:
            store.update_run_activity(
                run_id,
                heartbeat=heartbeat,
                activity=activity,
                last_activity_desc=last_activity_desc,
                current_tool=current_tool,
            )
        except Exception:
            logger.exception("Failed to update cron run activity for %s", run_id)
    return report
```

- [ ] **Step 4: Wire reporter through `_process_claimed()`**

After `mark_run_started()` succeeds:

```python
report_activity = _activity_reporter(store, run_id)
job["_activity_reporter"] = report_activity
job["timeout_settings"] = store.resolve_job_timeouts(job)
report_activity(activity=True, last_activity_desc="started")
```

Before `job_runner(job)`:

```python
report_activity(activity=True, last_activity_desc="agent_running")
```

Before delivery enqueue:

```python
report_activity(activity=True, last_activity_desc="delivery_pending")
```

After `process_due()`:

```python
report_activity(activity=True, last_activity_desc="delivery_dispatched")
```

Ensure `_activity_reporter` is not serialized into delivery payloads. Create a sanitized delivery job before calling `enqueue_result()`:

```python
delivery_job = {key: value for key, value in job.items() if not key.startswith("_")}
```

Use `delivery_job` for `enqueue_result()`.

- [ ] **Step 5: Add runner-side optional reporter usage**

In `cron/runner.py`, add:

```python
def _report_activity(job: dict[str, Any], desc: str, *, current_tool: str | None = None) -> None:
    reporter = job.get("_activity_reporter")
    if callable(reporter):
        reporter(activity=True, last_activity_desc=desc, current_tool=current_tool)
```

Call before script:

```python
if script:
    _report_activity(job, "script_running")
```

Call before agent invoke:

```python
_report_activity(job, "agent_running")
```

In `cron/runner_subprocess.py`, add parent heartbeat helper:

```python
def _heartbeat(job: dict[str, Any]) -> None:
    reporter = job.get("_activity_reporter")
    if callable(reporter):
        reporter(heartbeat=True, activity=False)
```

Use `_heartbeat(job)` in any wait loop added in Task 5.

- [ ] **Step 6: Run scheduler tests**

Run: `pytest tests/test_cron_scheduler.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cron/scheduler.py cron/runner.py cron/runner_subprocess.py tests/test_cron_scheduler.py
git commit -m "feat: record cron run activity phases"
```

---

### Task 5: Enforce Idle Timeout and Optional Max Runtime

**Files:**
- Modify: `cron/contracts.py`
- Modify: `cron/runner.py`
- Modify: `cron/runner_subprocess.py`
- Modify: `cron/scheduler.py`
- Modify: `cron/state_store.py`
- Test: `tests/test_cron_runner.py`
- Test: `tests/test_cron_runner_subprocess.py`
- Test: `tests/test_cron_scheduler.py`

- [ ] **Step 1: Write failing runner timeout tests**

For inprocess runner, add tests using fake queue/process hooks already present in `tests/test_cron_runner.py`:

```python
def test_inprocess_idle_timeout_uses_job_timeout(monkeypatch):
    from cron import runner

    class NeverRespondingProcess:
        pid = 12345
        def start(self): pass
        def is_alive(self): return True
        def join(self, timeout=None): pass
        def terminate(self): pass

    monkeypatch.setattr(runner, "_create_agent_process", lambda job, prompt, result_queue: NeverRespondingProcess())
    monkeypatch.setattr(runner, "_create_agent_result_queue", lambda: object())
    monkeypatch.setattr(runner, "_read_agent_result", lambda result_queue, timeout=None: (_ for _ in ()).throw(runner.queue.Empty()))

    result = runner.run_job({
        "id": "job-1",
        "name": "daily",
        "prompt": "work",
        "timeout_settings": {"idle_timeout_seconds": 1, "max_runtime_seconds": None},
    })

    assert result.success is False
    assert result.exit_reason == "idle_timeout"
    assert "idle" in result.error.lower()
```

For scheduler persistence:

```python
def test_scheduler_persists_idle_timeout_exit_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import create_job
    from cron.scheduler import tick
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", idle_timeout_seconds=1)

    def fake_runner(running_job):
        return JobRunResult(success=False, output_doc="timed out", final_response="", error="idle", exit_reason="idle_timeout")

    tick(job_runner=fake_runner)

    run = StateStore().latest_failed_run()
    assert run["exit_reason"] == "idle_timeout"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cron_runner.py::test_inprocess_idle_timeout_uses_job_timeout tests/test_cron_scheduler.py::test_scheduler_persists_idle_timeout_exit_reason -v`

Expected: FAIL because `JobRunResult.exit_reason` and timeout handling do not exist.

- [ ] **Step 3: Extend `JobRunResult`**

In `cron/contracts.py`:

```python
@dataclass(frozen=True)
class JobRunResult:
    success: bool
    output_doc: str | None = None
    final_response: str | None = None
    error: str | None = None
    exit_reason: str | None = None
```

Update result serialization in `cron/runner_worker.py` and parsing in `cron/runner_subprocess.py` so `exit_reason` is optional and round-trips:

```python
exit_reason = data.get("exit_reason")
if exit_reason is not None and not isinstance(exit_reason, str):
    raise _ResultFileError("Result field 'exit_reason' must be string or null")
```

- [ ] **Step 4: Add completion exit reason support**

In `StateStore.complete_run()`, add parameter:

```python
exit_reason: str | None = None,
```

Use it in terminal update:

```python
SET status = ?,
    finished_at = ?,
    output_path = ?,
    final_response = ?,
    error = ?,
    exit_reason = ?,
```

Pass `exit_reason` as:

```python
exit_reason if not success else None
```

Update existing callers to omit it except scheduler, where pass `result.exit_reason`.

- [ ] **Step 5: Implement timeout settings in inprocess runner**

In `cron/runner.py`, add:

```python
def _timeout_settings(job: dict[str, Any]) -> dict[str, int | None]:
    settings = dict(job.get("timeout_settings") or {})
    idle = settings.get("idle_timeout_seconds")
    max_runtime = settings.get("max_runtime_seconds")
    if idle is None:
        idle = _cron_timeout()
    return {
        "idle_timeout_seconds": idle if idle and idle > 0 else None,
        "max_runtime_seconds": max_runtime if max_runtime and max_runtime > 0 else None,
    }
```

Replace `_invoke_cron_agent()` deadline logic with idle/max checks:

```python
settings = _timeout_settings(job)
started = time.monotonic()
last_activity = started
idle_timeout = settings["idle_timeout_seconds"]
max_runtime = settings["max_runtime_seconds"]
```

On every heartbeat loop:

```python
reporter = job.get("_activity_reporter")
if callable(reporter):
    reporter(heartbeat=True, activity=False)
now_mono = time.monotonic()
if max_runtime is not None and now_mono - started >= max_runtime:
    _terminate_agent_process(process)
    raise _CronRunTimeout("max_runtime_exceeded", f"Cron job exceeded max runtime of {max_runtime} seconds.")
if idle_timeout is not None and now_mono - last_activity >= idle_timeout:
    _terminate_agent_process(process)
    raise _CronRunTimeout("idle_timeout", f"Cron job idle for {idle_timeout} seconds.")
```

Define:

```python
class _CronRunTimeout(TimeoutError):
    def __init__(self, exit_reason: str, message: str) -> None:
        super().__init__(message)
        self.exit_reason = exit_reason
```

Catch it in `run_job()`:

```python
except _CronRunTimeout as exc:
    error = str(exc)
    return JobRunResult(
        success=False,
        output_doc=_output_doc(job, "", script_output, error),
        final_response="",
        error=error,
        exit_reason=exc.exit_reason,
    )
```

- [ ] **Step 6: Implement subprocess parent heartbeat and max runtime**

In `cron/runner_subprocess.py`, add `import time` and replace single `proc.wait(timeout=timeout_seconds)` with a polling loop:

```python
settings = dict(job.get("timeout_settings") or {})
max_runtime = settings.get("max_runtime_seconds")
hard_timeout = max_runtime if max_runtime and max_runtime > 0 else timeout_seconds
timeout_reason = "max_runtime_exceeded" if max_runtime and max_runtime > 0 else "subprocess_timeout"
started = time.monotonic()
while proc.poll() is None:
    _heartbeat(job)
    elapsed = time.monotonic() - started
    if hard_timeout and elapsed >= hard_timeout:
        timed_out = True
        _terminate_child(proc, grace_seconds)
        exit_code = proc.returncode if proc.returncode is not None else -1
        break
    time.sleep(1)
else:
    exit_code = proc.returncode
```

Return `_make_failure(..., exit_reason=timeout_reason)` when timeout fires. Extend `_make_failure()` to accept `exit_reason`. The existing `AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT` remains a parent-side safety cap and reports `subprocess_timeout`; job-level `max_runtime_seconds` reports `max_runtime_exceeded`. Job-level idle timeout is enforced in the inprocess runner in this phase because subprocess child-to-parent fine-grained activity messages are reserved for the next instrumentation phase.

- [ ] **Step 7: Pass exit reason through scheduler**

In `_process_claimed()`, pass:

```python
exit_reason=result.exit_reason,
```

to `store.complete_run()`.

When lease lost errors are synthetic, use existing lease-related behavior and do not invent timeout reasons.

- [ ] **Step 8: Run timeout tests**

Run: `pytest tests/test_cron_runner.py tests/test_cron_runner_subprocess.py tests/test_cron_scheduler.py -v`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add cron/contracts.py cron/runner.py cron/runner_subprocess.py cron/scheduler.py cron/state_store.py tests/test_cron_runner.py tests/test_cron_runner_subprocess.py tests/test_cron_scheduler.py
git commit -m "feat: enforce cron idle and runtime timeouts"
```

---

### Task 6: Extend `cron status` Diagnostics

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing status tests**

```python
def test_cron_status_shows_next_due_running_stale_and_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore
    from agent_cli.cron_commands import cron_status

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local", idle_timeout_seconds=1)
    store = StateStore()
    run_id = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]["run"]["id"]
    store.mark_run_started(run_id)
    store.update_run_activity(run_id, activity=True, last_activity_desc="agent_running", current_tool="terminal")

    result = cron_status()

    assert "Running:" in result.text
    assert "daily" in result.text
    assert "agent_running" in result.text
```

Add a separate latest failed assertion:

```python
def test_cron_status_shows_latest_failed_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job
    from cron.state_store import StateStore
    from agent_cli.cron_commands import cron_status

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    store = StateStore()
    run_id = store.claim_due_jobs(now_text=job["next_run_at"], limit=1)[0]["run"]["id"]
    store.mark_run_started(run_id)
    store.complete_run(
        run_id,
        success=False,
        output_path=None,
        final_response=None,
        error="boom",
        next_run_at=None,
        completed=True,
        exit_reason="idle_timeout",
    )

    result = cron_status()

    assert "Last failed run:" in result.text
    assert "idle_timeout" in result.text
    assert "boom" in result.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent_cli_cron_commands.py::test_cron_status_shows_next_due_running_stale_and_failed tests/test_agent_cli_cron_commands.py::test_cron_status_shows_latest_failed_run -v`

Expected: FAIL because `cron_status()` does not show these sections.

- [ ] **Step 3: Add formatter helpers**

In `agent_cli/cron_commands.py`, add:

```python
_STATUS_DETAIL_LIMIT = 5


def _short_id(value: Any) -> str:
    text = str(value or "-")
    return text[:12]


def _run_status_line(row: dict[str, Any]) -> str:
    return (
        f"  {_short_id(row.get('job_id'))} "
        f"{row.get('job_name') or '-'} "
        f"run={_short_id(row.get('run_id'))} "
        f"started={row.get('started_at') or row.get('claimed_at') or '-'} "
        f"heartbeat={row.get('heartbeat_at') or '-'} "
        f"activity={row.get('last_activity_desc') or '-'}"
        f"{' tool=' + str(row.get('current_tool')) if row.get('current_tool') else ''}"
    )
```

Add:

```python
def _run_observability_lines(state_store) -> list[str]:
    lines: list[str] = []
    next_due = state_store.list_next_due_jobs(limit=_STATUS_DETAIL_LIMIT)
    if next_due:
        lines.append("Next due:")
        for job in next_due:
            lines.append(f"  {_short_id(job.get('id'))} {job.get('name') or '-'} at {job.get('next_run_at')}")
    running = state_store.list_running_runs(limit=_STATUS_DETAIL_LIMIT)
    if running:
        lines.append("Running:")
        lines.extend(_run_status_line(row) for row in running)
    stale = state_store.list_stale_running_runs(limit=_STATUS_DETAIL_LIMIT)
    if stale:
        lines.append("Stale running:")
        for row in stale:
            lines.append(f"{_run_status_line(row)} reason={row.get('stale_reason')}")
    failed = state_store.latest_failed_run()
    if failed:
        lines.append(
            "Last failed run: "
            f"job={_short_id(failed.get('job_id'))} "
            f"run={_short_id(failed.get('run_id'))} "
            f"exit={failed.get('exit_reason') or '-'} "
            f"error={failed.get('error') or '-'}"
        )
    return lines
```

- [ ] **Step 4: Wire into `cron_status()`**

After job states and delivery adapters:

```python
try:
    lines.extend(_run_observability_lines(state_store))
except Exception as exc:
    lines.append(f"Run observability: unavailable ({exc})")
```

- [ ] **Step 5: Run CLI status tests**

Run: `pytest tests/test_agent_cli_cron_commands.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: show cron run observability in status"
```

---

### Task 7: Final Verification and Cleanup

**Files:**
- Potentially modify files touched in Tasks 1-6 if verification exposes issues.

- [ ] **Step 1: Run focused cron test suite**

Run:

```bash
pytest \
  tests/test_cron_state_store.py \
  tests/test_cron_jobs.py \
  tests/test_cronjob_tool.py \
  tests/test_cron_scheduler.py \
  tests/test_cron_runner.py \
  tests/test_cron_runner_subprocess.py \
  tests/test_agent_cli_cron_commands.py \
  -v
```

Expected: PASS.

- [ ] **Step 2: Run import health and service tests**

Run:

```bash
pytest \
  tests/test_cron_import_health.py \
  tests/test_cron_service.py \
  tests/test_cron_service_state.py \
  tests/test_cron_leader.py \
  -v
```

Expected: PASS.

- [ ] **Step 3: Run formatting or lint checks available in the repo**

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 4: Inspect final diff**

Run: `git status --short`

Expected: only intentional tracked changes remain.

Run: `git diff --stat`

Expected: changes are limited to cron/state, runner, scheduler, CLI, tool schema, and tests.

- [ ] **Step 5: Commit final fixes if any**

If Step 1 or Step 2 required fixes, commit them:

```bash
git add cron agent_cli agent_tools tests
git commit -m "fix: stabilize cron observability tests"
```

If no fixes were needed, do not create an empty commit.

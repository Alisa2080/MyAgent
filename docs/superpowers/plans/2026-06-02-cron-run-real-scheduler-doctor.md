# Cron Run Real Scheduler And Doctor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agent cron run JOB_ID` execute through the real scheduler/run/delivery path and make `cron doctor` produce actionable Docker, Feishu, service env, next due, and missed-run diagnostics.

**Architecture:** Add persistent run metadata so manual runs survive queueing and service restarts without losing their “do not advance schedule” semantics. Reuse `cron.scheduler.tick()` as the only execution path: manual CLI run creates a manual claim, tick executes ready claimed runs, and completion preserves periodic schedule when the run is marked manual. Doctor gets small helper functions for Docker/backend readiness and schedule-progress diagnostics while preserving existing checks.

**Tech Stack:** Python, SQLite via `cron.state_store.StateStore`, existing cron scheduler/delivery modules, `agent_cli` command handlers, pytest with monkeypatch-based Docker/Feishu/service fakes.

---

## File Structure

- Modify `cron/state_store.py`: schema migration, manual run claim/dry-run plan, run metadata, schedule-preserving completion, missed-run summary.
- Modify `cron/scheduler.py`: include ready manual claims in ticks and preserve schedule/repeat when completing manual runs.
- Modify `agent_cli/cron_commands.py`: route `cron run` through manual claim + tick, improve dry-run output, add doctor diagnostics.
- Create `cron/docker_diagnostics.py`: deterministic Docker/backend readiness checks used by doctor and tests.
- Modify `tests/test_cron_state_store.py`: schema and manual claim state-machine tests.
- Modify `tests/test_cron_e2e.py`: scheduler/manual run/output/delivery/concurrency integration tests.
- Modify `tests/test_agent_cli_cron_commands.py`: CLI run/dry-run and doctor diagnostics tests.

Use this interpreter for tests:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest
```

---

### Task 1: Add Persistent Manual Run Metadata

**Files:**
- Modify: `cron/state_store.py`
- Test: `tests/test_cron_state_store.py`

- [ ] **Step 1: Write failing schema tests**

Append these tests to `tests/test_cron_state_store.py`:

```python
def test_state_store_initializes_manual_run_columns(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.state_store import StateStore

    store = StateStore()
    with store._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}

    assert "trigger_type" in columns
    assert "preserve_schedule" in columns
    assert store.schema_version() == 3


def test_state_store_migrates_manual_run_columns(monkeypatch, tmp_path):
    import sqlite3

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    db_path = tmp_path / "cron" / "cron.sqlite3"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')")
        conn.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                schedule_json TEXT NOT NULL,
                schedule_display TEXT,
                enabled INTEGER NOT NULL,
                state TEXT NOT NULL,
                next_run_at TEXT,
                last_run_at TEXT,
                last_status TEXT,
                last_error TEXT,
                last_delivery_error TEXT,
                repeat_json TEXT NOT NULL,
                deliver TEXT NOT NULL,
                delivery_targets_json TEXT,
                origin_json TEXT,
                workdir TEXT,
                script TEXT,
                context_from_json TEXT,
                skills_json TEXT,
                enabled_toolsets_json TEXT,
                model TEXT,
                provider TEXT,
                base_url TEXT,
                concurrency_key TEXT,
                lease_run_id TEXT,
                lease_expires_at TEXT,
                paused_reason TEXT,
                paused_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                lease_expires_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                attempt INTEGER NOT NULL,
                status TEXT NOT NULL,
                exit_reason TEXT,
                output_path TEXT,
                final_response TEXT,
                error TEXT,
                delivery_status TEXT,
                heartbeat_at TEXT,
                last_activity_at TEXT,
                last_activity_desc TEXT,
                current_tool TEXT,
                concurrency_key TEXT,
                concurrency_policy TEXT,
                replaced_by_run_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    from cron.state_store import StateStore

    store = StateStore()
    with store._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}

    assert columns >= {"trigger_type", "preserve_schedule"}
    assert store.schema_version() == 3
```

- [ ] **Step 2: Run schema tests and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py::test_state_store_initializes_manual_run_columns tests/test_cron_state_store.py::test_state_store_migrates_manual_run_columns -q
```

Expected: FAIL because `runs.trigger_type`, `runs.preserve_schedule`, and schema version `3` do not exist.

- [ ] **Step 3: Implement schema version 3**

In `cron/state_store.py`, change:

```python
SCHEMA_VERSION = 2
```

to:

```python
SCHEMA_VERSION = 3
```

In the `CREATE TABLE IF NOT EXISTS runs` statement, add these columns after `replaced_by_run_id TEXT,`:

```sql
                    trigger_type TEXT NOT NULL DEFAULT 'scheduled',
                    preserve_schedule INTEGER NOT NULL DEFAULT 0,
```

In `_migrate_schema()`, add this block before the final schema version write:

```python
        run_columns = self._columns(conn, "runs")
        if "trigger_type" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'scheduled'")
        if "preserve_schedule" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN preserve_schedule INTEGER NOT NULL DEFAULT 0")
```

If `_migrate_schema()` already has a local `run_columns` variable, reuse it and do not shadow unrelated variables.

- [ ] **Step 4: Update run row insertion**

In `cron/state_store.py`, change `_insert_run()` signature to:

```python
    def _insert_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        job: dict[str, Any],
        status: str,
        now_text: str,
        lease_expires_at: str | None,
        exit_reason: str | None = None,
        replaced_by_run_id: str | None = None,
        trigger_type: str = "scheduled",
        preserve_schedule: bool = False,
    ) -> dict[str, Any]:
```

In the `_insert_run()` SQL column list, add:

```sql
                trigger_type, preserve_schedule,
```

after `replaced_by_run_id,`.

In the VALUES list, add two SQL parameter slots before `created_at, updated_at` values:

```sql
                ?, ?,
```

In the parameters tuple, add:

```python
                trigger_type,
                1 if preserve_schedule else 0,
```

immediately before `now_text, now_text`.

- [ ] **Step 5: Run schema tests and verify pass**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py::test_state_store_initializes_manual_run_columns tests/test_cron_state_store.py::test_state_store_migrates_manual_run_columns -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cron/state_store.py tests/test_cron_state_store.py
git commit -m "feat: add manual cron run metadata"
```

---

### Task 2: Add Manual Claim And Manual Dry-Run Planning

**Files:**
- Modify: `cron/state_store.py`
- Test: `tests/test_cron_state_store.py`

- [ ] **Step 1: Write failing manual claim tests**

Append these tests to `tests/test_cron_state_store.py`:

```python
def _create_manual_claim_job(store, *, job_id="manual-job", next_run_at="2026-06-02T19:00:00+08:00", concurrency_policy="skip_if_running"):
    job = {
        "id": job_id,
        "name": "Manual job",
        "prompt": "Say hello",
        "schedule": {"kind": "interval", "every": 300},
        "schedule_display": "every 5m",
        "enabled": True,
        "state": "scheduled",
        "next_run_at": next_run_at,
        "repeat": {"times": None, "completed": 0},
        "deliver": ["local"],
        "delivery_targets": [{"target": "local", "target_type": "local", "adapter_key": "local", "address": None}],
        "origin": None,
        "workdir": None,
        "script": None,
        "context_from": [],
        "skills": [],
        "enabled_toolsets": [],
        "model": None,
        "provider": None,
        "base_url": None,
        "concurrency_key": job_id,
        "concurrency_policy": concurrency_policy,
        "created_at": "2026-06-02T18:00:00+08:00",
        "updated_at": "2026-06-02T18:00:00+08:00",
    }
    store.upsert_job(job)
    return job


def test_plan_manual_job_claim_ignores_due_time(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.state_store import StateStore

    store = StateStore()
    _create_manual_claim_job(store, next_run_at="2026-06-02T19:00:00+08:00")

    plan = store.plan_manual_job_claim("manual-job", now_text="2026-06-02T18:00:00+08:00")

    assert plan["decision"] == "would_claim"
    assert plan["manual"] is True
    assert plan["due"] is True
    assert plan["next_run_at"] == "2026-06-02T19:00:00+08:00"
    assert plan["preserve_schedule"] is True


def test_claim_manual_job_creates_claimed_run_without_advancing_job(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.state_store import StateStore

    store = StateStore()
    _create_manual_claim_job(store, next_run_at="2026-06-02T19:00:00+08:00")

    claimed = store.claim_manual_job("manual-job", now_text="2026-06-02T18:00:00+08:00")
    run = claimed["run"]
    job = store.get_job("manual-job")

    assert run["status"] == "claimed"
    assert run["trigger_type"] == "manual"
    assert run["preserve_schedule"] == 1
    assert run["scheduled_for"] == "2026-06-02T18:00:00+08:00"
    assert job["state"] == "running"
    assert job["next_run_at"] == "2026-06-02T19:00:00+08:00"
```

- [ ] **Step 2: Run manual claim tests and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py::test_plan_manual_job_claim_ignores_due_time tests/test_cron_state_store.py::test_claim_manual_job_creates_claimed_run_without_advancing_job -q
```

Expected: FAIL because `plan_manual_job_claim()` and `claim_manual_job()` do not exist.

- [ ] **Step 3: Implement shared claim planning helper**

In `cron/state_store.py`, add a private helper above `plan_job_claim()`:

```python
    def _plan_claim(
        self,
        job_id: str,
        *,
        now_text: str,
        manual: bool,
    ) -> dict[str, Any]:
        from cron.jobs import compute_next_run
        from cron.jobs import normalize_concurrency_key, normalize_concurrency_policy

        now_dt = _parse_time(now_text)
        job = self.get_job(job_id)
        if now_dt is None:
            return {"job_id": job_id, "decision": "invalid_now", "error": f"Invalid now_text: {now_text}", "manual": manual}
        if job is None:
            raise KeyError(job_id)
        key = normalize_concurrency_key(job.get("concurrency_key"), str(job["id"]))
        policy = normalize_concurrency_policy(job.get("concurrency_policy"))
        timeouts = self.resolve_job_timeouts(job)
        targets = job.get("delivery_targets")
        if targets is None:
            targets = self._delivery_targets_for_job(job, strict=False) or []
        base = {
            "job_id": job_id,
            "job": job,
            "manual": manual,
            "preserve_schedule": manual,
            "next_run_at": job.get("next_run_at"),
            "next_scheduled_at": job.get("next_run_at"),
            "concurrency_key": key,
            "concurrency_policy": policy,
            "timeouts": timeouts,
            "delivery_targets": targets,
        }
        if not job.get("enabled", True):
            return {**base, "decision": "disabled", "due": False}
        if str(job.get("state") or "") == "completed":
            return {**base, "decision": "completed", "due": False}
        due_at = _parse_time(job.get("next_run_at"))
        if not manual and (due_at is None or due_at > now_dt):
            return {**base, "decision": "not_due", "due": False}
        with self._connect() as conn:
            active = self._active_runs_for_key(conn, key)
        occupying = [run for run in active if run["status"] in self.OCCUPYING_RUN_STATUSES]
        queued = [run for run in active if run["status"] == "queued"]
        decision = "would_claim"
        if policy == "queue_one" and (occupying or queued):
            decision = "would_queue"
        elif policy == "queue_all" and occupying:
            decision = "would_queue"
        elif policy == "skip_if_running" and active:
            decision = "would_skip"
        elif policy == "replace_running" and active:
            decision = "would_replace"
        next_scheduled_at = job.get("next_run_at")
        if not manual and job.get("schedule", {}).get("kind") != "once":
            next_scheduled_at = compute_next_run(job["schedule"], job.get("next_run_at"))
        return {
            **base,
            "decision": decision,
            "due": True,
            "next_scheduled_at": next_scheduled_at,
            "active_run_count": len(active),
            "active_runs": active[:5],
        }
```

Replace the body of `plan_job_claim()` with:

```python
    def plan_job_claim(self, job_id: str, *, now_text: str) -> dict[str, Any]:
        return self._plan_claim(job_id, now_text=now_text, manual=False)
```

Add:

```python
    def plan_manual_job_claim(self, job_id: str, *, now_text: str) -> dict[str, Any]:
        return self._plan_claim(job_id, now_text=now_text, manual=True)
```

- [ ] **Step 4: Implement `claim_manual_job()`**

Add this method near `claim_due_jobs()`:

```python
    def claim_manual_job(self, job_id: str, *, now_text: str) -> dict[str, Any]:
        now_dt = _parse_time(now_text)
        if now_dt is None:
            raise ValueError(f"Invalid now_text: {now_text}")
        plan = self.plan_manual_job_claim(job_id, now_text=now_text)
        if plan["decision"] in {"disabled", "completed"}:
            raise ValueError(f"Cannot run cron job {job_id}: {plan['decision']}")
        job = dict(plan["job"])
        job["concurrency_key"] = plan["concurrency_key"]
        job["concurrency_policy"] = plan["concurrency_policy"]
        original_next_run_at = job.get("next_run_at")
        manual_scheduled_for = now_text

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._row_to_job(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
            current["concurrency_key"] = job["concurrency_key"]
            current["concurrency_policy"] = job["concurrency_policy"]
            current["next_run_at"] = manual_scheduled_for
            active_runs = self._active_runs_for_key(conn, job["concurrency_key"])
            occupying = [run for run in active_runs if run["status"] in self.OCCUPYING_RUN_STATUSES]
            queued = [run for run in active_runs if run["status"] == "queued"]
            policy = job["concurrency_policy"]
            run_id = uuid.uuid4().hex
            lease_expires_at = (now_dt + timedelta(seconds=self.lease_seconds)).isoformat()

            if policy == "queue_one" and queued:
                skipped = self._insert_run(
                    conn,
                    run_id=run_id,
                    job=current,
                    status="skipped",
                    now_text=now_text,
                    lease_expires_at=None,
                    exit_reason="manual_queue_one_already_queued",
                    trigger_type="manual",
                    preserve_schedule=True,
                )
                skipped["job"] = job
                return {"job": job, "run": skipped}
            if policy in {"queue_one", "queue_all"} and occupying:
                queued_run = self._insert_run(
                    conn,
                    run_id=run_id,
                    job=current,
                    status="queued",
                    now_text=now_text,
                    lease_expires_at=None,
                    trigger_type="manual",
                    preserve_schedule=True,
                )
                return {"job": job, "run": queued_run}
            if policy == "skip_if_running" and active_runs:
                skipped = self._insert_run(
                    conn,
                    run_id=run_id,
                    job=current,
                    status="skipped",
                    now_text=now_text,
                    lease_expires_at=None,
                    exit_reason="manual_concurrency_skip",
                    trigger_type="manual",
                    preserve_schedule=True,
                )
                return {"job": job, "run": skipped}
            if policy == "replace_running" and active_runs:
                for active in active_runs:
                    conn.execute(
                        """
                        UPDATE runs
                        SET status = 'abandoned',
                            finished_at = ?,
                            exit_reason = 'replaced_by_manual_run',
                            replaced_by_run_id = ?,
                            updated_at = ?
                        WHERE id = ? AND status IN ('queued', 'claimed', 'running')
                        """,
                        (now_text, run_id, now_text, active["id"]),
                    )
                    conn.execute(
                        """
                        UPDATE jobs
                        SET state = 'scheduled',
                            lease_run_id = NULL,
                            lease_expires_at = NULL,
                            updated_at = ?
                        WHERE id = ? AND lease_run_id = ?
                        """,
                        (now_text, active["job_id"], active["id"]),
                    )

            claimed = self._insert_run(
                conn,
                run_id=run_id,
                job=current,
                status="claimed",
                now_text=now_text,
                lease_expires_at=lease_expires_at,
                trigger_type="manual",
                preserve_schedule=True,
            )
            conn.execute(
                """
                UPDATE jobs
                SET state = 'running',
                    next_run_at = ?,
                    lease_run_id = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ? AND state = 'scheduled'
                """,
                (original_next_run_at, run_id, lease_expires_at, now_text, job_id),
            )
            claimed_job = self._row_to_job(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
            claimed_job["concurrency_key"] = job["concurrency_key"]
            claimed_job["concurrency_policy"] = job["concurrency_policy"]
            return {"job": claimed_job, "run": claimed}
```

During implementation, keep the method atomic and adjust only for actual existing helper signatures. The method must return the inserted run for skipped/queued/claimed outcomes.

- [ ] **Step 5: Run manual claim tests and state store regression**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py::test_plan_manual_job_claim_ignores_due_time tests/test_cron_state_store.py::test_claim_manual_job_creates_claimed_run_without_advancing_job tests/test_cron_state_store.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cron/state_store.py tests/test_cron_state_store.py
git commit -m "feat: claim manual cron runs"
```

---

### Task 3: Execute Manual Claims Through Scheduler Without Advancing Schedule

**Files:**
- Modify: `cron/state_store.py`
- Modify: `cron/scheduler.py`
- Test: `tests/test_cron_e2e.py`

- [ ] **Step 1: Write failing scheduler E2E test**

Append this test to `tests/test_cron_e2e.py`, reusing existing local test helpers in that file when available:

```python
def test_manual_run_tick_saves_output_delivery_and_preserves_next_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.runner import JobRunResult
    from cron.scheduler import tick
    from cron.state_store import StateStore

    store = StateStore()
    job = {
        "id": "manual-e2e",
        "name": "Manual E2E",
        "prompt": "Manual run test",
        "schedule": {"kind": "interval", "every": 300},
        "schedule_display": "every 5m",
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-06-02T19:00:00+08:00",
        "repeat": {"times": None, "completed": 0},
        "deliver": ["local"],
        "delivery_targets": [{"target": "local", "target_type": "local", "adapter_key": "local", "address": None}],
        "origin": None,
        "workdir": None,
        "script": None,
        "context_from": [],
        "skills": [],
        "enabled_toolsets": [],
        "model": None,
        "provider": None,
        "base_url": None,
        "concurrency_key": "manual-e2e",
        "concurrency_policy": "skip_if_running",
        "created_at": "2026-06-02T18:00:00+08:00",
        "updated_at": "2026-06-02T18:00:00+08:00",
    }
    store.upsert_job(job)
    claimed = store.claim_manual_job("manual-e2e", now_text="2026-06-02T18:10:00+08:00")

    def runner(job):
        return JobRunResult(
            success=True,
            output_doc="manual output",
            final_response="manual response",
            error=None,
            exit_reason=None,
        )

    result = tick(now_text="2026-06-02T18:10:00+08:00", job_runner=runner)
    run = store.get_run(claimed["run"]["id"])
    updated_job = store.get_job("manual-e2e")

    assert result.ran == 1
    assert run["status"] == "succeeded"
    assert run["trigger_type"] == "manual"
    assert run["output_path"]
    assert updated_job["next_run_at"] == "2026-06-02T19:00:00+08:00"
    assert updated_job["repeat"]["completed"] == 0
```

- [ ] **Step 2: Run scheduler E2E test and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py::test_manual_run_tick_saves_output_delivery_and_preserves_next_run -q
```

Expected: FAIL because tick does not claim ready manual runs and completion advances repeat/schedule.

- [ ] **Step 3: Add ready manual claim listing**

In `cron/state_store.py`, add:

```python
    def claim_ready_manual_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        claim_limit = max(0, int(limit))
        if claim_limit == 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT runs.*, jobs.id AS job_id
                FROM runs
                JOIN jobs ON jobs.id = runs.job_id
                WHERE runs.trigger_type = 'manual'
                  AND runs.status = 'claimed'
                  AND jobs.state = 'running'
                  AND jobs.lease_run_id = runs.id
                ORDER BY runs.claimed_at ASC, runs.id ASC
                LIMIT ?
                """,
                (claim_limit,),
            ).fetchall()
            claimed = []
            for row in rows:
                run = self._row_to_run(row)
                job = self.get_job(str(run["job_id"]))
                if job is not None:
                    claimed.append({"job": job, "run": run})
            return claimed
```

If the SQL row contains duplicate `job_id`, keep using `run["job_id"]` from the runs table.

- [ ] **Step 4: Preserve schedule in `complete_run()`**

Change `complete_run()` signature in `cron/state_store.py` to:

```python
    def complete_run(
        self,
        run_id: str,
        *,
        success: bool,
        output_path: str | None,
        final_response: str | None,
        error: str | None,
        next_run_at: str | None,
        completed: bool,
        delivery_error: str | None = None,
        run_status: str | None = None,
        exit_reason: str | None = None,
        advance_schedule: bool = True,
    ) -> dict[str, Any]:
```

Inside the branch that currently loads and increments `repeat`, add:

```python
                    job_row = conn.execute(
                        "SELECT repeat_json, next_run_at, enabled FROM jobs WHERE id = ? AND lease_run_id = ? AND state = 'running'",
                        (job_id, run_id),
                    ).fetchone()
                    repeat = _json_loads(job_row["repeat_json"], {"times": None, "completed": 0}) if job_row else {"times": None, "completed": 0}
                    if advance_schedule:
                        repeat["completed"] = int(repeat.get("completed") or 0) + 1
                        next_job_state = job_state
                        next_enabled = 0 if completed else 1
                        next_job_next_run_at = next_run_at
                    else:
                        next_job_state = "scheduled"
                        next_enabled = int(job_row["enabled"]) if job_row else 1
                        next_job_next_run_at = job_row["next_run_at"] if job_row else next_run_at
```

Then use `next_job_state`, `next_enabled`, and `next_job_next_run_at` in the jobs update parameters instead of `job_state`, `0 if completed else 1`, and `next_run_at`.

- [ ] **Step 5: Pass manual preserve flag from scheduler**

In `cron/scheduler.py`, inside `_process_claimed()`, after `run = dict(claimed["run"])`, add:

```python
    preserve_schedule = bool(run.get("preserve_schedule"))
```

For every `store.complete_run()` call in `_process_claimed()` and `_complete_stale_run()` where the run is available, pass:

```python
            advance_schedule=not preserve_schedule,
```

Do not change `_complete_stale_run()` unless the stale run row contains `preserve_schedule`; if it does, pass `advance_schedule=not bool(stale.get("preserve_schedule"))`.

In `tick()`, replace:

```python
        claimed = store.promote_queued_runs(now_text=run_at.isoformat(), limit=100)
        claimed = claimed + store.claim_due_jobs(now_text=run_at.isoformat(), limit=max(0, 100 - len(claimed)))
```

with:

```python
        claimed = store.claim_ready_manual_runs(limit=100)
        claimed = claimed + store.promote_queued_runs(now_text=run_at.isoformat(), limit=max(0, 100 - len(claimed)))
        claimed = claimed + store.claim_due_jobs(now_text=run_at.isoformat(), limit=max(0, 100 - len(claimed)))
```

- [ ] **Step 6: Run scheduler E2E and cron E2E suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py::test_manual_run_tick_saves_output_delivery_and_preserves_next_run tests/test_cron_e2e.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cron/state_store.py cron/scheduler.py tests/test_cron_e2e.py
git commit -m "feat: execute manual cron runs via scheduler"
```

---

### Task 4: Route `agent cron run` Through Manual Claim And Tick

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing CLI tests**

Append these tests to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_run_uses_scheduler_path_and_records_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.jobs import create_job
    from cron.runner import JobRunResult
    import cron.runner_client as runner_client

    job = create_job("every 5m", "manual cli output", name="Manual CLI", deliver=["local"])

    def fake_run_job(job_data):
        return JobRunResult(
            success=True,
            output_doc="manual cli output",
            final_response="manual cli response",
            error=None,
            exit_reason=None,
        )

    monkeypatch.setattr(runner_client, "run_job", fake_run_job)

    result = _run_cli(["cron", "run", job["id"]])

    assert result.exit_code == 0
    assert "Run:" in result.text
    assert "Status: succeeded" in result.text or "Status: ok" in result.text
    assert "Output:" in result.text
    runs = _run_cli(["cron", "runs", job["id"]])
    assert "status=succeeded" in runs.text


def test_cron_run_dry_run_reports_manual_schedule_preservation(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.jobs import create_job

    job = create_job("every 5m", "manual dry", name="Manual Dry", deliver=["local"])

    result = _run_cli(["cron", "run", job["id"], "--dry-run"])

    assert result.exit_code == 0
    assert "Manual run: yes" in result.text
    assert "Preserves periodic schedule: yes" in result.text
    assert "Decision: would_claim" in result.text
```

- [ ] **Step 2: Run CLI tests and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_run_uses_scheduler_path_and_records_run tests/test_agent_cli_cron_commands.py::test_cron_run_dry_run_reports_manual_schedule_preservation -q
```

Expected: FAIL because `cron run` still calls `simple_job_action("run")` and dry-run uses due-only planning.

- [ ] **Step 3: Update dry-run branch**

In `agent_cli/cron_commands.py`, in `run_cron_job()`, replace:

```python
        plan = store.plan_job_claim(job_id, now_text=now_text or utc_now().isoformat())
```

with:

```python
        plan = store.plan_manual_job_claim(job_id, now_text=now_text or utc_now().isoformat())
```

Add these lines to the dry-run output list:

```python
        "Manual run: yes",
        f"Preserves periodic schedule: {'yes' if plan.get('preserve_schedule') else 'no'}",
```

Place them after `Decision`.

- [ ] **Step 4: Implement non-dry manual run command**

In `agent_cli/cron_commands.py`, replace:

```python
    if not dry_run:
        return simple_job_action("run", job_id=job_id)
```

with:

```python
    if not dry_run:
        from cron.scheduler import tick
        from cron.state_store import StateStore, utc_now

        store = StateStore()
        run_at = now_text or utc_now().isoformat()
        try:
            claimed = store.claim_manual_job(job_id, now_text=run_at)
        except KeyError:
            return CronCommandResult(f"Cron job not found: {job_id}.", exit_code=2)
        except ValueError as exc:
            return CronCommandResult(str(exc), exit_code=2)

        run_id = str(claimed["run"]["id"])
        tick_result = tick(now_text=run_at)
        run = store.get_run(run_id) or claimed["run"]
        output_path = run.get("output_path") or "-"
        status = str(run.get("status") or "-")
        lines = [
            f"Ran cron job {job_id}.",
            f"Run: {run_id}",
            f"Status: {status}",
            f"Output: {output_path}",
            _delivery_tick_line(tick_result.delivery) or "Delivery: -",
        ]
        if run.get("error"):
            lines.append(str(run["error"]).splitlines()[0])
        if status in {"failed", "abandoned"}:
            lines.append(f"Inspect delivery: python -m agent_cli cron deliveries {run_id}")
        return CronCommandResult("\n".join(lines), exit_code=0 if status in {"succeeded", "skipped", "queued"} else 1)
```

If `_delivery_tick_line()` returns a line starting with `Delivery tick:`, keep it for consistency with `cron tick`.

- [ ] **Step 5: Run CLI tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_run_uses_scheduler_path_and_records_run tests/test_agent_cli_cron_commands.py::test_cron_run_dry_run_reports_manual_schedule_preservation tests/test_agent_cli_cron_commands.py::test_cron_run_dry_run_does_not_create_run tests/test_agent_cli_cron_commands.py::test_cron_run_dry_run_missing_job_returns_not_found -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: route cron run through scheduler"
```

---

### Task 5: Cover Manual Run Concurrency Policies

**Files:**
- Modify: `tests/test_cron_e2e.py`
- Modify: `cron/state_store.py`
- Modify: `cron/scheduler.py`

- [ ] **Step 1: Write failing concurrency tests**

Append parametrized tests to `tests/test_cron_e2e.py`:

```python
import pytest


@pytest.mark.parametrize(
    ("policy", "expected_status", "expected_exit"),
    [
        ("skip_if_running", "skipped", "manual_concurrency_skip"),
        ("queue_one", "queued", None),
        ("queue_all", "queued", None),
    ],
)
def test_manual_run_respects_non_replace_concurrency(monkeypatch, tmp_path, policy, expected_status, expected_exit):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.state_store import StateStore

    store = StateStore()
    job = {
        "id": f"manual-{policy}",
        "name": "Manual concurrency",
        "prompt": "run",
        "schedule": {"kind": "interval", "every": 300},
        "schedule_display": "every 5m",
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-06-02T19:00:00+08:00",
        "repeat": {"times": None, "completed": 0},
        "deliver": ["local"],
        "delivery_targets": [],
        "origin": None,
        "workdir": None,
        "script": None,
        "context_from": [],
        "skills": [],
        "enabled_toolsets": [],
        "model": None,
        "provider": None,
        "base_url": None,
        "concurrency_key": "manual-key",
        "concurrency_policy": policy,
        "created_at": "2026-06-02T18:00:00+08:00",
        "updated_at": "2026-06-02T18:00:00+08:00",
    }
    store.upsert_job(job)
    first = store.claim_manual_job(job["id"], now_text="2026-06-02T18:00:00+08:00")
    second = store.claim_manual_job(job["id"], now_text="2026-06-02T18:01:00+08:00")

    assert first["run"]["status"] == "claimed"
    assert second["run"]["status"] == expected_status
    assert second["run"]["exit_reason"] == expected_exit
    assert second["run"]["trigger_type"] == "manual"
    assert second["run"]["preserve_schedule"] == 1


def test_manual_run_replace_running_abandons_active_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.state_store import StateStore

    store = StateStore()
    job = {
        "id": "manual-replace",
        "name": "Manual replace",
        "prompt": "run",
        "schedule": {"kind": "interval", "every": 300},
        "schedule_display": "every 5m",
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-06-02T19:00:00+08:00",
        "repeat": {"times": None, "completed": 0},
        "deliver": ["local"],
        "delivery_targets": [],
        "origin": None,
        "workdir": None,
        "script": None,
        "context_from": [],
        "skills": [],
        "enabled_toolsets": [],
        "model": None,
        "provider": None,
        "base_url": None,
        "concurrency_key": "manual-replace-key",
        "concurrency_policy": "replace_running",
        "created_at": "2026-06-02T18:00:00+08:00",
        "updated_at": "2026-06-02T18:00:00+08:00",
    }
    store.upsert_job(job)
    first = store.claim_manual_job("manual-replace", now_text="2026-06-02T18:00:00+08:00")
    second = store.claim_manual_job("manual-replace", now_text="2026-06-02T18:01:00+08:00")

    assert store.get_run(first["run"]["id"])["status"] == "abandoned"
    assert store.get_run(first["run"]["id"])["exit_reason"] == "replaced_by_manual_run"
    assert second["run"]["status"] == "claimed"
```

- [ ] **Step 2: Run concurrency tests and verify current behavior**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py::test_manual_run_respects_non_replace_concurrency tests/test_cron_e2e.py::test_manual_run_replace_running_abandons_active_run -q
```

Expected: PASS if Task 2 already handled policies correctly. If any case fails, fix only `claim_manual_job()` policy branches.

- [ ] **Step 3: Add queued manual promotion preservation test**

Append:

```python
def test_queued_manual_run_preserves_schedule_after_promotion(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.runner import JobRunResult
    from cron.scheduler import tick
    from cron.state_store import StateStore

    store = StateStore()
    job = {
        "id": "manual-queue-promotion",
        "name": "Manual queue promotion",
        "prompt": "run",
        "schedule": {"kind": "interval", "every": 300},
        "schedule_display": "every 5m",
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-06-02T19:00:00+08:00",
        "repeat": {"times": None, "completed": 0},
        "deliver": ["local"],
        "delivery_targets": [],
        "origin": None,
        "workdir": None,
        "script": None,
        "context_from": [],
        "skills": [],
        "enabled_toolsets": [],
        "model": None,
        "provider": None,
        "base_url": None,
        "concurrency_key": "manual-queue-key",
        "concurrency_policy": "queue_all",
        "created_at": "2026-06-02T18:00:00+08:00",
        "updated_at": "2026-06-02T18:00:00+08:00",
    }
    store.upsert_job(job)
    first = store.claim_manual_job("manual-queue-promotion", now_text="2026-06-02T18:00:00+08:00")
    second = store.claim_manual_job("manual-queue-promotion", now_text="2026-06-02T18:01:00+08:00")
    store.complete_run(
        first["run"]["id"],
        success=True,
        output_path=None,
        final_response="done",
        error=None,
        next_run_at="2026-06-02T19:00:00+08:00",
        completed=False,
        advance_schedule=False,
    )

    def runner(job):
        return JobRunResult(True, "queued output", "queued response", None, None)

    tick(now_text="2026-06-02T18:02:00+08:00", job_runner=runner)

    assert store.get_run(second["run"]["id"])["status"] == "succeeded"
    assert store.get_job("manual-queue-promotion")["next_run_at"] == "2026-06-02T19:00:00+08:00"
```

- [ ] **Step 4: Run queued promotion test and fix promotion metadata if needed**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py::test_queued_manual_run_preserves_schedule_after_promotion -q
```

Expected: PASS. If FAIL because `promote_queued_runs()` loses manual metadata, modify `promote_queued_runs()` to return the run row including `trigger_type` and `preserve_schedule`; do not create transient defaults.

- [ ] **Step 5: Commit**

```bash
git add cron/state_store.py cron/scheduler.py tests/test_cron_e2e.py
git commit -m "test: cover manual cron concurrency"
```

---

### Task 6: Add Docker Diagnostics Helper And Doctor Checks

**Files:**
- Create: `cron/docker_diagnostics.py`
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing Docker doctor tests**

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_doctor_warns_when_prod_expects_docker_but_command_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")
    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    import cron.docker_diagnostics as docker_diagnostics

    monkeypatch.setattr(
        docker_diagnostics,
        "inspect_docker_runtime",
        lambda profile: docker_diagnostics.DockerRuntimeDiagnostic(
            expected=True,
            env_type="docker",
            command_path=None,
            version_ok=False,
            error="docker command not found",
            suggestion="Install Docker or set TERMINAL_ENV=local for development.",
        ),
    )

    result = _run_cli(["cron", "doctor"])

    assert result.exit_code == 1
    assert "docker runtime: docker command not found" in result.text
    assert "Install Docker or set TERMINAL_ENV=local for development." in result.text


def test_cron_doctor_warns_when_docker_version_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")

    import cron.docker_diagnostics as docker_diagnostics

    monkeypatch.setattr(
        docker_diagnostics,
        "inspect_docker_runtime",
        lambda profile: docker_diagnostics.DockerRuntimeDiagnostic(
            expected=True,
            env_type="docker",
            command_path="/usr/bin/docker",
            version_ok=False,
            error="Docker command is available but 'docker version' failed.",
            suggestion="Start Docker or fix permission to access the Docker daemon.",
        ),
    )

    result = _run_cli(["cron", "doctor"])

    assert result.exit_code == 1
    assert "docker runtime: Docker command is available but 'docker version' failed." in result.text
    assert "Start Docker or fix permission to access the Docker daemon." in result.text
```

- [ ] **Step 2: Run Docker doctor tests and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_when_prod_expects_docker_but_command_missing tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_when_docker_version_fails -q
```

Expected: FAIL because `cron.docker_diagnostics` does not exist.

- [ ] **Step 3: Create Docker diagnostics module**

Create `cron/docker_diagnostics.py`:

```python
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class DockerRuntimeDiagnostic:
    expected: bool
    env_type: str
    command_path: str | None
    version_ok: bool
    error: str | None = None
    suggestion: str | None = None


def _resolve_terminal_env(profile: str) -> str:
    try:
        from agent_core.permissions.profiles import default_terminal_env

        return default_terminal_env(profile)
    except Exception:
        import os

        return os.getenv("TERMINAL_ENV") or ("docker" if profile in {"prod", "hosted"} else "local")


def inspect_docker_runtime(profile: str) -> DockerRuntimeDiagnostic:
    env_type = _resolve_terminal_env(profile)
    expected = env_type == "docker"
    if not expected:
        return DockerRuntimeDiagnostic(expected=False, env_type=env_type, command_path=None, version_ok=True)

    command_path = shutil.which("docker")
    if command_path is None:
        return DockerRuntimeDiagnostic(
            expected=True,
            env_type=env_type,
            command_path=None,
            version_ok=False,
            error="docker command not found",
            suggestion="Install Docker or set TERMINAL_ENV=local for development.",
        )

    try:
        completed = subprocess.run(
            [command_path, "version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return DockerRuntimeDiagnostic(
            expected=True,
            env_type=env_type,
            command_path=command_path,
            version_ok=False,
            error=f"docker version failed: {exc}",
            suggestion="Start Docker or fix permission to access the Docker daemon.",
        )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "docker version failed").strip().splitlines()[0]
        return DockerRuntimeDiagnostic(
            expected=True,
            env_type=env_type,
            command_path=command_path,
            version_ok=False,
            error=f"Docker command is available but 'docker version' failed: {message}",
            suggestion="Start Docker or fix permission to access the Docker daemon.",
        )
    return DockerRuntimeDiagnostic(expected=True, env_type=env_type, command_path=command_path, version_ok=True)
```

- [ ] **Step 4: Wire Docker diagnostics into doctor**

In `agent_cli/cron_commands.py`, inside `cron_doctor()` after profile is resolved and before runner mode checks, add:

```python
    from cron.docker_diagnostics import inspect_docker_runtime

    docker_diag = inspect_docker_runtime(profile)
    if docker_diag.expected and docker_diag.version_ok:
        add("ok", f"docker runtime: ready ({docker_diag.command_path})")
    elif docker_diag.expected:
        message = f"docker runtime: {docker_diag.error or 'not ready'}"
        if docker_diag.suggestion:
            message = f"{message}. {docker_diag.suggestion}"
        add("warn", message)
    else:
        add("ok", f"docker runtime: not required for TERMINAL_ENV={docker_diag.env_type}")
```

- [ ] **Step 5: Run Docker doctor tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_when_prod_expects_docker_but_command_missing tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_when_docker_version_fails -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cron/docker_diagnostics.py agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: diagnose cron docker runtime"
```

---

### Task 7: Make Doctor Feishu And Service Env Messages More Actionable

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing actionable Feishu message test**

Append:

```python
def test_cron_doctor_feishu_service_env_includes_restart_command(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "shell-app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "shell-secret")
    from cron.jobs import create_job

    create_job("every 5m", "send", name="Feishu active", deliver=["feishu:oc_1234567890abcdef"])

    result = _run_cli(["cron", "doctor"])

    assert result.exit_code == 1
    assert "agent cron service env set FEISHU_APP_ID APP_ID_VALUE" in result.text
    assert "agent cron service env set FEISHU_APP_SECRET APP_SECRET_VALUE" in result.text
    assert "agent cron service restart" in result.text
    assert "background service delivery uses service.env" in result.text
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_feishu_service_env_includes_restart_command -q
```

Expected: FAIL if current text lacks restart or exact actionable commands.

- [ ] **Step 3: Update Feishu service env warning text**

In `agent_cli/cron_commands.py`, replace the current active Feishu missing service env warning text with:

```python
                "active Feishu cron jobs require service env; "
                f"service env missing {', '.join(missing_service_env)}. "
                "Set with `agent cron service env set FEISHU_APP_ID APP_ID_VALUE` and "
                "`agent cron service env set FEISHU_APP_SECRET APP_SECRET_VALUE`, then run "
                "`agent cron service restart`."
```

Keep the existing shell-vs-service warning, and ensure it still says:

```text
background service delivery uses service.env
```

- [ ] **Step 4: Run Feishu doctor tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_feishu_service_env_includes_restart_command tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_active_feishu_job_missing_service_env -q
```

If the second test name differs, run:

```bash
rg -n "service env missing FEISHU" tests/test_agent_cli_cron_commands.py
```

then rerun the matching test by exact name.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "fix: clarify feishu service env diagnostics"
```

---

### Task 8: Add Next Due And Missed-Run Doctor Diagnostics

**Files:**
- Modify: `cron/state_store.py`
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing next due and missed-run tests**

Append:

```python
def test_cron_doctor_warns_overdue_next_due_with_stale_service(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.jobs import create_job
    import agent_cli.cron_commands as cron_commands

    job = create_job("every 5m", "overdue", name="Overdue", deliver=["local"])
    from cron.state_store import StateStore

    store = StateStore()
    saved = store.get_job(job["id"])
    saved["next_run_at"] = "2026-06-02T18:00:00+08:00"
    store.upsert_job(saved)

    monkeypatch.setattr(
        cron_commands,
        "_service_status_lines",
        lambda: ["Heartbeat: stale-or-missing"],
    )

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert "next due job is overdue" in result.text
    assert "agent cron service start" in result.text
    assert "agent cron service restart" in result.text


def test_cron_doctor_warns_recent_missed_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job("every 5m", "missed", name="Missed", deliver=["local"])
    store = StateStore()
    saved = store.get_job(job["id"])
    saved["next_run_at"] = "2026-06-02T18:00:00+08:00"
    store.upsert_job(saved)
    store.claim_due_jobs(now_text="2026-06-02T19:00:00+08:00", limit=1)

    result = _run_cli(["cron", "doctor"])

    assert result.exit_code == 1
    assert "recent missed cron runs" in result.text
    assert f"agent cron runs {job['id']}" in result.text
```

- [ ] **Step 2: Run next due/missed tests and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_overdue_next_due_with_stale_service tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_recent_missed_runs -q
```

Expected: FAIL because doctor does not emit these actionable lines.

- [ ] **Step 3: Add missed-run summary helper**

In `cron/state_store.py`, add:

```python
    def recent_missed_runs(self, *, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT runs.*, jobs.name AS job_name
                FROM runs
                JOIN jobs ON jobs.id = runs.job_id
                WHERE runs.status = 'skipped'
                  AND runs.exit_reason = 'missed_run'
                ORDER BY COALESCE(runs.finished_at, runs.updated_at, runs.created_at) DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]
```

- [ ] **Step 4: Add doctor next due/missed diagnostics**

In `agent_cli/cron_commands.py`, inside `cron_doctor()` after `state_store = StateStore()` and job counts are loaded, add:

```python
        summary = state_store.cron_status_summary(top_n=5)
        next_due = summary.get("next_due")
        if next_due:
            add("ok", f"next due job: {next_due.get('id')} at {next_due.get('next_run_at')}")
            from cron.state_store import _parse_time, utc_now

            due_dt = _parse_time(next_due.get("next_run_at"))
            now_dt = utc_now()
            if due_dt is not None and due_dt < now_dt:
                add(
                    "warn",
                    "next due job is overdue; if service heartbeat is stale, run "
                    "`agent cron service start` or `agent cron service restart`. "
                    "Inspect with `agent cron service status` and `agent cron status`.",
                )
        else:
            add("ok", "next due job: -")
        missed = state_store.recent_missed_runs(limit=3)
        if missed:
            first = missed[0]
            add(
                "warn",
                f"recent missed cron runs: {len(missed)}; scheduled window was missed. "
                f"Inspect with `agent cron runs {first['job_id']}`.",
            )
```

This check intentionally does not parse service status internals. It gives the same executable commands whenever overdue jobs are present.

- [ ] **Step 5: Run doctor tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_overdue_next_due_with_stale_service tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_recent_missed_runs -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cron/state_store.py agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: diagnose cron schedule progress"
```

---

### Task 9: Final Regression And Cleanup

**Files:**
- Modify only files touched by prior tasks if verification exposes issues.

- [ ] **Step 1: Run focused cron regression suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py tests/test_cron_e2e.py tests/test_agent_cli_cron_commands.py tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_service.py tests/test_cron_service_env.py tests/test_cron_service_context.py tests/test_cron_service_platforms.py -q
```

Expected: PASS.

- [ ] **Step 2: Run format check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 3: Inspect git status**

Run:

```bash
git status --short --branch
```

Expected: only intentional tracked changes are present. The pre-existing untracked file `docs/superpowers/plans/2026-06-01-cron-concurrency-cli-management.md` may still appear and should not be added by this work.

- [ ] **Step 4: Commit any verification-only fixes**

If Step 1 or Step 2 required fixes, commit them:

```bash
git add cron/state_store.py cron/scheduler.py cron/docker_diagnostics.py agent_cli/cron_commands.py tests/test_cron_state_store.py tests/test_cron_e2e.py tests/test_agent_cli_cron_commands.py
git commit -m "fix: stabilize cron manual run diagnostics"
```

If no fixes were required, do not create an empty commit.


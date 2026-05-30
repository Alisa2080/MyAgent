# Cron Run State Machine Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Formalize the existing SQLite cron `jobs` / `runs` / `delivery_events` flow so every automatic execution has a durable run record, safe completion semantics, recovery after crashes, and delivery events linked to runs.

**Architecture:** Keep `cron.state_store.StateStore` as the persistence boundary and `cron.scheduler.tick()` as the orchestration boundary. Add focused tests first, then tighten existing methods instead of introducing a second store or rewriting public cron APIs.

**Tech Stack:** Python 3.11+, SQLite via `sqlite3`, pytest, existing cron modules under `cron/`, test runner `/home/miku/miniforge3/envs/langchain/bin/python -m pytest`.

---

## File Map

- Modify `cron/state_store.py`: enforce claim, complete, delivery aggregation, and recovery invariants.
- Modify `cron/scheduler.py`: keep automatic scheduling on `claim_due_jobs()` / `complete_run()` and avoid pre-advance paths.
- Modify `cron/jobs.py`: mark legacy helpers clearly if they remain reachable outside automatic scheduling.
- Modify `cron/delivery.py`: ensure every cron output event receives `run_id` when job has it.
- Modify `cron/delivery_dispatcher.py`: ensure event status changes sync run delivery status and job delivery error.
- Modify `tests/test_cron_state_store.py`: add state machine unit tests.
- Modify `tests/test_cron_scheduler.py`: add scheduler integration regression tests.
- Modify `tests/test_cron_delivery_dispatcher.py`: add delivery/run linkage tests.
- Modify `tests/test_cron_jobs.py`: add compatibility or legacy-helper tests if public wrappers need documentation.

## Task 1: Lock Claim-Time Invariants

**Files:**
- Modify: `tests/test_cron_state_store.py`
- Modify: `cron/state_store.py`

- [ ] **Step 1: Add failing tests for claim behavior**

Append these tests to `tests/test_cron_state_store.py`:

```python
from datetime import datetime, timezone


def test_claim_due_jobs_creates_run_without_advancing_next_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    due_at = "2026-05-30T10:00:00+00:00"
    update_job(job["id"], {"next_run_at": due_at})

    store = StateStore()
    claimed = store.claim_due_jobs(now_text=due_at, limit=1)

    assert len(claimed) == 1
    claimed_job = claimed[0]["job"]
    run = claimed[0]["run"]
    assert run["job_id"] == job["id"]
    assert run["scheduled_for"] == due_at
    assert run["status"] == "claimed"
    assert claimed_job["state"] == "running"
    assert claimed_job["lease_run_id"] == run["id"]
    assert claimed_job["next_run_at"] == due_at
    assert store.get_job(job["id"])["next_run_at"] == due_at


def test_second_claim_does_not_duplicate_running_job(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    due_at = "2026-05-30T10:00:00+00:00"
    update_job(job["id"], {"next_run_at": due_at})

    store = StateStore()
    first = store.claim_due_jobs(now_text=due_at, limit=1)
    second = store.claim_due_jobs(now_text=due_at, limit=1)

    assert len(first) == 1
    assert second == []
    assert len(store.runs_for_job(job["id"])) == 1
```

- [ ] **Step 2: Run the focused tests and confirm the result**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py::test_claim_due_jobs_creates_run_without_advancing_next_run tests/test_cron_state_store.py::test_second_claim_does_not_duplicate_running_job -q
```

Expected before implementation: at least one failure if current claim behavior violates the invariant, otherwise both pass and this task becomes a regression lock.

- [ ] **Step 3: Tighten `StateStore.claim_due_jobs()` only if tests fail**

In `cron/state_store.py`, make `claim_due_jobs()` preserve this shape:

```python
conn.execute(
    """
    INSERT INTO runs (
        id, job_id, scheduled_for, claimed_at, lease_expires_at,
        started_at, finished_at, attempt, status, exit_reason,
        output_path, final_response, error, delivery_status,
        created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'claimed', NULL, NULL, NULL, NULL, NULL, ?, ?)
    """,
    (
        run_id,
        job["id"],
        job["next_run_at"],
        now_actual,
        lease_expires,
        int(previous_attempts) + 1,
        now_actual,
        now_actual,
    ),
)
conn.execute(
    """
    UPDATE jobs
    SET state = 'running',
        lease_run_id = ?,
        lease_expires_at = ?,
        updated_at = ?
    WHERE id = ? AND state = 'scheduled'
    """,
    (run_id, lease_expires, now_actual, job["id"]),
)
```

Do not call `compute_next_run()` or update `next_run_at` in the claim path.

- [ ] **Step 4: Re-run the focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py::test_claim_due_jobs_creates_run_without_advancing_next_run tests/test_cron_state_store.py::test_second_claim_does_not_duplicate_running_job -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cron_state_store.py cron/state_store.py
git commit -m "test: lock cron claim run invariants"
```

## Task 2: Harden Completion Idempotency

**Files:**
- Modify: `tests/test_cron_state_store.py`
- Modify: `cron/state_store.py`

- [ ] **Step 1: Add failing tests for complete-run idempotency**

Append this test to `tests/test_cron_state_store.py`:

```python
def test_complete_run_cannot_advance_or_count_twice(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(
        prompt="write report",
        schedule="30m",
        deliver="local",
        repeat={"times": 2, "completed": 0},
    )
    due_at = "2026-05-30T10:00:00+00:00"
    update_job(job["id"], {"next_run_at": due_at})

    store = StateStore()
    claimed = store.claim_due_jobs(now_text=due_at, limit=1)[0]
    run_id = claimed["run"]["id"]
    store.mark_run_started(run_id)

    first = store.complete_run(
        run_id,
        success=True,
        output_path="/tmp/out.md",
        final_response="done",
        error=None,
        next_run_at="2026-05-30T10:30:00+00:00",
        completed=False,
    )
    second = store.complete_run(
        run_id,
        success=True,
        output_path="/tmp/out.md",
        final_response="done again",
        error=None,
        next_run_at="2026-05-30T11:00:00+00:00",
        completed=True,
    )

    first_job = first["job"]
    second_job = second["job"]
    assert first_job["repeat"]["completed"] == 1
    assert second_job["repeat"]["completed"] == 1
    assert second_job["next_run_at"] == "2026-05-30T10:30:00+00:00"
    assert second_job["state"] == "scheduled"
    assert second["run"]["status"] == "succeeded"
```

- [ ] **Step 2: Run the focused test and confirm the result**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py::test_complete_run_cannot_advance_or_count_twice -q
```

Expected before implementation: failure if `complete_run()` double-counts or converts a completed run into `abandoned`.

- [ ] **Step 3: Make `complete_run()` idempotent for terminal runs**

At the start of `StateStore.complete_run()` in `cron/state_store.py`, after loading the run, return the existing state when the run is already terminal:

```python
run = self.get_run(run_id)
if run.get("status") in {"succeeded", "failed", "skipped", "abandoned"}:
    return {"run": run, "job": self.get_job(str(run["job_id"]))}
```

Keep the existing late-completion check for non-terminal runs.

- [ ] **Step 4: Re-run the focused test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py::test_complete_run_cannot_advance_or_count_twice -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cron_state_store.py cron/state_store.py
git commit -m "fix: make cron run completion idempotent"
```

## Task 3: Recover Expired Run Leases Without Advancing Schedule

**Files:**
- Modify: `tests/test_cron_state_store.py`
- Modify: `cron/state_store.py`

- [ ] **Step 1: Add failing test for expired lease recovery**

Append this test to `tests/test_cron_state_store.py`:

```python
def test_recover_expired_run_lease_preserves_scheduled_occurrence(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    due_at = "2026-05-30T10:00:00+00:00"
    update_job(job["id"], {"next_run_at": due_at})

    store = StateStore(lease_seconds=1)
    claimed = store.claim_due_jobs(now_text=due_at, limit=1)[0]
    run_id = claimed["run"]["id"]

    recovered = store.recover_expired_leases(now_text="2026-05-30T10:10:00+00:00")
    recovered_job = store.get_job(job["id"])
    recovered_run = store.get_run(run_id)

    assert recovered == 1
    assert recovered_run["status"] == "abandoned"
    assert recovered_run["exit_reason"] == "lease_expired"
    assert recovered_job["state"] == "scheduled"
    assert recovered_job["lease_run_id"] is None
    assert recovered_job["lease_expires_at"] is None
    assert recovered_job["next_run_at"] == due_at
```

- [ ] **Step 2: Run the focused test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py::test_recover_expired_run_lease_preserves_scheduled_occurrence -q
```

Expected before implementation: failure if recovery advances `next_run_at`, leaves the job running, or does not mark the run abandoned.

- [ ] **Step 3: Adjust recovery only if the test fails**

In `StateStore.recover_expired_leases()`, preserve this update behavior:

```python
conn.execute(
    "UPDATE runs SET status = 'abandoned', finished_at = ?, exit_reason = 'lease_expired', updated_at = ? WHERE id = ?",
    (now_actual, now_actual, row["run_id"]),
)
conn.execute(
    "UPDATE jobs SET state = 'scheduled', lease_run_id = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
    (now_actual, row["job_id"]),
)
```

Do not update `jobs.next_run_at` in recovery.

- [ ] **Step 4: Re-run the focused test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_state_store.py::test_recover_expired_run_lease_preserves_scheduled_occurrence -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cron_state_store.py cron/state_store.py
git commit -m "test: lock cron run lease recovery"
```

## Task 4: Link Delivery Events To Runs And Aggregate Status

**Files:**
- Modify: `tests/test_cron_delivery_dispatcher.py`
- Modify: `cron/delivery.py`
- Modify: `cron/delivery_dispatcher.py`
- Modify: `cron/state_store.py`

- [ ] **Step 1: Add failing delivery linkage tests**

Append these tests to `tests/test_cron_delivery_dispatcher.py`:

```python
def test_delivery_events_from_cron_output_keep_run_id(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    update_job(job["id"], {"next_run_at": "2026-05-30T10:00:00+00:00"})
    store = StateStore()
    claimed = store.claim_due_jobs(now_text="2026-05-30T10:00:00+00:00", limit=1)[0]
    claimed_job = dict(claimed["job"])
    claimed_job["run_id"] = claimed["run"]["id"]

    event = enqueue_result(
        claimed_job,
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-30T10:00:00+00:00",
    )

    stored = store.get_delivery_event(event["id"])
    assert stored["job_id"] == job["id"]
    assert stored["run_id"] == claimed["run"]["id"]
    assert stored["status"] == "delivered"


def test_webhook_failure_updates_run_delivery_status(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="webhook:https://example.invalid/hook")
    update_job(job["id"], {"next_run_at": "2026-05-30T10:00:00+00:00"})
    store = StateStore()
    claimed = store.claim_due_jobs(now_text="2026-05-30T10:00:00+00:00", limit=1)[0]
    claimed_job = dict(claimed["job"])
    claimed_job["run_id"] = claimed["run"]["id"]
    enqueue_result(
        claimed_job,
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-30T10:00:00+00:00",
    )

    registry = default_delivery_registry(webhook_sender=lambda url, payload, timeout=10: (500, "down"))
    summary = DeliveryDispatcher(store=store, registry=registry).dispatch_due(limit=10)
    run = store.get_run(claimed["run"]["id"])
    job_after = store.get_job(job["id"])

    assert summary["failed"] == 1
    assert run["delivery_status"] == "failed"
    assert "HTTP 500" in job_after["last_delivery_error"]
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery_dispatcher.py::test_delivery_events_from_cron_output_keep_run_id tests/test_cron_delivery_dispatcher.py::test_webhook_failure_updates_run_delivery_status -q
```

Expected before implementation: failure if `run_id` is missing or dispatcher does not aggregate status.

- [ ] **Step 3: Ensure `enqueue_result()` passes `run_id`**

In `cron/delivery.py`, make the event enqueue path pass the job's run id:

```python
store.enqueue(
    job_id=job.get("id"),
    run_id=job.get("run_id"),
    job_name=job.get("name"),
    run_at=run_at_text,
    target=target.raw,
    target_type=target.target_type,
    adapter_key=target.adapter_key,
    address=target.address,
    thread_id=target.thread_id,
    origin=origin_payload,
    final_response=final_response,
    output_path=output_path,
    payload=payload,
    status=initial_status,
)
```

Use the local variable names already present in the function; preserve existing local/origin/webhook behavior.

- [ ] **Step 4: Ensure dispatcher syncs after every event update**

In `cron/delivery_dispatcher.py`, call `_sync_after_event_update(updated)` after delivered, failed, and dead updates. The helper should preserve this shape:

```python
def _sync_after_event_update(self, event: dict[str, Any]) -> None:
    run_id = event.get("run_id")
    if run_id:
        self.store.update_run_delivery_status(str(run_id))
    job_id = event.get("job_id")
    if job_id and event.get("status") in {"failed", "dead"}:
        self.store.update_job_delivery_error(str(job_id), str(event.get("last_error") or "delivery failed"))
```

If the existing helper already does this, keep it and only adjust missing branches.

- [ ] **Step 5: Re-run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery_dispatcher.py::test_delivery_events_from_cron_output_keep_run_id tests/test_cron_delivery_dispatcher.py::test_webhook_failure_updates_run_delivery_status -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit**

```bash
git add tests/test_cron_delivery_dispatcher.py cron/delivery.py cron/delivery_dispatcher.py cron/state_store.py
git commit -m "fix: link cron deliveries to runs"
```

## Task 5: Prove Scheduler Uses Claim/Complete Flow

**Files:**
- Modify: `tests/test_cron_scheduler.py`
- Modify: `cron/scheduler.py`
- Modify: `cron/jobs.py`

- [ ] **Step 1: Add scheduler regression test**

Append this test to `tests/test_cron_scheduler.py`:

```python
def test_tick_records_run_and_advances_after_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult
    from cron.jobs import create_job, update_job
    from cron.scheduler import tick
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    due_at = "2026-05-30T10:00:00+00:00"
    update_job(job["id"], {"next_run_at": due_at})

    def fake_runner(running_job):
        assert running_job["run_id"]
        assert running_job["next_run_at"] == due_at
        return JobRunResult(success=True, output_doc="# out", final_response="done")

    result = tick(now_dt="2026-05-30T10:00:00+00:00", job_runner=fake_runner)

    store = StateStore()
    runs = store.runs_for_job(job["id"])
    job_after = store.get_job(job["id"])
    assert result.ran == 1
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["scheduled_for"] == due_at
    assert job_after["state"] == "scheduled"
    assert job_after["next_run_at"] != due_at
    assert job_after["lease_run_id"] is None
```

- [ ] **Step 2: Run focused test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_scheduler.py::test_tick_records_run_and_advances_after_completion -q
```

Expected before implementation: failure if `tick()` still uses old due-job flow or does not inject `run_id`.

- [ ] **Step 3: Keep scheduler on claimed runs**

In `cron/scheduler.py`, preserve this flow in `tick()`:

```python
store = _store()
store.recover_expired_leases(now_text=run_at.isoformat())
claimed_jobs = store.claim_due_jobs(now_text=run_at.isoformat(), limit=max_jobs)
```

Then process only those claimed records:

```python
for claimed in claimed_jobs:
    result.results.append(_process_claimed(claimed, run_at, job_runner))
```

Do not call `get_due_jobs()` or `advance_next_run()` from `tick()`.

- [ ] **Step 4: Mark legacy helpers in `cron/jobs.py`**

Add docstrings to `get_due_jobs()` and `advance_next_run()` if they are still present:

```python
def get_due_jobs(now_dt: datetime | None = None) -> list[dict[str, Any]]:
    """Compatibility helper for manual inspection.

    Automatic scheduling must use StateStore.claim_due_jobs() so a run record
    exists before execution and next_run_at is not advanced at claim time.
    """
```

```python
def advance_next_run(job_id: str, run_at: datetime | None = None) -> dict[str, Any]:
    """Compatibility helper for manual schedule edits.

    Automatic scheduling must advance next_run_at through StateStore.complete_run().
    """
```

- [ ] **Step 5: Re-run focused test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_scheduler.py::test_tick_records_run_and_advances_after_completion -q
```

Expected: `1 passed`.

- [ ] **Step 6: Commit**

```bash
git add tests/test_cron_scheduler.py cron/scheduler.py cron/jobs.py
git commit -m "test: prove cron scheduler run state flow"
```

## Task 6: Legacy Import Enters The State Machine

**Files:**
- Modify: `tests/test_cron_jobs.py`
- Modify: `cron/state_store.py`
- Modify: `cron/jobs.py`

- [ ] **Step 1: Add import compatibility test**

Append this test to `tests/test_cron_jobs.py`:

```python
import json


def test_imported_jobs_json_job_can_be_claimed_and_completed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    due_at = "2026-05-30T10:00:00+00:00"
    jobs_file = cron_dir / "jobs.json"
    jobs_file.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "legacy-job",
                        "name": "Legacy Job",
                        "prompt": "write report",
                        "schedule": {"kind": "interval", "minutes": 30},
                        "schedule_display": "30m",
                        "enabled": True,
                        "state": "scheduled",
                        "next_run_at": due_at,
                        "repeat": {"times": None, "completed": 0},
                        "deliver": "local",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    from cron.state_store import StateStore

    store = StateStore()
    claimed = store.claim_due_jobs(now_text=due_at, limit=1)

    assert len(claimed) == 1
    assert claimed[0]["job"]["id"] == "legacy-job"
    assert claimed[0]["run"]["job_id"] == "legacy-job"
```

- [ ] **Step 2: Run focused test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_jobs.py::test_imported_jobs_json_job_can_be_claimed_and_completed -q
```

Expected before implementation: failure if legacy import is not triggered or imported jobs cannot be claimed.

- [ ] **Step 3: Preserve import behavior**

In `cron/state_store.py`, ensure initialization imports `jobs.json` when the SQLite jobs table is empty and records `jobs_json_imported_at`. Preserve this behavior:

```python
if not existing_jobs and jobs_json_path.exists():
    imported_jobs = _load_jobs_json(jobs_json_path)
    self.replace_jobs(imported_jobs)
    self.set_meta("jobs_json_imported_at", utc_now().isoformat())
```

Use the existing helper names in the file; do not add a second JSON parser if one already exists.

- [ ] **Step 4: Re-run focused test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_jobs.py::test_imported_jobs_json_job_can_be_claimed_and_completed -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cron_jobs.py cron/state_store.py cron/jobs.py
git commit -m "test: verify legacy cron jobs enter state machine"
```

## Task 7: Full Regression Verification

**Files:**
- No planned source changes. Fix only failures caused by this branch.

- [ ] **Step 1: Run cron regression suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_*.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py tests/test_agent_cli_repl.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run service smoke check**

Run:

```bash
AGENT_CRON_HOME=/tmp/agent-cron-run-state-machine-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli.main cron serve --once --interval 1 --lease-seconds 5
```

Expected output includes:

```text
Cron service exited.
```

- [ ] **Step 3: Inspect status smoke output**

Run:

```bash
AGENT_CRON_HOME=/tmp/agent-cron-run-state-machine-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli.main cron status
```

Expected output includes:

```text
Service status:
Scheduler service:
Last tick:
Delivery adapters:
```

- [ ] **Step 4: Commit final verification fixes if any**

If Step 1, Step 2, or Step 3 required code changes, commit them:

```bash
git add cron tests agent_cli
git commit -m "fix: stabilize cron run state machine"
```

If no code changes were needed, do not create an empty commit.

## Self-Review Notes

- Spec coverage: claim, run lifecycle, completion, recovery, delivery linkage, compatibility, and regression verification are covered by Tasks 1-7.
- Scope check: `cron logs`, `dry-run`, explicit backfill modes, job-level timeouts, and concurrency queues are not implemented in this plan.
- Type consistency: plan uses existing names from the current codebase: `StateStore`, `claim_due_jobs`, `mark_run_started`, `complete_run`, `recover_expired_leases`, `update_run_delivery_status`, `DeliveryDispatcher`, `enqueue_result`, `JobRunResult`, and `tick`.

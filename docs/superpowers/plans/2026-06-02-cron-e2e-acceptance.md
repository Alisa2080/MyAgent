# Cron E2E Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated cron runtime E2E acceptance suite that proves service-driven jobs create runs, outputs, deliveries, retries, recovery, and concurrency behavior without OS services, LLM calls, subprocess jobs, or network I/O.

**Architecture:** Create `tests/test_cron_e2e.py` with local fixtures and helpers that run `CronService(once=True)` against real `cron.scheduler.tick()` and `StateStore`, while injecting deterministic fake job runners and fake webhook senders. Keep production changes out unless an E2E test exposes a real integration bug.

**Tech Stack:** Python, pytest, `CronService`, `cron.scheduler.tick`, `StateStore`, `JobRunResult`, delivery registry webhook sender fakes, temporary `AGENT_CRON_HOME`.

---

## File Map

- Create `tests/test_cron_e2e.py`: E2E acceptance helpers and scenarios.
- No planned production changes. If a test exposes a runtime integration bug, keep the fix narrowly scoped and document it in the commit.

## Shared Test Harness

All tasks work in `tests/test_cron_e2e.py`. The initial task creates shared helpers that later tasks extend.

Key helper concepts:

- Use `monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))`.
- Use `cron.delivery_registry.clear_delivery_adapter_factories()` during cleanup.
- Use `cron.delivery_registry.register_delivery_adapter_factory(...)` to install a fake webhook adapter sender through the real registry path.
- Run the service with:

```python
def run_service_once(now_text: str, job_runner):
    from cron.service import CronService
    import cron.scheduler as scheduler

    tick_result = None

    def tick_fn():
        nonlocal tick_result
        tick_result = scheduler.tick(now_text=now_text, job_runner=job_runner)
        return tick_result

    service = CronService(
        interval_seconds=1,
        lease_seconds=60,
        owner_id="e2e-service",
        pid=4242,
        hostname="e2e-host",
        tick_fn=tick_fn,
        clock=lambda: now_text,
        sleeper=lambda _seconds: None,
    )
    exit_code = service.run(once=True)
    return service, tick_result, exit_code
```

This helper intentionally tests `CronService` plus `scheduler.tick`, while avoiding OS service managers and sleep loops.

## Task 1: Create E2E Harness and Successful Delivery Scenario

**Files:**
- Create: `tests/test_cron_e2e.py`

- [ ] **Step 1: Write the failing E2E success test**

Create `tests/test_cron_e2e.py` with:

```python
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


BASE_TIME = "2026-06-02T10:00:00+00:00"
WEBHOOK_URL = "https://example.invalid/hook"


@pytest.fixture(autouse=True)
def isolated_cron_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import cron.delivery_registry as delivery_registry

    delivery_registry.clear_delivery_adapter_factories()
    yield tmp_path
    delivery_registry.clear_delivery_adapter_factories()


def install_webhook_sender(sender: Callable[[str, dict[str, Any], int], tuple[int, str]]) -> None:
    import cron.delivery_registry as delivery_registry

    def factory(**kwargs):
        from cron.delivery_adapters import WebhookDeliveryAdapter

        return WebhookDeliveryAdapter(sender=sender)

    delivery_registry.register_delivery_adapter_factory(factory)


class RecordingRunner:
    def __init__(self, *, success: bool = True, output_doc: str = "# E2E Output", final_response: str = "delivered response", error: str | None = None):
        self.success = success
        self.output_doc = output_doc
        self.final_response = final_response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, job: dict[str, Any]):
        from cron.contracts import JobRunResult

        self.calls.append(dict(job))
        return JobRunResult(
            success=self.success,
            output_doc=self.output_doc,
            final_response=self.final_response,
            error=self.error,
        )


class ScriptedWebhookSender:
    def __init__(self, responses: list[tuple[int, str]]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def __call__(self, url: str, payload: dict[str, Any], timeout: int = 10) -> tuple[int, str]:
        self.calls.append((url, payload, timeout))
        if self.responses:
            return self.responses.pop(0)
        return 200, "ok"


def run_service_once(now_text: str, job_runner):
    from cron.service import CronService
    import cron.scheduler as scheduler

    tick_result = None

    def tick_fn():
        nonlocal tick_result
        tick_result = scheduler.tick(now_text=now_text, job_runner=job_runner)
        return tick_result

    service = CronService(
        interval_seconds=1,
        lease_seconds=60,
        owner_id="e2e-service",
        pid=4242,
        hostname="e2e-host",
        tick_fn=tick_fn,
        clock=lambda: now_text,
        sleeper=lambda _seconds: None,
    )
    exit_code = service.run(once=True)
    return service, tick_result, exit_code


def create_due_job(*, prompt: str = "write report", deliver: str = f"webhook:{WEBHOOK_URL}", concurrency_key: str | None = None, concurrency_policy: str | None = None, next_run_at: str = BASE_TIME):
    from cron.jobs import create_job, update_job

    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "schedule": "every 30m",
        "name": prompt,
        "deliver": deliver,
    }
    if concurrency_key is not None:
        kwargs["concurrency_key"] = concurrency_key
    if concurrency_policy is not None:
        kwargs["concurrency_policy"] = concurrency_policy
    job = create_job(**kwargs)
    return update_job(job["id"], {"next_run_at": next_run_at, "state": "scheduled", "enabled": True})


def test_service_executes_due_job_saves_output_and_delivers_webhook(isolated_cron_home):
    from cron.delivery_store import DeliveryStore
    from cron.service_state import read_service_status
    from cron.state_store import StateStore

    sender = ScriptedWebhookSender([(200, "ok")])
    install_webhook_sender(sender)
    job = create_due_job()
    runner = RecordingRunner(output_doc="# E2E Output\nbody", final_response="final e2e response")

    service, tick_result, exit_code = run_service_once(BASE_TIME, runner)

    store = StateStore()
    runs = store.runs_for_job(job["id"])
    events = DeliveryStore().list_events(job_id=job["id"], limit=10)
    service_status = read_service_status()

    assert exit_code == 0
    assert tick_result.ran == 1
    assert len(runner.calls) == 1
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["delivery_status"] == "delivered"
    assert runs[0]["output_path"]
    output_path = Path(runs[0]["output_path"])
    assert output_path.exists()
    assert "# E2E Output" in output_path.read_text(encoding="utf-8")
    assert len(events) == 1
    assert events[0]["status"] == "delivered"
    assert events[0]["attempt_count"] == 1
    assert events[0]["run_id"] == runs[0]["id"]
    assert sender.calls[0][0] == WEBHOOK_URL
    assert store.get_job(job["id"])["last_delivery_error"] is None
    assert service.status["last_tick"]["ran"] == 1
    assert service.status["last_tick"]["delivery"]["delivered"] == 1
    assert service_status["last_tick"]["ran"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py::test_service_executes_due_job_saves_output_and_delivers_webhook -q
```

Expected: FAIL if helper imports or delivery event listing assumptions need adjustment. If it unexpectedly passes, continue; this task is intentionally a test-only acceptance addition.

- [ ] **Step 3: Adjust only test helper details if needed**

If `DeliveryStore().list_events(job_id=..., limit=10)` does not exist or has a different signature, inspect `cron/delivery_store.py` and use the existing event listing method. Keep the assertion intent unchanged:

```python
events = [event for event in DeliveryStore().list_events(limit=20) if event["job_id"] == job["id"]]
```

If output content is wrapped with a cron heading, assert that the deterministic runner content appears anywhere in the file:

```python
assert "# E2E Output" in output_path.read_text(encoding="utf-8")
```

- [ ] **Step 4: Run Task 1 test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py -q
```

Expected: PASS for the new success scenario.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cron_e2e.py
git commit -m "test: add cron e2e success acceptance"
```

## Task 2: Delivery Failure and No-Due Retry Scenario

**Files:**
- Modify: `tests/test_cron_e2e.py`

- [ ] **Step 1: Write failing delivery retry E2E test**

Append this test to `tests/test_cron_e2e.py`:

```python
def test_service_retries_failed_delivery_on_no_due_tick(isolated_cron_home):
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    sender = ScriptedWebhookSender([(500, "down"), (200, "ok")])
    install_webhook_sender(sender)
    job = create_due_job()
    runner = RecordingRunner(output_doc="# retry output", final_response="retry me")

    _service1, first_tick, first_exit = run_service_once(BASE_TIME, runner)

    store = StateStore()
    first_run = store.runs_for_job(job["id"])[0]
    first_events = DeliveryStore().list_events(job_id=job["id"], limit=10)
    assert first_exit == 0
    assert first_tick.ran == 1
    assert len(runner.calls) == 1
    assert first_run["status"] == "succeeded"
    assert first_run["delivery_status"] == "retrying"
    assert first_events[0]["status"] == "failed"
    assert "HTTP 500" in (store.get_job(job["id"])["last_delivery_error"] or "")

    _service2, second_tick, second_exit = run_service_once("2026-06-02T10:01:00+00:00", runner)

    second_run = store.get_run(first_run["id"])
    second_events = DeliveryStore().list_events(job_id=job["id"], limit=10)
    assert second_exit == 0
    assert second_tick.due == 0
    assert second_tick.ran == 0
    assert len(runner.calls) == 1
    assert second_tick.delivery.claimed >= 1
    assert second_tick.delivery.delivered == 1
    assert second_events[0]["status"] == "delivered"
    assert second_run["delivery_status"] == "delivered"
    assert store.get_job(job["id"])["last_delivery_error"] is None
    assert len(sender.calls) == 2
```

- [ ] **Step 2: Run test to verify it fails or exposes assumptions**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py::test_service_retries_failed_delivery_on_no_due_tick -q
```

Expected: It may fail if failed delivery events are not immediately due for retry. If so, inspect the event `next_attempt_at` and set it to the second tick timestamp in the test with the existing store update method:

```python
DeliveryStore().update_event(first_events[0]["id"], next_attempt_at="2026-06-02T10:01:00+00:00")
```

Do not change production retry backoff unless this exposes a clear regression against existing delivery semantics.

- [ ] **Step 3: Adjust test to current delivery state names if needed**

If the run delivery status uses `"failed"` instead of `"retrying"` after the first failed attempt, assert the current model while preserving retry intent:

```python
assert first_run["delivery_status"] in {"retrying", "failed"}
```

The second tick must still assert that no job ran and delivery advanced.

- [ ] **Step 4: Run E2E tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cron_e2e.py
git commit -m "test: cover cron delivery retry e2e"
```

## Task 3: Service Restart Recovery Scenario

**Files:**
- Modify: `tests/test_cron_e2e.py`

- [ ] **Step 1: Write failing restart recovery E2E test**

Append this test to `tests/test_cron_e2e.py`:

```python
def test_fresh_service_recovers_stale_run_and_promotes_queued_run(isolated_cron_home):
    from cron.state_store import StateStore

    stale_job = create_due_job(
        prompt="stale",
        deliver="local",
        concurrency_key="repo:restart",
        concurrency_policy="queue_all",
        next_run_at="2026-06-02T09:00:00+00:00",
    )
    queued_job = create_due_job(
        prompt="queued",
        deliver="local",
        concurrency_key="repo:restart",
        concurrency_policy="queue_all",
        next_run_at="2026-06-02T09:00:00+00:00",
    )
    store = StateStore(lease_seconds=60)
    first_claim = store.claim_due_jobs(now_text="2026-06-02T09:00:00+00:00", limit=1)[0]
    stale_run_id = first_claim["run"]["id"]
    store.mark_run_started(stale_run_id)
    store.claim_due_jobs(now_text="2026-06-02T09:00:00+00:00", limit=10)

    with store._connect() as conn:
        conn.execute(
            "UPDATE runs SET heartbeat_at = ?, last_activity_at = ?, status = 'running' WHERE id = ?",
            ("2026-06-02T09:00:00+00:00", "2026-06-02T09:00:00+00:00", stale_run_id),
        )
        conn.execute(
            "UPDATE jobs SET state = 'running', lease_run_id = ?, lease_expires_at = ? WHERE id = ?",
            (stale_run_id, "2026-06-02T09:01:00+00:00", stale_job["id"]),
        )

    runner = RecordingRunner(output_doc="# recovered queue", final_response="queued done")
    service, tick_result, exit_code = run_service_once("2026-06-02T10:00:00+00:00", runner)

    stale_run = store.get_run(stale_run_id)
    queued_runs = store.runs_for_job(queued_job["id"])
    succeeded = [run for run in queued_runs if run["status"] == "succeeded"]

    assert exit_code == 0
    assert stale_run["status"] in {"abandoned", "failed"}
    assert stale_run["exit_reason"] in {"lease_expired", "idle_timeout", "heartbeat_stale"}
    assert len(succeeded) == 1
    assert Path(succeeded[0]["output_path"]).exists()
    assert len(runner.calls) == 1
    assert runner.calls[0]["id"] == queued_job["id"]
    assert tick_result.ran == 1
    assert service.status["owner_id"] == "e2e-service"
    assert service.status["last_tick"]["ran"] == 1
```

- [ ] **Step 2: Run test to verify it fails or exposes setup details**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py::test_fresh_service_recovers_stale_run_and_promotes_queued_run -q
```

Expected: May fail if the queue setup still has an occupying run for the same key or if `recover_expired_leases` marks the run `abandoned` before queued promotion. Adjust only the setup, not production code:

- ensure only `queued_job` has a queued run before the service tick;
- ensure stale job lease is expired relative to `"2026-06-02T10:00:00+00:00"`;
- assert the actual stale exit reason from `StateStore.recover_expired_leases` or scheduler stale completion.

- [ ] **Step 3: Run E2E tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_cron_e2e.py
git commit -m "test: cover cron service restart recovery e2e"
```

## Task 4: Concurrency Policy E2E Scenarios

**Files:**
- Modify: `tests/test_cron_e2e.py`

- [ ] **Step 1: Write failing skip/replace/queue E2E tests**

Append these tests to `tests/test_cron_e2e.py`:

```python
def test_skip_if_running_policy_is_enforced_through_service_tick(isolated_cron_home):
    from cron.state_store import StateStore

    first = create_due_job(prompt="first skip", deliver="local", concurrency_key="repo:skip", concurrency_policy="skip_if_running")
    second = create_due_job(prompt="second skip", deliver="local", concurrency_key="repo:skip", concurrency_policy="skip_if_running")
    store = StateStore()
    first_run = store.claim_due_jobs(now_text=BASE_TIME, limit=1)[0]["run"]

    runner = RecordingRunner()
    _service, tick_result, exit_code = run_service_once(BASE_TIME, runner)

    skipped = [run for run in store.runs_for_job(second["id"]) if run["status"] == "skipped"]
    assert exit_code == 0
    assert tick_result.ran == 0
    assert runner.calls == []
    assert len(skipped) == 1
    assert skipped[0]["exit_reason"] == "concurrency_skip"
    assert store.get_run(first_run["id"])["status"] == "claimed"


def test_replace_running_policy_replaces_old_run_and_executes_newer_through_service_tick(isolated_cron_home):
    from cron.state_store import StateStore

    first = create_due_job(prompt="first replace", deliver="local", concurrency_key="repo:replace", concurrency_policy="replace_running")
    second = create_due_job(prompt="second replace", deliver="local", concurrency_key="repo:replace", concurrency_policy="replace_running")
    store = StateStore()
    first_run = store.claim_due_jobs(now_text=BASE_TIME, limit=1)[0]["run"]

    runner = RecordingRunner(output_doc="# replacement", final_response="replacement done")
    _service, tick_result, exit_code = run_service_once(BASE_TIME, runner)

    first_after = store.get_run(first_run["id"])
    second_runs = store.runs_for_job(second["id"])
    succeeded = [run for run in second_runs if run["status"] == "succeeded"]
    assert exit_code == 0
    assert first_after["status"] == "abandoned"
    assert first_after["exit_reason"] == "replaced_by_newer_run"
    assert len(succeeded) == 1
    assert first_after["replaced_by_run_id"] == succeeded[0]["id"]
    assert [call["id"] for call in runner.calls] == [second["id"]]
    assert tick_result.ran == 1


def test_queue_all_policy_promotes_queued_run_after_key_is_free_through_service_tick(isolated_cron_home):
    from cron.state_store import StateStore

    first = create_due_job(prompt="first queue", deliver="local", concurrency_key="repo:queue", concurrency_policy="queue_all")
    second = create_due_job(prompt="second queue", deliver="local", concurrency_key="repo:queue", concurrency_policy="queue_all")
    store = StateStore()
    first_run = store.claim_due_jobs(now_text=BASE_TIME, limit=1)[0]["run"]
    store.claim_due_jobs(now_text=BASE_TIME, limit=10)
    queued_before = [run for run in store.runs_for_job(second["id"]) if run["status"] == "queued"]
    assert len(queued_before) == 1

    store.complete_run(
        first_run["id"],
        success=True,
        output_path=None,
        final_response="first complete",
        error=None,
        next_run_at=None,
        completed=False,
    )
    runner = RecordingRunner(output_doc="# queued promoted", final_response="queued complete")
    _service, tick_result, exit_code = run_service_once("2026-06-02T10:01:00+00:00", runner)

    second_runs = store.runs_for_job(second["id"])
    succeeded = [run for run in second_runs if run["status"] == "succeeded"]
    assert exit_code == 0
    assert len(succeeded) == 1
    assert Path(succeeded[0]["output_path"]).exists()
    assert [call["id"] for call in runner.calls] == [second["id"]]
    assert tick_result.due == 1
    assert tick_result.ran == 1
```

- [ ] **Step 2: Run tests to verify they fail or reveal setup assumptions**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py::test_skip_if_running_policy_is_enforced_through_service_tick tests/test_cron_e2e.py::test_replace_running_policy_replaces_old_run_and_executes_newer_through_service_tick tests/test_cron_e2e.py::test_queue_all_policy_promotes_queued_run_after_key_is_free_through_service_tick -q
```

Expected: These should either pass as new acceptance tests or fail on setup details. If `replace_running` processes both runs in one tick because the first claim is not occupying the key as expected, mark the first run `running` and job lease owner explicitly with `store.mark_run_started(first_run["id"])` before running the service tick.

- [ ] **Step 3: Keep assertions service-layer focused**

If any test accidentally asserts only `claim_due_jobs()` output, revise it so the final behavioral assertion happens after `run_service_once(...)` and includes:

```python
assert service.status["last_tick"] is not None
assert len(runner.calls) == expected_count
```

- [ ] **Step 4: Run E2E tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cron_e2e.py
git commit -m "test: cover cron concurrency policies e2e"
```

## Task 5: Full Cron Verification

**Files:**
- Modify: `tests/test_cron_e2e.py` only if verification reveals test isolation issues.

- [ ] **Step 1: Run focused E2E suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py -q
```

Expected: all E2E tests pass.

- [ ] **Step 2: Run scheduler/service/delivery/state suites**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_scheduler.py tests/test_cron_service.py tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_state_store.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run broader recent cron suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_manager.py tests/test_cron_service_platforms.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py tests/test_cron_runner_subprocess.py tests/test_cron_runner_worker.py tests/test_cron_import_health.py tests/test_cron_e2e.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Inspect git status**

Run:

```bash
git status --short
```

Expected: only intentional changes for this feature are present. The pre-existing untracked `docs/superpowers/plans/2026-06-01-cron-concurrency-cli-management.md` may appear in the main worktree and should not be staged.

- [ ] **Step 5: Commit isolation fixes only if needed**

If verification required test isolation changes, commit them:

```bash
git add tests/test_cron_e2e.py
git commit -m "test: stabilize cron e2e acceptance"
```

If no changes were needed, do not create an empty commit.

## Final Verification Before Merge

- [ ] Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py tests/test_cron_scheduler.py tests/test_cron_service.py tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_state_store.py -q
```

Expected: all tests pass.

- [ ] Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_manager.py tests/test_cron_service_platforms.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py tests/test_cron_runner_subprocess.py tests/test_cron_runner_worker.py tests/test_cron_import_health.py tests/test_cron_e2e.py -q
```

Expected: all tests pass.

- [ ] Confirm commits:

```bash
git log --oneline --max-count=10
```

Expected: E2E test commits appear after the plan commit in task order.

## Plan Self-Review

- Spec coverage: Tasks cover the success chain, delivery retry without due jobs, service restart recovery, `skip_if_running`, `replace_running`, queue policy, no OS service, no real runner, no real network, and focused verification.
- Plan scan: no incomplete markers remain; every task includes exact paths, test code, commands, expected results, and commit commands.
- Type consistency: helpers introduced in Task 1 (`RecordingRunner`, `ScriptedWebhookSender`, `run_service_once`, `create_due_job`) are reused with the same names and signatures in later tasks.

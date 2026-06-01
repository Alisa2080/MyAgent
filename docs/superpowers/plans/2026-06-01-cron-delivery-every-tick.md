# Cron Delivery Every Tick Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every scheduler tick recover stale delivery work and dispatch due non-origin delivery retries, even when no cron job is due.

**Architecture:** Keep delivery ownership in the existing SQLite-backed delivery queue. Add an explicit scheduler-level delivery maintenance phase that calls the existing dispatcher after explicit stale recovery, while preserving the fast dispatch path after a job completes. Origin events remain host-drained and are never claimed by scheduler delivery maintenance.

**Tech Stack:** Python dataclasses, SQLite `StateStore`, existing cron delivery registry/dispatcher, pytest.

---

## File Structure

- Modify `cron/delivery_dispatcher.py`: add `recover_stale` argument to `DeliveryDispatcher.dispatch_due()` and keep the existing default behavior.
- Modify `cron/delivery.py`: add `recover_stale` passthrough to `process_due()`.
- Modify `cron/scheduler.py`: add `DeliveryTickSummary`, add `_process_delivery_maintenance()`, include delivery maintenance in `tick()`, and keep same-tick dispatch after job completion.
- Modify `cron/service.py`: include nested delivery summary in `_tick_summary()`.
- Modify `agent_cli/cron_commands.py`: render delivery summary in `run_tick()`, `_last_tick_line()`, and service-status last tick output.
- Modify `README.md`: document that cron service handles outbound delivery retry, while origin drain and origin pollers remain host calls.
- Modify tests:
  - `tests/test_cron_delivery_dispatcher.py`
  - `tests/test_cron_delivery.py`
  - `tests/test_cron_scheduler.py`
  - `tests/test_cron_service.py`
  - `tests/test_agent_cli_cron_commands.py`

## Task 1: Dispatcher Stale Recovery Boundary

**Files:**
- Modify: `cron/delivery_dispatcher.py`
- Modify: `cron/delivery.py`
- Test: `tests/test_cron_delivery_dispatcher.py`
- Test: `tests/test_cron_delivery.py`

- [ ] **Step 1: Add dispatcher and process_due failing tests**

Add to `tests/test_cron_delivery_dispatcher.py`:

```python
def test_dispatcher_can_skip_stale_recovery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.state_store import StateStore

    store = StateStore()
    called = []
    monkeypatch.setattr(store, "recover_stale_delivery_events", lambda: called.append("recover") or 0)

    summary = DeliveryDispatcher(store=store).dispatch_due(limit=10, recover_stale=False)

    assert summary == {"claimed": 0, "delivered": 0, "failed": 0, "dead": 0}
    assert called == []
```

Add to `tests/test_cron_delivery.py` near `test_process_due_does_not_claim_origin_events`:

```python
def test_process_due_can_skip_stale_recovery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import process_due
    from cron.state_store import StateStore

    store = StateStore()
    called = []
    monkeypatch.setattr(store, "recover_stale_delivery_events", lambda: called.append("recover") or 0)

    summary = process_due(limit=10, store=store, recover_stale=False)

    assert summary == {"claimed": 0, "delivered": 0, "failed": 0, "dead": 0}
    assert called == []
```

- [ ] **Step 2: Run the new tests and verify red**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_delivery_dispatcher.py::test_dispatcher_can_skip_stale_recovery \
  tests/test_cron_delivery.py::test_process_due_can_skip_stale_recovery \
  -q
```

Expected: fail with `TypeError` because `recover_stale` is not accepted yet.

- [ ] **Step 3: Add the dispatcher flag and passthrough**

In `cron/delivery_dispatcher.py`, replace the `dispatch_due` signature and stale recovery call with:

```python
def dispatch_due(
    self,
    *,
    limit: int = 20,
    adapter_keys: set[str] | None = None,
    recover_stale: bool = True,
) -> dict[str, int]:
    summary = {"claimed": 0, "delivered": 0, "failed": 0, "dead": 0}
    dispatch_keys = adapter_keys if adapter_keys is not None else set(self.registry.active_adapter_keys())
    if recover_stale:
        self.store.recover_stale_delivery_events()
    if adapter_keys is None:
        for event in self.store.dead_letter_unsupported_delivery_events(
            supported_adapter_keys=set(self.registry.adapter_keys()),
            ignored_adapter_keys={"origin"},
            limit=limit,
        ):
            self._sync_after_event_update(event)
            summary["dead"] += 1
```

Keep the rest of the method body unchanged after this block.

In `cron/delivery.py`, change `process_due()` to accept and pass the flag:

```python
def process_due(
    *,
    limit: int = 20,
    store: DeliveryStore | None = None,
    webhook_sender: Callable[[str, dict[str, Any], int], tuple[int, str]] | None = None,
    recover_stale: bool = True,
) -> dict[str, int]:
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.state_store import StateStore

    registry = default_delivery_registry(webhook_sender=webhook_sender)
    if store is None:
        state_store = StateStore()
    elif hasattr(store, "_store"):
        state_store = store._store
    else:
        state_store = store
    dispatcher = DeliveryDispatcher(store=state_store, registry=registry)
    return dispatcher.dispatch_due(limit=limit, recover_stale=recover_stale)
```

- [ ] **Step 4: Run dispatcher/process_due tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_delivery_dispatcher.py \
  tests/test_cron_delivery.py \
  -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add cron/delivery_dispatcher.py cron/delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_delivery.py
git commit -m "feat: control cron delivery stale recovery"
```

## Task 2: Scheduler Delivery Maintenance Every Tick

**Files:**
- Modify: `cron/scheduler.py`
- Test: `tests/test_cron_scheduler.py`

- [ ] **Step 1: Add scheduler tests for no-due delivery progress and origin exclusion**

Add to `tests/test_cron_scheduler.py` near existing delivery tick tests:

```python
def test_tick_with_no_due_jobs_dispatches_due_failed_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_store import DeliveryStore
    import cron.delivery as delivery
    import cron.scheduler as scheduler

    sent = []
    monkeypatch.setattr(delivery, "default_webhook_sender", lambda url, payload, timeout=10: sent.append((url, payload)) or (204, "ok"))
    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-22T08:00:00+00:00",
    )
    DeliveryStore().update(
        event["id"],
        status="failed",
        next_attempt_at=RUN_AT.isoformat(),
        last_error="HTTP 500: down",
    )

    result = scheduler.tick(now_dt=RUN_AT, job_runner=lambda job: pytest.fail("no jobs should run"))

    assert result.due == 0
    assert result.delivery.claimed == 1
    assert result.delivery.delivered == 1
    assert sent[0][0] == "https://example.invalid/hook"
    assert DeliveryStore().get(event["id"])["status"] == "delivered"
```

Add:

```python
def test_tick_with_no_due_jobs_keeps_origin_delivery_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_store import DeliveryStore
    import cron.scheduler as scheduler

    event = enqueue_result(
        {
            "id": "job-origin",
            "name": "Origin",
            "deliver": "origin",
            "origin": {"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
        },
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-22T08:00:00+00:00",
    )

    result = scheduler.tick(now_dt=RUN_AT, job_runner=lambda job: pytest.fail("no jobs should run"))

    assert result.delivery.claimed == 0
    assert result.delivery.failed == 0
    assert result.delivery.dead == 0
    assert DeliveryStore().get(event["id"])["status"] == "pending"
```

- [ ] **Step 2: Run the scheduler no-due tests and verify red**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_scheduler.py::test_tick_with_no_due_jobs_dispatches_due_failed_delivery \
  tests/test_cron_scheduler.py::test_tick_with_no_due_jobs_keeps_origin_delivery_pending \
  -q
```

Expected: fail because `TickResult` has no `delivery` attribute and no delivery maintenance runs when no jobs are due.

- [ ] **Step 3: Add DeliveryTickSummary and maintenance helper**

In `cron/scheduler.py`, add after `JobTickResult`:

```python
@dataclass
class DeliveryTickSummary:
    recovered_stale: int = 0
    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    dead: int = 0
    error: str | None = None
```

Change `TickResult` to:

```python
@dataclass
class TickResult:
    due: int = 0
    ran: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    delivery: DeliveryTickSummary = field(default_factory=DeliveryTickSummary)
    results: list[JobTickResult] = field(default_factory=list)
```

Add before `tick()`:

```python
def _format_delivery_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _process_delivery_maintenance(store: StateStore, *, limit: int = 20) -> DeliveryTickSummary:
    summary = DeliveryTickSummary()
    try:
        summary.recovered_stale = store.recover_stale_delivery_events()
    except Exception as exc:
        summary.error = _format_delivery_error(exc)
        logger.exception("Cron delivery stale recovery failed during tick.")
        return summary

    try:
        from cron.delivery import process_due

        dispatched = process_due(limit=limit, store=store, recover_stale=False)
    except Exception as exc:
        summary.error = _format_delivery_error(exc)
        logger.exception("Cron delivery dispatch failed during tick.")
        return summary

    summary.claimed = int(dispatched.get("claimed", 0) or 0)
    summary.delivered = int(dispatched.get("delivered", 0) or 0)
    summary.failed = int(dispatched.get("failed", 0) or 0)
    summary.dead = int(dispatched.get("dead", 0) or 0)
    return summary
```

- [ ] **Step 4: Wire maintenance into tick before job claims**

In `cron/scheduler.py`, change the body after stale run completion to:

```python
        result = TickResult()
        result.delivery = _process_delivery_maintenance(store, limit=20)

        claimed = store.promote_queued_runs(now_text=run_at.isoformat(), limit=100)
        claimed = claimed + store.claim_due_jobs(now_text=run_at.isoformat(), limit=max(0, 100 - len(claimed)))
        result.due = len(claimed)

        if not claimed:
            return result
```

Keep the claimed job execution and counters unchanged after this block.

- [ ] **Step 5: Run scheduler tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_scheduler.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add cron/scheduler.py tests/test_cron_scheduler.py
git commit -m "feat: process cron delivery on every tick"
```

## Task 3: Stale Recovery and Maintenance Error Behavior

**Files:**
- Modify: `tests/test_cron_scheduler.py`
- Modify: `cron/scheduler.py`

- [ ] **Step 1: Add stale recovery and non-aborting error tests**

Add to `tests/test_cron_scheduler.py`:

```python
def test_tick_recovers_stale_delivery_before_claiming(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_store import DeliveryStore
    import cron.delivery as delivery
    import cron.scheduler as scheduler

    sent = []
    monkeypatch.setattr(delivery, "default_webhook_sender", lambda url, payload, timeout=10: sent.append(payload["event_id"]) or (204, "ok"))
    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-22T08:00:00+00:00",
    )
    DeliveryStore().update(
        event["id"],
        status="delivering",
        updated_at="2026-05-22T08:00:00+00:00",
        next_attempt_at=None,
    )

    result = scheduler.tick(now_dt=RUN_AT, job_runner=lambda job: pytest.fail("no jobs should run"))

    assert result.delivery.recovered_stale == 1
    assert result.delivery.claimed == 1
    assert result.delivery.delivered == 1
    assert sent == [event["id"]]
    assert DeliveryStore().get(event["id"])["status"] == "delivered"
```

Add:

```python
def test_delivery_maintenance_error_does_not_prevent_due_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.contracts import JobRunResult
    from cron.jobs import create_job, update_job
    import cron.scheduler as scheduler
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", name="daily", deliver="local")
    update_job(job["id"], {"next_run_at": RUN_AT.isoformat()})
    store = StateStore()
    monkeypatch.setattr(scheduler, "_store", lambda: store)
    monkeypatch.setattr(store, "recover_stale_delivery_events", lambda: (_ for _ in ()).throw(RuntimeError("delivery db locked")))
    monkeypatch.setattr(scheduler, "save_job_output", lambda job_id, doc, run_at=None: str(tmp_path / "out.md"))

    result = scheduler.tick(
        now_dt=RUN_AT,
        job_runner=lambda claimed_job: JobRunResult(True, "doc", "final", None),
    )

    assert result.delivery.error == "RuntimeError: delivery db locked"
    assert result.due == 1
    assert result.ran == 1
    assert result.succeeded == 1
    assert store.runs_for_job(job["id"])[0]["status"] == "succeeded"
```

- [ ] **Step 2: Run the new tests and verify red or targeted failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_scheduler.py::test_tick_recovers_stale_delivery_before_claiming \
  tests/test_cron_scheduler.py::test_delivery_maintenance_error_does_not_prevent_due_jobs \
  -q
```

Expected: stale recovery test may fail if the recovered event is not immediately due; error test should pass once Task 2 is implemented. If stale recovery does not make the event claimable, update `StateStore.recover_stale_delivery_events()` in this task to set recovered events to retryable `failed` with due `next_attempt_at`.

- [ ] **Step 3: Ensure recovered delivery events are due for retry**

If the stale test fails because recovered events are still not claimable, change `cron/state_store.py` inside `recover_stale_delivery_events()` so each stale event is marked failed through existing retry logic and has a retry time at or before the current tick time. Keep using `mark_delivery_failed()` for attempt counting and terminal dead-letter behavior:

```python
for row in rows:
    recovered = self.mark_delivery_failed(str(row["id"]), "delivery attempt abandoned")
    if recovered.get("status") == "failed":
        self.update_delivery_event(str(row["id"]), next_attempt_at=utc_now().isoformat())
```

- [ ] **Step 4: Run scheduler and state-store tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_scheduler.py \
  tests/test_cron_state_store.py \
  -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add cron/scheduler.py cron/state_store.py tests/test_cron_scheduler.py
git commit -m "test: cover cron delivery maintenance recovery"
```

## Task 4: Service and CLI Delivery Observability

**Files:**
- Modify: `cron/service.py`
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_cron_service.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Add service summary test**

Add to `tests/test_cron_service.py`:

```python
def test_service_tick_summary_includes_delivery():
    from types import SimpleNamespace

    from cron.service import _tick_summary

    result = SimpleNamespace(
        due=0,
        ran=0,
        succeeded=0,
        failed=0,
        skipped=0,
        delivery=SimpleNamespace(
            recovered_stale=1,
            claimed=2,
            delivered=1,
            failed=1,
            dead=0,
            error=None,
        ),
    )

    assert _tick_summary(result)["delivery"] == {
        "recovered_stale": 1,
        "claimed": 2,
        "delivered": 1,
        "failed": 1,
        "dead": 0,
        "error": None,
    }
```

- [ ] **Step 2: Add CLI rendering tests**

Add to `tests/test_agent_cli_cron_commands.py` near tick tests:

```python
def test_tick_renders_delivery_summary(monkeypatch):
    import agent_cli.cron_commands as cron_commands

    tick_result = SimpleNamespace(
        due=0,
        ran=0,
        succeeded=0,
        failed=0,
        skipped=0,
        delivery=SimpleNamespace(
            recovered_stale=1,
            claimed=2,
            delivered=1,
            failed=1,
            dead=0,
            error=None,
        ),
        results=[],
    )
    monkeypatch.setattr(cron_commands, "cron_tick", lambda: tick_result)

    result = cron_commands.run_tick()

    assert result.exit_code == 0
    assert "Delivery tick: recovered=1 claimed=2 delivered=1 failed=1 dead=0" in result.text
```

Add near service status tests:

```python
def test_cron_service_status_renders_delivery_summary(monkeypatch):
    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick={
                "due": 0,
                "ran": 0,
                "succeeded": 0,
                "failed": 0,
                "skipped": 0,
                "delivery": {
                    "recovered_stale": 1,
                    "claimed": 2,
                    "delivered": 1,
                    "failed": 1,
                    "dead": 0,
                    "error": None,
                },
            },
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_service_status()

    assert "Last tick: due=0 ran=0 succeeded=0 failed=0 skipped=0" in result.text
    assert "Delivery tick: recovered=1 claimed=2 delivered=1 failed=1 dead=0" in result.text
```

- [ ] **Step 3: Run the new observability tests and verify red**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_service.py::test_service_tick_summary_includes_delivery \
  tests/test_agent_cli_cron_commands.py::test_tick_renders_delivery_summary \
  tests/test_agent_cli_cron_commands.py::test_cron_service_status_renders_delivery_summary \
  -q
```

Expected: fail because delivery summary is not yet serialized or rendered.

- [ ] **Step 4: Serialize delivery in service status**

In `cron/service.py`, change `_tick_summary()` to:

```python
def _tick_summary(result: Any) -> dict[str, Any]:
    delivery = getattr(result, "delivery", None)
    summary: dict[str, Any] = {
        "due": int(getattr(result, "due", 0) or 0),
        "ran": int(getattr(result, "ran", 0) or 0),
        "succeeded": int(getattr(result, "succeeded", 0) or 0),
        "failed": int(getattr(result, "failed", 0) or 0),
        "skipped": int(getattr(result, "skipped", 0) or 0),
    }
    if delivery is not None:
        summary["delivery"] = {
            "recovered_stale": int(getattr(delivery, "recovered_stale", 0) or 0),
            "claimed": int(getattr(delivery, "claimed", 0) or 0),
            "delivered": int(getattr(delivery, "delivered", 0) or 0),
            "failed": int(getattr(delivery, "failed", 0) or 0),
            "dead": int(getattr(delivery, "dead", 0) or 0),
            "error": getattr(delivery, "error", None),
        }
    return summary
```

- [ ] **Step 5: Render delivery summary in CLI**

In `agent_cli/cron_commands.py`, add near `_last_tick_line()`:

```python
def _delivery_tick_line(delivery: Any) -> str | None:
    if delivery is None:
        return None
    if isinstance(delivery, dict):
        error = delivery.get("error")
        recovered_stale = delivery.get("recovered_stale", 0)
        claimed = delivery.get("claimed", 0)
        delivered = delivery.get("delivered", 0)
        failed = delivery.get("failed", 0)
        dead = delivery.get("dead", 0)
    else:
        error = getattr(delivery, "error", None)
        recovered_stale = getattr(delivery, "recovered_stale", 0)
        claimed = getattr(delivery, "claimed", 0)
        delivered = getattr(delivery, "delivered", 0)
        failed = getattr(delivery, "failed", 0)
        dead = getattr(delivery, "dead", 0)
    if error:
        return f"Delivery tick: error={error}"
    return (
        "Delivery tick: "
        f"recovered={recovered_stale} "
        f"claimed={claimed} "
        f"delivered={delivered} "
        f"failed={failed} "
        f"dead={dead}"
    )
```

In `cron_service_status()`, after appending `_last_tick_line(status.last_tick)`, append the delivery line:

```python
        delivery_line = _delivery_tick_line(status.last_tick.get("delivery"))
        if delivery_line:
            lines.append(delivery_line)
```

In `_service_status_lines()`, after the manual last tick line, append:

```python
        delivery_line = _delivery_tick_line(last_tick.get("delivery"))
        if delivery_line:
            lines.append(delivery_line)
```

In `run_tick()`, after the `Tick:` line creation and before lease rendering, append:

```python
    delivery_line = _delivery_tick_line(getattr(result, "delivery", None))
    if delivery_line:
        lines.append(delivery_line)
```

- [ ] **Step 6: Run service and CLI tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_service.py \
  tests/test_agent_cli_cron_commands.py \
  -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add cron/service.py agent_cli/cron_commands.py tests/test_cron_service.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: expose cron delivery tick summary"
```

## Task 5: Host Integration Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update cron delivery documentation**

In `README.md`, replace the cron delivery sentence around line 129 with:

```markdown
Cron output is saved under the configured terminal toolkit home. The cron service also advances outbound delivery every tick: stale `delivering` events are recovered, and due `pending` or `failed` non-origin delivery events are retried even when no job is due. `deliver="origin"` queues thread-scoped notifications that embedding applications must drain with `cron.notifications.drain_cron_notifications_for_thread_id(thread_id)`. Inbound origin/platform polling remains a host responsibility; hosts that own pollers can call `cron.origin_poller.poll_deliveries(pollers, store=None, limit=100)` on their own cadence.
```

- [ ] **Step 2: Verify documentation text**

Run:

```bash
rg -n "outbound delivery every tick|drain_cron_notifications_for_thread_id|poll_deliveries" README.md
```

Expected: one README section states outbound retry is cron-service driven and origin drain/poller integration is host-driven.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document cron delivery host contract"
```

## Task 6: Final Verification

**Files:**
- Verify all changed files from previous tasks.

- [ ] **Step 1: Run focused cron delivery suites**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_scheduler.py \
  tests/test_cron_delivery.py \
  tests/test_cron_delivery_dispatcher.py \
  tests/test_cron_service.py \
  tests/test_agent_cli_cron_commands.py \
  -q
```

Expected: all pass.

- [ ] **Step 2: Run service/runtime regression suites**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_service_manager.py \
  tests/test_cron_service_platforms.py \
  tests/test_cron_service_state.py \
  tests/test_agent_cli_main.py \
  -q
```

Expected: all pass.

- [ ] **Step 3: Check import health**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_import_health.py -q
```

Expected: all pass; `cron.scheduler` still avoids importing heavy runner modules at import time.

- [ ] **Step 4: Inspect git status**

Run:

```bash
git status --short
```

Expected: no modified tracked files. The pre-existing untracked file `docs/superpowers/plans/2026-06-01-cron-concurrency-cli-management.md` may still appear and should not be included in this work unless the user explicitly asks.

## Self-Review

Spec coverage:

- Every tick delivery maintenance: Task 2.
- Stale delivery recovery at tick start: Task 2 and Task 3.
- Retry pending/failed delivery when no due jobs exist: Task 2.
- Keep origin pending for host drain: Task 2.
- `TickResult`, service status, and CLI observability: Task 4.
- Host contract for notification drain and origin poller: Task 5.
- Backward compatibility for `process_due()` and dispatcher summary keys: Task 1.

Type consistency:

- `DeliveryTickSummary` fields match the spec: `recovered_stale`, `claimed`, `delivered`, `failed`, `dead`, `error`.
- `TickResult.delivery` uses `field(default_factory=DeliveryTickSummary)`.
- `DeliveryDispatcher.dispatch_due()` and `process_due()` both use `recover_stale: bool = True`.
- CLI output uses `Delivery tick: recovered=1 claimed=2 delivered=1 failed=1 dead=0` and `Delivery tick: error=RuntimeError: delivery db locked`.

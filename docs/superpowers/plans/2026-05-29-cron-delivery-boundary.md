# Cron Delivery Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `DeliveryRegistry.validate_targets()` the single cron delivery target validation boundary, and prove a registered fake `slack` adapter works from create through enqueue and dispatch without Slack-specific core branches.

**Architecture:** `enqueue_result()` will accept an optional `DeliveryRegistry`, call `registry.validate_targets()` once, and persist either validated events or one dead validation-failure event. `DeliveryDispatcher` remains adapter-key based and already supports custom registries; tests will use a fake adapter to prove platform extensibility.

**Tech Stack:** Python, pytest, SQLite-backed `cron.state_store.StateStore`, existing `cron.delivery_*` modules.

---

## File Structure

- Modify `cron/delivery.py`
  - Add an optional `registry` parameter to `enqueue_result()`.
  - Replace target-specific webhook/platform validation branches with a single `registry.validate_targets()` call.
  - Keep current local/origin event status semantics.
- Modify `tests/test_cron_delivery.py`
  - Add a fake Slack adapter.
  - Add tests for registered fake adapter enqueue and dispatch.
  - Add a regression test that unsupported platform validation is durable and comes from the registry path.
- Modify `tests/test_cron_jobs.py`
  - Add a create-time fake adapter test proving `delivery_targets` is persisted for `slack:C123`.
- Modify `agent_tools/public/cronjob.py`
  - Update the public `deliver` field description to mention `platform:chat_id[:thread_id]`.

Do not implement a real Slack adapter in this plan.

---

### Task 1: Add Failing Tests For Registry-Owned Platform Delivery

**Files:**
- Modify: `tests/test_cron_delivery.py`
- Modify: `tests/test_cron_jobs.py`

- [ ] **Step 1: Add fake adapter helpers to `tests/test_cron_delivery.py`**

Add these helpers near the top of `tests/test_cron_delivery.py`, after `from __future__ import annotations`:

```python
class FakeSlackAdapter:
    key = "slack"

    def __init__(self) -> None:
        self.delivered: list[dict] = []

    def validate(self, target, job):
        from cron.delivery_adapters import AdapterValidation

        if not target.address:
            return AdapterValidation(False, "slack delivery requires a channel id")
        return AdapterValidation(True)

    def deliver(self, event, job, run):
        from cron.delivery_adapters import DeliveryResult

        self.delivered.append({"event": event, "job": job, "run": run})
        return DeliveryResult(True)
```

- [ ] **Step 2: Add a failing enqueue/dispatch test to `tests/test_cron_delivery.py`**

Add this test after `test_bare_webhook_dispatch_uses_env_url`:

```python
def test_registered_platform_adapter_enqueues_and_dispatches_without_core_branch(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    adapter = FakeSlackAdapter()
    registry = default_delivery_registry()
    registry.register(adapter)

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "slack:C123"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
        registry=registry,
    )

    assert event is not None
    stored = DeliveryStore().get(event["id"])
    assert stored["target"] == "slack:C123"
    assert stored["target_type"] == "platform"
    assert stored["adapter_key"] == "slack"
    assert stored["address"] == "C123"
    assert stored["status"] == "pending"

    summary = DeliveryDispatcher(store=StateStore(), registry=registry).dispatch_due(
        limit=10,
        adapter_keys={"slack"},
    )

    assert summary["claimed"] == 1
    assert summary["delivered"] == 1
    assert DeliveryStore().get(event["id"])["status"] == "delivered"
    assert adapter.delivered[0]["event"]["adapter_key"] == "slack"
    assert adapter.delivered[0]["event"]["address"] == "C123"
```

- [ ] **Step 3: Add a failing registry validation dead-event test to `tests/test_cron_delivery.py`**

Add this test after the fake Slack dispatch test:

```python
def test_enqueue_result_records_registry_validation_failure_as_dead_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_store import DeliveryStore

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "slack:C123"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert event is not None
    stored = DeliveryStore().get(event["id"])
    assert stored["target"] == "slack:C123"
    assert stored["target_type"] == "platform"
    assert stored["adapter_key"] == "slack"
    assert stored["address"] == "C123"
    assert stored["status"] == "dead"
    assert stored["last_error"] == "unsupported delivery target: slack:C123"
```

- [ ] **Step 4: Add fake adapter helpers to `tests/test_cron_jobs.py`**

Add these helpers near the top of `tests/test_cron_jobs.py`, after imports:

```python
class FakeSlackAdapter:
    key = "slack"

    def validate(self, target, job):
        from cron.delivery_adapters import AdapterValidation

        if not target.address:
            return AdapterValidation(False, "slack delivery requires a channel id")
        return AdapterValidation(True)

    def deliver(self, event, job, run):
        from cron.delivery_adapters import DeliveryResult

        return DeliveryResult(True)
```

- [ ] **Step 5: Add a failing create-time persistence test to `tests/test_cron_jobs.py`**

Add this test near the existing create/update delivery tests:

```python
def test_create_job_accepts_registered_platform_adapter_and_persists_target(jobs_module, monkeypatch):
    from cron.delivery_registry import default_delivery_registry

    registry = default_delivery_registry()
    registry.register(FakeSlackAdapter())
    monkeypatch.setattr("cron.delivery_registry.default_delivery_registry", lambda **kwargs: registry)

    job = jobs_module.create_job(
        prompt="write a report",
        schedule="30m",
        deliver="slack:C123",
    )

    assert job["deliver"] == "slack:C123"
    assert job["origin"] is None
    assert job["delivery_targets"] == [
        {
            "raw": "slack:C123",
            "target_type": "platform",
            "adapter_key": "slack",
            "address": "C123",
            "thread_id": None,
            "metadata": {},
        }
    ]
```

- [ ] **Step 6: Run the new tests and verify they fail for the expected reasons**

Run:

```bash
pytest tests/test_cron_delivery.py::test_registered_platform_adapter_enqueues_and_dispatches_without_core_branch tests/test_cron_delivery.py::test_enqueue_result_records_registry_validation_failure_as_dead_event tests/test_cron_jobs.py::test_create_job_accepts_registered_platform_adapter_and_persists_target -q
```

Expected:

- The fake Slack enqueue test fails because `enqueue_result()` does not accept `registry`.
- The dead-event test may fail because current unsupported platform handling is still hardcoded.
- The create-job test should pass or fail only on monkeypatch/import details; if it already passes, keep it as regression coverage.

- [ ] **Step 7: Commit the failing tests**

```bash
git add tests/test_cron_delivery.py tests/test_cron_jobs.py
git commit -m "test: capture cron delivery registry boundary"
```

---

### Task 2: Refactor `enqueue_result()` To Use The Registry Boundary

**Files:**
- Modify: `cron/delivery.py`
- Test: `tests/test_cron_delivery.py`

- [ ] **Step 1: Update the `enqueue_result()` signature**

In `cron/delivery.py`, change the function signature from:

```python
def enqueue_result(
    job: dict[str, Any],
    result: "JobRunResult",
    output_path: str,
    run_at: datetime | str,
    *,
    store: DeliveryStore | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
```

to:

```python
def enqueue_result(
    job: dict[str, Any],
    result: "JobRunResult",
    output_path: str,
    run_at: datetime | str,
    *,
    store: DeliveryStore | None = None,
    registry: Any | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
```

- [ ] **Step 2: Replace target-specific validation with registry validation**

In `cron/delivery.py`, replace the body section from:

```python
    from cron.delivery_targets import DeliveryIdentity, DeliveryTargetError

    origin = DeliveryIdentity.from_job_origin(job.get("origin"))
    try:
        targets = _targets_for_job(job)
    except DeliveryTargetError as exc:
        targets = []
        error_text = str(exc)
    else:
        error_text = None

    events = []
    if error_text:
        events.append(
            store.enqueue(
                job_id=str(job.get("id") or ""),
                run_id=job.get("run_id"),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=str(job.get("deliver") or "origin"),
                target_type="origin",
                target_id=None,
                address=origin.session_id if origin else None,
                thread_id=origin.thread_id if origin else None,
                origin=origin.to_json() if origin else None,
                final_response=result.final_response,
                output_path=output_path,
                payload=payload,
                status="dead",
                last_error=error_text,
            )
        )
    for target in targets:
        target_error = None
        if target.target_type == "webhook" and not target.address:
            target_error = validate_webhook_url(None)
        elif target.target_type == "webhook":
            target_error = validate_webhook_url(target.address)
        elif target.target_type == "platform":
            from cron.delivery_registry import default_delivery_registry
            if default_delivery_registry().get(target.adapter_key) is None:
                target_error = f"unsupported delivery target: {target.raw}"
        if target_error:
            events.append(
                store.enqueue(
                    job_id=str(job.get("id") or ""),
                    run_id=job.get("run_id"),
                    job_name=job.get("name"),
                    run_at=run_at_text,
                    target=target.raw,
                    target_type=target.target_type,
                    target_id=target.address,
                    address=target.address,
                    thread_id=target.thread_id,
                    origin=target.metadata.get("origin"),
                    final_response=result.final_response,
                    output_path=output_path,
                    payload=payload,
                    status="dead",
                    last_error=target_error,
                )
            )
            continue
        status = "delivered" if target.target_type == "local" else "pending"
        events.append(
            store.enqueue(
                job_id=str(job.get("id") or ""),
                run_id=job.get("run_id"),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=target.raw,
                target_type=target.target_type,
                target_id=target.address,
                address=target.address,
                thread_id=target.thread_id,
                origin=target.metadata.get("origin"),
                final_response=result.final_response,
                output_path=output_path,
                payload=payload,
                status=status,
            )
        )
```

with:

```python
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_targets import DeliveryIdentity

    origin = DeliveryIdentity.from_job_origin(job.get("origin"))
    registry = registry or default_delivery_registry()
    validation = registry.validate_targets(job.get("deliver"), origin=origin, job=job)

    events = []
    if not validation.ok:
        fallback_target = None
        if validation.targets:
            fallback_target = validation.targets[0]
        events.append(
            store.enqueue(
                job_id=str(job.get("id") or ""),
                run_id=job.get("run_id"),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=fallback_target.raw if fallback_target else str(job.get("deliver") or "local"),
                target_type=fallback_target.target_type if fallback_target else "unsupported",
                adapter_key=fallback_target.adapter_key if fallback_target else "unsupported",
                target_id=fallback_target.address if fallback_target else None,
                address=fallback_target.address if fallback_target else None,
                thread_id=fallback_target.thread_id if fallback_target else None,
                origin=fallback_target.metadata.get("origin") if fallback_target else (origin.to_json() if origin else None),
                final_response=result.final_response,
                output_path=output_path,
                payload=payload,
                status="dead",
                last_error=validation.error or "delivery target validation failed",
            )
        )
    for target in validation.targets if validation.ok else []:
        status = "delivered" if target.target_type == "local" else "pending"
        events.append(
            store.enqueue(
                job_id=str(job.get("id") or ""),
                run_id=job.get("run_id"),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=target.raw,
                target_type=target.target_type,
                adapter_key=target.adapter_key,
                target_id=target.address,
                address=target.address,
                thread_id=target.thread_id,
                origin=target.metadata.get("origin"),
                final_response=result.final_response,
                output_path=output_path,
                payload=payload,
                status=status,
            )
        )
```

- [ ] **Step 3: Remove now-unused `DeliveryTargetError` import path from `enqueue_result()`**

Verify there is no `DeliveryTargetError` import inside `enqueue_result()` after Step 2. Do not remove the `_targets_for_job()` helper in this task; it may still be useful for backward compatibility or can be cleaned up in a later focused change.

- [ ] **Step 4: Run the targeted delivery tests**

Run:

```bash
pytest tests/test_cron_delivery.py::test_registered_platform_adapter_enqueues_and_dispatches_without_core_branch tests/test_cron_delivery.py::test_enqueue_result_records_registry_validation_failure_as_dead_event -q
```

Expected: both tests pass.

- [ ] **Step 5: Run the existing delivery test file**

Run:

```bash
pytest tests/test_cron_delivery.py -q
```

Expected: all tests pass. If a local/origin/webhook test fails, adjust only expectations that depended on the old hardcoded validation branch; do not reintroduce target-specific validation into `enqueue_result()`.

- [ ] **Step 6: Commit the enqueue refactor**

```bash
git add cron/delivery.py tests/test_cron_delivery.py
git commit -m "refactor: validate cron delivery targets through registry"
```

---

### Task 3: Verify Create-Time Registry Extensibility And Full Cron Delivery Suite

**Files:**
- Modify: `tests/test_cron_jobs.py`
- Modify: `agent_tools/public/cronjob.py`

- [ ] **Step 1: Run the create-time fake adapter test**

Run:

```bash
pytest tests/test_cron_jobs.py::test_create_job_accepts_registered_platform_adapter_and_persists_target -q
```

Expected: pass. If it fails because the monkeypatch does not affect `_normalize_delivery_config()`, keep the production code unchanged and replace the monkeypatch line with:

```python
import cron.delivery_registry as delivery_registry

monkeypatch.setattr(delivery_registry, "default_delivery_registry", lambda **kwargs: registry)
```

Then rerun the same command and expect pass.

- [ ] **Step 2: Update the public tool delivery target description**

Update `agent_tools/public/cronjob.py` field description from:

```python
deliver: str | None = Field(default=None, description="Delivery target(s), comma-separated: local, origin, webhook:<url>.")
```

to:

```python
deliver: str | None = Field(
    default=None,
    description="Delivery target(s), comma-separated: local, origin, webhook:<url>, or platform:chat_id[:thread_id].",
)
```

This step is documentation only; it must not add platform-specific validation to the tool.

- [ ] **Step 3: Run focused cron delivery suites**

Run:

```bash
pytest tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_delivery_targets.py tests/test_cron_jobs.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Search for forbidden validation branches in `enqueue_result()`**

Run:

```bash
python - <<'PY'
from pathlib import Path
text = Path("cron/delivery.py").read_text()
start = text.index("def enqueue_result(")
end = text.index("\\ndef process_due(", start)
body = text[start:end]
for forbidden in [
    'target.target_type == "webhook"',
    "target.target_type == 'webhook'",
    'target.target_type == "platform"',
    "target.target_type == 'platform'",
    "validate_webhook_url(target.address)",
    "default_delivery_registry().get(target.adapter_key)",
]:
    assert forbidden not in body, forbidden
print("enqueue_result boundary check passed")
PY
```

Expected output:

```text
enqueue_result boundary check passed
```

- [ ] **Step 5: Run lint or broader tests if the repository uses them locally**

Run the smallest broader command already used for this area. If no project-specific command is documented, run:

```bash
pytest tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_delivery_targets.py tests/test_cron_jobs.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit final verification/doc updates**

If `tests/test_cron_jobs.py` or `agent_tools/public/cronjob.py` changed after Task 1, commit them:

```bash
git add tests/test_cron_jobs.py agent_tools/public/cronjob.py
git commit -m "test: prove cron delivery adapter extensibility"
```

If there were no new changes after Task 2, skip this commit.

---

## Final Verification

- [ ] Run:

```bash
pytest tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_delivery_targets.py tests/test_cron_jobs.py -q
```

Expected: all tests pass.

- [ ] Run:

```bash
git status --short
```

Expected: only unrelated pre-existing files are dirty, such as `.codegraph/daemon.pid`, `cron_reference/`, or previously untracked plan files. No implementation files from this plan should be left unstaged unless intentionally uncommitted.

- [ ] Summarize:
  - `enqueue_result()` now uses `DeliveryRegistry.validate_targets()`.
  - fake `slack:C123` works with a registered adapter.
  - unsupported platform targets fail through registry validation.
  - no real Slack/Email/Discord adapter was added.

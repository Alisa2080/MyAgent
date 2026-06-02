# Cron CLI Delivery Service Env Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `cron test-delivery`, `cron tick`, and `cron run` use cron `service.env` credentials by default when they synchronously process delivery.

**Architecture:** Add a focused environment overlay context manager in `agent_cli/cron_commands.py`. The overlay reads `cron.service_env.read_service_env()`, temporarily supplies keys missing from the current shell, lets shell values win, and restores `os.environ` after the command finishes. Wrap only the CLI commands that synchronously process delivery.

**Tech Stack:** Python 3.11, pytest, existing cron CLI command functions, existing `cron.service_env`, fake gateway adapters in tests.

---

## File Structure

- Modify `agent_cli/cron_commands.py`
  - Add `_cron_delivery_env()` context manager near other cron command helpers.
  - Wrap `run_cron_job()` non-dry-run scheduler execution.
  - Wrap `run_tick()` scheduler execution.
  - Wrap `test_delivery()` enqueue/process execution.
- Modify `tests/test_agent_cli_cron_commands.py`
  - Add Feishu fake adapter tests for `test-delivery`, shell override behavior, secret redaction, environment restoration, and one scheduler path.

No gateway or Feishu adapter changes are planned. Adapters should continue reading credentials from `os.environ`.

---

### Task 1: Add Failing Tests for `cron test-delivery` Service Env Overlay

**Files:**
- Modify: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Add imports if needed**

At the top of `tests/test_agent_cli_cron_commands.py`, keep the existing imports and add nothing unless the file does not already import `os`. It currently imports `os`, so no import change should be needed.

- [ ] **Step 2: Add the fake Feishu adapter helper and tests after `test_test_delivery_dead_target_returns_exit_code_2`**

```python
def _register_env_recording_feishu(monkeypatch, sent_envs):
    import gateway.registry as gateway_registry
    from gateway.contracts import SendResult

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            return SendResult(True)

        def token_smoke(self):
            return SendResult(True)

        def send_text(self, target, message):
            sent_envs.append(
                {
                    "FEISHU_APP_ID": os.environ.get("FEISHU_APP_ID"),
                    "FEISHU_APP_SECRET": os.environ.get("FEISHU_APP_SECRET"),
                    "target_id": target.target_id,
                    "text": message.text,
                }
            )
            missing = [
                key
                for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
                if not os.environ.get(key)
            ]
            if missing:
                return SendResult(
                    False,
                    error=f"missing required Feishu environment variables: {', '.join(missing)}",
                )
            return SendResult(True)

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    monkeypatch.addfinalizer(gateway_registry.clear_gateway_adapter_factories)


def test_test_delivery_uses_service_env_for_feishu(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    from agent_cli import cron_commands
    from cron.service_env import write_service_env

    write_service_env(
        {
            "FEISHU_APP_ID": "service-app",
            "FEISHU_APP_SECRET": "service-secret",
        }
    )
    sent_envs = []
    _register_env_recording_feishu(monkeypatch, sent_envs)

    result = cron_commands.test_delivery(target="feishu:oc_service")

    assert result.exit_code == 0
    assert "status=delivered" in result.text
    assert "service-app" not in result.text
    assert "service-secret" not in result.text
    assert sent_envs == [
        {
            "FEISHU_APP_ID": "service-app",
            "FEISHU_APP_SECRET": "service-secret",
            "target_id": "oc_service",
            "text": "Cron job update: test-delivery\nstatus: ok\njob_id: test-delivery",
        }
    ]
    assert os.environ.get("FEISHU_APP_ID") is None
    assert os.environ.get("FEISHU_APP_SECRET") is None


def test_test_delivery_shell_env_overrides_service_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "shell-app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "shell-secret")

    from agent_cli import cron_commands
    from cron.service_env import write_service_env

    write_service_env(
        {
            "FEISHU_APP_ID": "service-app",
            "FEISHU_APP_SECRET": "service-secret",
        }
    )
    sent_envs = []
    _register_env_recording_feishu(monkeypatch, sent_envs)

    result = cron_commands.test_delivery(target="feishu:oc_shell")

    assert result.exit_code == 0
    assert "status=delivered" in result.text
    assert sent_envs[0]["FEISHU_APP_ID"] == "shell-app"
    assert sent_envs[0]["FEISHU_APP_SECRET"] == "shell-secret"
    assert os.environ["FEISHU_APP_ID"] == "shell-app"
    assert os.environ["FEISHU_APP_SECRET"] == "shell-secret"
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_test_delivery_uses_service_env_for_feishu tests/test_agent_cli_cron_commands.py::test_test_delivery_shell_env_overrides_service_env -q
```

Expected: the first test fails before implementation because the fake Feishu adapter sees missing env values and the event is `dead`. The second test may pass because shell env is already visible.

---

### Task 2: Implement the Cron Delivery Environment Overlay Helper

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Add imports**

Near the top of `agent_cli/cron_commands.py`, ensure these imports exist:

```python
from contextlib import contextmanager
import os
from collections.abc import Iterator
```

If `collections.abc` is already imported for other names, extend that import instead of adding a duplicate.

- [ ] **Step 2: Add `_cron_delivery_env()` near `_cron_service_result()`**

```python
@contextmanager
def _cron_delivery_env() -> Iterator[None]:
    from cron.service_env import read_service_env

    service_values = read_service_env()
    if not service_values:
        yield
        return

    original = {key: os.environ.get(key) for key in service_values}
    try:
        for key, value in service_values.items():
            if key not in os.environ:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
```

This implementation intentionally only supplies service env keys missing from the shell. That directly implements shell-over-service precedence and avoids touching unrelated process environment keys.

- [ ] **Step 3: Wrap `test_delivery()` processing**

Change the body of `test_delivery()` so the enqueue/process section runs inside the helper:

```python
    with _cron_delivery_env():
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
```

Leave the reporting code after the `with` block unchanged.

- [ ] **Step 4: Run test-delivery focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_test_delivery_uses_service_env_for_feishu tests/test_agent_cli_cron_commands.py::test_test_delivery_shell_env_overrides_service_env -q
```

Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "fix: load service env for cron test delivery"
```

---

### Task 3: Cover `cron run` Scheduler Path With Service Env

**Files:**
- Modify: `tests/test_agent_cli_cron_commands.py`
- Modify: `agent_cli/cron_commands.py`

- [ ] **Step 1: Add failing `cron run` test near existing `test_cron_run_uses_scheduler_path`**

```python
def test_cron_run_uses_service_env_for_feishu_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    import cron.state_store as state_store
    from cron.jobs import create_job
    from cron.runner import JobRunResult
    import cron.runner_client as runner_client
    from cron.service_env import write_service_env

    run_time = datetime.fromisoformat("2026-06-03T09:00:00+08:00")
    state_store.utc_now = lambda: run_time
    write_service_env(
        {
            "FEISHU_APP_ID": "service-run-app",
            "FEISHU_APP_SECRET": "service-run-secret",
        }
    )
    sent_envs = []
    _register_env_recording_feishu(monkeypatch, sent_envs)
    job = create_job(
        "manual feishu output",
        "every 5m",
        name="Manual Feishu",
        deliver="feishu:oc_run",
    )

    def fake_run_job(job_data):
        return JobRunResult(
            success=True,
            output_doc="manual feishu output",
            final_response="manual feishu response",
            error=None,
            exit_reason=None,
        )

    monkeypatch.setattr(runner_client, "run_job", fake_run_job)

    result = _run_cli(["cron", "run", job["id"]])

    assert result.exit_code == 0
    assert "Status: ok" in result.text
    assert "Delivery: delivered=1 failed=0 pending=0 dead=0" in result.text
    assert sent_envs[0]["FEISHU_APP_ID"] == "service-run-app"
    assert sent_envs[0]["FEISHU_APP_SECRET"] == "service-run-secret"
    assert "service-run-app" not in result.text
    assert "service-run-secret" not in result.text
    assert os.environ.get("FEISHU_APP_ID") is None
    assert os.environ.get("FEISHU_APP_SECRET") is None
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_run_uses_service_env_for_feishu_delivery -q
```

Expected: FAIL before implementation because `run_cron_job()` calls `tick()` without the service env overlay.

- [ ] **Step 3: Wrap non-dry `run_cron_job()` scheduler execution**

Inside `run_cron_job()`, change the non-dry branch so `claim_manual_job()`, `tick()`, and `store.get_run()` execute inside `_cron_delivery_env()`:

```python
        with _cron_delivery_env():
            try:
                claimed = store.claim_manual_job(job_id, now_text=run_at)
            except KeyError:
                return CronCommandResult(f"Cron job not found: {job_id}.", exit_code=2)
            except ValueError as exc:
                return CronCommandResult(str(exc), exit_code=2)

            run_id = str(claimed["run"]["id"])
            tick(now_text=run_at)
            run = store.get_run(run_id) or claimed["run"]
```

Keep output formatting after this block. Ensure `run_id` and `run` remain in scope.

- [ ] **Step 4: Run focused test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_run_uses_service_env_for_feishu_delivery -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "fix: load service env for manual cron run delivery"
```

---

### Task 4: Cover `cron tick` Scheduler Path With Service Env

**Files:**
- Modify: `tests/test_agent_cli_cron_commands.py`
- Modify: `agent_cli/cron_commands.py`

- [ ] **Step 1: Add failing `cron tick` test near other tick/doctor command tests**

```python
def test_cron_tick_uses_service_env_for_feishu_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    import cron.state_store as state_store
    from cron.jobs import create_job
    from cron.runner import JobRunResult
    import cron.runner_client as runner_client
    from cron.service_env import write_service_env

    run_time = datetime.fromisoformat("2026-06-03T09:10:00+08:00")
    state_store.utc_now = lambda: run_time
    write_service_env(
        {
            "FEISHU_APP_ID": "service-tick-app",
            "FEISHU_APP_SECRET": "service-tick-secret",
        }
    )
    sent_envs = []
    _register_env_recording_feishu(monkeypatch, sent_envs)
    create_job(
        "tick feishu output",
        "every 1m",
        name="Tick Feishu",
        deliver="feishu:oc_tick",
        next_run_at=(run_time - timedelta(minutes=1)).isoformat(),
    )

    def fake_run_job(job_data):
        return JobRunResult(
            success=True,
            output_doc="tick feishu output",
            final_response="tick feishu response",
            error=None,
            exit_reason=None,
        )

    monkeypatch.setattr(runner_client, "run_job", fake_run_job)

    result = _run_cli(["cron", "tick"])

    assert result.exit_code == 0
    assert "Tick: due=1 ran=1 succeeded=1 failed=0 skipped=0" in result.text
    assert "Delivery tick:" in result.text
    assert sent_envs[0]["FEISHU_APP_ID"] == "service-tick-app"
    assert sent_envs[0]["FEISHU_APP_SECRET"] == "service-tick-secret"
    assert "service-tick-app" not in result.text
    assert "service-tick-secret" not in result.text
    assert os.environ.get("FEISHU_APP_ID") is None
    assert os.environ.get("FEISHU_APP_SECRET") is None
```

If `create_job()` does not accept `next_run_at`, create the job first and use `cron.state_store.StateStore().update_job(...)` or edit the job object through the existing test helper pattern in this file. Do not change production schedule parsing for this test.

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_tick_uses_service_env_for_feishu_delivery -q
```

Expected: FAIL before implementation because `run_tick()` calls scheduler tick without the service env overlay.

- [ ] **Step 3: Wrap `run_tick()` scheduler execution**

In `agent_cli/cron_commands.py`, change the first scheduler call in `run_tick()` from:

```python
    result = _get_cron_tick()()
```

to:

```python
    with _cron_delivery_env():
        result = _get_cron_tick()()
```

Leave service lease reporting and output formatting outside the helper.

- [ ] **Step 4: Run focused test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_tick_uses_service_env_for_feishu_delivery -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "fix: load service env for cron tick delivery"
```

---

### Task 5: Regression and Review

**Files:**
- Verify: `agent_cli/cron_commands.py`
- Verify: `tests/test_agent_cli_cron_commands.py`
- Verify: `docs/superpowers/specs/2026-06-03-cron-cli-delivery-service-env-design.md`

- [ ] **Step 1: Run focused CLI tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py -q
```

Expected: PASS.

- [ ] **Step 2: Run cron delivery regression set**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_e2e.py -q
```

Expected: PASS.

- [ ] **Step 3: Check formatting and unstaged changes**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: `git diff --check` exits 0. `git status` shows only intentional tracked changes plus the known pre-existing untracked file `docs/superpowers/plans/2026-06-01-cron-concurrency-cli-management.md`, if it is still present.

- [ ] **Step 4: Commit any final test-only adjustments**

If Task 5 required any test stabilization edits, commit them:

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "test: cover cron cli delivery service env"
```

If no changes remain, skip this commit.


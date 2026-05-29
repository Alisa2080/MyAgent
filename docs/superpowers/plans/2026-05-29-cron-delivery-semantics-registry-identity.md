# Cron Delivery Semantics Registry Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clarify cron delivery semantics, make delivery registry extensible and introspectable, improve status/tool feedback, and persist stable CLI/gateway/web origin identity without adding real platform delivery providers.

**Architecture:** Keep the current durable `delivery_events` and adapter-dispatch model. Treat `local` as synchronous audit completion, `origin` as poll/bridge pending work, and active external dispatch as adapter-owned. Add a canonical registry builder with in-process adapter factories, and add an origin identity extraction helper used by the cronjob tool.

**Tech Stack:** Python 3.11, pytest, SQLite-backed `cron.state_store.StateStore`, existing `cron.delivery_*`, `agent_cli`, `agent_tools`, and `agent_core.session_context` modules.

---

## File Structure

- Modify `cron/delivery_adapters.py`
  - Make local/origin adapter behavior explicit: local is validation-only compatibility; origin is validation-only poll/bridge target.
- Modify `cron/delivery_dispatcher.py`
  - Default dispatch should only claim active dispatch adapters, not local or origin.
- Modify `cron/delivery_registry.py`
  - Add canonical `build_delivery_registry()`.
  - Add in-process adapter factory registration helpers.
  - Expose active dispatch keys and known adapter keys for status/errors.
- Modify `cron/delivery.py`
  - Preserve local synchronous `delivered` event behavior.
  - Use canonical registry construction.
- Modify `cron/delivery_store.py` and/or `cron/state_store.py`
  - Add small helpers for origin-pending stats if needed for status wording.
- Modify `agent_cli/cron_commands.py`
  - Use canonical registry introspection.
  - Improve status/doctor adapter output and origin pending wording.
- Modify `agent_tools/public/cronjob.py`
  - Use structured origin identity instead of only thread id.
  - Improve unsupported delivery error text with known adapter keys.
- Modify `agent_core/session_context.py`
  - Add stable origin identity extraction from runtime/config metadata.
- Modify tests:
  - `tests/test_cron_delivery.py`
  - `tests/test_cron_delivery_dispatcher.py`
  - `tests/test_cron_delivery_targets.py`
  - `tests/test_cronjob_tool.py`
  - `tests/test_cron_jobs.py`
  - `tests/test_agent_cli_cron_commands.py`
  - `tests/test_session_context.py`

Use this interpreter for all Python test commands:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest ...
```

---

### Task 1: Make Local And Origin Delivery Semantics Explicit

**Files:**
- Modify: `cron/delivery_adapters.py`
- Modify: `cron/delivery_dispatcher.py`
- Modify: `cron/delivery_registry.py`
- Test: `tests/test_cron_delivery.py`
- Test: `tests/test_cron_delivery_dispatcher.py`

- [ ] **Step 1: Write local/origin semantics tests**

Add these tests to `tests/test_cron_delivery_dispatcher.py`:

```python
def test_default_dispatcher_does_not_claim_local_or_origin_events(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_store import DeliveryStore

    local_event = enqueue_result(
        {"id": "job-local", "name": "Local", "deliver": "local"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    origin_event = enqueue_result(
        {
            "id": "job-origin",
            "name": "Origin",
            "deliver": "origin",
            "origin": {"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
        },
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    summary = DeliveryDispatcher().dispatch_due(limit=10)

    assert summary == {"claimed": 0, "delivered": 0, "failed": 0, "dead": 0}
    assert DeliveryStore().get(local_event["id"])["status"] == "delivered"
    assert DeliveryStore().get(origin_event["id"])["status"] == "pending"
```

Add this test to `tests/test_cron_delivery.py` near the existing local test:

```python
def test_local_delivery_is_synchronous_audit_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_store import DeliveryStore

    event = enqueue_result(
        {"id": "job-local", "name": "Local", "deliver": "local"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    stored = DeliveryStore().get(event["id"])
    assert stored["target_type"] == "local"
    assert stored["adapter_key"] == "local"
    assert stored["status"] == "delivered"
    assert stored["next_attempt_at"] is None
```

Add this test to `tests/test_cron_delivery_dispatcher.py` near the dispatcher tests:

```python
def test_default_registry_active_dispatch_keys_exclude_local_and_origin():
    from cron.delivery_registry import default_delivery_registry

    assert default_delivery_registry().active_adapter_keys() == ["webhook"]
```

- [ ] **Step 2: Run the new tests and verify red**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_delivery.py::test_local_delivery_is_synchronous_audit_event \
  tests/test_cron_delivery_dispatcher.py::test_default_dispatcher_does_not_claim_local_or_origin_events \
  tests/test_cron_delivery_dispatcher.py::test_default_registry_active_dispatch_keys_exclude_local_and_origin \
  -q
```

Expected: the local test may already pass; the dispatcher test should fail because the current default dispatcher claims `local` events when any pending local event exists, or because the exact summary includes local handling.
The active-key test should fail before implementation because `active_adapter_keys()` does not exist yet.

- [ ] **Step 3: Make adapter classes explicit**

In `cron/delivery_adapters.py`, replace `LocalDeliveryAdapter.deliver()` with compatibility-only behavior:

```python
class LocalDeliveryAdapter:
    key = "local"
    active_dispatch = False

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        return AdapterValidation(True)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        return DeliveryResult(False, retryable=False, error="local delivery is completed synchronously when output is saved")
```

Replace `OriginDeliveryAdapter` with:

```python
class OriginDeliveryAdapter:
    key = "origin"
    active_dispatch = False

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        if not target.address:
            return AdapterValidation(False, "origin delivery requires session_id or chat_id")
        return AdapterValidation(True)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        return DeliveryResult(False, retryable=False, error="origin delivery waits for origin poll or host bridge pickup")
```

Add `active_dispatch = True` to `WebhookDeliveryAdapter`:

```python
class WebhookDeliveryAdapter:
    key = "webhook"
    active_dispatch = True
```

- [ ] **Step 4: Add active adapter introspection and update dispatcher**

In `cron/delivery_registry.py`, add this method to `DeliveryRegistry` after `adapter_keys()`:

```python
def active_adapter_keys(self) -> list[str]:
    return sorted(
        key
        for key, adapter in self._adapters.items()
        if bool(getattr(adapter, "active_dispatch", True))
    )
```

In `cron/delivery_dispatcher.py`, change:

```python
dispatch_keys = adapter_keys if adapter_keys is not None else {"local", "webhook"}
```

to:

```python
dispatch_keys = adapter_keys if adapter_keys is not None else set(self.registry.active_adapter_keys())
```

- [ ] **Step 5: Run local/origin focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_delivery.py::test_local_delivery_is_synchronous_audit_event \
  tests/test_cron_delivery_dispatcher.py::test_default_dispatcher_does_not_claim_local_or_origin_events \
  tests/test_cron_delivery_dispatcher.py::test_default_registry_active_dispatch_keys_exclude_local_and_origin \
  tests/test_cron_delivery.py::test_process_due_does_not_claim_origin_events \
  -q
```

Expected: all pass.

- [ ] **Step 6: Run delivery dispatcher suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery_dispatcher.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add cron/delivery_adapters.py cron/delivery_dispatcher.py cron/delivery_registry.py tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py
git commit -m "refactor: clarify local and origin delivery semantics"
```

---

### Task 2: Add Canonical Registry Builder And Extension Hooks

**Files:**
- Modify: `cron/delivery_registry.py`
- Modify: `cron/delivery.py`
- Modify: `cron/delivery_dispatcher.py`
- Test: `tests/test_cron_delivery_targets.py`
- Test: `tests/test_cron_delivery.py`

- [ ] **Step 1: Write registry extension tests**

Add to `tests/test_cron_delivery_targets.py`:

```python
def test_build_delivery_registry_includes_registered_adapter_factory(monkeypatch):
    from cron.delivery_adapters import AdapterValidation, DeliveryResult
    import cron.delivery_registry as delivery_registry

    class FakeSlackAdapter:
        key = "slack"
        active_dispatch = True

        def validate(self, target, job):
            return AdapterValidation(bool(target.address), None if target.address else "slack channel required")

        def deliver(self, event, job, run):
            return DeliveryResult(True)

    delivery_registry.clear_delivery_adapter_factories()
    delivery_registry.register_delivery_adapter_factory(lambda **kwargs: FakeSlackAdapter())
    try:
        registry = delivery_registry.build_delivery_registry()
        result = registry.validate_targets("slack:C123", origin=None, job={})
    finally:
        delivery_registry.clear_delivery_adapter_factories()

    assert "slack" in registry.adapter_keys()
    assert "slack" in registry.active_adapter_keys()
    assert result.ok is True
    assert result.targets[0].adapter_key == "slack"
```

Add to `tests/test_cron_delivery.py`:

```python
def test_process_due_uses_registry_factory_for_platform_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result, process_due
    from cron.delivery_adapters import AdapterValidation, DeliveryResult
    from cron.delivery_store import DeliveryStore
    import cron.delivery_registry as delivery_registry

    delivered = []

    class FakeSlackAdapter:
        key = "slack"
        active_dispatch = True

        def validate(self, target, job):
            return AdapterValidation(True)

        def deliver(self, event, job, run):
            delivered.append(event)
            return DeliveryResult(True)

    delivery_registry.clear_delivery_adapter_factories()
    delivery_registry.register_delivery_adapter_factory(lambda **kwargs: FakeSlackAdapter())
    try:
        event = enqueue_result(
            {"id": "job-1", "name": "Daily", "deliver": "slack:C123"},
            JobRunResult(success=True, output_doc="# out", final_response="done"),
            "/tmp/out.md",
            "2026-05-28T10:00:00+00:00",
        )
        summary = process_due(limit=10)
    finally:
        delivery_registry.clear_delivery_adapter_factories()

    assert summary["delivered"] == 1
    assert delivered[0]["adapter_key"] == "slack"
    assert DeliveryStore().get(event["id"])["status"] == "delivered"
```

- [ ] **Step 2: Run registry extension tests and verify red**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_delivery_targets.py::test_build_delivery_registry_includes_registered_adapter_factory \
  tests/test_cron_delivery.py::test_process_due_uses_registry_factory_for_platform_adapter \
  -q
```

Expected: fail because `build_delivery_registry()`, `register_delivery_adapter_factory()`, or `clear_delivery_adapter_factories()` do not exist.

- [ ] **Step 3: Implement registry factory API**

In `cron/delivery_registry.py`, add after `DeliveryRegistry`:

```python
DeliveryAdapterFactory = Any
_ADAPTER_FACTORIES: list[DeliveryAdapterFactory] = []


def register_delivery_adapter_factory(factory: DeliveryAdapterFactory) -> None:
    _ADAPTER_FACTORIES.append(factory)


def clear_delivery_adapter_factories() -> None:
    _ADAPTER_FACTORIES.clear()
```

Task 1 already added `active_adapter_keys()` to `DeliveryRegistry`; keep that method unchanged.

Replace `default_delivery_registry()` with:

```python
def build_delivery_registry(*, webhook_sender=None, extra_adapters=None) -> DeliveryRegistry:
    from cron.delivery_adapters import LocalDeliveryAdapter, OriginDeliveryAdapter, WebhookDeliveryAdapter

    registry = DeliveryRegistry()
    registry.register(LocalDeliveryAdapter())
    registry.register(OriginDeliveryAdapter())
    registry.register(WebhookDeliveryAdapter(sender=webhook_sender))
    for adapter in extra_adapters or []:
        registry.register(adapter)
    for factory in list(_ADAPTER_FACTORIES):
        adapter = factory(webhook_sender=webhook_sender)
        if adapter is not None:
            registry.register(adapter)
    return registry


def default_delivery_registry(*, webhook_sender=None) -> DeliveryRegistry:
    return build_delivery_registry(webhook_sender=webhook_sender)
```

- [ ] **Step 4: Verify registry users still call the compatibility wrapper**

Run:

```bash
rg -n "default_delivery_registry\\(|build_delivery_registry\\(" cron agent_cli agent_tools tests
```

Expected: existing callers may still use `default_delivery_registry()`. New tests should use `build_delivery_registry()` directly where they verify the canonical API.

- [ ] **Step 5: Run registry extension tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_delivery_targets.py::test_build_delivery_registry_includes_registered_adapter_factory \
  tests/test_cron_delivery.py::test_process_due_uses_registry_factory_for_platform_adapter \
  -q
```

Expected: pass.

- [ ] **Step 6: Run delivery target and delivery suites**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery_targets.py tests/test_cron_delivery.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add cron/delivery_registry.py cron/delivery.py cron/delivery_dispatcher.py tests/test_cron_delivery_targets.py tests/test_cron_delivery.py
git commit -m "feat: add cron delivery registry extension hooks"
```

---

### Task 3: Improve Tool, Status, Doctor, And Error Feedback

**Files:**
- Modify: `cron/delivery_registry.py`
- Modify: `agent_cli/cron_commands.py`
- Modify: `agent_tools/public/cronjob.py`
- Test: `tests/test_agent_cli_cron_commands.py`
- Test: `tests/test_cronjob_tool.py`

- [ ] **Step 1: Write unsupported target known-adapters test**

Add to `tests/test_cronjob_tool.py` near delivery validation tests:

```python
def test_cronjob_unsupported_delivery_lists_known_adapters():
    cronjob_tool = _cronjob_tool()

    result = cronjob_tool._cronjob_impl(
        action="create",
        prompt="write report",
        schedule="30m",
        deliver="telegram:123",
        runtime=None,
    )

    assert result["success"] is False
    assert result["code"] == "unsupported_delivery"
    assert "unsupported delivery target: telegram:123" in result["error"]
    assert "known adapters:" in result["error"]
    assert "webhook" in result["error"]
```

- [ ] **Step 2: Write status/doctor adapter visibility tests**

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_status_lists_registered_delivery_adapters(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_status
    from cron.delivery_adapters import AdapterValidation, DeliveryResult
    import cron.delivery_registry as delivery_registry

    class FakeSlackAdapter:
        key = "slack"
        active_dispatch = True

        def validate(self, target, job):
            return AdapterValidation(True)

        def deliver(self, event, job, run):
            return DeliveryResult(True)

    delivery_registry.clear_delivery_adapter_factories()
    delivery_registry.register_delivery_adapter_factory(lambda **kwargs: FakeSlackAdapter())
    try:
        result = cron_status()
    finally:
        delivery_registry.clear_delivery_adapter_factories()

    assert "Delivery adapters:" in result.message
    assert "slack" in result.message
```

Add:

```python
def test_cron_doctor_lists_delivery_adapters(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_doctor

    result = cron_doctor()

    assert "delivery adapters:" in result.message
    assert "local" in result.message
    assert "origin" in result.message
    assert "webhook" in result.message
```

- [ ] **Step 3: Run UX tests and verify red**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cronjob_tool.py::test_cronjob_unsupported_delivery_lists_known_adapters \
  tests/test_agent_cli_cron_commands.py::test_cron_status_lists_registered_delivery_adapters \
  tests/test_agent_cli_cron_commands.py::test_cron_doctor_lists_delivery_adapters \
  -q
```

Expected: unsupported error and doctor wording tests fail until implementation is added.

- [ ] **Step 4: Add registry error formatting**

In `cron/delivery_registry.py`, add:

```python
def known_adapter_error(registry: DeliveryRegistry, error: str | None) -> str:
    known = ", ".join(registry.adapter_keys()) or "-"
    base = error or "unsupported delivery target"
    return f"{base} (known adapters: {known})"
```

- [ ] **Step 5: Use known adapter error in cronjob tool validation**

In `agent_tools/public/cronjob.py`, change `_validate_delivery()` so it keeps the registry object and formats unsupported errors:

```python
from cron.delivery_registry import default_delivery_registry, known_adapter_error
from cron.delivery_targets import DeliveryIdentity

registry = default_delivery_registry()
validation = registry.validate_targets(
    str(deliver),
    origin=DeliveryIdentity.from_job_origin(origin),
    job={},
)
if validation.ok:
    return None
code = "invalid_webhook" if validation.error and "webhook URL" in validation.error else "unsupported_delivery"
error = validation.error or f"Unsupported delivery target: {deliver}"
if code == "unsupported_delivery":
    error = known_adapter_error(registry, error)
return {
    "success": False,
    "code": code,
    "error": error,
}
```

- [ ] **Step 6: Improve doctor adapter line**

In `agent_cli/cron_commands.py`, inside `cron_doctor()`, add after runner checks or near delivery checks:

```python
registry = default_delivery_registry()
adapter_keys = registry.adapter_keys()
required = {"local", "origin", "webhook"}
missing = sorted(required - set(adapter_keys))
if missing:
    add("fail", f"delivery adapters: missing {', '.join(missing)} (registered: {', '.join(adapter_keys) or '-'})")
else:
    add("ok", f"delivery adapters: {', '.join(adapter_keys)}")
```

If `cron_doctor()` already creates a registry variable later, reuse one variable and avoid duplicate construction.

- [ ] **Step 7: Run UX tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cronjob_tool.py::test_cronjob_unsupported_delivery_lists_known_adapters \
  tests/test_agent_cli_cron_commands.py::test_cron_status_lists_registered_delivery_adapters \
  tests/test_agent_cli_cron_commands.py::test_cron_doctor_lists_delivery_adapters \
  -q
```

Expected: pass.

- [ ] **Step 8: Run command/tool suites**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cronjob_tool.py tests/test_agent_cli_cron_commands.py -q
```

Expected: all pass.

- [ ] **Step 9: Commit Task 3**

```bash
git add cron/delivery_registry.py agent_tools/public/cronjob.py agent_cli/cron_commands.py tests/test_cronjob_tool.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: expose cron delivery adapter capabilities"
```

---

### Task 4: Add Stable Origin Identity Extraction

**Files:**
- Modify: `agent_core/session_context.py`
- Modify: `agent_tools/public/cronjob.py`
- Test: `tests/test_session_context.py`
- Test: `tests/test_cronjob_tool.py`

- [ ] **Step 1: Write RuntimeContext origin identity tests**

Add to `tests/test_session_context.py`:

```python
from types import SimpleNamespace


def test_runtime_context_extracts_cli_origin_identity_from_config_thread():
    from agent_core.session_context import origin_identity_from_runtime

    runtime = SimpleNamespace(config={"configurable": {"thread_id": "thread-1", "session_id": "session-1"}})

    assert origin_identity_from_runtime(runtime) == {
        "source_type": "cli",
        "session_id": "session-1",
        "thread_id": "thread-1",
    }


def test_runtime_context_extracts_gateway_origin_identity_from_config():
    from agent_core.session_context import origin_identity_from_runtime

    runtime = SimpleNamespace(
        config={
            "configurable": {
                "source_type": "gateway",
                "platform": "slack",
                "chat_id": "C123",
                "thread_id": "T456",
                "session_id": "gateway-session-1",
                "display_name": "ops",
            }
        }
    )

    assert origin_identity_from_runtime(runtime) == {
        "source_type": "gateway",
        "platform": "slack",
        "chat_id": "C123",
        "thread_id": "T456",
        "session_id": "gateway-session-1",
        "display_name": "ops",
    }


def test_runtime_context_extracts_web_origin_identity_from_config():
    from agent_core.session_context import origin_identity_from_runtime

    runtime = SimpleNamespace(config={"configurable": {"source_type": "web", "session_id": "web-session-1"}})

    assert origin_identity_from_runtime(runtime) == {
        "source_type": "web",
        "session_id": "web-session-1",
    }
```

- [ ] **Step 2: Write cronjob tool origin identity tests**

Replace `test_cronjob_create_captures_runtime_thread` expectations in `tests/test_cronjob_tool.py` with session-aware identity:

```python
runtime = SimpleNamespace(config={"configurable": {"thread_id": "thread-1", "session_id": "session-1"}})
...
assert created["origin"] == {
    "source_type": "cli",
    "session_id": "session-1",
    "thread_id": "thread-1",
}
```

Add:

```python
def test_cronjob_create_captures_gateway_origin_identity(monkeypatch):
    cronjob_tool = _cronjob_tool()
    created = {}

    def fake_create_job(**kwargs):
        created.update(kwargs)
        return _job(skills=[], prompt=kwargs["prompt"], workdir=None, last_run_at=None, last_status=None)

    runtime = SimpleNamespace(
        config={
            "configurable": {
                "source_type": "gateway",
                "platform": "slack",
                "chat_id": "C123",
                "thread_id": "T456",
                "session_id": "gateway-session-1",
            }
        }
    )
    monkeypatch.setattr(cronjob_tool, "create_job", fake_create_job)

    result = cronjob_tool._cronjob_impl(
        action="create",
        prompt="write report",
        schedule="30m",
        runtime=runtime,
    )

    assert result["success"] is True
    assert created["origin"] == {
        "source_type": "gateway",
        "platform": "slack",
        "chat_id": "C123",
        "thread_id": "T456",
        "session_id": "gateway-session-1",
    }
```

Add:

```python
def test_cronjob_create_captures_web_origin_identity(monkeypatch):
    cronjob_tool = _cronjob_tool()
    created = {}

    def fake_create_job(**kwargs):
        created.update(kwargs)
        return _job(skills=[], prompt=kwargs["prompt"], workdir=None, last_run_at=None, last_status=None)

    runtime = SimpleNamespace(config={"configurable": {"source_type": "web", "session_id": "web-session-1"}})
    monkeypatch.setattr(cronjob_tool, "create_job", fake_create_job)

    result = cronjob_tool._cronjob_impl(
        action="create",
        prompt="write report",
        schedule="30m",
        runtime=runtime,
    )

    assert result["success"] is True
    assert created["origin"] == {
        "source_type": "web",
        "session_id": "web-session-1",
    }
```

- [ ] **Step 3: Run origin identity tests and verify red**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_session_context.py::test_runtime_context_extracts_cli_origin_identity_from_config_thread \
  tests/test_session_context.py::test_runtime_context_extracts_gateway_origin_identity_from_config \
  tests/test_session_context.py::test_runtime_context_extracts_web_origin_identity_from_config \
  tests/test_cronjob_tool.py::test_cronjob_create_captures_gateway_origin_identity \
  tests/test_cronjob_tool.py::test_cronjob_create_captures_web_origin_identity \
  -q
```

Expected: fail because `origin_identity_from_runtime()` does not exist and cronjob tool still uses thread-only origin construction.

- [ ] **Step 4: Implement origin identity helper**

In `agent_core/session_context.py`, add:

```python
def origin_identity_from_runtime(runtime: Any | None) -> dict[str, str] | None:
    config = getattr(runtime, "config", None)
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None

    source_type = str(configurable.get("source_type") or "cli")
    identity = {
        "source_type": source_type,
        "platform": configurable.get("platform"),
        "chat_id": configurable.get("chat_id"),
        "thread_id": configurable.get("thread_id"),
        "session_id": configurable.get("session_id"),
        "display_name": configurable.get("display_name") or configurable.get("chat_name"),
    }
    if source_type == "cli" and not identity["session_id"]:
        identity["session_id"] = identity["thread_id"]
    if source_type == "cli" and not identity["thread_id"]:
        return None
    if source_type == "gateway" and not (identity["platform"] and identity["chat_id"]):
        return None
    if source_type == "web" and not identity["session_id"]:
        return None
    return {key: str(value) for key, value in identity.items() if value is not None}
```

- [ ] **Step 5: Wire cronjob tool to origin identity helper**

In `agent_tools/public/cronjob.py`, import:

```python
from agent_core.session_context import RuntimeContext, origin_identity_from_runtime
```

Replace `_runtime_thread_id()` and `_origin_identity_from_thread()` usage with `_runtime_origin_identity()`:

```python
def _runtime_origin_identity(runtime: ToolRuntime | None, origin_thread_id: str | None = None) -> dict[str, str] | None:
    identity = origin_identity_from_runtime(runtime)
    if identity is not None:
        return identity
    if origin_thread_id:
        return {
            "source_type": "cli",
            "session_id": str(origin_thread_id),
            "thread_id": str(origin_thread_id),
        }
    return None
```

In `_cronjob_impl()` create path, replace:

```python
thread_id = origin_thread_id or _runtime_thread_id(runtime)
if _deliver_mentions_origin(deliver) and not thread_id:
...
origin = _origin_identity_from_thread(thread_id) if thread_id and (deliver is None or _deliver_mentions_origin(deliver)) else None
```

with:

```python
origin = _runtime_origin_identity(runtime, origin_thread_id)
if _deliver_mentions_origin(deliver) and origin is None:
    return {
        "success": False,
        "code": "missing_origin_thread",
        "error": "deliver='origin' requires an active origin identity.",
    }
if deliver is not None and not _deliver_mentions_origin(deliver):
    origin = None
```

In update path, replace origin update logic similarly:

```python
if _deliver_mentions_origin(updates.get("deliver")):
    origin = _runtime_origin_identity(runtime, origin_thread_id)
    if origin is None:
        return {
            "success": False,
            "code": "missing_origin_thread",
            "error": "deliver='origin' requires an active origin identity.",
        }
    updates["origin"] = origin
elif "deliver" in updates:
    updates["origin"] = None
```

- [ ] **Step 6: Run origin identity tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_session_context.py tests/test_cronjob_tool.py -q
```

Expected: all pass. If existing tests intentionally require ignoring `execution_info.thread_id` without config thread, keep that behavior: `origin_identity_from_runtime()` should read config metadata only.

- [ ] **Step 7: Commit Task 4**

```bash
git add agent_core/session_context.py agent_tools/public/cronjob.py tests/test_session_context.py tests/test_cronjob_tool.py
git commit -m "feat: persist structured cron origin identity"
```

---

### Task 5: Persist And Display Origin Poll State

**Files:**
- Modify: `cron/delivery_store.py`
- Modify: `cron/state_store.py`
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_cron_delivery_store.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write origin pending store/status tests**

Add to `tests/test_cron_delivery_store.py`:

```python
def test_origin_pending_stats_counts_origin_events(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_store import DeliveryStore

    enqueue_result(
        {
            "id": "job-origin",
            "name": "Origin",
            "deliver": "origin",
            "origin": {"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
        },
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert DeliveryStore().origin_pending_count() == 1
```

Add to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_status_labels_origin_poll_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_status
    from cron.delivery import JobRunResult, enqueue_result

    enqueue_result(
        {
            "id": "job-origin",
            "name": "Origin",
            "deliver": "origin",
            "origin": {"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
        },
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    result = cron_status()

    assert "Origin poll pending: 1" in result.message
```

- [ ] **Step 2: Run origin status tests and verify red**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_delivery_store.py::test_origin_pending_stats_counts_origin_events \
  tests/test_agent_cli_cron_commands.py::test_cron_status_labels_origin_poll_pending \
  -q
```

Expected: fail because `origin_pending_count()` and status line do not exist.

- [ ] **Step 3: Add origin pending count store helper**

In `cron/state_store.py`, add:

```python
def origin_pending_count(self) -> int:
    with self._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM delivery_events WHERE adapter_key = 'origin' AND status = 'pending'"
        ).fetchone()
    return int(row["count"])
```

In `cron/delivery_store.py`, add:

```python
def origin_pending_count(self) -> int:
    return self._store.origin_pending_count()
```

- [ ] **Step 4: Add status line**

In `agent_cli/cron_commands.py`, update `_delivery_stats_lines()`:

```python
origin_pending = store.origin_pending_count()
if origin_pending:
    lines.append(f"Origin poll pending: {origin_pending}")
```

- [ ] **Step 5: Run origin status tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_delivery_store.py::test_origin_pending_stats_counts_origin_events \
  tests/test_agent_cli_cron_commands.py::test_cron_status_labels_origin_poll_pending \
  -q
```

Expected: pass.

- [ ] **Step 6: Run status/store suites**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery_store.py tests/test_agent_cli_cron_commands.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit Task 5**

```bash
git add cron/state_store.py cron/delivery_store.py agent_cli/cron_commands.py tests/test_cron_delivery_store.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: label origin poll delivery state"
```

---

## Final Verification

- [ ] Run delivery-focused suites:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_cron_delivery.py \
  tests/test_cron_delivery_dispatcher.py \
  tests/test_cron_delivery_targets.py \
  tests/test_cron_delivery_store.py \
  tests/test_cronjob_tool.py \
  tests/test_agent_cli_cron_commands.py \
  tests/test_session_context.py \
  -q
```

Expected: all pass.

- [ ] Run cron jobs excluding the two known croniter/dateutil-stub tests if they still fail in this environment:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_jobs.py -q -k 'not parse_interval_cron_and_iso_schedules and not advance_next_run_once_and_cron'
```

Expected: pass.

- [ ] Run boundary check:

```bash
/home/miku/miniforge3/envs/langchain/bin/python - <<'PY'
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

Expected:

```text
enqueue_result boundary check passed
```

- [ ] Run git status:

```bash
git status --short
```

Expected: no uncommitted files from this plan. Pre-existing unrelated untracked files may remain outside this plan's write set.

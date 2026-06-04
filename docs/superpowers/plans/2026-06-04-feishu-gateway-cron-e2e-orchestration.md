# Feishu Gateway Cron E2E Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a true Feishu inbound-to-cron-to-Feishu outbound acceptance test, default gateway-origin cron reports to new Feishu chat messages, preserve explicit Feishu thread targets, and add `gateway service install --with-cron`.

**Architecture:** Keep the current dual-service model. Cron origin delivery still uses delivery adapters and gateway registry, but gateway-origin delivery suppresses `thread_id` only at the final send target. Service orchestration is install-time only: gateway install runs first, and cron install runs second only when `--with-cron` is present.

**Tech Stack:** Python, pytest, argparse, sqlite-backed cron/gateway stores, existing `GatewayInboxWorker`, `GatewayDispatcher`, `run_cronjob_action`, `CronService`, and delivery adapter registry.

---

## File Map

- Modify: `cron/delivery_targets.py`
  - Parse `feishu:<chat_id>:<thread_id>` into `DeliveryTarget(address=<chat_id>, thread_id=<thread_id>)`.
- Modify: `cron/delivery_adapters.py`
  - Pass explicit Feishu target `thread_id` through `FeishuDeliveryAdapter`.
  - Suppress `origin.thread_id` for gateway-origin default sends in `OriginDeliveryAdapter` and `GatewayOriginDeliveryAdapter`.
- Modify: `tests/test_cron_delivery_targets.py`
  - Add parser coverage for explicit Feishu thread targets.
- Modify: `tests/test_cron_e2e.py`
  - Add or update E2E coverage for explicit `feishu:<chat_id>:<thread_id>`.
- Modify: `tests/test_cron_origin_gateway_delivery.py`
  - Update origin delivery assertions to expect `thread_id is None`.
  - Add `GatewayOriginDeliveryAdapter` parity coverage.
- Modify: `tests/test_gateway_feishu_ws_e2e.py`
  - Rename/update the old thread-reply expectation.
  - Add the full Feishu inbound -> gateway runtime origin -> cron job create -> cron service tick -> origin delivery -> Feishu send acceptance test.
- Modify: `agent_cli/main.py`
  - Add `--with-cron` to `gateway service install`.
- Modify: `agent_cli/command_handlers/gateway.py`
  - Install cron service after gateway service install when requested.
- Modify: `tests/test_gateway_cli.py`
  - Add handler tests for `--with-cron` success and failure behavior.
- Modify: `tests/test_agent_cli_main.py`
  - Add parser/CLI routing coverage for `gateway service install --with-cron`.

---

### Task 1: Preserve Explicit Feishu Thread Targets

**Files:**
- Modify: `tests/test_cron_delivery_targets.py`
- Modify: `tests/test_cron_e2e.py`
- Modify: `cron/delivery_targets.py`
- Modify: `cron/delivery_adapters.py`

- [ ] **Step 1: Add failing parser test for explicit Feishu thread targets**

Add this test to `tests/test_cron_delivery_targets.py`:

```python
def test_parse_feishu_target_with_thread_id():
    from cron.delivery_targets import parse_delivery_targets

    targets = parse_delivery_targets("feishu:oc_123:mid_456", origin=None)

    assert len(targets) == 1
    assert targets[0].adapter_key == "feishu"
    assert targets[0].target_type == "platform"
    assert targets[0].address == "oc_123"
    assert targets[0].thread_id == "mid_456"
```

- [ ] **Step 2: Run parser test and verify it fails**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery_targets.py::test_parse_feishu_target_with_thread_id -q
```

Expected: FAIL because the current `feishu:` branch treats `oc_123:mid_456` as the address and leaves `thread_id` unset.

- [ ] **Step 3: Implement Feishu target parsing**

In `cron/delivery_targets.py`, replace the current `lowered.startswith("feishu:")` branch with:

```python
    if lowered.startswith("feishu:"):
        rest = raw.split(":", 1)[1]
        chat_id, sep, thread_id = rest.partition(":")
        normalized_thread_id = thread_id.strip() if sep else None
        return DeliveryTarget(
            raw=raw,
            target_type="platform",
            adapter_key="feishu",
            address=chat_id.strip() or None,
            thread_id=normalized_thread_id or None,
        )
```

- [ ] **Step 4: Run parser test and verify it passes**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery_targets.py::test_parse_feishu_target_with_thread_id -q
```

Expected: PASS.

- [ ] **Step 5: Add failing explicit Feishu delivery E2E assertion**

In `tests/test_cron_e2e.py`, update `test_service_executes_due_job_and_delivers_feishu_text` to create a threaded explicit target:

```python
    job = create_due_job(deliver="feishu:oc_123:mid_456")
```

Update the target assertion in that same test to:

```python
    assert target == PlatformMessageTarget(
        platform="feishu",
        target_type="chat_id",
        target_id="oc_123",
        thread_id="mid_456",
    )
```

- [ ] **Step 6: Run explicit Feishu delivery test and verify it fails**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py::test_service_executes_due_job_and_delivers_feishu_text -q
```

Expected: FAIL because `FeishuDeliveryAdapter.deliver()` does not pass `event["thread_id"]` into `PlatformMessageTarget`.

- [ ] **Step 7: Pass explicit thread_id through FeishuDeliveryAdapter**

In `cron/delivery_adapters.py`, update `FeishuDeliveryAdapter.validate()` to validate the explicit target with its thread id:

```python
        result = adapter.validate_target(
            PlatformMessageTarget(
                platform="feishu",
                target_type="chat_id",
                target_id=str(target.address),
                thread_id=target.thread_id,
            )
        )
```

Update `FeishuDeliveryAdapter.deliver()` to pass the delivery event thread:

```python
        result = adapter.send_text(
            PlatformMessageTarget(
                platform="feishu",
                target_type="chat_id",
                target_id=str(event["address"]),
                thread_id=event.get("thread_id"),
            ),
            OutboundMessage(text=_format_feishu_text(event, job, run)),
        )
```

- [ ] **Step 8: Run explicit Feishu target tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery_targets.py::test_parse_feishu_target_with_thread_id tests/test_cron_e2e.py::test_service_executes_due_job_and_delivers_feishu_text -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add cron/delivery_targets.py cron/delivery_adapters.py tests/test_cron_delivery_targets.py tests/test_cron_e2e.py
git commit -m "fix: preserve explicit feishu cron thread targets"
```

---

### Task 2: Default Gateway-Origin Cron Delivery to New Feishu Messages

**Files:**
- Modify: `tests/test_cron_origin_gateway_delivery.py`
- Modify: `tests/test_gateway_feishu_ws_e2e.py`
- Modify: `cron/delivery_adapters.py`

- [ ] **Step 1: Update failing origin delivery expectation**

In `tests/test_cron_origin_gateway_delivery.py`, update `test_origin_delivery_sends_gateway_origin`:

```python
        assert target.thread_id is None
```

Keep the job origin unchanged:

```python
            "origin": {
                "source_type": "gateway",
                "platform": "feishu",
                "chat_id": "oc_123",
                "thread_id": "thread-1",
            },
```

- [ ] **Step 2: Add GatewayOriginDeliveryAdapter parity test**

Add this test to `tests/test_cron_origin_gateway_delivery.py`:

```python
def test_gateway_origin_delivery_sends_new_chat_message(monkeypatch):
    import gateway.registry as gateway_registry
    from cron.delivery_adapters import GatewayOriginDeliveryAdapter

    fake = FakeGatewayAdapter()
    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: fake)
    try:
        adapter = GatewayOriginDeliveryAdapter()
        job = {
            "id": "job-1",
            "origin": {
                "source_type": "gateway",
                "platform": "feishu",
                "chat_id": "oc_123",
                "thread_id": "thread-1",
            },
        }

        result = adapter.deliver(delivery_event(), job, {"id": "run-1"})

        assert result.delivered is True
        target, message = fake.sent[0]
        assert target.platform == "feishu"
        assert target.target_type == "chat_id"
        assert target.target_id == "oc_123"
        assert target.thread_id is None
        assert "Daily" in message.text
        assert "done" in message.text
    finally:
        gateway_registry.clear_gateway_adapter_factories()
```

- [ ] **Step 3: Update old Feishu WS origin delivery test expectation**

In `tests/test_gateway_feishu_ws_e2e.py`, rename:

```python
def test_feishu_ws_origin_delivery_sends_cron_result_to_original_thread(monkeypatch, tmp_path):
```

to:

```python
def test_feishu_ws_origin_delivery_sends_cron_result_to_original_chat(monkeypatch, tmp_path):
```

Replace the URL reply assertion:

```python
        assert any("mid-1" in item[0] for item in sent)
```

with:

```python
        assert sent
        assert "/im/v1/messages?receive_id_type=chat_id" in sent[-1][0]
        assert "mid-1" not in sent[-1][0]
```

- [ ] **Step 4: Run origin tests and verify they fail**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_origin_gateway_delivery.py::test_origin_delivery_sends_gateway_origin tests/test_cron_origin_gateway_delivery.py::test_gateway_origin_delivery_sends_new_chat_message tests/test_gateway_feishu_ws_e2e.py::test_feishu_ws_origin_delivery_sends_cron_result_to_original_chat -q
```

Expected: FAIL because both origin delivery adapters currently pass `origin["thread_id"]` to `PlatformMessageTarget`.

- [ ] **Step 5: Suppress thread_id for gateway-origin default sends**

In `cron/delivery_adapters.py`, update both `OriginDeliveryAdapter.deliver()` and `GatewayOriginDeliveryAdapter.deliver()` target construction to:

```python
        target = PlatformMessageTarget(
            platform=platform,
            target_type="chat_id",
            target_id=chat_id,
            thread_id=None,
        )
```

- [ ] **Step 6: Run origin tests and verify they pass**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_origin_gateway_delivery.py tests/test_gateway_feishu_ws_e2e.py::test_feishu_ws_origin_delivery_sends_cron_result_to_original_chat -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add cron/delivery_adapters.py tests/test_cron_origin_gateway_delivery.py tests/test_gateway_feishu_ws_e2e.py
git commit -m "fix: send gateway cron reports as new feishu messages"
```

---

### Task 3: Add True Feishu Gateway Cron E2E Acceptance Test

**Files:**
- Modify: `tests/test_gateway_feishu_ws_e2e.py`

- [ ] **Step 1: Add helper classes/imports to E2E test file**

At the top of `tests/test_gateway_feishu_ws_e2e.py`, add:

```python
from types import SimpleNamespace
```

Add these helpers below the imports:

```python
BASE_TIME = "2026-06-02T10:00:00+00:00"


class RecordingFeishuGatewayAdapter:
    key = "feishu"

    def __init__(self):
        from gateway.contracts import SendResult

        self.calls = []
        self.default_result = SendResult(ok=True)

    def validate_target(self, target):
        from gateway.contracts import SendResult

        if target.platform != "feishu":
            return SendResult(ok=False, error="wrong platform")
        if target.target_type != "chat_id":
            return SendResult(ok=False, error="wrong target type")
        if not target.target_id:
            return SendResult(ok=False, error="missing chat_id")
        return SendResult(ok=True)

    def token_smoke(self):
        from gateway.contracts import SendResult

        return SendResult(ok=True)

    def send_text(self, target, message):
        self.calls.append((target, message))
        return self.default_result


class RecordingCronRunner:
    def __init__(self, final_response="scheduled report ready"):
        self.final_response = final_response
        self.calls = []

    def __call__(self, job):
        from cron.contracts import JobRunResult

        self.calls.append(dict(job))
        return JobRunResult(
            success=True,
            output_doc="# Cron Output\nbody",
            final_response=self.final_response,
            error=None,
        )
```

- [ ] **Step 2: Add the full E2E test**

Add this test to `tests/test_gateway_feishu_ws_e2e.py`:

```python
def test_feishu_inbound_creates_origin_cron_and_delivers_report_to_chat(monkeypatch, tmp_path):
    import cron.delivery_store as delivery_store_module
    import cron.state_store as state_store_module
    import gateway.registry as gateway_registry
    from agent_tools.public.cronjob import run_cronjob_action
    from cron.delivery_store import DeliveryStore
    from cron.jobs import get_job, update_job
    from cron.service import CronService
    import cron.scheduler as scheduler
    from cron.state_store import StateStore
    from gateway.dispatch import GatewayDispatcher, gateway_agent_config
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    gateway_registry.clear_gateway_adapter_factories()
    feishu = RecordingFeishuGatewayAdapter()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: feishu)

    registry = GatewayRegistry()
    registry.register(feishu)
    created = {}

    def gateway_runner(event, session, origin):
        runtime = SimpleNamespace(config=gateway_agent_config(origin))
        result = run_cronjob_action(
            "create",
            runtime=runtime,
            schedule="every 30m",
            prompt="write the Feishu report",
            name="Feishu Report",
            deliver="origin",
        )
        assert result["success"] is True
        created["job_id"] = result["job_id"]
        return "Cron job Feishu Report created."

    try:
        dispatcher = GatewayDispatcher(
            store=GatewaySessionStore(tmp_path / "gateway" / "gateway.sqlite"),
            registry=registry,
            runner=gateway_runner,
        )
        service = GatewayService(home=tmp_path, registry=registry, dispatch=dispatcher.dispatch)
        inbox = GatewayInboxStore(tmp_path / "gateway" / "gateway.sqlite")
        payload = {
            "header": {
                "event_id": "evt-e2e",
                "event_type": "im.message.receive_v1",
                "create_time": "1760000000000",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}, "sender_name": "Miku"},
                "message": {
                    "message_id": "mid-create",
                    "chat_id": "oc_e2e",
                    "message_type": "text",
                    "content": '{"text":"create a report every 30 minutes"}',
                },
            },
        }

        event = normalize_feishu_ws_event(payload)
        assert event is not None
        inbox.enqueue(event)
        processed = GatewayInboxWorker(store=inbox, dispatch=service.handle_event, sleeper=lambda _: None).run_once()

        assert processed == 1
        job = get_job(created["job_id"])
        assert job is not None
        assert job["deliver"] == "origin"
        assert job["origin"]["source_type"] == "gateway"
        assert job["origin"]["platform"] == "feishu"
        assert job["origin"]["chat_id"] == "oc_e2e"
        assert job["origin"]["thread_id"] == "mid-create"
        assert job["origin"]["session_id"]

        due_job = update_job(
            job["id"],
            {"next_run_at": BASE_TIME, "state": "scheduled", "enabled": True},
        )
        assert due_job is not None

        now = state_store_module.datetime.fromisoformat(BASE_TIME)
        original_state_utc_now = state_store_module.utc_now
        original_delivery_utc_now = delivery_store_module.utc_now
        runner = RecordingCronRunner(final_response="scheduled report ready")
        tick_result = None

        def tick_fn():
            nonlocal tick_result
            tick_result = scheduler.tick(now_text=BASE_TIME, job_runner=runner)
            return tick_result

        try:
            state_store_module.utc_now = lambda: now
            delivery_store_module.utc_now = lambda: now
            cron_service = CronService(
                interval_seconds=1,
                lease_seconds=60,
                owner_id="gateway-e2e",
                pid=4242,
                hostname="gateway-e2e-host",
                tick_fn=tick_fn,
                clock=lambda: BASE_TIME,
                sleeper=lambda _seconds: None,
            )
            exit_code = cron_service.run(once=True)
        finally:
            state_store_module.utc_now = original_state_utc_now
            delivery_store_module.utc_now = original_delivery_utc_now

        store = StateStore()
        delivery_store = DeliveryStore()
        runs = store.runs_for_job(job["id"])
        events = delivery_store.list_events(job_id=job["id"], limit=10)
        target, message = feishu.calls[-1]

        assert exit_code == 0
        assert tick_result.ran == 1
        assert len(runner.calls) == 1
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["delivery_status"] == "delivered"
        assert len(events) == 1
        assert events[0]["adapter_key"] == "origin"
        assert events[0]["status"] == "delivered"
        assert target.platform == "feishu"
        assert target.target_type == "chat_id"
        assert target.target_id == "oc_e2e"
        assert target.thread_id is None
        assert "scheduled report ready" in message.text
    finally:
        gateway_registry.clear_gateway_adapter_factories()
```

- [ ] **Step 3: Run the new E2E test**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_feishu_ws_e2e.py::test_feishu_inbound_creates_origin_cron_and_delivers_report_to_chat -q
```

Expected: PASS if Tasks 1 and 2 are complete. If it fails due to `datetime` lookup, replace `state_store_module.datetime.fromisoformat(BASE_TIME)` with an explicit import:

```python
from datetime import datetime
now = datetime.fromisoformat(BASE_TIME)
```

- [ ] **Step 4: Run all gateway Feishu WS E2E tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_feishu_ws_e2e.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add tests/test_gateway_feishu_ws_e2e.py
git commit -m "test: cover feishu gateway cron origin e2e"
```

---

### Task 4: Add Gateway Service Install `--with-cron`

**Files:**
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/command_handlers/gateway.py`
- Modify: `tests/test_gateway_cli.py`
- Modify: `tests/test_agent_cli_main.py`

- [ ] **Step 1: Add handler tests for install orchestration**

Add these tests to `tests/test_gateway_cli.py`:

```python
def test_gateway_service_install_with_cron_installs_gateway_then_cron(monkeypatch, capsys):
    from types import SimpleNamespace
    from agent_cli.command_handlers.gateway import handle_gateway_service
    from gateway.service_manager import GatewayServiceManagerResult
    from cron.service_manager import ServiceCommandResult

    calls = []

    def fake_gateway_install(*, transport, force):
        calls.append(("gateway", transport, force))
        return GatewayServiceManagerResult("Installed gateway service at /tmp/gateway.service.", 0)

    def fake_cron_install(*, interval_seconds, lease_seconds, force):
        calls.append(("cron", interval_seconds, lease_seconds, force))
        return ServiceCommandResult("Installed cron service at /tmp/cron.service.", 0)

    monkeypatch.setattr("gateway.service_manager.install_service", fake_gateway_install)
    monkeypatch.setattr("cron.service_manager.install_service", fake_cron_install)

    exit_code = handle_gateway_service(
        SimpleNamespace(transport="feishu-ws", force=True, with_cron=True),
        "install",
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls == [
        ("gateway", "feishu-ws", True),
        ("cron", 60, 180, True),
    ]
    assert "Installed gateway service" in output
    assert "Installed cron service" in output


def test_gateway_service_install_with_cron_skips_cron_when_gateway_fails(monkeypatch, capsys):
    from types import SimpleNamespace
    from agent_cli.command_handlers.gateway import handle_gateway_service
    from gateway.service_manager import GatewayServiceManagerResult

    calls = []

    def fake_gateway_install(*, transport, force):
        calls.append(("gateway", transport, force))
        return GatewayServiceManagerResult("gateway install failed", 2)

    def fake_cron_install(**kwargs):
        raise AssertionError("cron install must not run when gateway install fails")

    monkeypatch.setattr("gateway.service_manager.install_service", fake_gateway_install)
    monkeypatch.setattr("cron.service_manager.install_service", fake_cron_install)

    exit_code = handle_gateway_service(
        SimpleNamespace(transport="feishu-ws", force=False, with_cron=True),
        "install",
    )

    output = capsys.readouterr().out
    assert exit_code == 2
    assert calls == [("gateway", "feishu-ws", False)]
    assert "gateway install failed" in output


def test_gateway_service_install_with_cron_returns_cron_failure(monkeypatch, capsys):
    from types import SimpleNamespace
    from agent_cli.command_handlers.gateway import handle_gateway_service
    from gateway.service_manager import GatewayServiceManagerResult
    from cron.service_manager import ServiceCommandResult

    def fake_gateway_install(*, transport, force):
        return GatewayServiceManagerResult("Installed gateway service at /tmp/gateway.service.", 0)

    def fake_cron_install(*, interval_seconds, lease_seconds, force):
        return ServiceCommandResult("cron install failed", 3)

    monkeypatch.setattr("gateway.service_manager.install_service", fake_gateway_install)
    monkeypatch.setattr("cron.service_manager.install_service", fake_cron_install)

    exit_code = handle_gateway_service(
        SimpleNamespace(transport="feishu-ws", force=False, with_cron=True),
        "install",
    )

    output = capsys.readouterr().out
    assert exit_code == 3
    assert "Installed gateway service" in output
    assert "cron install failed" in output
```

- [ ] **Step 2: Add parser/routing test**

Add this test to `tests/test_agent_cli_main.py` near existing gateway service CLI tests:

```python
def test_gateway_service_install_accepts_with_cron(monkeypatch, capsys):
    import agent_cli.command_handlers.gateway as gateway_handler
    from agent_cli.main import main

    seen = {}

    def fake_handle(args, subcommand):
        seen["subcommand"] = subcommand
        seen["with_cron"] = args.with_cron
        seen["transport"] = args.transport
        seen["force"] = args.force
        return 0

    monkeypatch.setattr(gateway_handler, "handle_gateway_service", fake_handle)

    exit_code = main(["gateway", "service", "install", "--with-cron", "--force"])

    assert exit_code == 0
    assert seen == {
        "subcommand": "install",
        "with_cron": True,
        "transport": "feishu-ws",
        "force": True,
    }
```

- [ ] **Step 3: Run new service orchestration tests and verify they fail**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py::test_gateway_service_install_with_cron_installs_gateway_then_cron tests/test_gateway_cli.py::test_gateway_service_install_with_cron_skips_cron_when_gateway_fails tests/test_gateway_cli.py::test_gateway_service_install_with_cron_returns_cron_failure tests/test_agent_cli_main.py::test_gateway_service_install_accepts_with_cron -q
```

Expected: FAIL because `--with-cron` is not parsed and the handler ignores it.

- [ ] **Step 4: Add parser flag**

In `agent_cli/main.py`, add this line after the existing `gateway_service_install.add_argument("--force", action="store_true")`:

```python
    gateway_service_install.add_argument(
        "--with-cron",
        action="store_true",
        help="Also install the cron service after gateway service install succeeds.",
    )
```

- [ ] **Step 5: Implement handler orchestration**

In `agent_cli/command_handlers/gateway.py`, replace the `install` branch in `handle_gateway_service()` with:

```python
    if subcommand == "install":
        gateway_result = service_manager.install_service(
            transport=getattr(args, "transport", "feishu-ws"),
            force=bool(getattr(args, "force", False)),
        )
        if not bool(getattr(args, "with_cron", False)):
            return _print_result(gateway_result)
        if gateway_result.exit_code != 0:
            return _print_result(gateway_result)

        from cron import service_manager as cron_service_manager

        cron_result = cron_service_manager.install_service(
            interval_seconds=60,
            lease_seconds=180,
            force=bool(getattr(args, "force", False)),
        )
        print(gateway_result.message)
        print(cron_result.message)
        return cron_result.exit_code
```

- [ ] **Step 6: Run orchestration tests and verify they pass**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py::test_gateway_service_install_with_cron_installs_gateway_then_cron tests/test_gateway_cli.py::test_gateway_service_install_with_cron_skips_cron_when_gateway_fails tests/test_gateway_cli.py::test_gateway_service_install_with_cron_returns_cron_failure tests/test_agent_cli_main.py::test_gateway_service_install_accepts_with_cron -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add agent_cli/main.py agent_cli/command_handlers/gateway.py tests/test_gateway_cli.py tests/test_agent_cli_main.py
git commit -m "feat: orchestrate cron install from gateway service install"
```

---

### Task 5: Focused and Full Verification

**Files:**
- No code changes unless verification finds a bug.

- [ ] **Step 1: Run focused Feishu/gateway/cron tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery_targets.py tests/test_cron_origin_gateway_delivery.py tests/test_cron_e2e.py::test_service_executes_due_job_and_delivers_feishu_text tests/test_gateway_feishu_ws_e2e.py tests/test_gateway_cli.py tests/test_agent_cli_main.py::test_gateway_service_install_accepts_with_cron -q
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 3: Inspect git status**

Run:

```bash
git status --short --branch
```

Expected: only intended commits from Tasks 1-4, plus any unrelated pre-existing local changes that were present before this plan.

- [ ] **Step 4: Request final code review**

Use `superpowers:requesting-code-review` on the complete branch. Review focus:

- The new full E2E test truly covers Feishu inbound through final Feishu send.
- Gateway-origin delivery suppresses only the default send target `thread_id`, not stored origin metadata.
- Explicit `feishu:<chat_id>:<thread_id>` still delivers with `thread_id`.
- `gateway service install --with-cron` does not start cron and handles partial failure correctly.

# Feishu Gateway Cron Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Feishu WebSocket gateway visibly depend on the separate cron service, extend unified doctor diagnostics, and add Feishu WS event logging that explains received versus ignored events.

**Architecture:** Keep gateway and cron as separate services. Add small helper functions in the existing gateway CLI handler and doctor module instead of introducing a new command or service layer. Add a reason-returning Feishu WS normalization helper while preserving the existing `normalize_feishu_ws_event(payload)` API.

**Tech Stack:** Python 3.11, pytest, monkeypatch/capsys/caplog, existing `gateway`, `cron`, and `agent_cli` modules.

---

## File Structure

- Modify `agent_cli/command_handlers/gateway.py`
  - Add a non-blocking cron service health warning helper.
  - Call it from `gateway_feishu_ws()` after Feishu env validation and transport conflict check, before printing the listening line.

- Modify `agent_cli/doctor.py`
  - Add checks for cron service, Feishu token smoke, cron Feishu delivery readiness, gateway inbox stats, and cron delivery queue stats.
  - Strengthen gateway service check to include fresh status and transport where possible.
  - Add the new checks to `run_health_checks()`.

- Modify `gateway/transports/feishu_ws.py`
  - Add module logger.
  - Add `normalize_feishu_ws_event_with_reason(payload)`.
  - Keep `normalize_feishu_ws_event(payload)` as a wrapper.
  - Log received, enqueued, ignored, and exception paths in `on_message`.

- Modify `tests/test_gateway_cli.py`
  - Add tests for cron service warning behavior during `gateway_feishu_ws()`.

- Modify `tests/test_agent_cli_doctor.py`
  - Add tests for the new doctor checks.

- Modify `tests/test_gateway_feishu_ws_e2e.py`
  - Add tests for reason-returning normalization and log behavior.

---

### Task 1: Gateway Startup Cron Service Warning

**Files:**
- Modify: `agent_cli/command_handlers/gateway.py`
- Test: `tests/test_gateway_cli.py`

- [ ] **Step 1: Write failing tests for warning and non-blocking startup**

Append these tests to `tests/test_gateway_cli.py`:

```python
def test_gateway_feishu_ws_warns_when_cron_service_not_running(monkeypatch, tmp_path, capsys):
    from argparse import Namespace
    from types import SimpleNamespace

    import agent_cli.command_handlers.gateway as gateway_commands

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setattr(gateway_commands, "serve_feishu_ws_gateway", lambda **kwargs: None)

    fake_status = SimpleNamespace(
        supported=True,
        active=False,
        heartbeat_fresh=False,
        process_state="exited",
        last_error=None,
        detail="not running",
    )
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: fake_status,
    )

    result = gateway_commands.gateway_feishu_ws(Namespace(), home=tmp_path)
    output = capsys.readouterr().out

    assert result == 0
    assert "WARN Cron service is not running" in output
    assert "python3 -m agent_cli.main cron service start" in output
    assert "Gateway service listening with transport feishu-ws" in output


def test_gateway_feishu_ws_warns_with_cron_serve_on_unsupported_platform(monkeypatch, tmp_path, capsys):
    from argparse import Namespace
    from types import SimpleNamespace

    import agent_cli.command_handlers.gateway as gateway_commands

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setattr(gateway_commands, "serve_feishu_ws_gateway", lambda **kwargs: None)

    fake_status = SimpleNamespace(
        supported=False,
        active=False,
        heartbeat_fresh=False,
        process_state=None,
        last_error=None,
        detail="unsupported",
    )
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: fake_status,
    )

    result = gateway_commands.gateway_feishu_ws(Namespace(), home=tmp_path)
    output = capsys.readouterr().out

    assert result == 0
    assert "WARN Cron service is not available on this platform" in output
    assert "python3 -m agent_cli.main cron serve" in output


def test_gateway_feishu_ws_does_not_warn_when_cron_service_is_healthy(monkeypatch, tmp_path, capsys):
    from argparse import Namespace
    from types import SimpleNamespace

    import agent_cli.command_handlers.gateway as gateway_commands

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setattr(gateway_commands, "serve_feishu_ws_gateway", lambda **kwargs: None)

    fake_status = SimpleNamespace(
        supported=True,
        active=True,
        heartbeat_fresh=True,
        process_state="running",
        last_error=None,
        detail="running",
    )
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: fake_status,
    )

    result = gateway_commands.gateway_feishu_ws(Namespace(), home=tmp_path)
    output = capsys.readouterr().out

    assert result == 0
    assert "WARN Cron service" not in output
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py::test_gateway_feishu_ws_warns_when_cron_service_not_running tests/test_gateway_cli.py::test_gateway_feishu_ws_warns_with_cron_serve_on_unsupported_platform tests/test_gateway_cli.py::test_gateway_feishu_ws_does_not_warn_when_cron_service_is_healthy -q
```

Expected: FAIL because no cron warning helper exists yet.

- [ ] **Step 3: Implement cron warning helper**

In `agent_cli/command_handlers/gateway.py`, add this helper near `_refuse_if_other_transport()`:

```python
def _warn_if_cron_service_not_running() -> None:
    try:
        from cron.service_manager import compose_service_status

        status = compose_service_status()
    except Exception as exc:
        print(f"WARN Cron service status could not be checked: {exc}")
        print("Run: python3 -m agent_cli.main cron service status")
        return

    if not getattr(status, "supported", False):
        print("WARN Cron service is not available on this platform; scheduled cron jobs will not run automatically.")
        print("Run: python3 -m agent_cli.main cron serve")
        return

    healthy = (
        bool(getattr(status, "active", False))
        and bool(getattr(status, "heartbeat_fresh", False))
        and getattr(status, "process_state", None) == "running"
    )
    if healthy:
        return

    print("WARN Cron service is not running; scheduled cron jobs will not run automatically.")
    print("Run: python3 -m agent_cli.main cron service start")
```

Call it inside `gateway_feishu_ws()` after `_refuse_if_other_transport(...)` returns false and before creating `GatewayService`:

```python
    if _refuse_if_other_transport(home=target_home, desired="feishu-ws"):
        return 2
    _warn_if_cron_service_not_running()
    service = GatewayService(home=target_home)
```

- [ ] **Step 4: Run gateway CLI tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit task 1**

Run:

```bash
git add agent_cli/command_handlers/gateway.py tests/test_gateway_cli.py
git commit -m "feat: warn when cron service is not running for feishu ws"
```

---

### Task 2: Unified Doctor Gateway and Cron Diagnostics

**Files:**
- Modify: `agent_cli/doctor.py`
- Test: `tests/test_agent_cli_doctor.py`

- [ ] **Step 1: Write failing tests for cron service, inbox, and delivery queue checks**

Append these tests to `tests/test_agent_cli_doctor.py`:

```python
def test_doctor_reports_cron_service_unhealthy(monkeypatch):
    from types import SimpleNamespace

    from agent_cli.doctor import check_cron_service

    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: SimpleNamespace(
            supported=True,
            active=False,
            heartbeat_fresh=False,
            process_state="exited",
            leader_state=None,
            last_heartbeat_at=None,
            last_tick=None,
            last_error="not running",
            detail="inactive",
        ),
    )

    result = check_cron_service()

    assert result.name == "Cron Service"
    assert result.status == "WARN"
    assert "not healthy" in result.message
    assert "not running" in result.message


def test_doctor_reports_cron_service_healthy(monkeypatch):
    from types import SimpleNamespace

    from agent_cli.doctor import check_cron_service

    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: SimpleNamespace(
            supported=True,
            active=True,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-04T00:00:00+00:00",
            last_tick={"due": 0, "ran": 0, "delivery": {"pending": 0}},
            last_error=None,
            detail="running",
        ),
    )

    result = check_cron_service()

    assert result.name == "Cron Service"
    assert result.status == "OK"
    assert "leader" in result.message


def test_doctor_reports_gateway_inbox_stats(tmp_path):
    from gateway.contracts import InboundEvent
    from gateway.inbox_store import GatewayInboxStore
    from agent_cli.doctor import check_gateway_inbox

    store = GatewayInboxStore(tmp_path / "gateway" / "gateway.sqlite")
    item_id = store.enqueue(
        InboundEvent(
            platform="feishu",
            event_id="evt-1",
            event_type="im.message.receive_v1",
            chat_id="oc_1",
            thread_id="mid-1",
            text="hello",
            timestamp="2026-06-04T00:00:00+00:00",
            raw={},
        )
    )
    store.fail(item_id, "boom", max_attempts=1)

    result = check_gateway_inbox(tmp_path)

    assert result.name == "Gateway Inbox"
    assert result.status == "WARN"
    assert "dead=1" in result.message


def test_doctor_reports_cron_delivery_queue_stats(tmp_path, monkeypatch):
    from cron.delivery_store import DeliveryStore
    from agent_cli.doctor import check_cron_delivery_queue

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    store = DeliveryStore()
    event = store.enqueue(
        job_id="job-1",
        job_name="Daily",
        run_at="2026-06-04T00:00:00+00:00",
        target="origin",
        target_type="origin",
        adapter_key="origin",
        payload={"ok": True},
        status="pending",
    )
    store.mark_dead(event["id"], "delivery failed")

    result = check_cron_delivery_queue(cron_service_healthy=False)

    assert result.name == "Cron Delivery Queue"
    assert result.status == "WARN"
    assert "dead=1" in result.message
```

- [ ] **Step 2: Write failing tests for Feishu token and cron delivery readiness checks**

Append these tests to `tests/test_agent_cli_doctor.py`:

```python
def test_doctor_feishu_token_smoke_uses_gateway_adapter(monkeypatch):
    from gateway.contracts import SendResult
    from agent_cli.doctor import check_feishu_token

    class FakeAdapter:
        key = "feishu"

        def token_smoke(self):
            return SendResult(ok=True)

    class FakeRegistry:
        def get(self, platform):
            return FakeAdapter() if platform == "feishu" else None

    monkeypatch.setattr("gateway.registry.default_gateway_registry", lambda: FakeRegistry())

    result = check_feishu_token()

    assert result.name == "Feishu Token"
    assert result.status == "OK"


def test_doctor_feishu_token_smoke_warns_on_failure(monkeypatch):
    from gateway.contracts import SendResult
    from agent_cli.doctor import check_feishu_token

    class FakeAdapter:
        key = "feishu"

        def token_smoke(self):
            return SendResult(ok=False, error="bad secret")

    class FakeRegistry:
        def get(self, platform):
            return FakeAdapter() if platform == "feishu" else None

    monkeypatch.setattr("gateway.registry.default_gateway_registry", lambda: FakeRegistry())

    result = check_feishu_token()

    assert result.name == "Feishu Token"
    assert result.status == "WARN"
    assert "bad secret" in result.message


def test_doctor_cron_feishu_delivery_ready(monkeypatch):
    from agent_cli.doctor import check_cron_feishu_delivery

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    result = check_cron_feishu_delivery()

    assert result.name == "Cron Feishu Delivery"
    assert result.status in {"OK", "WARN"}
    assert "origin" in result.message
    assert "feishu" in result.message
```

- [ ] **Step 3: Run doctor tests and verify they fail**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_doctor.py -q
```

Expected: FAIL because the new check functions do not exist yet.

- [ ] **Step 4: Implement doctor helpers**

In `agent_cli/doctor.py`, add these helpers after `check_gateway_service()`:

```python
def _format_counts(stats: dict[str, int], keys: tuple[str, ...]) -> str:
    return " ".join(f"{key}={int(stats.get(key, 0) or 0)}" for key in keys)


def check_cron_service() -> HealthCheck:
    try:
        from cron.service_manager import compose_service_status

        status = compose_service_status()
    except Exception as exc:
        return warn("Cron Service", f"check failed: {exc}")

    if not getattr(status, "supported", False):
        return warn("Cron Service", "managed service unsupported; run `python3 -m agent_cli.main cron serve`")

    parts = [
        f"active={getattr(status, 'active', None)}",
        f"fresh={getattr(status, 'heartbeat_fresh', None)}",
        f"state={getattr(status, 'process_state', None) or '-'}",
        f"leader={getattr(status, 'leader_state', None) or '-'}",
    ]
    if getattr(status, "last_heartbeat_at", None):
        parts.append(f"heartbeat={status.last_heartbeat_at}")
    if getattr(status, "last_tick", None):
        parts.append(f"last_tick={status.last_tick}")
    if getattr(status, "last_error", None):
        parts.append(f"error={status.last_error}")

    healthy = (
        bool(getattr(status, "active", False))
        and bool(getattr(status, "heartbeat_fresh", False))
        and getattr(status, "process_state", None) == "running"
    )
    message = "; ".join(parts)
    if healthy:
        return ok("Cron Service", message)
    return warn("Cron Service", f"not healthy; {message}")


def check_feishu_token() -> HealthCheck:
    try:
        from gateway.registry import default_gateway_registry

        adapter = default_gateway_registry().get("feishu")
        if adapter is None:
            return warn("Feishu Token", "feishu gateway adapter is not registered")
        result = adapter.token_smoke()
    except Exception as exc:
        return warn("Feishu Token", f"token smoke failed: {exc}")
    if result.ok:
        return ok("Feishu Token", "tenant token available")
    return warn("Feishu Token", result.error or "tenant token unavailable")


def check_cron_feishu_delivery() -> HealthCheck:
    try:
        from cron.delivery_registry import default_delivery_registry
        from gateway.contracts import PlatformMessageTarget
        from gateway.registry import default_gateway_registry

        delivery_registry = default_delivery_registry()
        gateway_registry = default_gateway_registry()
        active = set(delivery_registry.active_adapter_keys())
        gateway_adapter = gateway_registry.get("feishu")
        missing = []
        if "origin" not in active:
            missing.append("origin delivery adapter")
        if "feishu" not in delivery_registry.adapter_keys():
            missing.append("feishu delivery adapter")
        if gateway_adapter is None:
            missing.append("feishu gateway adapter")
        if missing:
            return warn("Cron Feishu Delivery", "missing: " + ", ".join(missing))
        validation = gateway_adapter.validate_target(
            PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id="doctor-probe")
        )
    except Exception as exc:
        return warn("Cron Feishu Delivery", f"check failed: {exc}")

    message = "active adapters: " + ", ".join(sorted(active))
    if validation.ok:
        return ok("Cron Feishu Delivery", message)
    return warn("Cron Feishu Delivery", f"{message}; target validation: {validation.error}")


def check_gateway_inbox(cli_home: Path) -> HealthCheck:
    path = cli_home / "gateway" / "gateway.sqlite"
    if not path.exists():
        return ok("Gateway Inbox", f"database not found: {path}")
    try:
        from gateway.inbox_store import GatewayInboxStore

        stats = GatewayInboxStore(path).stats()
    except Exception as exc:
        return warn("Gateway Inbox", f"cannot inspect {path}: {exc}")
    message = _format_counts(stats, ("pending", "processing", "failed", "dead", "succeeded"))
    if int(stats.get("failed", 0) or 0) or int(stats.get("dead", 0) or 0):
        return warn("Gateway Inbox", message)
    return ok("Gateway Inbox", message)


def check_cron_delivery_queue(*, cron_service_healthy: bool | None = None) -> HealthCheck:
    try:
        from cron.delivery_store import DeliveryStore

        stats = DeliveryStore().stats()
    except Exception as exc:
        return warn("Cron Delivery Queue", f"cannot inspect delivery queue: {exc}")
    message = _format_counts(stats, ("pending", "delivering", "failed", "dead", "delivered"))
    if int(stats.get("pending", 0) or 0) and cron_service_healthy is False:
        message += "; pending events may not dispatch because cron service is not healthy"
    if int(stats.get("failed", 0) or 0) or int(stats.get("dead", 0) or 0):
        return warn("Cron Delivery Queue", message)
    return ok("Cron Delivery Queue", message)
```

- [ ] **Step 5: Add new checks to `run_health_checks()`**

In `agent_cli/doctor.py`, inside `run_health_checks()`, compute cron health and include the new checks:

```python
    cron_service = check_cron_service()
    cron_service_healthy = cron_service.status == "OK"

    results: list[HealthCheck] = [
        check_python_version(),
        check_platform(),
        *check_dependencies(),
        check_workdir(workdir),
        check_cli_home(cli_home),
        check_sqlite_db(db_path),
        check_logs(cli_home),
        check_config(cli_home),
        check_dotenv(cli_home, cwd),
        check_openai_api_key(),
        check_background_tasks(db_path),
        check_feishu_gateway(),
        check_feishu_ws_gateway(),
        check_gateway_service(),
        cron_service,
        check_feishu_token(),
        check_cron_feishu_delivery(),
        check_gateway_inbox(cli_home),
        check_cron_delivery_queue(cron_service_healthy=cron_service_healthy),
    ]
```

- [ ] **Step 6: Run doctor tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_doctor.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit task 2**

Run:

```bash
git add agent_cli/doctor.py tests/test_agent_cli_doctor.py
git commit -m "feat: extend doctor with gateway cron diagnostics"
```

---

### Task 3: Feishu WS Normalization Reasons and Logs

**Files:**
- Modify: `gateway/transports/feishu_ws.py`
- Test: `tests/test_gateway_feishu_ws_e2e.py`

- [ ] **Step 1: Write failing tests for normalization reasons**

Append this test to `tests/test_gateway_feishu_ws_e2e.py`:

```python
def test_feishu_ws_normalize_returns_ignored_reasons():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event_with_reason

    base = {
        "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1", "create_time": "1760000000000"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "mid-1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }

    event, reason = normalize_feishu_ws_event_with_reason(base)
    assert event is not None
    assert reason is None

    payload = {**base, "header": {**base["header"], "event_type": "other.event"}}
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "unsupported_event_type"

    payload = {
        **base,
        "event": {
            **base["event"],
            "message": {**base["event"]["message"], "message_type": "image"},
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "unsupported_message_type"

    payload = {
        **base,
        "event": {
            **base["event"],
            "message": {**base["event"]["message"], "content": '{"text":"   "}'},
        },
    }
    event, reason = normalize_feishu_ws_event_with_reason(payload)
    assert event is None
    assert reason == "empty_text"
```

- [ ] **Step 2: Write failing tests for transport logs**

Append this test to `tests/test_gateway_feishu_ws_e2e.py`:

```python
def test_feishu_ws_logs_received_enqueued_and_ignored_events(monkeypatch, tmp_path, caplog):
    import json
    import logging
    import threading
    import types

    import gateway.transports.feishu_ws as feishu_ws

    captured = {}

    class FakeJSON:
        @staticmethod
        def marshal(data):
            return json.dumps(data)

    class FakeBuilder:
        def __init__(self):
            self.callback = None

        def register_p2_im_message_receive_v1(self, callback):
            self.callback = callback
            return self

        def build(self):
            captured["callback"] = self.callback
            return "handler"

    class FakeHandler:
        @staticmethod
        def builder(_verification_token, _encrypt_key):
            return FakeBuilder()

    class FakeClient:
        def __init__(self, app_id, app_secret, event_handler):
            self.event_handler = event_handler

        def start(self):
            captured["started"] = True

        def stop(self):
            captured["stopped"] = True

    fake_lark = types.SimpleNamespace(
        JSON=FakeJSON,
        EventDispatcherHandler=FakeHandler,
        ws=types.SimpleNamespace(Client=FakeClient),
    )
    monkeypatch.setitem(__import__("sys").modules, "lark_oapi", fake_lark)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    stop_event = threading.Event()
    stop_event.set()

    with caplog.at_level(logging.INFO, logger="gateway.transports.feishu_ws"):
        feishu_ws.serve_feishu_ws_gateway(home=tmp_path, stop_event=stop_event)

    text_payload = {
        "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1", "create_time": "1760000000000"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "mid-1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": '{"text":"hello"}',
            },
        },
    }
    ignored_payload = {
        "header": {"event_id": "evt-2", "event_type": "im.message.receive_v1", "create_time": "1760000000000"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "mid-2",
                "chat_id": "oc_123",
                "message_type": "image",
                "content": "{}",
            },
        },
    }

    with caplog.at_level(logging.INFO, logger="gateway.transports.feishu_ws"):
        captured["callback"](text_payload)
        captured["callback"](ignored_payload)

    assert "Feishu WS event received" in caplog.text
    assert "Feishu WS event enqueued" in caplog.text
    assert "Feishu WS event ignored" in caplog.text
    assert "unsupported_message_type" in caplog.text
    assert "hello" not in caplog.text
```

- [ ] **Step 3: Run Feishu WS tests and verify they fail**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_feishu_ws_e2e.py -q
```

Expected: FAIL because reason helper and logs do not exist.

- [ ] **Step 4: Implement reason helper and logger**

In `gateway/transports/feishu_ws.py`, add imports and logger:

```python
import logging
```

After imports:

```python
logger = logging.getLogger(__name__)
```

Replace `normalize_feishu_ws_event()` with this pair:

```python
def _feishu_event_metadata(payload: dict[str, Any]) -> dict[str, str]:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    return {
        "event_type": str(header.get("event_type") or ""),
        "event_id": str(header.get("event_id") or ""),
        "chat_id": str(message.get("chat_id") or ""),
        "message_id": str(message.get("message_id") or ""),
        "message_type": str(message.get("message_type") or ""),
    }


def normalize_feishu_ws_event_with_reason(payload: dict[str, Any]) -> tuple[InboundEvent | None, str | None]:
    if not isinstance(payload, dict):
        return None, "malformed_payload"

    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}

    event_type = str(header.get("event_type") or "im.message.receive_v1")
    if event_type != "im.message.receive_v1":
        return None, "unsupported_event_type"

    if message.get("message_type") != "text":
        return None, "unsupported_message_type"

    chat_id = str(message.get("chat_id") or "").strip()
    message_id = str(message.get("message_id") or "").strip()
    event_id = str(header.get("event_id") or "").strip()
    text = _extract_text_content(message.get("content"))

    if not chat_id:
        return None, "missing_chat_id"
    if not message_id:
        return None, "missing_message_id"
    if not event_id:
        return None, "missing_event_id"
    if not text:
        return None, "empty_text"

    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    timestamp = _utc_timestamp_from_ms(header.get("create_time"))
    thread_id = str(message.get("thread_id") or message_id).strip() or None

    return InboundEvent(
        platform="feishu",
        event_id=event_id,
        event_type=event_type,
        chat_id=chat_id,
        thread_id=thread_id,
        sender_id=str(sender_id.get("open_id") or sender_id.get("union_id") or sender_id.get("user_id") or "").strip() or None,
        sender_name=str(sender.get("sender_name") or "").strip() or None,
        text=text,
        timestamp=timestamp,
        raw=payload,
    ), None


def normalize_feishu_ws_event(payload: dict[str, Any]) -> InboundEvent | None:
    event, _reason = normalize_feishu_ws_event_with_reason(payload)
    return event
```

- [ ] **Step 5: Add logging in `on_message()`**

In `serve_feishu_ws_gateway()`, replace `on_message` with:

```python
    def on_message(data):
        try:
            payload = json.loads(lark.JSON.marshal(data))
            metadata = _feishu_event_metadata(payload if isinstance(payload, dict) else {})
            logger.info("Feishu WS event received: %s", metadata)
            event, reason = normalize_feishu_ws_event_with_reason(payload)
            if event is None:
                logger.warning("Feishu WS event ignored: reason=%s metadata=%s", reason, metadata)
                return
            inbox_id = inbox.enqueue(event)
            logger.info(
                "Feishu WS event enqueued: event_id=%s inbox_id=%s chat_id=%s message_id=%s",
                event.event_id,
                inbox_id,
                event.chat_id,
                metadata.get("message_id"),
            )
        except Exception:
            logger.exception("Feishu WS event handler failed")
            raise
```

- [ ] **Step 6: Run Feishu WS tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_feishu_ws_e2e.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit task 3**

Run:

```bash
git add gateway/transports/feishu_ws.py tests/test_gateway_feishu_ws_e2e.py
git commit -m "feat: add feishu ws diagnostic logging"
```

---

### Task 4: Full Verification and Cleanup

**Files:**
- Inspect: `agent_cli/command_handlers/gateway.py`
- Inspect: `agent_cli/doctor.py`
- Inspect: `gateway/transports/feishu_ws.py`
- Inspect: `tests/test_gateway_cli.py`
- Inspect: `tests/test_agent_cli_doctor.py`
- Inspect: `tests/test_gateway_feishu_ws_e2e.py`

- [ ] **Step 1: Run targeted test suite**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py tests/test_agent_cli_doctor.py tests/test_gateway_feishu_ws_e2e.py -q
```

Expected: PASS.

- [ ] **Step 2: Run related cron delivery tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_origin_gateway_delivery.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 4: Check worktree status**

Run:

```bash
git status --short
```

Expected: only intended files changed by this implementation plus any pre-existing unrelated local changes. Do not revert unrelated files.

- [ ] **Step 5: Final commit if verification changed files**

If any verification-driven fix was needed after the task commits, commit only those implementation files:

```bash
git add agent_cli/command_handlers/gateway.py agent_cli/doctor.py gateway/transports/feishu_ws.py tests/test_gateway_cli.py tests/test_agent_cli_doctor.py tests/test_gateway_feishu_ws_e2e.py
git commit -m "test: verify feishu gateway cron diagnostics"
```

Skip this commit if there are no new changes after Task 3.

---

## Self-Review

- Spec coverage:
  - Non-blocking startup warning: Task 1.
  - Unified doctor checks for gateway/cron/token/delivery/inbox/queue: Task 2.
  - Feishu WS received/enqueued/ignored/error logging with reasons: Task 3.
  - Verification: Task 4.
- Placeholder scan: no placeholder markers or unspecified "add tests" steps remain.
- Type consistency:
  - `check_cron_delivery_queue(cron_service_healthy=...)` is defined and called with the same keyword.
  - `normalize_feishu_ws_event_with_reason(payload)` returns `tuple[InboundEvent | None, str | None]` and the compatibility wrapper keeps returning `InboundEvent | None`.
  - Test monkeypatch targets match existing modules: `cron.service_manager.compose_service_status`, `gateway.registry.default_gateway_registry`, and `gateway.transports.feishu_ws`.

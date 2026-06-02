# Feishu Gateway Outbound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lightweight outbound gateway core and Feishu application bot delivery for cron jobs.

**Architecture:** Introduce a small `gateway/` package with outbound contracts, registry, and a Feishu platform adapter. Add a cron `feishu` delivery adapter that formats cron results as plain text and delegates sending to the gateway registry, reusing the existing cron delivery retry/dead-letter state machine.

**Tech Stack:** Python dataclasses/protocols, `urllib.request`, JSON, pytest, existing cron `DeliveryRegistry`, `DeliveryDispatcher`, `CronService`, and `StateStore`.

---

## File Structure

- Create `gateway/__init__.py`
  - Re-export gateway contracts and registry helpers.
- Create `gateway/contracts.py`
  - Define `PlatformMessageTarget`, `OutboundMessage`, `SendResult`, and `PlatformAdapter`.
- Create `gateway/registry.py`
  - Define `GatewayRegistry`, adapter factory hooks for tests, and `default_gateway_registry()`.
- Create `gateway/platforms/__init__.py`
  - Package marker for platform adapters.
- Create `gateway/platforms/feishu.py`
  - Implement Feishu application bot token and text send logic.
- Create `tests/test_gateway_core.py`
  - Unit tests for contracts and registry.
- Create `tests/test_gateway_feishu.py`
  - Unit tests for Feishu token, caching, payload, and error mapping.
- Modify `cron/delivery_targets.py`
  - Parse `feishu:<chat_id>` before the generic platform branch.
  - Make bare `feishu` fail through the adapter with a clear unsupported-default message.
- Modify `cron/delivery_adapters.py`
  - Add `FeishuDeliveryAdapter` as a cron bridge.
- Modify `cron/delivery_registry.py`
  - Register `FeishuDeliveryAdapter` by default.
- Modify `agent_cli/cron_commands.py`
  - Add Feishu validation and token smoke checks in `cron_doctor()`.
- Modify `tests/test_cron_delivery.py`
  - Add cron delivery integration tests for Feishu.
- Modify `tests/test_agent_cli_cron_commands.py`
  - Add doctor tests for Feishu adapter/config/token smoke.
- Modify `tests/test_cron_e2e.py`
  - Add service E2E success and no-due retry tests for Feishu.

Use `/home/miku/miniforge3/envs/langchain/bin/python -m pytest ...` for verification.

---

## Task 1: Gateway Core Contracts And Registry

**Files:**
- Create: `gateway/__init__.py`
- Create: `gateway/contracts.py`
- Create: `gateway/registry.py`
- Create: `gateway/platforms/__init__.py`
- Test: `tests/test_gateway_core.py`

- [ ] **Step 1: Write failing gateway core tests**

Create `tests/test_gateway_core.py`:

```python
from __future__ import annotations


def test_gateway_contracts_are_plain_values():
    from gateway.contracts import OutboundMessage, PlatformMessageTarget, SendResult

    target = PlatformMessageTarget(
        platform="feishu",
        target_type="chat_id",
        target_id="oc_123",
        thread_id="thread-1",
    )
    message = OutboundMessage(text="hello", metadata={"job_id": "job-1"})
    result = SendResult(ok=True)

    assert target.platform == "feishu"
    assert target.target_type == "chat_id"
    assert target.target_id == "oc_123"
    assert target.thread_id == "thread-1"
    assert message.text == "hello"
    assert message.metadata == {"job_id": "job-1"}
    assert result.ok is True
    assert result.error is None
    assert result.retryable is False


def test_gateway_registry_registers_and_returns_adapter():
    from gateway.contracts import SendResult
    from gateway.registry import GatewayRegistry

    class FakeAdapter:
        key = "fake"

        def validate_target(self, target):
            return SendResult(ok=True)

        def send_text(self, target, message):
            return SendResult(ok=True)

    registry = GatewayRegistry()
    adapter = FakeAdapter()
    registry.register(adapter)

    assert registry.get("fake") is adapter
    assert registry.get("missing") is None
    assert registry.platform_keys() == ["fake"]


def test_default_gateway_registry_includes_feishu():
    from gateway.registry import default_gateway_registry

    registry = default_gateway_registry()

    assert "feishu" in registry.platform_keys()
    assert registry.get("feishu") is not None


def test_gateway_registry_factory_override_is_isolated():
    import gateway.registry as gateway_registry
    from gateway.contracts import SendResult

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            return SendResult(ok=True)

        def send_text(self, target, message):
            return SendResult(ok=True)

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        registry = gateway_registry.default_gateway_registry()
        assert isinstance(registry.get("feishu"), FakeFeishuAdapter)
    finally:
        gateway_registry.clear_gateway_adapter_factories()
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_core.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'gateway'`.

- [ ] **Step 3: Implement contracts**

Create `gateway/contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class PlatformMessageTarget:
    platform: str
    target_type: str
    target_id: str
    thread_id: str | None = None


@dataclass(frozen=True)
class OutboundMessage:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: str | None = None
    retryable: bool = False


class PlatformAdapter(Protocol):
    key: str

    def validate_target(self, target: PlatformMessageTarget) -> SendResult:
        ...

    def send_text(self, target: PlatformMessageTarget, message: OutboundMessage) -> SendResult:
        ...
```

- [ ] **Step 4: Implement registry and package exports**

Create `gateway/registry.py`:

```python
from __future__ import annotations

from typing import Any


GatewayAdapterFactory = Any
_ADAPTER_FACTORIES: list[GatewayAdapterFactory] = []


def register_gateway_adapter_factory(factory: GatewayAdapterFactory) -> None:
    _ADAPTER_FACTORIES.append(factory)


def clear_gateway_adapter_factories() -> None:
    _ADAPTER_FACTORIES.clear()


class GatewayRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    def register(self, adapter: Any) -> None:
        self._adapters[str(adapter.key)] = adapter

    def get(self, platform: str) -> Any | None:
        return self._adapters.get(str(platform))

    def platform_keys(self) -> list[str]:
        return sorted(self._adapters)


def build_gateway_registry(*, http_sender=None, extra_adapters=None) -> GatewayRegistry:
    from gateway.platforms.feishu import FeishuPlatformAdapter

    registry = GatewayRegistry()
    registry.register(FeishuPlatformAdapter(http_sender=http_sender))
    for adapter in extra_adapters or []:
        registry.register(adapter)
    for factory in list(_ADAPTER_FACTORIES):
        adapter = factory(http_sender=http_sender)
        if adapter is not None:
            registry.register(adapter)
    return registry


def default_gateway_registry(*, http_sender=None) -> GatewayRegistry:
    return build_gateway_registry(http_sender=http_sender)
```

Create `gateway/__init__.py`:

```python
from gateway.contracts import OutboundMessage, PlatformAdapter, PlatformMessageTarget, SendResult
from gateway.registry import GatewayRegistry, default_gateway_registry

__all__ = [
    "GatewayRegistry",
    "OutboundMessage",
    "PlatformAdapter",
    "PlatformMessageTarget",
    "SendResult",
    "default_gateway_registry",
]
```

Create `gateway/platforms/__init__.py`:

```python
"""Gateway platform adapters."""
```

- [ ] **Step 5: Add minimal Feishu stub for registry**

Create `gateway/platforms/feishu.py` with a stub that Task 2 will complete:

```python
from __future__ import annotations

from gateway.contracts import OutboundMessage, PlatformMessageTarget, SendResult


class FeishuPlatformAdapter:
    key = "feishu"

    def __init__(self, *, http_sender=None) -> None:
        self.http_sender = http_sender

    def validate_target(self, target: PlatformMessageTarget) -> SendResult:
        if target.platform != "feishu":
            return SendResult(False, error="feishu adapter only supports feishu targets")
        if target.target_type != "chat_id":
            return SendResult(False, error="feishu delivery only supports chat_id targets")
        if not target.target_id:
            return SendResult(False, error="feishu delivery requires a chat_id")
        return SendResult(True)

    def send_text(self, target: PlatformMessageTarget, message: OutboundMessage) -> SendResult:
        validation = self.validate_target(target)
        if not validation.ok:
            return validation
        return SendResult(False, error="feishu sender is not implemented", retryable=False)
```

- [ ] **Step 6: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_core.py -q
```

Expected: `4 passed`.

- [ ] **Step 7: Commit**

```bash
git add gateway tests/test_gateway_core.py
git commit -m "feat: add outbound gateway core"
```

---

## Task 2: Feishu Platform Adapter

**Files:**
- Modify: `gateway/platforms/feishu.py`
- Test: `tests/test_gateway_feishu.py`

- [ ] **Step 1: Write failing Feishu adapter tests**

Create `tests/test_gateway_feishu.py`:

```python
from __future__ import annotations

import json


TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"


class ScriptedFeishuHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers=None, timeout=10):
        self.calls.append((url, payload, headers or {}, timeout))
        if self.responses:
            return self.responses.pop(0)
        return 200, '{"code":0,"msg":"ok"}'


def test_feishu_validate_requires_env(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    from gateway.contracts import PlatformMessageTarget
    from gateway.platforms.feishu import FeishuPlatformAdapter

    adapter = FeishuPlatformAdapter()
    result = adapter.validate_target(
        PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id="oc_123")
    )

    assert result.ok is False
    assert result.retryable is False
    assert "FEISHU_APP_ID" in result.error


def test_feishu_token_success_is_cached(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    http = ScriptedFeishuHttp(
        [
            (200, '{"code":0,"tenant_access_token":"token-1","expire":7200}'),
            (200, '{"code":0,"msg":"ok"}'),
            (200, '{"code":0,"msg":"ok"}'),
        ]
    )

    from gateway.contracts import OutboundMessage, PlatformMessageTarget
    from gateway.platforms.feishu import FeishuPlatformAdapter

    adapter = FeishuPlatformAdapter(http_sender=http)
    target = PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id="oc_123")

    first = adapter.send_text(target, OutboundMessage(text="hello"))
    second = adapter.send_text(target, OutboundMessage(text="again"))

    assert first.ok is True
    assert second.ok is True
    token_calls = [call for call in http.calls if call[0] == TOKEN_URL]
    assert len(token_calls) == 1
    send_calls = [call for call in http.calls if call[0] == SEND_URL]
    assert len(send_calls) == 2


def test_feishu_send_text_payload(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    http = ScriptedFeishuHttp(
        [
            (200, '{"code":0,"tenant_access_token":"token-1","expire":7200}'),
            (200, '{"code":0,"msg":"ok"}'),
        ]
    )

    from gateway.contracts import OutboundMessage, PlatformMessageTarget
    from gateway.platforms.feishu import FeishuPlatformAdapter

    adapter = FeishuPlatformAdapter(http_sender=http)
    result = adapter.send_text(
        PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id="oc_123"),
        OutboundMessage(text="hello"),
    )

    assert result.ok is True
    url, payload, headers, timeout = http.calls[1]
    assert url == SEND_URL
    assert headers["Authorization"] == "Bearer token-1"
    assert payload["receive_id"] == "oc_123"
    assert payload["msg_type"] == "text"
    assert json.loads(payload["content"]) == {"text": "hello"}
    assert timeout == 10


def test_feishu_token_network_failure_is_retryable(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    def failing_sender(url, payload, headers=None, timeout=10):
        raise OSError("network down")

    from gateway.contracts import OutboundMessage, PlatformMessageTarget
    from gateway.platforms.feishu import FeishuPlatformAdapter

    result = FeishuPlatformAdapter(http_sender=failing_sender).send_text(
        PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id="oc_123"),
        OutboundMessage(text="hello"),
    )

    assert result.ok is False
    assert result.retryable is True
    assert "network down" in result.error


def test_feishu_token_credential_failure_is_not_retryable(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "bad")
    http = ScriptedFeishuHttp([(200, '{"code":99991663,"msg":"invalid app_secret"}')])

    from gateway.contracts import OutboundMessage, PlatformMessageTarget
    from gateway.platforms.feishu import FeishuPlatformAdapter

    result = FeishuPlatformAdapter(http_sender=http).send_text(
        PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id="oc_123"),
        OutboundMessage(text="hello"),
    )

    assert result.ok is False
    assert result.retryable is False
    assert "invalid app_secret" in result.error


def test_feishu_send_retryable_and_permanent_errors(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    from gateway.contracts import OutboundMessage, PlatformMessageTarget
    from gateway.platforms.feishu import FeishuPlatformAdapter

    target = PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id="oc_123")

    retry_http = ScriptedFeishuHttp(
        [
            (200, '{"code":0,"tenant_access_token":"token-1","expire":7200}'),
            (429, "rate limited"),
        ]
    )
    retry_result = FeishuPlatformAdapter(http_sender=retry_http).send_text(
        target,
        OutboundMessage(text="hello"),
    )
    assert retry_result.ok is False
    assert retry_result.retryable is True
    assert "HTTP 429" in retry_result.error

    permanent_http = ScriptedFeishuHttp(
        [
            (200, '{"code":0,"tenant_access_token":"token-1","expire":7200}'),
            (200, '{"code":230001,"msg":"chat not found"}'),
        ]
    )
    permanent_result = FeishuPlatformAdapter(http_sender=permanent_http).send_text(
        target,
        OutboundMessage(text="hello"),
    )
    assert permanent_result.ok is False
    assert permanent_result.retryable is False
    assert "chat not found" in permanent_result.error
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_feishu.py -q
```

Expected: tests fail because the Feishu adapter is still a stub.

- [ ] **Step 3: Implement Feishu HTTP sender and token handling**

Replace `gateway/platforms/feishu.py` with:

```python
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable
import urllib.error
import urllib.request

from gateway.contracts import OutboundMessage, PlatformMessageTarget, SendResult

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
_TOKEN_REFRESH_SKEW_SECONDS = 60
_RETRYABLE_FEISHU_CODES = {99991668}

FeishuHttpSender = Callable[[str, dict[str, Any], dict[str, str] | None, int], tuple[int, str]]


def default_feishu_http_sender(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: int = 10) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return int(response.status), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body


def _json_body(body: str) -> dict[str, Any] | None:
    try:
        value = json.loads(body or "{}")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _http_error(prefix: str, status: int, body: str) -> SendResult:
    retryable = status in {408, 429} or status >= 500
    return SendResult(False, error=f"{prefix}: HTTP {status}: {body}", retryable=retryable)


def _api_error(prefix: str, body: str) -> SendResult:
    parsed = _json_body(body)
    if parsed is None:
        return SendResult(False, error=f"{prefix}: invalid JSON response: {body}", retryable=True)
    code = parsed.get("code", 0)
    msg = str(parsed.get("msg") or parsed.get("message") or body)
    if code in (0, "0", None):
        return SendResult(True)
    try:
        normalized_code = int(code)
    except (TypeError, ValueError):
        normalized_code = None
    retryable = normalized_code in _RETRYABLE_FEISHU_CODES
    return SendResult(False, error=f"{prefix}: code={code} msg={msg}", retryable=retryable)


class FeishuPlatformAdapter:
    key = "feishu"

    def __init__(
        self,
        *,
        http_sender: FeishuHttpSender | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.http_sender = http_sender or default_feishu_http_sender
        self.clock = clock
        self._tenant_access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _app_id(self) -> str | None:
        return os.getenv("FEISHU_APP_ID") or None

    def _app_secret(self) -> str | None:
        return os.getenv("FEISHU_APP_SECRET") or None

    def validate_target(self, target: PlatformMessageTarget) -> SendResult:
        if target.platform != "feishu":
            return SendResult(False, error="feishu adapter only supports feishu targets")
        if target.target_type != "chat_id":
            return SendResult(False, error="feishu delivery only supports chat_id targets")
        if not target.target_id:
            return SendResult(False, error="feishu delivery requires a chat_id")
        if not self._app_id():
            return SendResult(False, error="feishu delivery requires FEISHU_APP_ID")
        if not self._app_secret():
            return SendResult(False, error="feishu delivery requires FEISHU_APP_SECRET")
        return SendResult(True)

    def token_smoke(self) -> SendResult:
        return self._ensure_token()

    def _ensure_token(self) -> SendResult:
        if self._tenant_access_token and self.clock() < self._token_expires_at - _TOKEN_REFRESH_SKEW_SECONDS:
            return SendResult(True)
        app_id = self._app_id()
        app_secret = self._app_secret()
        if not app_id:
            return SendResult(False, error="feishu delivery requires FEISHU_APP_ID")
        if not app_secret:
            return SendResult(False, error="feishu delivery requires FEISHU_APP_SECRET")
        try:
            status, body = self.http_sender(
                TOKEN_URL,
                {"app_id": app_id, "app_secret": app_secret},
                None,
                10,
            )
        except Exception as exc:
            return SendResult(False, error=f"Feishu token failed: {exc}", retryable=True)
        if not (200 <= status < 300):
            return _http_error("Feishu token failed", status, body)
        parsed = _json_body(body)
        if parsed is None:
            return SendResult(False, error=f"Feishu token failed: invalid JSON response: {body}", retryable=True)
        code = parsed.get("code", 0)
        if code not in (0, "0", None):
            msg = str(parsed.get("msg") or parsed.get("message") or body)
            return SendResult(False, error=f"Feishu token failed: code={code} msg={msg}", retryable=False)
        token = str(parsed.get("tenant_access_token") or "")
        if not token:
            return SendResult(False, error="Feishu token failed: tenant_access_token missing", retryable=True)
        expire = int(parsed.get("expire") or 0)
        self._tenant_access_token = token
        self._token_expires_at = self.clock() + max(0, expire)
        return SendResult(True)

    def send_text(self, target: PlatformMessageTarget, message: OutboundMessage) -> SendResult:
        validation = self.validate_target(target)
        if not validation.ok:
            return validation
        token_result = self._ensure_token()
        if not token_result.ok:
            return token_result
        payload = {
            "receive_id": target.target_id,
            "msg_type": "text",
            "content": json.dumps({"text": message.text}, ensure_ascii=False),
        }
        try:
            status, body = self.http_sender(
                SEND_URL,
                payload,
                {"Authorization": f"Bearer {self._tenant_access_token}"},
                10,
            )
        except Exception as exc:
            return SendResult(False, error=f"Feishu send failed: {exc}", retryable=True)
        if not (200 <= status < 300):
            return _http_error("Feishu send failed", status, body)
        return _api_error("Feishu send failed", body)
```

- [ ] **Step 4: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_core.py tests/test_gateway_feishu.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add gateway/platforms/feishu.py tests/test_gateway_feishu.py
git commit -m "feat: add feishu platform adapter"
```

---

## Task 3: Cron Feishu Delivery Adapter

**Files:**
- Modify: `cron/delivery_targets.py`
- Modify: `cron/delivery_adapters.py`
- Modify: `cron/delivery_registry.py`
- Test: `tests/test_cron_delivery.py`

- [ ] **Step 1: Write failing cron delivery tests**

Add these tests to `tests/test_cron_delivery.py` near WeCom tests:

```python
def test_feishu_delivery_target_requires_explicit_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    from cron.jobs import create_job

    with pytest.raises(ValueError, match="feishu delivery requires explicit target: feishu:<chat_id>"):
        create_job(prompt="write report", schedule="30m", deliver="feishu")


def test_feishu_delivery_target_stores_chat_id(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    from cron.jobs import create_job

    job = create_job(prompt="write report", schedule="30m", deliver="feishu:oc_123")

    target = job["delivery_targets"][0]
    assert target["raw"] == "feishu:oc_123"
    assert target["target_type"] == "platform"
    assert target["adapter_key"] == "feishu"
    assert target["address"] == "oc_123"


def test_feishu_delivery_dispatches_through_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    from cron.delivery import JobRunResult, enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    import gateway.registry as gateway_registry
    from gateway.contracts import SendResult

    sent = []

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            return SendResult(ok=True)

        def send_text(self, target, message):
            sent.append((target, message))
            return SendResult(ok=True)

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        event = enqueue_result(
            {"id": "job-1", "name": "Daily", "deliver": "feishu:oc_123"},
            JobRunResult(success=True, output_doc="# out", final_response="done"),
            "/tmp/out.md",
            "2026-06-02T10:00:00+00:00",
        )
        summary = process_due(limit=10)
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    assert summary["delivered"] == 1
    assert DeliveryStore().get(event["id"])["status"] == "delivered"
    target, message = sent[0]
    assert target.platform == "feishu"
    assert target.target_type == "chat_id"
    assert target.target_id == "oc_123"
    assert "Daily" in message.text
    assert "job-1" in message.text
    assert "done" in message.text
    assert "/tmp/out.md" in message.text


def test_feishu_delivery_retryable_and_dead_results(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore
    import gateway.registry as gateway_registry
    from gateway.contracts import SendResult

    class FakeFeishuAdapter:
        key = "feishu"

        def __init__(self, result):
            self.result = result

        def validate_target(self, target):
            return SendResult(ok=True)

        def send_text(self, target, message):
            return self.result

    for result, expected_status in [
        (SendResult(ok=False, error="temporary", retryable=True), "failed"),
        (SendResult(ok=False, error="chat not found", retryable=False), "dead"),
    ]:
        def factory(**kwargs):
            return FakeFeishuAdapter(result)

        gateway_registry.clear_gateway_adapter_factories()
        gateway_registry.register_gateway_adapter_factory(factory)
        try:
            event = enqueue_result(
                {"id": f"job-{expected_status}", "name": "Daily", "deliver": "feishu:oc_123"},
                JobRunResult(success=True, output_doc="# out", final_response="done"),
                "/tmp/out.md",
                "2026-06-02T10:00:00+00:00",
            )
            DeliveryDispatcher(store=StateStore()).dispatch_due(limit=10, adapter_keys={"feishu"})
        finally:
            gateway_registry.clear_gateway_adapter_factories()
        stored = DeliveryStore().get(event["id"])
        assert stored["status"] == expected_status
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery.py::test_feishu_delivery_target_requires_explicit_chat tests/test_cron_delivery.py::test_feishu_delivery_target_stores_chat_id tests/test_cron_delivery.py::test_feishu_delivery_dispatches_through_gateway tests/test_cron_delivery.py::test_feishu_delivery_retryable_and_dead_results -q
```

Expected: fail because `feishu` adapter is not registered and bare `feishu` has no explicit error.

- [ ] **Step 3: Implement target parsing**

In `cron/delivery_targets.py`, add before generic `if ":" in raw:`:

```python
    if lowered == "feishu":
        return DeliveryTarget(raw=raw, target_type="platform", adapter_key="feishu")
    if lowered.startswith("feishu:"):
        return DeliveryTarget(
            raw=raw,
            target_type="platform",
            adapter_key="feishu",
            address=raw.split(":", 1)[1].strip() or None,
        )
```

- [ ] **Step 4: Implement cron Feishu bridge**

In `cron/delivery_adapters.py`, add helpers after WeCom code:

```python
_FEISHU_MAX_TEXT_CHARS = 3900


def _shorten_feishu_text(text: Any, max_chars: int) -> str:
    value = "" if text is None else str(text)
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    marker = "\n\n...(truncated; full output saved locally)"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return value[: max_chars - len(marker)].rstrip() + marker


def _format_feishu_text(event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> str:
    payload = json.loads(event["payload_json"])
    job_name = payload.get("job_name") or (job or {}).get("name") or event.get("job_name") or "(unnamed)"
    job_id = payload.get("job_id") or event.get("job_id") or (job or {}).get("id") or "-"
    run_id = payload.get("run_id") or event.get("run_id") or (run or {}).get("id") or "-"
    status = payload.get("status") or (run or {}).get("status") or ("error" if payload.get("error") else "success")
    output_path = payload.get("output_path") or event.get("output_path") or (run or {}).get("output_path")
    detail = payload.get("final_response") or payload.get("error") or ""
    header = [
        f"Cron job update: {job_name}",
        f"status: {status}",
        f"job_id: {job_id}",
    ]
    if run_id:
        header.append(f"run_id: {run_id}")
    if output_path:
        header.append(f"output_path: {output_path}")
    body_limit = _FEISHU_MAX_TEXT_CHARS - len("\n".join(header)) - 20
    return "\n".join(header + ["", _shorten_feishu_text(detail, body_limit)])


class FeishuDeliveryAdapter:
    key = "feishu"
    active_dispatch = True

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        if not target.address:
            return AdapterValidation(False, "feishu delivery requires explicit target: feishu:<chat_id>")
        from gateway.contracts import PlatformMessageTarget
        from gateway.registry import default_gateway_registry

        adapter = default_gateway_registry().get("feishu")
        if adapter is None:
            return AdapterValidation(False, "feishu gateway adapter is not registered")
        result = adapter.validate_target(
            PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id=str(target.address))
        )
        return AdapterValidation(result.ok, result.error)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        if not event.get("address"):
            return DeliveryResult(False, retryable=False, error="feishu delivery requires explicit target: feishu:<chat_id>")
        from gateway.contracts import OutboundMessage, PlatformMessageTarget
        from gateway.registry import default_gateway_registry

        adapter = default_gateway_registry().get("feishu")
        if adapter is None:
            return DeliveryResult(False, retryable=False, error="feishu gateway adapter is not registered")
        text = _format_feishu_text(event, job, run)
        result = adapter.send_text(
            PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id=str(event["address"])),
            OutboundMessage(text=text),
        )
        return DeliveryResult(result.ok, retryable=result.retryable, error=result.error)
```

- [ ] **Step 5: Register the adapter by default**

In `cron/delivery_registry.py`, import/register `FeishuDeliveryAdapter`:

```python
def build_delivery_registry(*, webhook_sender=None, extra_adapters=None) -> DeliveryRegistry:
    from cron.delivery_adapters import FeishuDeliveryAdapter, LocalDeliveryAdapter, OriginDeliveryAdapter, WebhookDeliveryAdapter, WeComDeliveryAdapter

    registry = DeliveryRegistry()
    registry.register(LocalDeliveryAdapter())
    registry.register(OriginDeliveryAdapter())
    registry.register(WebhookDeliveryAdapter(sender=webhook_sender))
    registry.register(WeComDeliveryAdapter(sender=webhook_sender))
    registry.register(FeishuDeliveryAdapter())
    ...
```

- [ ] **Step 6: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py -q
```

Expected: pass after updating any active adapter expectation from `["webhook", "wecom"]` to `["feishu", "webhook", "wecom"]`.

- [ ] **Step 7: Commit**

```bash
git add cron/delivery_targets.py cron/delivery_adapters.py cron/delivery_registry.py tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py
git commit -m "feat: add feishu cron delivery adapter"
```

---

## Task 4: Feishu Doctor Validation

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing doctor tests**

Add these tests near existing delivery adapter doctor tests in `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_doctor_lists_feishu_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_doctor

    result = cron_doctor()

    assert "delivery adapters:" in result.text
    assert "feishu" in result.text


def test_cron_doctor_fails_active_feishu_job_missing_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(prompt="write report", schedule="30m", deliver="local")
    with StateStore()._connect() as conn:
        conn.execute(
            "UPDATE jobs SET deliver = ?, delivery_targets_json = NULL WHERE id = ?",
            ("feishu:oc_123", job["id"]),
        )

    result = cron_doctor()

    assert result.exit_code == 2
    assert f"active job {job['id']} delivery invalid" in result.text
    assert "FEISHU_APP_ID" in result.text


def test_cron_doctor_feishu_token_smoke_invalid_credentials_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "bad")

    import gateway.registry as gateway_registry
    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from gateway.contracts import SendResult

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            return SendResult(ok=True)

        def token_smoke(self):
            return SendResult(ok=False, error="Feishu token failed: code=99991663 msg=invalid app_secret")

        def send_text(self, target, message):
            return SendResult(ok=True)

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        job = create_job(prompt="write report", schedule="30m", deliver="feishu:oc_123")
        result = cron_doctor()
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    assert result.exit_code == 2
    assert f"active job {job['id']} feishu token smoke failed" in result.text
    assert "invalid app_secret" in result.text


def test_cron_doctor_feishu_token_smoke_temporary_failure_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    import gateway.registry as gateway_registry
    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from gateway.contracts import SendResult

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            return SendResult(ok=True)

        def token_smoke(self):
            return SendResult(ok=False, error="Feishu token failed: network down", retryable=True)

        def send_text(self, target, message):
            return SendResult(ok=True)

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        create_job(prompt="write report", schedule="30m", deliver="feishu:oc_123")
        result = cron_doctor()
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    assert result.exit_code == 1
    assert "feishu token smoke temporary failure" in result.text
    assert "network down" in result.text
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_lists_feishu_adapter tests/test_agent_cli_cron_commands.py::test_cron_doctor_fails_active_feishu_job_missing_env tests/test_agent_cli_cron_commands.py::test_cron_doctor_feishu_token_smoke_invalid_credentials_fails tests/test_agent_cli_cron_commands.py::test_cron_doctor_feishu_token_smoke_temporary_failure_warns -q
```

Expected: fail until doctor includes Feishu token smoke behavior.

- [ ] **Step 3: Implement doctor checks**

In `agent_cli/cron_commands.py`, inside `cron_doctor()` target loop, add Feishu branch:

```python
            if adapter_key == "feishu":
                if not address:
                    add("fail", f"active job {job.get('id')} feishu delivery invalid: feishu delivery requires explicit target: feishu:<chat_id>")
                    continue
                from gateway.contracts import PlatformMessageTarget
                from gateway.registry import default_gateway_registry

                feishu_adapter = default_gateway_registry().get("feishu")
                if feishu_adapter is None:
                    add("fail", f"active job {job.get('id')} feishu delivery invalid: feishu gateway adapter is not registered")
                    continue
                target_result = feishu_adapter.validate_target(
                    PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id=str(address))
                )
                if not target_result.ok:
                    add("fail", f"active job {job.get('id')} feishu delivery invalid: {target_result.error}")
                    continue
                smoke = getattr(feishu_adapter, "token_smoke", lambda: target_result)()
                if not smoke.ok:
                    if smoke.retryable:
                        add("warn", f"active job {job.get('id')} feishu token smoke temporary failure: {smoke.error}")
                    else:
                        add("fail", f"active job {job.get('id')} feishu token smoke failed: {smoke.error}")
```

Keep existing webhook and WeCom checks unchanged.

- [ ] **Step 4: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py -q
```

Expected: all cron command tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: validate feishu cron delivery in doctor"
```

---

## Task 5: Feishu Service E2E

**Files:**
- Modify: `tests/test_cron_e2e.py`

- [ ] **Step 1: Write failing E2E tests**

Add helper near WeCom helper in `tests/test_cron_e2e.py`:

```python
def install_feishu_gateway_adapter(adapter) -> None:
    import gateway.registry as gateway_registry

    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: adapter)
```

Add fake adapter:

```python
class RecordingFeishuGatewayAdapter:
    key = "feishu"

    def __init__(self, results):
        from gateway.contracts import SendResult

        self.results = list(results)
        self.calls = []
        self.default_result = SendResult(ok=True)

    def validate_target(self, target):
        from gateway.contracts import SendResult

        return SendResult(ok=bool(target.target_id), error=None if target.target_id else "feishu delivery requires a chat_id")

    def token_smoke(self):
        from gateway.contracts import SendResult

        return SendResult(ok=True)

    def send_text(self, target, message):
        self.calls.append((target, message))
        if self.results:
            return self.results.pop(0)
        return self.default_result
```

Add tests:

```python
def test_service_executes_due_job_and_delivers_feishu_text(isolated_cron_home, monkeypatch):
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore
    from gateway.contracts import SendResult
    import gateway.registry as gateway_registry

    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    adapter = RecordingFeishuGatewayAdapter([SendResult(ok=True)])
    gateway_registry.clear_gateway_adapter_factories()
    install_feishu_gateway_adapter(adapter)
    try:
        job = create_due_job(deliver="feishu:oc_123")
        runner = RecordingRunner(output_doc="# Feishu E2E", final_response="feishu final")

        service, tick_result, exit_code = run_service_once(BASE_TIME, runner)
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    store = StateStore()
    events = DeliveryStore().list_events(job_id=job["id"], limit=10)
    run = store.runs_for_job(job["id"])[0]

    assert exit_code == 0
    assert tick_result.ran == 1
    assert run["status"] == "succeeded"
    assert run["delivery_status"] == "delivered"
    assert Path(run["output_path"]).exists()
    assert events[0]["adapter_key"] == "feishu"
    assert events[0]["status"] == "delivered"
    target, message = adapter.calls[0]
    assert target.platform == "feishu"
    assert target.target_id == "oc_123"
    assert "feishu final" in message.text
    assert job["id"] in message.text
    assert run["id"] in message.text
    assert run["output_path"] in message.text
    assert service.status["last_tick"]["delivery"]["delivered"] == 1


def test_service_retries_failed_feishu_delivery_on_no_due_tick(isolated_cron_home, monkeypatch):
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore
    from gateway.contracts import SendResult
    import gateway.registry as gateway_registry

    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    adapter = RecordingFeishuGatewayAdapter(
        [
            SendResult(ok=False, error="temporary", retryable=True),
            SendResult(ok=True),
        ]
    )
    gateway_registry.clear_gateway_adapter_factories()
    install_feishu_gateway_adapter(adapter)
    try:
        job = create_due_job(deliver="feishu:oc_123")
        runner = RecordingRunner(output_doc="# retry", final_response="retry feishu")
        delivery_store = DeliveryStore()

        _service1, first_tick, first_exit = run_service_once(BASE_TIME, runner)
        store = StateStore()
        first_run = store.runs_for_job(job["id"])[0]
        first_event = delivery_store.list_events(job_id=job["id"], limit=10)[0]
        delivery_store.update_event(first_event["id"], next_attempt_at="2000-01-01T00:00:00+00:00")

        _service2, second_tick, second_exit = run_service_once("2026-06-02T10:01:00+00:00", runner)
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    second_event = delivery_store.list_events(job_id=job["id"], limit=10)[0]
    second_run = store.get_run(first_run["id"])
    assert first_exit == 0
    assert first_tick.ran == 1
    assert second_exit == 0
    assert second_tick.due == 0
    assert second_tick.ran == 0
    assert len(runner.calls) == 1
    assert len(adapter.calls) == 2
    assert second_tick.delivery.delivered == 1
    assert second_event["status"] == "delivered"
    assert second_run["delivery_status"] == "delivered"
    assert store.get_job(job["id"])["last_delivery_error"] is None
```

- [ ] **Step 2: Run E2E tests to verify failure or pass after prior tasks**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py -q
```

Expected: pass after Tasks 1-4 are complete.

- [ ] **Step 3: Commit**

```bash
git add tests/test_cron_e2e.py
git commit -m "test: cover feishu cron delivery e2e"
```

---

## Task 6: Final Verification

**Files:**
- No new code changes unless verification reveals a bug.

- [ ] **Step 1: Run focused suites**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_core.py tests/test_gateway_feishu.py tests/test_cron_delivery.py tests/test_cron_e2e.py tests/test_agent_cli_cron_commands.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run broader cron/gateway regression**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_core.py tests/test_gateway_feishu.py tests/test_cron_scheduler.py tests/test_cron_service.py tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_state_store.py tests/test_cron_delivery_store.py tests/test_cron_e2e.py tests/test_agent_cli_cron_commands.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Inspect git status and commits**

Run:

```bash
git status --short
git log --oneline --max-count=10
```

Expected: clean worktree except for pre-existing unrelated untracked files in the main checkout.

- [ ] **Step 4: Request final code review**

Use `superpowers:requesting-code-review`. Ask reviewer to verify:

- Gateway contracts are narrow and reusable.
- Feishu token caching and error mapping match the spec.
- Cron adapter does not leak Feishu OpenAPI details into scheduler/dispatcher.
- Stored target behavior works.
- Doctor token smoke does not send messages.
- Service E2E proves success and no-due retry.

- [ ] **Step 5: Address review feedback**

If review requests changes, use `superpowers:receiving-code-review`, make focused fixes, rerun affected tests plus the broader suite, and commit.

- [ ] **Step 6: Finish branch**

Use `superpowers:finishing-a-development-branch`. Offer merge locally, PR, keep branch, or discard.

# WeCom Cron Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class Enterprise WeChat group robot webhook delivery for cron jobs.

**Architecture:** Add a native `wecom` delivery target and `WeComDeliveryAdapter` that plugs into the existing cron delivery registry, dispatcher, retry, and service tick flow. Keep gateway reference routing, personal WeChat, application messages, mentions, media, and message splitting out of scope.

**Tech Stack:** Python, pytest, cron `StateStore`/`DeliveryStore`, existing `DeliveryRegistry`, existing HTTP sender shape `(url, payload, timeout) -> (status, body)`.

---

## File Structure

- Modify `cron/delivery_targets.py`
  - Resolve `deliver="wecom"` from `AGENT_CRON_WECOM_WEBHOOK_URL`.
  - Resolve `deliver="wecom:<url>"` before the generic `platform:chat_id` branch so URLs are not split at `https:`.
- Modify `cron/delivery_adapters.py`
  - Add `validate_wecom_webhook_url()`.
  - Add markdown formatting helpers.
  - Add `WeComDeliveryAdapter`.
- Modify `cron/delivery_registry.py`
  - Register `WeComDeliveryAdapter` by default.
- Modify `agent_cli/cron_commands.py`
  - Import and run WeCom URL validation in `cron_doctor()` for active WeCom targets.
- Modify `tests/test_cron_delivery.py`
  - Add target parsing, stored target, adapter payload, retry, and dead-letter coverage.
- Modify `tests/test_agent_cli_cron_commands.py`
  - Add doctor coverage for `wecom` adapter visibility and missing/malformed URL failures.
- Modify `tests/test_cron_e2e.py`
  - Add fake WeCom sender helper and service E2E coverage for success and no-due retry.

Use `/home/miku/miniforge3/envs/langchain/bin/python -m pytest ...` for verification, matching the existing project environment.

---

## Task 1: WeCom Target Semantics

**Files:**
- Modify: `cron/delivery_targets.py`
- Test: `tests/test_cron_delivery.py`

- [ ] **Step 1: Write failing target resolution tests**

Add these tests near the existing webhook target tests in `tests/test_cron_delivery.py`:

```python
def test_bare_wecom_delivery_uses_env_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv(
        "AGENT_CRON_WECOM_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=env-key",
    )

    from cron.jobs import create_job

    job = create_job(prompt="write report", schedule="30m", deliver="wecom")

    target = job["delivery_targets"][0]
    assert target["raw"] == "wecom"
    assert target["target_type"] == "platform"
    assert target["adapter_key"] == "wecom"
    assert target["address"] == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=env-key"


def test_explicit_wecom_delivery_overrides_env_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv(
        "AGENT_CRON_WECOM_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=env-key",
    )
    explicit = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=explicit-key"

    from cron.jobs import create_job

    job = create_job(prompt="write report", schedule="30m", deliver=f"wecom:{explicit}")

    target = job["delivery_targets"][0]
    assert target["raw"] == f"wecom:{explicit}"
    assert target["target_type"] == "platform"
    assert target["adapter_key"] == "wecom"
    assert target["address"] == explicit


def test_bare_wecom_delivery_requires_env_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_CRON_WECOM_WEBHOOK_URL", raising=False)

    from cron.jobs import create_job

    with pytest.raises(ValueError, match="wecom delivery requires AGENT_CRON_WECOM_WEBHOOK_URL or explicit webhook URL"):
        create_job(prompt="write report", schedule="30m", deliver="wecom")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery.py::test_bare_wecom_delivery_uses_env_url tests/test_cron_delivery.py::test_explicit_wecom_delivery_overrides_env_url tests/test_cron_delivery.py::test_bare_wecom_delivery_requires_env_url -q
```

Expected: failures because `wecom` is not a default adapter and `wecom:<url>` is parsed by the generic platform branch incorrectly.

- [ ] **Step 3: Implement WeCom target parsing**

In `cron/delivery_targets.py`, insert these branches after the webhook branches and before the generic `if ":" in raw:` branch:

```python
    if lowered == "wecom":
        return DeliveryTarget(
            raw=raw,
            target_type="platform",
            adapter_key="wecom",
            address=os.getenv("AGENT_CRON_WECOM_WEBHOOK_URL") or None,
        )
    if lowered.startswith("wecom:"):
        return DeliveryTarget(
            raw=raw,
            target_type="platform",
            adapter_key="wecom",
            address=raw.split(":", 1)[1].strip() or None,
        )
```

- [ ] **Step 4: Run tests and observe remaining failure**

Run the same pytest command from Step 2.

Expected: target URL parsing now behaves correctly, but validation still fails because `wecom` is not registered as a default adapter.

- [ ] **Step 5: Commit target parsing after Task 2 passes**

Do not commit yet if Task 1 tests are still failing because the default adapter is missing. Commit Task 1 and Task 2 together after Task 2 Step 6 passes.

---

## Task 2: WeCom Adapter And Registry

**Files:**
- Modify: `cron/delivery_adapters.py`
- Modify: `cron/delivery_registry.py`
- Test: `tests/test_cron_delivery.py`

- [ ] **Step 1: Write failing adapter tests**

Add these tests to `tests/test_cron_delivery.py` after the webhook tests:

```python
def test_wecom_adapter_sends_markdown_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    sent = []
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_adapters import WeComDeliveryAdapter
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    def fake_post(post_url, payload, timeout=10):
        sent.append((post_url, payload, timeout))
        return 200, '{"errcode":0,"errmsg":"ok"}'

    registry = default_delivery_registry()
    registry.register(WeComDeliveryAdapter(sender=fake_post))
    event = enqueue_result(
        {"id": "job-1", "name": "Daily Report", "deliver": f"wecom:{url}"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
        registry=registry,
    )

    summary = DeliveryDispatcher(store=StateStore(), registry=registry).dispatch_due(
        limit=10,
        adapter_keys={"wecom"},
    )

    assert summary["delivered"] == 1
    assert sent[0][0] == url
    assert sent[0][2] == 10
    payload = sent[0][1]
    assert payload["msgtype"] == "markdown"
    content = payload["markdown"]["content"]
    assert "Daily Report" in content
    assert "job-1" in content
    assert "done" in content
    assert "/tmp/out.md" in content
    assert DeliveryStore().get(event["id"])["status"] == "delivered"


def test_wecom_adapter_retries_http_429_and_5xx(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_adapters import WeComDeliveryAdapter
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    for status in (429, 500):
        sent = []

        def fake_post(post_url, payload, timeout=10, status=status):
            sent.append((post_url, payload, timeout))
            return status, "temporary"

        registry = default_delivery_registry()
        registry.register(WeComDeliveryAdapter(sender=fake_post))
        event = enqueue_result(
            {"id": f"job-{status}", "name": "Daily", "deliver": f"wecom:{url}"},
            JobRunResult(success=True, output_doc="# out", final_response="done"),
            "/tmp/out.md",
            "2026-05-28T10:00:00+00:00",
            registry=registry,
        )

        summary = DeliveryDispatcher(store=StateStore(), registry=registry).dispatch_due(
            limit=10,
            adapter_keys={"wecom"},
        )

        stored = DeliveryStore().get(event["id"])
        assert summary["failed"] == 1
        assert stored["status"] == "failed"
        assert stored["next_attempt_at"] is not None
        assert f"HTTP {status}" in stored["last_error"]


def test_wecom_adapter_dead_letters_http_400(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_adapters import WeComDeliveryAdapter
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    registry = default_delivery_registry()
    registry.register(WeComDeliveryAdapter(sender=lambda url, payload, timeout=10: (400, "bad request")))
    event = enqueue_result(
        {"id": "job-400", "name": "Daily", "deliver": f"wecom:{url}"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
        registry=registry,
    )

    summary = DeliveryDispatcher(store=StateStore(), registry=registry).dispatch_due(
        limit=10,
        adapter_keys={"wecom"},
    )

    stored = DeliveryStore().get(event["id"])
    assert summary["dead"] == 1
    assert stored["status"] == "dead"
    assert "HTTP 400" in stored["last_error"]


def test_wecom_adapter_handles_wecom_business_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_adapters import WeComDeliveryAdapter
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    registry = default_delivery_registry()
    registry.register(WeComDeliveryAdapter(sender=lambda url, payload, timeout=10: (200, '{"errcode":40058,"errmsg":"bad webhook"}')))
    event = enqueue_result(
        {"id": "job-business", "name": "Daily", "deliver": f"wecom:{url}"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
        registry=registry,
    )

    summary = DeliveryDispatcher(store=StateStore(), registry=registry).dispatch_due(
        limit=10,
        adapter_keys={"wecom"},
    )

    stored = DeliveryStore().get(event["id"])
    assert summary["dead"] == 1
    assert stored["status"] == "dead"
    assert "bad webhook" in stored["last_error"]


def test_wecom_adapter_treats_non_json_2xx_as_delivered(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_adapters import WeComDeliveryAdapter
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    registry = default_delivery_registry()
    registry.register(WeComDeliveryAdapter(sender=lambda url, payload, timeout=10: (200, "ok")))
    event = enqueue_result(
        {"id": "job-non-json", "name": "Daily", "deliver": f"wecom:{url}"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
        registry=registry,
    )

    summary = DeliveryDispatcher(store=StateStore(), registry=registry).dispatch_due(
        limit=10,
        adapter_keys={"wecom"},
    )

    assert summary["delivered"] == 1
    assert DeliveryStore().get(event["id"])["status"] == "delivered"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery.py::test_wecom_adapter_sends_markdown_payload tests/test_cron_delivery.py::test_wecom_adapter_retries_http_429_and_5xx tests/test_cron_delivery.py::test_wecom_adapter_dead_letters_http_400 tests/test_cron_delivery.py::test_wecom_adapter_handles_wecom_business_errors tests/test_cron_delivery.py::test_wecom_adapter_treats_non_json_2xx_as_delivered -q
```

Expected: import failure for `WeComDeliveryAdapter`.

- [ ] **Step 3: Implement validation and adapter**

In `cron/delivery_adapters.py`, update imports:

```python
from urllib.parse import parse_qs, urlparse
```

Add these helpers and class after `WebhookDeliveryAdapter`:

```python
_WECOM_MAX_MARKDOWN_CHARS = 3900
_RETRYABLE_WECOM_ERRCODES = {45009}


def validate_wecom_webhook_url(url: str | None) -> str | None:
    if not url:
        return "wecom delivery requires AGENT_CRON_WECOM_WEBHOOK_URL or explicit webhook URL"
    parsed = urlparse(str(url))
    if parsed.scheme not in {"http", "https"}:
        return "wecom webhook URL must use http or https"
    if parsed.netloc != "qyapi.weixin.qq.com":
        return "wecom webhook URL host must be qyapi.weixin.qq.com"
    if "/cgi-bin/webhook/send" not in parsed.path:
        return "wecom webhook URL path must include /cgi-bin/webhook/send"
    if not parse_qs(parsed.query).get("key"):
        return "wecom webhook URL requires key query parameter"
    return None


def _shorten_wecom_text(text: Any, max_chars: int) -> str:
    value = "" if text is None else str(text)
    if len(value) <= max_chars:
        return value
    marker = "\n\n...(truncated; full output saved locally)"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return value[: max_chars - len(marker)].rstrip() + marker


def _wecom_status(payload: dict[str, Any], run: dict[str, Any] | None) -> str:
    if payload.get("status"):
        return str(payload["status"])
    if run and run.get("status"):
        return str(run["status"])
    if payload.get("error"):
        return "error"
    return "success"


def _format_wecom_markdown(event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> str:
    payload = json.loads(event["payload_json"])
    job_name = payload.get("job_name") or (job or {}).get("name") or event.get("job_name") or "(unnamed)"
    job_id = payload.get("job_id") or event.get("job_id") or (job or {}).get("id") or "-"
    run_id = payload.get("run_id") or event.get("run_id") or (run or {}).get("id") or "-"
    status = _wecom_status(payload, run)
    output_path = payload.get("output_path") or event.get("output_path") or (run or {}).get("output_path")
    detail = payload.get("final_response") or payload.get("error") or ""

    header = [
        f"### Cron job update: {job_name}",
        f">status: {status}",
        f">job_id: {job_id}",
        f">run_id: {run_id}",
    ]
    if output_path:
        header.append(f">output_path: {output_path}")
    body = _shorten_wecom_text(detail, _WECOM_MAX_MARKDOWN_CHARS - len("\n".join(header)) - 20)
    return "\n".join(header + ["", body])


class WeComDeliveryAdapter:
    key = "wecom"
    active_dispatch = True

    def __init__(self, sender: Callable[[str, dict[str, Any], int], tuple[int, str]] | None = None) -> None:
        from cron.delivery import default_webhook_sender

        self.sender = sender or default_webhook_sender

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        error = validate_wecom_webhook_url(target.address)
        return AdapterValidation(error is None, error)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        if not event.get("address"):
            return DeliveryResult(False, retryable=False, error="wecom delivery requires AGENT_CRON_WECOM_WEBHOOK_URL or explicit webhook URL")
        markdown = _format_wecom_markdown(event, job, run)
        payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
        try:
            status, body = self.sender(str(event["address"]), payload, 10)
        except Exception as exc:
            return DeliveryResult(False, retryable=True, error=str(exc))
        if status in {408, 429} or status >= 500:
            return DeliveryResult(False, retryable=True, error=f"HTTP {status}: {body}")
        if not (200 <= status < 300):
            return DeliveryResult(False, retryable=False, error=f"HTTP {status}: {body}")
        try:
            parsed = json.loads(body or "{}")
        except json.JSONDecodeError:
            return DeliveryResult(True)
        errcode = parsed.get("errcode", 0)
        if errcode in (0, "0", None):
            return DeliveryResult(True)
        errmsg = str(parsed.get("errmsg") or body or "wecom delivery failed")
        retryable = errcode in _RETRYABLE_WECOM_ERRCODES
        return DeliveryResult(False, retryable=retryable, error=f"WeCom errcode {errcode}: {errmsg}")
```

- [ ] **Step 4: Register the adapter by default**

In `cron/delivery_registry.py`, change `build_delivery_registry()` import and registration:

```python
def build_delivery_registry(*, webhook_sender=None, extra_adapters=None) -> DeliveryRegistry:
    from cron.delivery_adapters import LocalDeliveryAdapter, OriginDeliveryAdapter, WebhookDeliveryAdapter, WeComDeliveryAdapter

    registry = DeliveryRegistry()
    registry.register(LocalDeliveryAdapter())
    registry.register(OriginDeliveryAdapter())
    registry.register(WebhookDeliveryAdapter(sender=webhook_sender))
    registry.register(WeComDeliveryAdapter())
    for adapter in extra_adapters or []:
        registry.register(adapter)
    for factory in list(_ADAPTER_FACTORIES):
        adapter = factory(webhook_sender=webhook_sender)
        if adapter is not None:
            registry.register(adapter)
    return registry
```

- [ ] **Step 5: Run Task 1 and Task 2 tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery.py::test_bare_wecom_delivery_uses_env_url tests/test_cron_delivery.py::test_explicit_wecom_delivery_overrides_env_url tests/test_cron_delivery.py::test_bare_wecom_delivery_requires_env_url tests/test_cron_delivery.py::test_wecom_adapter_sends_markdown_payload tests/test_cron_delivery.py::test_wecom_adapter_retries_http_429_and_5xx tests/test_cron_delivery.py::test_wecom_adapter_dead_letters_http_400 tests/test_cron_delivery.py::test_wecom_adapter_handles_wecom_business_errors tests/test_cron_delivery.py::test_wecom_adapter_treats_non_json_2xx_as_delivered -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Run focused delivery suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery.py -q
```

Expected: all tests in `tests/test_cron_delivery.py` pass.

- [ ] **Step 7: Commit target semantics and adapter**

```bash
git add cron/delivery_targets.py cron/delivery_adapters.py cron/delivery_registry.py tests/test_cron_delivery.py
git commit -m "feat: add wecom cron delivery adapter"
```

---

## Task 3: Doctor Validation

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Update adapter-list test expectation**

In `tests/test_agent_cli_cron_commands.py`, update `test_cron_doctor_lists_delivery_adapters`:

```python
def test_cron_doctor_lists_delivery_adapters(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_doctor

    result = cron_doctor()

    assert "delivery adapters:" in result.text
    assert "local" in result.text
    assert "origin" in result.text
    assert "webhook" in result.text
    assert "wecom" in result.text
```

- [ ] **Step 2: Add missing and malformed WeCom doctor tests**

Add these tests near `test_cron_doctor_lists_delivery_adapters`:

```python
def test_cron_doctor_fails_active_wecom_job_without_default_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv(
        "AGENT_CRON_WECOM_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=env-key",
    )

    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job

    job = create_job(
        prompt="write report",
        schedule="every 30m",
        name="WeCom Job",
        deliver="wecom",
    )
    monkeypatch.delenv("AGENT_CRON_WECOM_WEBHOOK_URL", raising=False)

    result = cron_doctor()

    assert result.exit_code == 2
    assert f"active job {job['id']} delivery invalid" in result.text
    assert "wecom delivery requires AGENT_CRON_WECOM_WEBHOOK_URL or explicit webhook URL" in result.text


def test_cron_doctor_fails_active_wecom_job_with_malformed_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from cron.state_store import StateStore

    job = create_job(
        prompt="write report",
        schedule="every 30m",
        name="WeCom Bad URL",
        deliver="local",
    )
    with StateStore()._connect() as conn:
        conn.execute(
            "UPDATE jobs SET deliver = ?, delivery_targets_json = NULL WHERE id = ?",
            ("wecom:https://example.invalid/hook", job["id"]),
        )

    result = cron_doctor()

    assert result.exit_code == 2
    assert f"active job {job['id']} delivery invalid" in result.text
    assert "wecom webhook URL host must be qyapi.weixin.qq.com" in result.text
```

- [ ] **Step 3: Run tests to verify the new ones fail if doctor lacks explicit WeCom branch**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_lists_delivery_adapters tests/test_agent_cli_cron_commands.py::test_cron_doctor_fails_active_wecom_job_without_default_url tests/test_agent_cli_cron_commands.py::test_cron_doctor_fails_active_wecom_job_with_malformed_url -q
```

Expected: adapter-list test may already pass from Task 2; malformed/missing validation should fail until doctor imports and applies WeCom URL validation to stored targets and parsed targets.

- [ ] **Step 4: Implement doctor WeCom validation**

In `agent_cli/cron_commands.py`, add this import inside `cron_doctor()` with the existing delivery imports:

```python
    from cron.delivery_adapters import validate_wecom_webhook_url
```

Then extend the target validation loop near the existing webhook branch:

```python
        for target in validation.targets:
            if target.target_type == "webhook":
                webhook_error = validate_webhook_url(target.address)
                if webhook_error:
                    add("fail", f"active job {job.get('id')} webhook delivery invalid: {webhook_error}")
            if target.adapter_key == "wecom":
                wecom_error = validate_wecom_webhook_url(target.address)
                if wecom_error:
                    add("fail", f"active job {job.get('id')} wecom delivery invalid: {wecom_error}")
```

If the registry validation already fails first, keep that failure. The explicit loop is still needed for stored targets and future validation clarity.

- [ ] **Step 5: Run doctor tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_lists_delivery_adapters tests/test_agent_cli_cron_commands.py::test_cron_doctor_fails_active_wecom_job_without_default_url tests/test_agent_cli_cron_commands.py::test_cron_doctor_fails_active_wecom_job_with_malformed_url -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Run cron command tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py -q
```

Expected: all tests in `tests/test_agent_cli_cron_commands.py` pass.

- [ ] **Step 7: Commit doctor support**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: validate wecom cron delivery in doctor"
```

---

## Task 4: Service E2E For WeCom Delivery And Retry

**Files:**
- Modify: `tests/test_cron_e2e.py`

- [ ] **Step 1: Add WeCom constants and sender installer**

Near existing `WEBHOOK_URL`, add:

```python
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=e2e"
```

After `install_webhook_sender()`, add:

```python
def install_wecom_sender(
    sender: Callable[[str, dict[str, Any], int], tuple[int, str]],
) -> None:
    import cron.delivery_registry as delivery_registry

    def factory(**kwargs):
        from cron.delivery_adapters import WeComDeliveryAdapter

        return WeComDeliveryAdapter(sender=sender)

    delivery_registry.register_delivery_adapter_factory(factory)
```

- [ ] **Step 2: Add service success E2E**

Add this test after `test_service_executes_due_job_saves_output_and_delivers_webhook`:

```python
def test_service_executes_due_job_and_delivers_wecom_markdown(isolated_cron_home, monkeypatch):
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    monkeypatch.setenv("AGENT_CRON_WECOM_WEBHOOK_URL", WECOM_URL)
    sender = ScriptedWebhookSender([(200, '{"errcode":0,"errmsg":"ok"}')])
    install_wecom_sender(sender)
    job = create_due_job(deliver="wecom")
    runner = RecordingRunner(
        output_doc="# WeCom E2E Output\nbody",
        final_response="wecom final response",
    )

    service, tick_result, exit_code = run_service_once(BASE_TIME, runner)

    store = StateStore()
    delivery_store = DeliveryStore()
    runs = store.runs_for_job(job["id"])
    events = delivery_store.list_events(job_id=job["id"], limit=10)

    assert exit_code == 0
    assert tick_result.ran == 1
    assert len(runner.calls) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["delivery_status"] == "delivered"
    assert Path(runs[0]["output_path"]).exists()
    assert events[0]["adapter_key"] == "wecom"
    assert events[0]["status"] == "delivered"
    assert sender.calls[0][0] == WECOM_URL
    payload = sender.calls[0][1]
    assert payload["msgtype"] == "markdown"
    content = payload["markdown"]["content"]
    assert "wecom final response" in content
    assert job["id"] in content
    assert runs[0]["id"] in content
    assert runs[0]["output_path"] in content
    assert service.status["last_tick"]["delivery"]["delivered"] == 1
```

- [ ] **Step 3: Add no-due retry E2E**

Add this test after `test_service_retries_failed_delivery_on_no_due_tick`:

```python
def test_service_retries_failed_wecom_delivery_on_no_due_tick(isolated_cron_home, monkeypatch):
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    monkeypatch.setenv("AGENT_CRON_WECOM_WEBHOOK_URL", WECOM_URL)
    sender = ScriptedWebhookSender([(500, "down"), (200, '{"errcode":0,"errmsg":"ok"}')])
    install_wecom_sender(sender)
    job = create_due_job(deliver="wecom")
    runner = RecordingRunner(output_doc="# retry output", final_response="retry wecom")
    delivery_store = DeliveryStore()

    _service1, first_tick, first_exit = run_service_once(BASE_TIME, runner)

    store = StateStore()
    first_run = store.runs_for_job(job["id"])[0]
    first_events = delivery_store.list_events(job_id=job["id"], limit=10)
    assert first_exit == 0
    assert first_tick.ran == 1
    assert first_events[0]["status"] == "failed"
    assert "HTTP 500" in (store.get_job(job["id"])["last_delivery_error"] or "")

    delivery_store.update_event(
        first_events[0]["id"],
        next_attempt_at="2000-01-01T00:00:00+00:00",
    )
    _service2, second_tick, second_exit = run_service_once(
        "2026-06-02T10:01:00+00:00",
        runner,
    )

    second_run = store.get_run(first_run["id"])
    second_events = delivery_store.list_events(job_id=job["id"], limit=10)
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

- [ ] **Step 4: Run E2E tests to verify they pass**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_e2e.py -q
```

Expected: all E2E cron tests pass.

- [ ] **Step 5: Commit E2E coverage**

```bash
git add tests/test_cron_e2e.py
git commit -m "test: cover wecom cron delivery e2e"
```

---

## Task 5: Final Verification

**Files:**
- No new code changes unless verification reveals a bug.

- [ ] **Step 1: Run focused cron delivery and E2E suites**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_delivery.py tests/test_cron_e2e.py tests/test_agent_cli_cron_commands.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run broader cron regression suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_scheduler.py tests/test_cron_service.py tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_state_store.py tests/test_cron_delivery_store.py tests/test_cron_e2e.py tests/test_agent_cli_cron_commands.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Inspect git status and recent commits**

Run:

```bash
git status --short
git log --oneline --max-count=8
```

Expected: only expected worktree changes remain. The pre-existing untracked file `docs/superpowers/plans/2026-06-01-cron-concurrency-cli-management.md` may still be present and must not be staged unless the user explicitly requests it.

- [ ] **Step 4: Request code review before merge**

Use `superpowers:requesting-code-review`. Ask the review agent to check:

- WeCom target parsing does not break generic `platform:chat_id`.
- URL validation matches the spec.
- Adapter failure classification correctly updates delivery retry/dead-letter state through the existing dispatcher.
- Doctor validation catches missing/malformed WeCom configuration.
- E2E tests prove service tick success and no-due retry.

- [ ] **Step 5: Address review feedback**

If review requests changes, use `superpowers:receiving-code-review`, make the minimal corrections, rerun the relevant tests from Step 1 and Step 2, and commit the fixes.

- [ ] **Step 6: Finish branch**

Use `superpowers:finishing-a-development-branch`. Offer the user the standard completion choices: merge locally, open PR, or keep branch for inspection.

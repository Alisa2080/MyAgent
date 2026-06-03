from __future__ import annotations

import json

import pytest


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


def test_enqueue_local_result_marks_delivered(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "local"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert event is not None
    stored = DeliveryStore().get(event["id"])
    assert stored["target_type"] == "local"
    assert stored["adapter_key"] == "local"
    assert stored["status"] == "delivered"


def test_silent_success_does_not_enqueue(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "origin", "origin": {"thread_id": "t1"}},
        JobRunResult(success=True, output_doc="# out", final_response="[SILENT] nothing"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert event is None
    assert DeliveryStore().stats()["pending"] == 0


def test_failed_origin_result_enqueues_error_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "origin", "origin": {"thread_id": "t1"}},
        JobRunResult(success=False, output_doc="# out", final_response="", error="boom"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert event is not None
    stored = DeliveryStore().get(event["id"])
    assert stored["target_type"] == "origin"
    assert stored["status"] == "pending"
    assert stored["last_error"] is None
    assert '"status": "error"' in stored["payload_json"]
    assert '"error": "boom"' in stored["payload_json"]


def test_webhook_success(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    sent = []

    def fake_post(url, payload, timeout=10):
        sent.append((url, payload, timeout))
        return 204, "ok"

    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    summary = process_due(limit=10, webhook_sender=fake_post)

    assert summary["delivered"] == 1
    assert sent[0][0] == "https://example.invalid/hook"
    assert sent[0][1]["job_id"] == "job-1"
    assert DeliveryStore().get(event["id"])["status"] == "delivered"


def test_bare_webhook_dispatch_uses_env_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_WEBHOOK_URL", "https://example.invalid/env-hook")
    sent = []

    def fake_post(url, payload, timeout=10):
        sent.append((url, payload, timeout))
        return 204, "ok"

    from cron.delivery import JobRunResult, enqueue_result, process_due
    from cron.delivery_store import DeliveryStore

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    summary = process_due(limit=10, webhook_sender=fake_post)

    assert summary["delivered"] == 1
    assert sent[0][0] == "https://example.invalid/env-hook"
    assert DeliveryStore().get(event["id"])["address"] == "https://example.invalid/env-hook"


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

    with pytest.raises(
        ValueError,
        match="wecom delivery requires AGENT_CRON_WECOM_WEBHOOK_URL or explicit webhook URL",
    ):
        create_job(prompt="write report", schedule="30m", deliver="wecom")


def test_wecom_delivery_rejects_invalid_port_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    invalid_url = "https://qyapi.weixin.qq.com:bad/cgi-bin/webhook/send?key=abc"

    from cron.jobs import create_job

    with pytest.raises(ValueError, match="wecom webhook URL port is invalid"):
        create_job(prompt="write report", schedule="30m", deliver=f"wecom:{invalid_url}")


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


def test_process_due_injects_webhook_sender_into_default_wecom_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    sent = []
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"

    from cron.delivery import JobRunResult, enqueue_result, process_due
    from cron.delivery_store import DeliveryStore

    def fake_post(post_url, payload, timeout=10):
        sent.append((post_url, payload, timeout))
        return 200, '{"errcode":0,"errmsg":"ok"}'

    event = enqueue_result(
        {"id": "job-default-wecom", "name": "Daily", "deliver": f"wecom:{url}"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    summary = process_due(limit=10, webhook_sender=fake_post)

    assert summary["delivered"] == 1
    assert sent[0][0] == url
    assert sent[0][1]["msgtype"] == "markdown"
    assert DeliveryStore().get(event["id"])["status"] == "delivered"


def test_wecom_adapter_treats_non_dict_json_2xx_as_delivered():
    from cron.delivery_adapters import WeComDeliveryAdapter

    adapter = WeComDeliveryAdapter(sender=lambda url, payload, timeout=10: (200, '["ok"]'))
    result = adapter.deliver(
        {
            "id": "event-1",
            "address": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            "payload_json": '{"job_id":"job-1","job_name":"Daily","status":"ok"}',
        },
        None,
        None,
    )

    assert result.delivered is True


def test_wecom_adapter_retries_string_temporary_errcode():
    from cron.delivery_adapters import WeComDeliveryAdapter

    adapter = WeComDeliveryAdapter(sender=lambda url, payload, timeout=10: (200, '{"errcode":"45009","errmsg":"rate limited"}'))
    result = adapter.deliver(
        {
            "id": "event-1",
            "address": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            "payload_json": '{"job_id":"job-1","job_name":"Daily","status":"ok"}',
        },
        None,
        None,
    )

    assert result.delivered is False
    assert result.retryable is True
    assert "rate limited" in result.error


def test_wecom_adapter_validates_persisted_event_address_before_sending(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    sent = []

    from cron.delivery_adapters import WeComDeliveryAdapter
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.state_store import StateStore

    def fake_post(post_url, payload, timeout=10):
        sent.append((post_url, payload, timeout))
        return 200, '{"errcode":0,"errmsg":"ok"}'

    store = StateStore()
    event = store.enqueue_delivery_event(
        job_id="job-invalid-wecom",
        run_id=None,
        job_name="Daily",
        run_at="2026-05-28T10:00:00+00:00",
        target="wecom:https://qyapi.weixin.qq.com/cgi-bin/webhook/send-extra?key=abc",
        target_type="platform",
        adapter_key="wecom",
        address="https://qyapi.weixin.qq.com/cgi-bin/webhook/send-extra?key=abc",
        thread_id=None,
        origin=None,
        final_response="done",
        output_path="/tmp/out.md",
        payload={"job_id": "job-invalid-wecom", "job_name": "Daily", "status": "ok"},
        status="pending",
    )
    registry = default_delivery_registry()
    registry.register(WeComDeliveryAdapter(sender=fake_post))

    summary = DeliveryDispatcher(store=store, registry=registry).dispatch_due(
        limit=10,
        adapter_keys={"wecom"},
    )

    stored = store.get_delivery_event(event["id"])
    assert summary["dead"] == 1
    assert stored["status"] == "dead"
    assert "path must be /cgi-bin/webhook/send" in stored["last_error"]
    assert sent == []


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


def test_feishu_delivery_reuses_default_gateway_token_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    import gateway.platforms.feishu as feishu_platform
    import gateway.registry as gateway_registry
    from cron.delivery_adapters import FeishuDeliveryAdapter
    from gateway.platforms.feishu import FeishuPlatformAdapter

    calls = []

    def fake_http_sender(url, payload, headers=None):
        calls.append((url, payload, headers or {}))
        if url == FeishuPlatformAdapter.TOKEN_URL:
            return 200, json.dumps({"code": 0, "tenant_access_token": "cached-token", "expire": 7200})
        if url == FeishuPlatformAdapter.SEND_URL:
            return 200, json.dumps({"code": 0, "msg": "ok"})
        raise AssertionError(f"unexpected Feishu URL: {url}")

    gateway_registry.clear_gateway_adapter_factories()
    monkeypatch.setattr(feishu_platform, "_default_http_sender", fake_http_sender)
    adapter = FeishuDeliveryAdapter()
    event = {
        "id": "event-1",
        "job_id": "job-1",
        "job_name": "Daily",
        "address": "oc_123",
        "payload_json": json.dumps({"job_id": "job-1", "job_name": "Daily", "status": "ok"}),
    }

    try:
        first = adapter.deliver(event, None, None)
        second = adapter.deliver({**event, "id": "event-2"}, None, None)
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    assert first.delivered is True
    assert second.delivered is True
    token_calls = [call for call in calls if call[0] == FeishuPlatformAdapter.TOKEN_URL]
    assert len(token_calls) == 1


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


def test_enqueue_result_uses_stored_delivery_targets(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_WEBHOOK_URL", "https://example.invalid/create-hook")
    sent = []

    from cron.delivery import JobRunResult, enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.jobs import create_job

    job = create_job(prompt="write report", schedule="30m", deliver="webhook")
    monkeypatch.setenv("AGENT_CRON_WEBHOOK_URL", "https://example.invalid/changed-hook")

    event = enqueue_result(
        job,
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    summary = process_due(
        limit=10,
        webhook_sender=lambda url, payload, timeout=10: sent.append(url) or (204, "ok"),
    )

    assert summary["delivered"] == 1
    assert sent == ["https://example.invalid/create-hook"]
    assert DeliveryStore().get(event["id"])["address"] == "https://example.invalid/create-hook"


def test_process_due_uses_supplied_store(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "home"))
    sent = []

    def fake_post(url, payload, timeout=10):
        sent.append((url, payload, timeout))
        return 204, "ok"

    from cron.delivery import JobRunResult, enqueue_result, process_due
    from cron.delivery_store import DeliveryStore

    store = DeliveryStore(tmp_path / "custom.sqlite3")
    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
        store=store,
    )

    summary = process_due(limit=10, store=store, webhook_sender=fake_post)

    assert summary["delivered"] == 1
    assert sent[0][0] == "https://example.invalid/hook"
    assert store.get(event["id"])["status"] == "delivered"


def test_webhook_4xx_marks_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    def fake_post(url, payload, timeout=10):
        return 401, "unauthorized"

    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    summary = process_due(limit=10, webhook_sender=fake_post)

    assert summary["dead"] == 1
    stored = DeliveryStore().get(event["id"])
    assert stored["status"] == "dead"
    assert "HTTP 401" in stored["last_error"]


def test_invalid_webhook_url_is_dead_without_network(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:file:///etc/passwd"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    stored = DeliveryStore().get(event["id"])
    assert stored["status"] == "dead"
    assert "http or https" in stored["last_error"]


def test_webhook_sender_rejects_dns_private_address(monkeypatch):
    import socket

    from cron.delivery import default_webhook_sender

    def fake_getaddrinfo(host, port, type=0):
        assert host == "internal.example"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.5", port),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    try:
        default_webhook_sender("https://internal.example/hook", {"ok": True})
    except ValueError as exc:
        assert "private or local" in str(exc)
    else:
        raise AssertionError("expected private DNS target to be rejected")


def test_webhook_sender_does_not_follow_redirects(monkeypatch):
    import urllib.error
    import urllib.request

    from cron.delivery import default_webhook_sender

    opened = {}

    class FakeOpener:
        def open(self, request, timeout=10):
            opened["url"] = request.full_url
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "http://127.0.0.1/admin"},
                None,
            )

    monkeypatch.setenv("AGENT_CRON_ALLOW_PRIVATE_WEBHOOKS", "1")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())

    status, _body = default_webhook_sender("https://example.invalid/hook", {"ok": True})

    assert status == 302
    assert opened["url"] == "https://example.invalid/hook"


def test_process_due_dead_letters_non_gateway_origin_events(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.delivery import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "origin", "origin": {"thread_id": "t1"}},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    summary = process_due(limit=10)
    stored = DeliveryStore().get(event["id"])
    assert summary["claimed"] == 1
    assert summary["dead"] == 1
    assert stored["status"] == "dead"
    assert stored["attempt_count"] == 1


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


def test_enqueue_result_creates_event_per_delivery_target(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result
    from cron.delivery_store import DeliveryStore

    events = enqueue_result(
        {
            "id": "job-1",
            "name": "Daily",
            "deliver": "origin,webhook:https://example.invalid/hook,local",
            "origin": {"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
        },
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert isinstance(events, list)
    assert len(events) == 3
    stored = [DeliveryStore().get(event["id"]) for event in events]
    assert [(event["target_type"], event["adapter_key"], event["address"]) for event in stored] == [
        ("origin", "origin", "session-1"),
        ("webhook", "webhook", "https://example.invalid/hook"),
        ("local", "local", None),
    ]


def test_dispatcher_updates_run_delivery_status(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import JobRunResult, enqueue_result, process_due
    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore

    job = create_job(
        prompt="write report",
        schedule="30m",
        name="Daily",
        deliver="webhook:https://example.invalid/hook",
    )
    update_job(job["id"], {"next_run_at": "2026-05-28T10:00:00+00:00"})
    store = StateStore()
    claimed = store.claim_due_jobs(now_text="2026-05-28T10:00:00+00:00", limit=1)[0]
    claimed_job = dict(claimed["job"])
    claimed_job["run_id"] = claimed["run"]["id"]

    enqueue_result(
        claimed_job,
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    summary = process_due(
        limit=10,
        webhook_sender=lambda url, payload, timeout=10: (204, "ok"),
    )

    assert summary["delivered"] == 1
    assert store.get_run(claimed["run"]["id"])["delivery_status"] == "delivered"


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


def test_process_due_does_not_duplicate_origin_events_from_previous_poll(tmp_path, monkeypatch):
    import cron.origin_poller as origin_poller
    from cron.delivery_store import DeliveryStore
    from cron.delivery import process_due
    from cron.delivery_adapters import DeliveryResult
    import cron.delivery_registry as delivery_registry

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    events = []

    class MockOriginAdapter:
        key = "test-origin"

        def validate(self, target, job):
            from cron.delivery_adapters import AdapterValidation
            return AdapterValidation(True)

        def poll(self, target, job, *, cursor=None, limit=100):
            if cursor is None:
                return {
                    "events": [
                        {"id": "orig1", "type": "origin_event", "created_at": "2026-05-28T10:00:00Z"},
                    ],
                    "next_cursor": "page-1",
                }
            return {"events": [], "next_cursor": cursor}

        def deliver(self, event, job, run):
            events.append(event)
            return DeliveryResult(False, retryable=False, error="not for me")

    adapter = MockOriginAdapter()
    delivery_registry.clear_delivery_adapter_factories()
    delivery_registry.register_delivery_adapter_factory(lambda **kwargs: MockOriginAdapter())
    try:
        store = DeliveryStore()
        store.register_delivery_adapter(adapter)

        origin_poller.poll_deliveries([adapter], store=store, limit=100)
        assert len(events) == 0

        summary = process_due(limit=10)
        assert summary["claimed"] == 1

        events.clear()
        origin_poller.poll_deliveries([adapter], store=store, limit=100)
        assert len(events) == 0
    finally:
        delivery_registry.clear_delivery_adapter_factories()

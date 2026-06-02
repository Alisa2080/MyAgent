from __future__ import annotations

import json

import pytest

from gateway.contracts import OutboundMessage, PlatformMessageTarget
from gateway.platforms.feishu import FeishuPlatformAdapter


class FakeHttpSender:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers=None):
        self.calls.append((url, payload, headers or {}))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def feishu_target(target_id="oc_123"):
    return PlatformMessageTarget(
        platform="feishu",
        target_type="chat_id",
        target_id=target_id,
    )


def feishu_message(text="hello"):
    return OutboundMessage(text=text)


def token_response(token="tenant-token", expire=7200):
    return (
        200,
        json.dumps(
            {
                "code": 0,
                "tenant_access_token": token,
                "expire": expire,
            }
        ),
    )


def send_response():
    return (200, json.dumps({"code": 0, "msg": "ok"}))


def test_env_validation_requires_feishu_app_id_and_secret(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    adapter = FeishuPlatformAdapter(http_sender=FakeHttpSender([]))

    result = adapter.validate_target(feishu_target())

    assert result.ok is False
    assert result.retryable is False
    assert "FEISHU_APP_ID" in (result.error or "")
    assert "FEISHU_APP_SECRET" in (result.error or "")


@pytest.mark.parametrize(
    ("target", "error_text"),
    [
        (
            PlatformMessageTarget(platform="slack", target_type="chat_id", target_id="oc_123"),
            "only supports feishu",
        ),
        (
            PlatformMessageTarget(platform="feishu", target_type="email", target_id="oc_123"),
            "only supports chat_id",
        ),
        (
            PlatformMessageTarget(platform="feishu", target_type="chat_id", target_id=""),
            "requires a chat_id",
        ),
    ],
)
def test_validate_target_rejects_unsupported_targets(monkeypatch, target, error_text):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    adapter = FeishuPlatformAdapter(http_sender=FakeHttpSender([]))

    result = adapter.validate_target(target)

    assert result.ok is False
    assert result.retryable is False
    assert error_text in (result.error or "")


def test_token_smoke_calls_token_path_without_sending_message(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([token_response()])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.token_smoke()

    assert result.ok is True
    assert sender.calls == [
        (
            FeishuPlatformAdapter.TOKEN_URL,
            {"app_id": "app-id", "app_secret": "app-secret"},
            {},
        )
    ]


def test_token_success_caches_token_before_expiry(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([token_response(), send_response(), send_response()])
    adapter = FeishuPlatformAdapter(http_sender=sender, now=lambda: 1000)

    first = adapter.send_text(feishu_target(), feishu_message("first"))
    second = adapter.send_text(feishu_target(), feishu_message("second"))

    assert first.ok is True
    assert second.ok is True
    token_calls = [call for call in sender.calls if call[0] == FeishuPlatformAdapter.TOKEN_URL]
    assert len(token_calls) == 1


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_token_retryable_http_statuses_are_retryable(monkeypatch, status):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([(status, json.dumps({"code": 0, "msg": "temporary"}))])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is True
    assert str(status) in (result.error or "")


def test_token_invalid_json_is_retryable(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([(200, "not json")])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is True
    assert "valid JSON" in (result.error or "")


def test_token_missing_tenant_access_token_is_retryable(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([(200, json.dumps({"code": 0, "expire": 7200}))])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is True
    assert "tenant_access_token" in (result.error or "")


def test_send_payload_uses_feishu_message_api_shape(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([token_response(token="abc123"), send_response()])
    adapter = FeishuPlatformAdapter(http_sender=sender, now=lambda: 1000)

    result = adapter.send_text(feishu_target("oc_chat"), feishu_message("hello feishu"))

    assert result.ok is True
    url, payload, headers = sender.calls[1]
    assert url == FeishuPlatformAdapter.SEND_URL
    assert headers["Authorization"] == "Bearer abc123"
    assert payload == {
        "receive_id": "oc_chat",
        "msg_type": "text",
        "content": json.dumps({"text": "hello feishu"}, ensure_ascii=False),
    }


def test_token_network_exception_is_retryable(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    adapter = FeishuPlatformAdapter(http_sender=FakeHttpSender([OSError("network down")]))

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is True
    assert "token" in (result.error or "").lower()


def test_token_credential_failure_is_non_retryable(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "bad-app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "bad-app-secret")
    sender = FakeHttpSender([(200, json.dumps({"code": 99991663, "msg": "invalid app"}))])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is False
    assert "invalid app" in (result.error or "")


def test_send_exception_is_retryable(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([token_response(), OSError("send failed")])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is True
    assert "send" in (result.error or "").lower()


def test_send_http_429_is_retryable(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([token_response(), (429, json.dumps({"code": 0, "msg": "rate limit"}))])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is True
    assert "429" in (result.error or "")


def test_send_http_400_with_retryable_feishu_api_code_is_retryable(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([token_response(), (400, json.dumps({"code": 230020, "msg": "too many requests"}))])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is True
    assert "230020" in (result.error or "")


@pytest.mark.parametrize("status", [408, 500, 503])
def test_send_retryable_http_statuses_are_retryable(monkeypatch, status):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([token_response(), (status, json.dumps({"code": 0, "msg": "temporary"}))])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is True
    assert str(status) in (result.error or "")


def test_send_api_chat_not_found_is_non_retryable(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([token_response(), (200, json.dumps({"code": 230001, "msg": "chat not found"}))])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is False
    assert "chat not found" in (result.error or "")


@pytest.mark.parametrize("code", [230020, 99991400])
def test_send_known_temporary_or_rate_limit_api_errors_are_retryable(monkeypatch, code):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([token_response(), (200, json.dumps({"code": code, "msg": "rate limited"}))])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is True
    assert str(code) in (result.error or "")


@pytest.mark.parametrize(
    ("code", "msg"),
    [
        (230002, "permission denied"),
        (230099, "invalid parameter"),
    ],
)
def test_send_permission_and_parameter_api_errors_are_non_retryable_by_default(monkeypatch, code, msg):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    sender = FakeHttpSender([token_response(), (200, json.dumps({"code": code, "msg": msg}))])
    adapter = FeishuPlatformAdapter(http_sender=sender)

    result = adapter.send_text(feishu_target(), feishu_message())

    assert result.ok is False
    assert result.retryable is False
    assert msg in (result.error or "")

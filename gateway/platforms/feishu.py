from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from gateway.contracts import OutboundMessage, PlatformMessageTarget, SendResult


def _default_http_sender(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class FeishuPlatformAdapter:
    key = "feishu"
    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    _EXPIRY_SKEW_SECONDS = 60

    def __init__(
        self,
        *,
        http_sender: Callable[[str, dict[str, Any], dict[str, str] | None], tuple[int, str]] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.http_sender = http_sender or _default_http_sender
        self._now = now or time.time
        self._tenant_access_token: str | None = None
        self._tenant_access_token_expires_at = 0.0

    def validate_target(self, target: PlatformMessageTarget) -> SendResult:
        if target.platform != "feishu":
            return SendResult(False, error="feishu adapter only supports feishu targets")
        if target.target_type != "chat_id":
            return SendResult(False, error="feishu delivery only supports chat_id targets")
        if not target.target_id:
            return SendResult(False, error="feishu delivery requires a chat_id")
        missing = [
            name
            for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
            if not os.environ.get(name)
        ]
        if missing:
            return SendResult(False, error=f"missing required Feishu environment variables: {', '.join(missing)}")
        return SendResult(True)

    def token_smoke(self) -> SendResult:
        _, result = self._ensure_token()
        return result

    def send_text(self, target: PlatformMessageTarget, message: OutboundMessage) -> SendResult:
        validation = self.validate_target(target)
        if not validation.ok:
            return validation
        token, token_result = self._ensure_token()
        if not token_result.ok or token is None:
            return token_result

        payload = {
            "receive_id": target.target_id,
            "msg_type": "text",
            "content": json.dumps({"text": message.text}, ensure_ascii=False),
        }
        try:
            status, body = self.http_sender(
                self.SEND_URL,
                payload,
                {"Authorization": f"Bearer {token}"},
            )
        except Exception as exc:
            return SendResult(False, error=f"feishu send request failed: {exc}", retryable=True)

        if self._is_retryable_http_status(status):
            return SendResult(False, error=f"feishu send HTTP {status}: {body}", retryable=True)
        if status < 200 or status >= 300:
            return SendResult(False, error=f"feishu send HTTP {status}: {body}", retryable=False)

        parsed = self._parse_json(body)
        if parsed is None:
            return SendResult(False, error="feishu send response was not valid JSON", retryable=True)

        code = parsed.get("code", 0)
        if code != 0:
            msg = parsed.get("msg") or parsed.get("message") or "unknown error"
            return SendResult(
                False,
                error=f"feishu send API error {code}: {msg}",
                retryable=self._is_retryable_send_code(code),
            )
        return SendResult(True)

    def _ensure_token(self) -> tuple[str | None, SendResult]:
        if self._tenant_access_token and self._now() < self._tenant_access_token_expires_at:
            return self._tenant_access_token, SendResult(True)

        app_id = os.environ.get("FEISHU_APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET")
        missing = [
            name
            for name, value in (
                ("FEISHU_APP_ID", app_id),
                ("FEISHU_APP_SECRET", app_secret),
            )
            if not value
        ]
        if missing:
            return None, SendResult(False, error=f"missing required Feishu environment variables: {', '.join(missing)}")

        try:
            status, body = self.http_sender(
                self.TOKEN_URL,
                {
                    "app_id": app_id,
                    "app_secret": app_secret,
                },
                None,
            )
        except Exception as exc:
            return None, SendResult(False, error=f"feishu token request failed: {exc}", retryable=True)

        if self._is_retryable_http_status(status):
            return None, SendResult(False, error=f"feishu token HTTP {status}: {body}", retryable=True)
        if status < 200 or status >= 300:
            return None, SendResult(False, error=f"feishu token HTTP {status}: {body}", retryable=False)

        parsed = self._parse_json(body)
        if parsed is None:
            return None, SendResult(False, error="feishu token response was not valid JSON", retryable=True)

        code = parsed.get("code", 0)
        if code != 0:
            msg = parsed.get("msg") or parsed.get("message") or "unknown error"
            return None, SendResult(False, error=f"feishu token API error {code}: {msg}", retryable=False)

        token = parsed.get("tenant_access_token")
        if not token:
            return None, SendResult(False, error="feishu token response did not include tenant_access_token", retryable=True)

        expire_seconds = parsed.get("expire", 0)
        try:
            expire_seconds = int(expire_seconds)
        except (TypeError, ValueError):
            expire_seconds = 0
        self._tenant_access_token = str(token)
        self._tenant_access_token_expires_at = self._now() + max(0, expire_seconds - self._EXPIRY_SKEW_SECONDS)
        return self._tenant_access_token, SendResult(True)

    @staticmethod
    def _parse_json(body: str | bytes) -> dict[str, Any] | None:
        try:
            if isinstance(body, bytes):
                body = body.decode("utf-8")
            parsed = json.loads(body)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    @staticmethod
    def _is_retryable_http_status(status: int) -> bool:
        return status in {408, 429} or status >= 500

    @staticmethod
    def _is_retryable_send_code(code: Any) -> bool:
        return code != 230001

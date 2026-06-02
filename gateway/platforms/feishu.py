from __future__ import annotations

from typing import Any

from gateway.contracts import OutboundMessage, PlatformMessageTarget, SendResult


class FeishuPlatformAdapter:
    key = "feishu"

    def __init__(self, *, http_sender: Any = None) -> None:
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

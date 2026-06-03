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


@dataclass(frozen=True)
class InboundEvent:
    platform: str
    event_id: str
    event_type: str
    chat_id: str
    text: str
    timestamp: str
    thread_id: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundParseResult:
    ok: bool
    event: InboundEvent | None = None
    response_body: dict[str, Any] | None = None
    status_code: int = 200
    error: str | None = None


class InboundPlatformAdapter(Protocol):
    key: str

    def parse_callback(self, headers: dict[str, str], body: dict[str, Any]) -> InboundParseResult:
        ...

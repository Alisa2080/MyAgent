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

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any


class DeliveryTargetError(ValueError):
    pass


@dataclass(frozen=True)
class DeliveryIdentity:
    source_type: str
    platform: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    session_id: str | None = None
    display_name: str | None = None

    @classmethod
    def from_job_origin(cls, origin: dict[str, Any] | None) -> "DeliveryIdentity | None":
        if not origin:
            return None
        return cls(
            source_type=str(origin.get("source_type") or ("gateway" if origin.get("platform") and origin.get("chat_id") else "cli")),
            platform=origin.get("platform"),
            chat_id=origin.get("chat_id"),
            thread_id=origin.get("thread_id"),
            session_id=origin.get("session_id") or origin.get("thread_id"),
            display_name=origin.get("display_name") or origin.get("chat_name"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "source_type": self.source_type,
                "platform": self.platform,
                "chat_id": self.chat_id,
                "thread_id": self.thread_id,
                "session_id": self.session_id,
                "display_name": self.display_name,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class DeliveryTarget:
    raw: str
    target_type: str
    adapter_key: str
    address: str | None = None
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def dedupe_key(self) -> tuple[str, str, str | None, str | None]:
        return (self.target_type, self.adapter_key, self.address, self.thread_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "target_type": self.target_type,
            "adapter_key": self.adapter_key,
            "address": self.address,
            "thread_id": self.thread_id,
            "metadata": self.metadata,
        }


def parse_delivery_targets(deliver: str | None, *, origin: DeliveryIdentity | None) -> list[DeliveryTarget]:
    raw = str(deliver or "local").strip() or "local"
    targets: list[DeliveryTarget] = []
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for part in [item.strip() for item in raw.split(",") if item.strip()]:
        target = _parse_one(part, origin=origin)
        key = target.dedupe_key()
        if key not in seen:
            seen.add(key)
            targets.append(target)
    if not targets:
        raise DeliveryTargetError("delivery target is required")
    return targets


def _parse_one(raw: str, *, origin: DeliveryIdentity | None) -> DeliveryTarget:
    lowered = raw.lower()
    if lowered == "local":
        return DeliveryTarget(raw=raw, target_type="local", adapter_key="local")
    if lowered == "origin":
        if origin is None:
            raise DeliveryTargetError("origin delivery requires origin identity")
        if origin.source_type == "cli" and not (origin.session_id or origin.thread_id):
            raise DeliveryTargetError("origin delivery requires CLI session_id or thread_id")
        if origin.source_type in {"gateway", "web"} and not (origin.chat_id or origin.session_id):
            raise DeliveryTargetError("origin delivery requires chat_id or session_id")
        return DeliveryTarget(
            raw=raw,
            target_type="origin",
            adapter_key="origin",
            address=origin.session_id or origin.chat_id or origin.thread_id,
            thread_id=origin.thread_id,
            metadata={"origin": origin.to_json()},
        )
    if lowered == "webhook":
        return DeliveryTarget(
            raw=raw,
            target_type="webhook",
            adapter_key="webhook",
            address=os.getenv("AGENT_CRON_WEBHOOK_URL") or None,
        )
    if lowered.startswith("webhook:"):
        return DeliveryTarget(
            raw=raw,
            target_type="webhook",
            adapter_key="webhook",
            address=raw.split(":", 1)[1].strip() or None,
        )
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
    if lowered == "feishu":
        return DeliveryTarget(raw=raw, target_type="platform", adapter_key="feishu")
    if lowered.startswith("feishu:"):
        rest = raw.split(":", 1)[1]
        chat_id, sep, thread_id = rest.partition(":")
        return DeliveryTarget(
            raw=raw,
            target_type="platform",
            adapter_key="feishu",
            address=chat_id.strip() or None,
            thread_id=(thread_id.strip() if sep else None) or None,
        )
    if ":" in raw:
        platform, rest = raw.split(":", 1)
        chat_id, sep, thread_id = rest.partition(":")
        return DeliveryTarget(
            raw=raw,
            target_type="platform",
            adapter_key=platform.lower(),
            address=chat_id or None,
            thread_id=thread_id if sep else None,
        )
    return DeliveryTarget(raw=raw, target_type="platform", adapter_key=lowered)

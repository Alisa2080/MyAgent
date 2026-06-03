from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from gateway.contracts import InboundEvent, OutboundMessage, PlatformMessageTarget
from gateway.registry import GatewayRegistry
from gateway.session_store import GatewaySession, GatewaySessionStore


@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    session_id: str
    duplicate: bool = False
    error: str | None = None


GatewayRunner = Callable[[InboundEvent, GatewaySession, dict[str, Any]], str | None]


class GatewayDispatcher:
    def __init__(self, *, store: GatewaySessionStore, registry: GatewayRegistry, runner: GatewayRunner) -> None:
        self.store = store
        self.registry = registry
        self.runner = runner

    def dispatch(self, event: InboundEvent) -> DispatchResult:
        session = self.store.get_or_create_session(
            platform=event.platform,
            chat_id=event.chat_id,
            thread_id=event.thread_id,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
        )
        if not self.store.claim_event(event.platform, event.event_id):
            return DispatchResult(True, session.session_id, duplicate=True)

        self.store.record_message(
            session.session_id,
            direction="inbound",
            platform=event.platform,
            event_id=event.event_id,
            text=event.text,
            raw=event.raw,
        )

        origin = {
            "source_type": "gateway",
            "platform": event.platform,
            "chat_id": event.chat_id,
            "thread_id": event.thread_id,
            "sender_id": event.sender_id,
            "display_name": event.sender_name,
            "session_id": session.session_id,
        }
        response_text = self.runner(event, session, origin)
        if response_text:
            adapter = self.registry.get(event.platform)
            if adapter is None:
                return DispatchResult(False, session.session_id, error=f"unsupported gateway platform: {event.platform}")

            target = PlatformMessageTarget(
                platform=event.platform,
                target_type="chat_id",
                target_id=event.chat_id,
                thread_id=event.thread_id,
            )
            send_result = adapter.send_text(target, OutboundMessage(text=response_text, metadata={"session_id": session.session_id}))
            if not send_result.ok:
                return DispatchResult(False, session.session_id, error=send_result.error)

            self.store.record_message(
                session.session_id,
                direction="outbound",
                platform=event.platform,
                event_id=None,
                text=response_text,
                raw={"send_result": "ok"},
            )

        return DispatchResult(True, session.session_id)
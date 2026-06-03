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


def _assistant_text_from_result(result: Any) -> str | None:
    if not isinstance(result, dict):
        return str(result) if result is not None else None
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        final = result.get("final_response")
        return str(final) if final else None
    last = messages[-1]
    if isinstance(last, dict):
        content = last.get("content")
    else:
        content = getattr(last, "content", None)
    return str(content) if content else None


def gateway_agent_config(origin: dict[str, Any]) -> dict[str, Any]:
    session_id = str(origin["session_id"])
    configurable = {
        "thread_id": session_id,
        "source_type": "gateway",
        "platform": origin.get("platform"),
        "chat_id": origin.get("chat_id"),
        "session_id": session_id,
        "display_name": origin.get("display_name"),
    }
    if origin.get("thread_id") is not None:
        configurable["origin_thread_id"] = origin.get("thread_id")
    return {
        "configurable": {
            key: value
            for key, value in configurable.items()
            if value is not None
        }
    }


def run_gateway_agent_turn(
    event: InboundEvent,
    session: GatewaySession,
    origin: dict[str, Any],
    *,
    agent: Any | None = None,
    checkpointer: Any | None = None,
) -> str | None:
    if agent is None:
        from agent_core.builders import build_agent

        agent = build_agent(include_cron_tools=True, checkpointer=checkpointer)

    from agent_core.agent_runner import invoke_agent_with_terminal_notifications

    result = invoke_agent_with_terminal_notifications(
        agent,
        {"messages": [{"role": "user", "content": event.text}]},
        gateway_agent_config(origin),
    )
    return _assistant_text_from_result(result)


class GatewayDispatcher:
    def __init__(self, *, store: GatewaySessionStore, registry: GatewayRegistry, runner: GatewayRunner | None = None) -> None:
        self.store = store
        self.registry = registry
        self.runner = runner or run_gateway_agent_turn

    def dispatch(self, event: InboundEvent) -> DispatchResult:
        session = self.store.get_or_create_session(
            platform=event.platform,
            chat_id=event.chat_id,
            thread_id=event.thread_id,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
        )
        if not self.store.begin_event_processing(event.platform, event.event_id):
            return DispatchResult(True, session.session_id, duplicate=True)

        try:
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
                    self.store.release_event(event.platform, event.event_id)
                    return DispatchResult(False, session.session_id, error=f"unsupported gateway platform: {event.platform}")

                target = PlatformMessageTarget(
                    platform=event.platform,
                    target_type="chat_id",
                    target_id=event.chat_id,
                    thread_id=event.thread_id,
                )
                send_result = adapter.send_text(
                    target,
                    OutboundMessage(text=response_text, metadata={"session_id": session.session_id}),
                )
                if not send_result.ok:
                    self.store.release_event(event.platform, event.event_id)
                    return DispatchResult(False, session.session_id, error=send_result.error)

                self.store.record_message(
                    session.session_id,
                    direction="outbound",
                    platform=event.platform,
                    event_id=None,
                    text=response_text,
                    raw={"send_result": "ok"},
                )

            self.store.complete_event(event.platform, event.event_id)
        except Exception:
            self.store.release_event(event.platform, event.event_id)
            raise
        return DispatchResult(True, session.session_id)

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable

from langgraph.types import Command

from agent_cli.interrupts import extract_interrupt_review_requests, has_interrupt
from gateway.contracts import InboundEvent, OutboundMessage, PlatformMessageTarget
from gateway.registry import GatewayRegistry
from gateway.session_store import GatewaySession, GatewaySessionStore

logger = logging.getLogger(__name__)

_EMPTY_AGENT_RESPONSE_TEXT = (
    "抱歉，本次请求已被接收，但我无法生成回复。"
    "请稍后重试，或把问题拆得更具体一些。"
)


@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    session_id: str
    duplicate: bool = False
    error: str | None = None


GatewayRunner = Callable[[InboundEvent, GatewaySession, dict[str, Any]], Any]


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


def _clarify_request_from_result(result: Any):
    if not has_interrupt(result):
        return None
    for request in extract_interrupt_review_requests(result):
        if request.tool_name == "clarify" or request.review_config.get("kind") == "clarify":
            return request
    return None


def _clarify_choices_from_payload(payload: dict[str, Any]) -> list[str]:
    action = payload.get("action_request") or {}
    args = action.get("args") if isinstance(action, dict) else {}
    choices = args.get("choices") if isinstance(args, dict) else None
    if not isinstance(choices, list):
        return []
    return [str(choice).strip() for choice in choices if str(choice).strip()]


def _clarify_answer_from_text(payload: dict[str, Any], text: str) -> str:
    answer = str(text or "").strip()
    choices = _clarify_choices_from_payload(payload)
    if choices and answer.isdigit():
        selected = int(answer)
        if 1 <= selected <= len(choices):
            return choices[selected - 1]
        if selected == len(choices) + 1:
            return ""
    return answer


def _clarify_payload(request: Any) -> dict[str, Any]:
    return {
        "action_request": request.action_request,
        "review_config": request.review_config,
    }


def _format_clarify_message(payload: dict[str, Any]) -> str:
    action = payload.get("action_request") or {}
    args = action.get("args") if isinstance(action, dict) else {}
    question = str(args.get("question") or "Clarification needed.").strip()
    choices = _clarify_choices_from_payload(payload)
    lines = [question]
    if choices:
        lines.append("")
        for index, choice in enumerate(choices, start=1):
            lines.append(f"{index}. {choice}")
        lines.append(f"{len(choices) + 1}. Other (type your answer)")
    return "\n".join(lines)


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
) -> Any:
    if agent is None:
        from agent_core.builders import build_agent

        agent = build_agent(include_cron_tools=True, checkpointer=checkpointer)

    from agent_core.agent_runner import invoke_agent_with_terminal_notifications

    result = invoke_agent_with_terminal_notifications(
        agent,
        {"messages": [{"role": "user", "content": event.text}]},
        gateway_agent_config(origin),
    )
    return result


class GatewayDispatcher:
    def __init__(self, *, store: GatewaySessionStore, registry: GatewayRegistry, runner: GatewayRunner | None = None) -> None:
        self.store = store
        self.registry = registry
        self.runner = runner or run_gateway_agent_turn

    def _send_and_record_response(self, event: InboundEvent, session: GatewaySession, response_text: str) -> DispatchResult | None:
        adapter = self.registry.get(event.platform)
        if adapter is None:
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
            return DispatchResult(False, session.session_id, error=send_result.error)

        self.store.record_message(
            session.session_id,
            direction="outbound",
            platform=event.platform,
            event_id=None,
            text=response_text,
            raw={"send_result": "ok"},
        )
        return None

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

            pending = self.store.get_pending_interrupt(session.session_id)
            if pending is not None and pending.kind == "clarify":
                answer = _clarify_answer_from_text(pending.payload, event.text)
                response_text = self.runner(
                    event,
                    session,
                    {
                        "source_type": "gateway",
                        "platform": event.platform,
                        "chat_id": event.chat_id,
                        "thread_id": event.thread_id,
                        "sender_id": event.sender_id,
                        "display_name": event.sender_name,
                        "session_id": session.session_id,
                        "resume": {
                            "decisions": [
                                {
                                    "type": "respond",
                                    "message": answer,
                                }
                            ]
                        },
                    },
                )
                response_text = _assistant_text_from_result(response_text)
                if not response_text:
                    response_text = _EMPTY_AGENT_RESPONSE_TEXT
                send_error = self._send_and_record_response(event, session, response_text)
                if send_error is not None:
                    self.store.release_event(event.platform, event.event_id)
                    return send_error
                self.store.clear_pending_interrupt(session.session_id)
                self.store.complete_event(event.platform, event.event_id)
                return DispatchResult(True, session.session_id)

            origin = {
                "source_type": "gateway",
                "platform": event.platform,
                "chat_id": event.chat_id,
                "thread_id": event.thread_id,
                "sender_id": event.sender_id,
                "display_name": event.sender_name,
                "session_id": session.session_id,
            }
            runner_result = self.runner(event, session, origin)

            clarify_request = _clarify_request_from_result(runner_result)
            if clarify_request is not None:
                payload = _clarify_payload(clarify_request)
                response_text = _format_clarify_message(payload)
                send_error = self._send_and_record_response(event, session, response_text)
                if send_error is not None:
                    self.store.release_event(event.platform, event.event_id)
                    return send_error
                self.store.set_pending_interrupt(session.session_id, kind="clarify", payload=payload)
                self.store.complete_event(event.platform, event.event_id)
                return DispatchResult(True, session.session_id)

            response_text = _assistant_text_from_result(runner_result)
            if not response_text:
                logger.warning(
                    "gateway dispatch: runner returned no response platform=%s event_id=%s chat_id=%s session_id=%s",
                    event.platform,
                    event.event_id,
                    event.chat_id,
                    session.session_id,
                )
                response_text = _EMPTY_AGENT_RESPONSE_TEXT
            send_error = self._send_and_record_response(event, session, response_text)
            if send_error is not None:
                self.store.release_event(event.platform, event.event_id)
                return send_error

            self.store.complete_event(event.platform, event.event_id)
        except Exception:
            self.store.release_event(event.platform, event.event_id)
            raise
        return DispatchResult(True, session.session_id)

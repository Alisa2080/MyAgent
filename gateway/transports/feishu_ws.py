from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway.contracts import InboundEvent

_logger = logging.getLogger(__name__)


def _utc_timestamp_from_ms(value: Any) -> str:
    try:
        millis = int(str(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


def _extract_text_content(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return content.strip() or None
    if isinstance(content, dict):
        text = content.get("text")
        if text is None:
            return None
        text = str(text).strip()
        return text or None
    return str(content).strip() or None


_IGNORED_REASONS = frozenset({
    "unsupported_event_type",
    "unsupported_message_type",
    "missing_chat_id",
    "missing_message_id",
    "missing_event_id",
    "empty_text",
    "malformed_payload",
})


def normalize_feishu_ws_event_with_reason(
    payload: Any,
) -> tuple[InboundEvent | None, str | None]:
    """Normalize a Feishu WS payload into an InboundEvent, or return an ignore reason.

    Returns (event, None) for valid events and (None, reason) for ignored events.
    Reason values are stable short strings from _IGNORED_REASONS.
    """
    if not isinstance(payload, dict):
        return None, "malformed_payload"

    header = payload.get("header") or {}
    if not isinstance(header, dict):
        return None, "malformed_payload"
    event = payload.get("event") or {}
    if not isinstance(event, dict):
        return None, "malformed_payload"
    message = event.get("message") or {}
    if not isinstance(message, dict):
        return None, "malformed_payload"
    sender = event.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {}

    message_type = str(message.get("message_type") or "")
    if message_type != "text":
        return None, "unsupported_message_type"

    event_type = str(header.get("event_type") or "")
    if event_type != "im.message.receive_v1":
        return None, "unsupported_event_type"

    chat_id = str(message.get("chat_id") or "").strip()
    message_id = str(message.get("message_id") or "").strip()
    event_id = str(header.get("event_id") or "").strip()
    text = _extract_text_content(message.get("content"))

    if not chat_id:
        return None, "missing_chat_id"
    if not message_id:
        return None, "missing_message_id"
    if not event_id:
        return None, "missing_event_id"
    if not text:
        return None, "empty_text"

    sender_id_dict = sender.get("sender_id") or {}
    if not isinstance(sender_id_dict, dict):
        sender_id_dict = {}
    timestamp = _utc_timestamp_from_ms(header.get("create_time"))
    thread_id = str(message.get("thread_id") or message_id).strip() or None

    return (
        InboundEvent(
            platform="feishu",
            event_id=event_id,
            event_type=str(header.get("event_type") or "im.message.receive_v1"),
            chat_id=chat_id,
            thread_id=thread_id,
            sender_id=str(
                sender_id_dict.get("open_id")
                or sender_id_dict.get("union_id")
                or sender_id_dict.get("user_id")
                or ""
            ).strip()
            or None,
            sender_name=str(sender.get("sender_name") or "").strip() or None,
            text=text,
            timestamp=timestamp,
            raw=payload,
        ),
        None,
    )


def normalize_feishu_ws_event(payload: dict[str, Any]) -> InboundEvent | None:
    """Compatibility wrapper: normalizes a Feishu WS payload into an InboundEvent.

    Ignored events return None (no reason is exposed).
    """
    event, _ = normalize_feishu_ws_event_with_reason(payload)
    return event


def _event_metadata(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {
            "event_type": "",
            "event_id": "",
            "chat_id": "",
            "message_id": "",
            "message_type": "",
        }
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_data = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event_data.get("message") if isinstance(event_data.get("message"), dict) else {}
    return {
        "event_type": str(header.get("event_type") or ""),
        "event_id": str(header.get("event_id") or message.get("message_id") or ""),
        "chat_id": str(message.get("chat_id") or ""),
        "message_id": str(message.get("message_id") or ""),
        "message_type": str(message.get("message_type") or ""),
    }


def validate_feishu_ws_env(environ: dict[str, str] | None = None) -> list[str]:
    env = environ or os.environ
    return [key for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET") if not env.get(key)]


def _stop_client(client: Any) -> None:
    for method_name in ("stop", "close"):
        method = getattr(client, method_name, None)
        if callable(method):
            method()
            return


def _start_client(client: Any, stop_event: threading.Event | None) -> None:
    if stop_event is None:
        client.start()
        return
    client_thread = threading.Thread(target=client.start, daemon=True)
    client_thread.start()
    stop_event.wait()
    _stop_client(client)
    client_thread.join(timeout=5)


def _make_on_message(
    inbox: Any,
    logger: logging.Logger,
) -> Any:
    """Build an on_message callback that logs received, enqueued, and ignored events.

    The callback logs:
    - INFO: received event metadata (event_type, event_id, chat_id, message_id, message_type)
    - INFO: successful enqueue (event_id, inbox id)
    - WARNING: ignored event (reason + available event metadata)
    - EXCEPTION: JSON marshal/parse or unexpected handler errors
    """

    def on_message(data: Any) -> None:
        try:
            import lark_oapi as lark

            payload = json.loads(lark.JSON.marshal(data))
        except Exception as exc:
            logger.exception("Feishu WS: failed to parse message payload: %s", exc)
            return

        metadata = _event_metadata(payload)
        try:
            event, reason = normalize_feishu_ws_event_with_reason(payload)
        except Exception as exc:
            logger.exception("Feishu WS: normalization error for event metadata=%s: %s", metadata, exc)
            return

        event_id_str = metadata["event_id"] or "unknown"
        event_type_str = metadata["event_type"] or "unknown"
        chat_id_str = metadata["chat_id"]
        message_id_str = metadata["message_id"]
        message_type_str = metadata["message_type"]

        logger.info(
            "Feishu WS: received event event_type=%r event_id=%r chat_id=%r message_id=%r message_type=%r",
            event_type_str,
            event_id_str,
            chat_id_str,
            message_id_str,
            message_type_str,
        )

        if reason is not None:
            logger.warning(
                "Feishu WS: ignored event reason=%r event_type=%r event_id=%r chat_id=%r message_id=%r message_type=%r",
                reason,
                event_type_str,
                event_id_str,
                chat_id_str,
                message_id_str,
                message_type_str,
            )
            return

        assert event is not None
        try:
            inbox_id = inbox.enqueue(event)
        except Exception:
            logger.exception(
                "Feishu WS: failed to enqueue event event_type=%r event_id=%r chat_id=%r message_id=%r message_type=%r",
                event_type_str,
                event_id_str,
                chat_id_str,
                message_id_str,
                message_type_str,
            )
            raise
        logger.info(
            "Feishu WS: enqueued event_id=%r inbox_id=%r",
            event_id_str,
            inbox_id,
        )

    return on_message


def serve_feishu_ws_gateway(*, home: str | Path, stop_event: threading.Event | None = None) -> None:
    try:
        import lark_oapi as lark
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "missing lark-oapi; install it in the active Python environment with `python -m pip install lark-oapi`"
        ) from exc

    from gateway.inbox_store import GatewayInboxStore
    from gateway.inbox_worker import GatewayInboxWorker
    from gateway.service import GatewayService

    service = GatewayService(home=home)
    inbox = GatewayInboxStore(Path(home) / "gateway" / "gateway.sqlite")
    worker = GatewayInboxWorker(store=inbox, dispatch=service.handle_event)
    worker_thread = threading.Thread(target=worker.run_forever, daemon=True)
    worker_thread.start()

    on_message_cb = _make_on_message(inbox, _logger)

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message_cb).build()
    client = lark.ws.Client(os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"], event_handler=handler)
    try:
        _start_client(client, stop_event)
    finally:
        worker.request_stop()
        worker_thread.join(timeout=5)

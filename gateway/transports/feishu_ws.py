from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway.contracts import InboundEvent


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


def normalize_feishu_ws_event(payload: dict[str, Any]) -> InboundEvent | None:
    header = payload.get("header") or {}
    event = payload.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}

    if message.get("message_type") != "text":
        return None

    chat_id = str(message.get("chat_id") or "").strip()
    message_id = str(message.get("message_id") or "").strip()
    event_id = str(header.get("event_id") or "").strip()
    text = _extract_text_content(message.get("content"))

    if not chat_id or not message_id or not event_id or not text:
        return None

    sender_id = sender.get("sender_id") or {}
    timestamp = _utc_timestamp_from_ms(header.get("create_time"))
    thread_id = str(message.get("thread_id") or message_id).strip() or None

    return InboundEvent(
        platform="feishu",
        event_id=event_id,
        event_type=str(header.get("event_type") or "im.message.receive_v1"),
        chat_id=chat_id,
        thread_id=thread_id,
        sender_id=str(sender_id.get("open_id") or sender_id.get("union_id") or sender_id.get("user_id") or "").strip() or None,
        sender_name=str(sender.get("sender_name") or "").strip() or None,
        text=text,
        timestamp=timestamp,
        raw=payload,
    )


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

    def on_message(data):
        payload = json.loads(lark.JSON.marshal(data))
        event = normalize_feishu_ws_event(payload)
        if event is not None:
            inbox.enqueue(event)

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build()
    client = lark.ws.Client(os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"], event_handler=handler)
    try:
        _start_client(client, stop_event)
    finally:
        worker.request_stop()
        worker_thread.join(timeout=5)

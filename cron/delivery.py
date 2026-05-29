from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse
from typing import Any, Callable

from cron.contracts import JobRunResult
from cron.delivery_store import DeliveryStore
from cron.notifications import SILENT_MARKER


@dataclass(frozen=True)
class DeliveryTarget:
    raw: str
    target_type: str
    target_id: str | None


def parse_target(job: dict[str, Any]) -> DeliveryTarget:
    raw = str(job.get("deliver") or "local").strip() or "local"
    lowered = raw.lower()
    if lowered == "local":
        return DeliveryTarget(raw=raw, target_type="local", target_id=None)
    if lowered == "origin":
        thread_id = (job.get("origin") or {}).get("thread_id")
        return DeliveryTarget(raw=raw, target_type="origin", target_id=str(thread_id) if thread_id else None)
    if lowered == "webhook":
        return DeliveryTarget(raw=raw, target_type="webhook", target_id=os.getenv("AGENT_CRON_WEBHOOK_URL"))
    if lowered.startswith("webhook:"):
        return DeliveryTarget(raw=raw, target_type="webhook", target_id=raw.split(":", 1)[1].strip() or None)
    return DeliveryTarget(raw=raw, target_type="unsupported", target_id=None)


def _private_webhook_error() -> str:
    return "webhook URL must not target private or local addresses unless AGENT_CRON_ALLOW_PRIVATE_WEBHOOKS=1"


def _allow_private_webhooks() -> bool:
    return os.getenv("AGENT_CRON_ALLOW_PRIVATE_WEBHOOKS", "").lower() in {"1", "true", "yes"}


def _resolved_addresses(host: str, port: int | None) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    addresses = set()
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        sockaddr = info[4]
        if not sockaddr:
            continue
        addresses.add(ipaddress.ip_address(str(sockaddr[0])))
    return addresses


def validate_webhook_url(url: str | None, *, resolve_host: bool = False) -> str | None:
    if not url:
        return "webhook delivery requires a URL"
    parsed = urlparse(str(url))
    if parsed.scheme not in {"http", "https"}:
        return "webhook URL must use http or https"
    if not parsed.hostname:
        return "webhook URL must include a hostname"
    try:
        port = parsed.port
    except ValueError:
        return "webhook URL port is invalid"

    host = parsed.hostname.lower()
    if not _allow_private_webhooks():
        if host == "localhost" or host.endswith(".localhost"):
            return "webhook URL must not target localhost unless AGENT_CRON_ALLOW_PRIVATE_WEBHOOKS=1"
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if resolve_host:
                try:
                    addresses = _resolved_addresses(host, port)
                except socket.gaierror as exc:
                    return f"webhook URL hostname could not be resolved: {exc}"
                except OSError as exc:
                    return f"webhook URL hostname resolution failed: {exc}"
                if not addresses:
                    return "webhook URL hostname did not resolve to an address"
                if any(not address.is_global for address in addresses):
                    return _private_webhook_error()
        else:
            if not ip.is_global:
                return _private_webhook_error()
    return None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _payload(job: dict[str, Any], result: JobRunResult, output_path: str, run_at: str) -> dict[str, Any]:
    return {
        "type": "cron_result",
        "job_id": job.get("id"),
        "job_name": job.get("name"),
        "status": "ok" if result.success else "error",
        "run_at": run_at,
        "final_response": result.final_response if result.success else "",
        "error": result.error,
        "output_path": output_path,
    }


def _targets_for_job(job: dict[str, Any]):
    from cron.delivery_targets import DeliveryIdentity, DeliveryTarget, DeliveryTargetError, parse_delivery_targets

    stored_targets = job.get("delivery_targets")
    if stored_targets is not None:
        targets = [
            DeliveryTarget(
                raw=str(item.get("raw") or item.get("target_type") or ""),
                target_type=str(item["target_type"]),
                adapter_key=str(item["adapter_key"]),
                address=item.get("address"),
                thread_id=item.get("thread_id"),
                metadata=dict(item.get("metadata") or {}),
            )
            for item in stored_targets
        ]
        if not targets:
            raise DeliveryTargetError("delivery target is required")
        return targets

    origin = DeliveryIdentity.from_job_origin(job.get("origin"))
    return parse_delivery_targets(job.get("deliver"), origin=origin)


def enqueue_result(
    job: dict[str, Any],
    result: "JobRunResult",
    output_path: str,
    run_at: datetime | str,
    *,
    store: DeliveryStore | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    if result.success and str(result.final_response or "").lstrip().startswith(SILENT_MARKER):
        return None

    store = store or DeliveryStore()
    run_at_text = run_at.isoformat() if isinstance(run_at, datetime) else str(run_at)
    payload = _payload(job, result, output_path, run_at_text)

    from cron.delivery_targets import DeliveryIdentity, DeliveryTargetError

    origin = DeliveryIdentity.from_job_origin(job.get("origin"))
    try:
        targets = _targets_for_job(job)
    except DeliveryTargetError as exc:
        targets = []
        error_text = str(exc)
    else:
        error_text = None

    events = []
    if error_text:
        events.append(
            store.enqueue(
                job_id=str(job.get("id") or ""),
                run_id=job.get("run_id"),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=str(job.get("deliver") or "origin"),
                target_type="origin",
                target_id=None,
                address=origin.session_id if origin else None,
                thread_id=origin.thread_id if origin else None,
                origin=origin.to_json() if origin else None,
                final_response=result.final_response,
                output_path=output_path,
                payload=payload,
                status="dead",
                last_error=error_text,
            )
        )
    for target in targets:
        target_error = None
        if target.target_type == "webhook" and not target.address:
            target_error = validate_webhook_url(None)
        elif target.target_type == "webhook":
            target_error = validate_webhook_url(target.address)
        elif target.target_type == "platform":
            from cron.delivery_registry import default_delivery_registry
            if default_delivery_registry().get(target.adapter_key) is None:
                target_error = f"unsupported delivery target: {target.raw}"
        if target_error:
            events.append(
                store.enqueue(
                    job_id=str(job.get("id") or ""),
                    run_id=job.get("run_id"),
                    job_name=job.get("name"),
                    run_at=run_at_text,
                    target=target.raw,
                    target_type=target.target_type,
                    target_id=target.address,
                    address=target.address,
                    thread_id=target.thread_id,
                    origin=target.metadata.get("origin"),
                    final_response=result.final_response,
                    output_path=output_path,
                    payload=payload,
                    status="dead",
                    last_error=target_error,
                )
            )
            continue
        status = "delivered" if target.target_type == "local" else "pending"
        events.append(
            store.enqueue(
                job_id=str(job.get("id") or ""),
                run_id=job.get("run_id"),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=target.raw,
                target_type=target.target_type,
                target_id=target.address,
                address=target.address,
                thread_id=target.thread_id,
                origin=target.metadata.get("origin"),
                final_response=result.final_response,
                output_path=output_path,
                payload=payload,
                status=status,
            )
        )
    if not events:
        return None
    return events[0] if len(events) == 1 else events


def default_webhook_sender(url: str, payload: dict[str, Any], timeout: int = 10) -> tuple[int, str]:
    webhook_error = validate_webhook_url(url, resolve_host=True)
    if webhook_error:
        raise ValueError(webhook_error)
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(event["payload_json"])
    payload["event_id"] = event["id"]
    return payload


def process_due(
    *,
    limit: int = 20,
    store: DeliveryStore | None = None,
    webhook_sender: Callable[[str, dict[str, Any], int], tuple[int, str]] | None = None,
) -> dict[str, int]:
    from cron.delivery_dispatcher import DeliveryDispatcher
    from cron.delivery_registry import default_delivery_registry
    from cron.state_store import StateStore

    registry = default_delivery_registry(webhook_sender=webhook_sender)
    if store is None:
        state_store = StateStore()
    elif hasattr(store, "_store"):
        state_store = store._store
    else:
        state_store = store
    dispatcher = DeliveryDispatcher(store=state_store, registry=registry)
    return dispatcher.dispatch_due(limit=limit)

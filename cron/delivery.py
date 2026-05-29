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


def enqueue_result(
    job: dict[str, Any],
    result: "JobRunResult",
    output_path: str,
    run_at: datetime | str,
    *,
    store: DeliveryStore | None = None,
    registry: Any | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    if result.success and str(result.final_response or "").lstrip().startswith(SILENT_MARKER):
        return None

    store = store or DeliveryStore()
    run_at_text = run_at.isoformat() if isinstance(run_at, datetime) else str(run_at)
    payload = _payload(job, result, output_path, run_at_text)

    from cron.delivery_registry import default_delivery_registry
    from cron.delivery_targets import DeliveryIdentity

    origin = DeliveryIdentity.from_job_origin(job.get("origin"))
    registry = registry or default_delivery_registry()
    validation = registry.validate_targets(
        job.get("deliver"),
        origin=origin,
        job=job,
        use_stored_targets=True,
    )

    events = []
    if not validation.ok:
        fallback_target = None
        if validation.targets:
            fallback_target = validation.targets[0]
        events.append(
            store.enqueue(
                job_id=str(job.get("id") or ""),
                run_id=job.get("run_id"),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=fallback_target.raw if fallback_target else str(job.get("deliver") or "local"),
                target_type=fallback_target.target_type if fallback_target else "unsupported",
                adapter_key=fallback_target.adapter_key if fallback_target else "unsupported",
                target_id=fallback_target.address if fallback_target else None,
                address=fallback_target.address if fallback_target else None,
                thread_id=fallback_target.thread_id if fallback_target else None,
                origin=fallback_target.metadata.get("origin") if fallback_target else (origin.to_json() if origin else None),
                final_response=result.final_response,
                output_path=output_path,
                payload=payload,
                status="dead",
                last_error=validation.error or "delivery target validation failed",
            )
        )
    for target in validation.targets if validation.ok else []:
        status = "delivered" if target.target_type == "local" else "pending"
        events.append(
            store.enqueue(
                job_id=str(job.get("id") or ""),
                run_id=job.get("run_id"),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=target.raw,
                target_type=target.target_type,
                adapter_key=target.adapter_key,
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

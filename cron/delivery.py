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

from cron.delivery_store import DeliveryStore
from cron.notifications import SILENT_MARKER


@dataclass(frozen=True)
class JobRunResult:
    success: bool
    output_doc: str | None = None
    final_response: str | None = None
    error: str | None = None


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


def enqueue_result(
    job: dict[str, Any],
    result: "JobRunResult",
    output_path: str,
    run_at: datetime | str,
    *,
    store: DeliveryStore | None = None,
) -> dict[str, Any] | None:
    if result.success and str(result.final_response or "").lstrip().startswith(SILENT_MARKER):
        return None

    store = store or DeliveryStore()
    run_at_text = run_at.isoformat() if isinstance(run_at, datetime) else str(run_at)
    target = parse_target(job)
    payload = _payload(job, result, output_path, run_at_text)

    if target.target_type == "local":
        return store.enqueue(
            job_id=str(job.get("id") or ""),
            job_name=job.get("name"),
            run_at=run_at_text,
            target=target.raw,
            target_type="local",
            target_id=None,
            final_response=result.final_response,
            output_path=output_path,
            payload=payload,
            status="delivered",
        )

    if target.target_type == "origin" and not target.target_id:
        return store.enqueue(
            job_id=str(job.get("id") or ""),
            job_name=job.get("name"),
            run_at=run_at_text,
            target=target.raw,
            target_type="origin",
            target_id=None,
            final_response=result.final_response,
            output_path=output_path,
            payload=payload,
            status="dead",
            last_error="origin delivery requires origin.thread_id",
        )

    if target.target_type == "webhook":
        webhook_error = validate_webhook_url(target.target_id)
        if not webhook_error:
            return store.enqueue(
                job_id=str(job.get("id") or ""),
                job_name=job.get("name"),
                run_at=run_at_text,
                target=target.raw,
                target_type=target.target_type,
                target_id=target.target_id,
                final_response=result.final_response,
                output_path=output_path,
                payload=payload,
            )
        return store.enqueue(
            job_id=str(job.get("id") or ""),
            job_name=job.get("name"),
            run_at=run_at_text,
            target=target.raw,
            target_type="webhook",
            target_id=None,
            final_response=result.final_response,
            output_path=output_path,
            payload=payload,
            status="dead",
            last_error=webhook_error,
        )

    if target.target_type == "unsupported":
        return store.enqueue(
            job_id=str(job.get("id") or ""),
            job_name=job.get("name"),
            run_at=run_at_text,
            target=target.raw,
            target_type="unsupported",
            target_id=None,
            final_response=result.final_response,
            output_path=output_path,
            payload=payload,
            status="dead",
            last_error=f"unsupported delivery target: {target.raw}",
        )

    return store.enqueue(
        job_id=str(job.get("id") or ""),
        job_name=job.get("name"),
        run_at=run_at_text,
        target=target.raw,
        target_type=target.target_type,
        target_id=target.target_id,
        final_response=result.final_response,
        output_path=output_path,
        payload=payload,
    )


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
    store = store or DeliveryStore()
    webhook_sender = webhook_sender or default_webhook_sender
    summary = {"claimed": 0, "delivered": 0, "failed": 0, "dead": 0}

    for event in store.claim_due(limit=limit, target_types={"local", "webhook", "unsupported"}):
        summary["claimed"] += 1
        if event["target_type"] == "local":
            store.mark_delivered(event["id"])
            summary["delivered"] += 1
            continue
        if event["target_type"] != "webhook":
            store.mark_dead(event["id"], f"unsupported delivery target: {event['target']}")
            summary["dead"] += 1
            continue
        try:
            status, body = webhook_sender(str(event["target_id"]), _event_payload(event), 10)
        except Exception as exc:
            store.mark_failed(event["id"], str(exc))
            summary["failed"] += 1
            continue
        if 200 <= status < 300:
            store.mark_delivered(event["id"])
            summary["delivered"] += 1
        elif status in {408, 429} or status >= 500:
            store.mark_failed(event["id"], f"HTTP {status}: {body}")
            summary["failed"] += 1
        else:
            store.mark_dead(event["id"], f"HTTP {status}: {body}")
            summary["dead"] += 1
    return summary

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
import urllib.error
import urllib.request
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
    if raw == "local":
        return DeliveryTarget(raw=raw, target_type="local", target_id=None)
    if raw == "origin":
        thread_id = (job.get("origin") or {}).get("thread_id")
        return DeliveryTarget(raw=raw, target_type="origin", target_id=str(thread_id) if thread_id else None)
    if raw == "webhook":
        return DeliveryTarget(raw=raw, target_type="webhook", target_id=os.getenv("AGENT_CRON_WEBHOOK_URL"))
    if raw.startswith("webhook:"):
        return DeliveryTarget(raw=raw, target_type="webhook", target_id=raw.split(":", 1)[1].strip() or None)
    return DeliveryTarget(raw=raw, target_type="unsupported", target_id=None)


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

    if target.target_type == "webhook" and not target.target_id:
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
            last_error="webhook delivery requires a URL",
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
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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

    for event in store.claim_due(limit=limit):
        summary["claimed"] += 1
        if event["target_type"] == "origin":
            store.update_event(event["id"], status="pending")
            continue
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

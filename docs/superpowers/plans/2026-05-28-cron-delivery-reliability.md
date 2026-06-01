# Cron Delivery Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reliable, persisted cron delivery diagnostics for `origin`, `local`, and `webhook` targets.

**Architecture:** Introduce a SQLite-backed delivery queue as the source of truth for cron delivery events. Keep cron execution and job storage unchanged, but route scheduler delivery through `cron.delivery`, persist events in `cron.delivery_store`, and expose diagnostics through cron CLI commands.

**Tech Stack:** Python standard library (`sqlite3`, `urllib.request`, `json`, `uuid`, `datetime`), existing cron modules, pytest.

---

## File Structure

- Create `cron/delivery_store.py`: SQLite schema, enqueue/claim/ack/fail/dead/stats APIs.
- Create `cron/delivery.py`: target parsing, enqueue from cron results, due delivery processing, webhook sender, test-delivery helper.
- Modify `cron/notifications.py`: preserve public API while backing origin notifications with SQLite.
- Modify `cron/scheduler.py`: replace `_deliver_result()` with delivery enqueue/process calls.
- Modify `agent_cli/cron_commands.py`: richer status plus `cron_doctor()` and `test_delivery()`.
- Modify `agent_cli/main.py`: add `cron doctor` and `cron test-delivery` subcommands.
- Modify `agent_cli/command_handlers/cron.py`: add `/cron doctor` and `/cron test-delivery`.
- Add tests:
  - `tests/test_cron_delivery_store.py`
  - `tests/test_cron_delivery.py`
  - update `tests/test_cron_notifications.py`
  - update `tests/test_cron_scheduler.py`
  - update `tests/test_agent_cli_cron_commands.py`
  - update `tests/test_agent_cli_main.py`

Use `AGENT_CRON_HOME` in tests to isolate all delivery DB files under `tmp_path`.

---

### Task 1: SQLite Delivery Store

**Files:**
- Create: `cron/delivery_store.py`
- Test: `tests/test_cron_delivery_store.py`

- [ ] **Step 1: Write failing store tests**

Create `tests/test_cron_delivery_store.py`:

```python
from __future__ import annotations

from datetime import timedelta


def test_enqueue_and_claim_due_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_store import DeliveryStore

    store = DeliveryStore()
    event = store.enqueue(
        job_id="job-1",
        job_name="Daily",
        run_at="2026-05-28T10:00:00+00:00",
        target="webhook:https://example.invalid/hook",
        target_type="webhook",
        target_id="https://example.invalid/hook",
        final_response="done",
        output_path="/tmp/out.md",
        payload={"type": "cron_result", "status": "ok"},
    )

    claimed = store.claim_due(limit=10)

    assert len(claimed) == 1
    assert claimed[0]["id"] == event["id"]
    assert claimed[0]["status"] == "delivering"
    assert claimed[0]["attempt_count"] == 1


def test_mark_failed_then_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_store import DeliveryStore

    store = DeliveryStore(max_attempts=2)
    event = store.enqueue(
        job_id="job-1",
        job_name="Daily",
        run_at=None,
        target="webhook:https://example.invalid/hook",
        target_type="webhook",
        target_id="https://example.invalid/hook",
        final_response="done",
        output_path=None,
        payload={"type": "cron_result"},
    )

    first = store.claim_due(limit=1)[0]
    store.mark_failed(first["id"], "timeout")
    failed = store.get(event["id"])
    assert failed["status"] == "failed"
    assert failed["last_error"] == "timeout"
    assert failed["next_attempt_at"] is not None

    store.update_event(event["id"], status="pending", next_attempt_at=None, attempt_count=2)
    second = store.claim_due(limit=1)[0]
    store.mark_failed(second["id"], "still failing")
    dead = store.get(event["id"])
    assert dead["status"] == "dead"
    assert dead["last_error"] == "still failing"


def test_stats_and_stale_delivering(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_store import DeliveryStore, utc_now

    store = DeliveryStore()
    event = store.enqueue(
        job_id="job-1",
        job_name="Daily",
        run_at=None,
        target="origin",
        target_type="origin",
        target_id="thread-1",
        final_response="done",
        output_path=None,
        payload={"type": "cron_result"},
    )
    store.update_event(
        event["id"],
        status="delivering",
        updated_at=(utc_now() - timedelta(minutes=20)).isoformat(),
    )

    stats = store.stats()
    stale = store.stale_delivering(max_age_seconds=600)

    assert stats["delivering"] == 1
    assert stale[0]["id"] == event["id"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_cron_delivery_store.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'cron.delivery_store'`.

- [ ] **Step 3: Implement `cron/delivery_store.py`**

Create `cron/delivery_store.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from cron.paths import ensure_cron_dirs, get_cron_dir, secure_file

STATUSES = {"pending", "delivering", "delivered", "failed", "dead"}
RETRY_DELAYS = (60, 300, 900, 3600, 21600)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_delivery_db_path() -> Path:
    return get_cron_dir() / "delivery.sqlite3"


class DeliveryStore:
    def __init__(self, path: Path | None = None, *, max_attempts: int = 5) -> None:
        ensure_cron_dirs()
        self.path = path or get_delivery_db_path()
        self.max_attempts = max(1, int(max_attempts))
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_events (
                    id TEXT PRIMARY KEY,
                    job_id TEXT,
                    job_name TEXT,
                    run_at TEXT,
                    target TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_attempt_at TEXT,
                    last_error TEXT,
                    output_path TEXT,
                    final_response TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_due ON delivery_events(status, next_attempt_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_origin ON delivery_events(target_type, target_id, status, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_job ON delivery_events(job_id, created_at)")
        secure_file(self.path)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def enqueue(
        self,
        *,
        job_id: str | None,
        job_name: str | None,
        run_at: str | None,
        target: str,
        target_type: str,
        target_id: str | None,
        final_response: str | None,
        output_path: str | None,
        payload: dict[str, Any],
        status: str = "pending",
        last_error: str | None = None,
    ) -> dict[str, Any]:
        event_id = uuid.uuid4().hex
        now = utc_now().isoformat()
        if status not in STATUSES:
            raise ValueError(f"invalid delivery status: {status}")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO delivery_events (
                    id, job_id, job_name, run_at, target, target_type, target_id,
                    status, attempt_count, next_attempt_at, last_attempt_at,
                    last_error, output_path, final_response, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, job_id, job_name, run_at, target, target_type, target_id,
                    status, now if status in {"pending", "failed"} else None,
                    last_error, output_path, final_response,
                    json.dumps(payload, ensure_ascii=False), now, now,
                ),
            )
        return self.get(event_id)

    def get(self, event_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM delivery_events WHERE id = ?", (event_id,)).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(event_id)
        return result

    def update_event(self, event_id: str, **updates: Any) -> dict[str, Any]:
        if not updates:
            return self.get(event_id)
        if "status" in updates and updates["status"] not in STATUSES:
            raise ValueError(f"invalid delivery status: {updates['status']}")
        updates.setdefault("updated_at", utc_now().isoformat())
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [event_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE delivery_events SET {assignments} WHERE id = ?", values)
        return self.get(event_id)

    def claim_due(self, *, limit: int = 20) -> list[dict[str, Any]]:
        now = utc_now().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE status IN ('pending', 'failed')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at
                LIMIT ?
                """,
                (now, max(0, int(limit))),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                event = dict(row)
                conn.execute(
                    """
                    UPDATE delivery_events
                    SET status = 'delivering',
                        attempt_count = attempt_count + 1,
                        last_attempt_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, event["id"]),
                )
                claimed.append(dict(conn.execute("SELECT * FROM delivery_events WHERE id = ?", (event["id"],)).fetchone()))
        return claimed

    def mark_delivered(self, event_id: str) -> dict[str, Any]:
        return self.update_event(event_id, status="delivered", last_error=None, next_attempt_at=None)

    def mark_failed(self, event_id: str, error: str) -> dict[str, Any]:
        event = self.get(event_id)
        if int(event["attempt_count"]) >= self.max_attempts:
            return self.update_event(event_id, status="dead", last_error=error, next_attempt_at=None)
        index = max(0, min(int(event["attempt_count"]) - 1, len(RETRY_DELAYS) - 1))
        next_attempt = utc_now() + timedelta(seconds=RETRY_DELAYS[index])
        return self.update_event(
            event_id,
            status="failed",
            last_error=error,
            next_attempt_at=next_attempt.isoformat(),
        )

    def mark_dead(self, event_id: str, error: str) -> dict[str, Any]:
        return self.update_event(event_id, status="dead", last_error=error, next_attempt_at=None)

    def pending_origin_events(self, thread_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE target_type = 'origin'
                  AND target_id = ?
                  AND status IN ('pending', 'failed')
                ORDER BY created_at
                LIMIT ?
                """,
                (str(thread_id), max(0, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        stats = {status: 0 for status in STATUSES}
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM delivery_events GROUP BY status").fetchall()
        for row in rows:
            stats[str(row["status"])] = int(row["count"])
        return stats

    def recent_errors(self, *, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE status IN ('failed', 'dead') OR last_error IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def stale_delivering(self, *, max_age_seconds: int = 600) -> list[dict[str, Any]]:
        cutoff = (utc_now() - timedelta(seconds=max_age_seconds)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM delivery_events WHERE status = 'delivering' AND updated_at < ?",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_cron_delivery_store.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/delivery_store.py tests/test_cron_delivery_store.py
git commit -m "feat: add cron delivery store"
```

---

### Task 2: Delivery Target Parsing and Local/Webhook Processing

**Files:**
- Create: `cron/delivery.py`
- Test: `tests/test_cron_delivery.py`

- [ ] **Step 1: Write failing delivery tests**

Create `tests/test_cron_delivery.py`:

```python
from __future__ import annotations

import json


def test_enqueue_local_result_marks_delivered(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result
    from cron.delivery_store import DeliveryStore
    from cron.runner import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "local"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert event is not None
    stored = DeliveryStore().get(event["id"])
    assert stored["target_type"] == "local"
    assert stored["status"] == "delivered"


def test_silent_success_does_not_enqueue(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery import enqueue_result
    from cron.delivery_store import DeliveryStore
    from cron.runner import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "origin", "origin": {"thread_id": "t1"}},
        JobRunResult(success=True, output_doc="# out", final_response="[SILENT] nothing"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )

    assert event is None
    assert DeliveryStore().stats()["pending"] == 0


def test_webhook_success(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    sent = []

    def fake_post(url, payload, timeout=10):
        sent.append((url, payload, timeout))
        return 204, "ok"

    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.runner import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    summary = process_due(limit=10, webhook_sender=fake_post)

    assert summary["delivered"] == 1
    assert sent[0][0] == "https://example.invalid/hook"
    assert sent[0][1]["job_id"] == "job-1"
    assert DeliveryStore().get(event["id"])["status"] == "delivered"


def test_webhook_4xx_marks_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    def fake_post(url, payload, timeout=10):
        return 401, "unauthorized"

    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.runner import JobRunResult

    event = enqueue_result(
        {"id": "job-1", "name": "Daily", "deliver": "webhook:https://example.invalid/hook"},
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-05-28T10:00:00+00:00",
    )
    summary = process_due(limit=10, webhook_sender=fake_post)

    assert summary["dead"] == 1
    stored = DeliveryStore().get(event["id"])
    assert stored["status"] == "dead"
    assert "HTTP 401" in stored["last_error"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_cron_delivery.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'cron.delivery'`.

- [ ] **Step 3: Implement `cron/delivery.py`**

Create `cron/delivery.py`:

```python
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
from cron.runner import JobRunResult


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
    result: JobRunResult,
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
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_cron_delivery.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/delivery.py tests/test_cron_delivery.py
git commit -m "feat: add cron delivery routing"
```

---

### Task 3: Back Origin Notifications with SQLite

**Files:**
- Modify: `cron/notifications.py`
- Test: `tests/test_cron_notifications.py`

- [ ] **Step 1: Add persistence-focused notification test**

Append to `tests/test_cron_notifications.py`:

```python
def test_origin_notification_survives_module_reload(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import importlib
    import cron.notifications as notifications

    notifications.queue_cron_notification(
        "thread-1",
        {"type": "cron_result", "job_id": "job-1", "final_response": "done"},
    )

    reloaded = importlib.reload(notifications)

    assert reloaded.drain_cron_notifications_for_thread_id("thread-1") == [
        {"type": "cron_result", "job_id": "job-1", "final_response": "done"}
    ]
    assert reloaded.drain_cron_notifications_for_thread_id("thread-1") == []
```

- [ ] **Step 2: Run notification tests to verify failure**

Run: `pytest tests/test_cron_notifications.py -q`

Expected: FAIL because reload loses the in-memory queue.

- [ ] **Step 3: Update `cron/notifications.py` queue/drain implementation**

Replace the bodies of `queue_cron_notification()` and `drain_cron_notifications_for_thread_id()` while keeping formatting helpers unchanged:

```python
def queue_cron_notification(thread_id: str | None, event: dict[str, Any]) -> None:
    if not thread_id:
        return
    from cron.delivery_store import DeliveryStore

    payload = dict(event)
    DeliveryStore().enqueue(
        job_id=str(payload.get("job_id") or ""),
        job_name=payload.get("job_name"),
        run_at=payload.get("run_at"),
        target="origin",
        target_type="origin",
        target_id=str(thread_id),
        final_response=payload.get("final_response"),
        output_path=payload.get("output_path"),
        payload=payload,
    )


def drain_cron_notifications_for_thread_id(
    thread_id: str | None,
    max_events: int = 10,
) -> list[dict[str, Any]]:
    if not thread_id:
        return []
    import json
    from cron.delivery_store import DeliveryStore

    store = DeliveryStore()
    events: list[dict[str, Any]] = []
    for stored in store.pending_origin_events(str(thread_id), limit=max_events):
        payload = json.loads(stored["payload_json"])
        events.append(payload)
        store.mark_delivered(stored["id"])
    return events
```

Remove unused in-memory queue globals if tests no longer reference them directly.

- [ ] **Step 4: Run notification tests**

Run: `pytest tests/test_cron_notifications.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/notifications.py tests/test_cron_notifications.py
git commit -m "feat: persist cron origin notifications"
```

---

### Task 4: Wire Scheduler to Delivery Queue

**Files:**
- Modify: `cron/scheduler.py`
- Test: `tests/test_cron_scheduler.py`

- [ ] **Step 1: Add scheduler delivery queue test**

Append to `tests/test_cron_scheduler.py`:

```python
def test_tick_enqueues_delivery_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import cron.scheduler as scheduler
    from cron.delivery_store import DeliveryStore
    from cron.runner import JobRunResult

    job = {
        "id": "job-1",
        "name": "Daily",
        "deliver": "origin",
        "origin": {"thread_id": "thread-1"},
        "workdir": None,
    }

    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: [job])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda job_id, run_at: job)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda advanced: JobRunResult(success=True, output_doc="# out", final_response="done"),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda job_id, output_doc, run_at=None: "/tmp/out.md")
    marked = []
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, run_at=None, delivery_error=None: marked.append(delivery_error),
    )

    result = scheduler.tick()

    assert result.ran == 1
    assert DeliveryStore().stats()["pending"] == 1
    assert marked == [None]
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_cron_scheduler.py::test_tick_enqueues_delivery_event -q`

Expected: FAIL because scheduler still calls the old `_deliver_result()`.

- [ ] **Step 3: Replace `_deliver_result()` usage in scheduler**

In `cron/scheduler.py`:

1. Remove imports of `queue_cron_notification` and `should_notify`.
2. Import delivery helpers:

```python
from cron.delivery import enqueue_result, process_due
```

3. Replace `_deliver_result()` with:

```python
def _delivery_error_from_event(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None
    if event.get("status") == "dead":
        return str(event.get("last_error") or "delivery target is not deliverable")
    return None
```

4. In `_process_job()`, replace:

```python
delivery_error = _deliver_result(advanced, result, output_path, run_at)
```

with:

```python
delivery_event = enqueue_result(advanced, result, output_path, run_at)
process_due(limit=20)
delivery_error = _delivery_error_from_event(delivery_event)
```

Keep `mark_job_run(... delivery_error=delivery_error)`.

- [ ] **Step 4: Run scheduler tests**

Run: `pytest tests/test_cron_scheduler.py tests/test_cron_notifications.py tests/test_cron_delivery.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/scheduler.py tests/test_cron_scheduler.py
git commit -m "feat: route cron scheduler delivery through queue"
```

---

### Task 5: Cron Status and Doctor

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write CLI command tests**

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_status_includes_delivery_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.delivery_store import DeliveryStore

    store = DeliveryStore()
    store.enqueue(
        job_id="job-1",
        job_name="Daily",
        run_at=None,
        target="webhook:https://example.invalid/hook",
        target_type="webhook",
        target_id="https://example.invalid/hook",
        final_response="done",
        output_path=None,
        payload={"type": "cron_result"},
    )

    result = cron_commands.cron_status()

    assert result.exit_code == 0
    assert "Delivery queue:" in result.text
    assert "pending=1" in result.text


def test_cron_doctor_reports_delivery_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands

    result = cron_commands.cron_doctor()

    assert "delivery db" in result.text.lower()
    assert result.exit_code in {0, 1}
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_agent_cli_cron_commands.py -q`

Expected: FAIL because `cron_doctor()` is missing and status lacks delivery stats.

- [ ] **Step 3: Implement status and doctor helpers**

In `agent_cli/cron_commands.py`, add imports as needed and update `cron_status()`:

```python
def _delivery_stats_lines() -> list[str]:
    from cron.delivery_store import DeliveryStore

    store = DeliveryStore()
    stats = store.stats()
    lines = [
        "Delivery queue: "
        f"pending={stats.get('pending', 0)} "
        f"failed={stats.get('failed', 0)} "
        f"dead={stats.get('dead', 0)} "
        f"delivered={stats.get('delivered', 0)}"
    ]
    errors = store.recent_errors(limit=1)
    if errors:
        error = errors[0]
        lines.append(
            "Last delivery error: "
            f"job={error.get('job_id') or '-'} "
            f"target={error.get('target') or '-'} "
            f"status={error.get('status') or '-'} "
            f"error={error.get('last_error') or '-'}"
        )
    return lines
```

Extend `cron_status()`:

```python
    active_jobs = [job for job in jobs if job.get("enabled", True)]
    lines.append(f"Active jobs: {len(active_jobs)}")
    next_runs = [job.get("next_run_at") for job in active_jobs if job.get("next_run_at")]
    if next_runs:
        lines.append(f"Next run: {min(next_runs)}")
    lines.extend(_delivery_stats_lines())
```

Add:

```python
def _check_line(status: str, message: str) -> str:
    return f"[{status}] {message}"


def cron_doctor() -> CronCommandResult:
    from cron.delivery_store import DeliveryStore

    lines: list[str] = []
    warnings = 0
    failures = 0

    def add(status: str, message: str) -> None:
        nonlocal warnings, failures
        normalized = status.lower()
        if normalized == "warn":
            warnings += 1
        elif normalized == "fail":
            failures += 1
        lines.append(_check_line(normalized, message))

    try:
        cron_home = display_cron_home()
        add("ok", f"cron home: {cron_home}")
        get_jobs_file().parent.mkdir(parents=True, exist_ok=True)
        get_output_dir().mkdir(parents=True, exist_ok=True)
        get_scripts_dir().mkdir(parents=True, exist_ok=True)
        add("ok", f"jobs file path: {get_jobs_file()}")
        add("ok", f"output dir writable: {get_output_dir()}")
        add("ok", f"scripts dir writable: {get_scripts_dir()}")
    except Exception as exc:
        add("fail", f"cron paths are not writable: {exc}")

    try:
        store = DeliveryStore()
        stats = store.stats()
        add("ok", f"delivery db readable/writable: {store.path}")
        if stats.get("dead", 0):
            add("fail", f"dead delivery events: {stats['dead']}")
        if stats.get("failed", 0):
            add("warn", f"failed delivery events pending retry: {stats['failed']}")
        stale = store.stale_delivering(max_age_seconds=600)
        if stale:
            add("warn", f"stale delivering events: {len(stale)}")
    except Exception as exc:
        add("fail", f"delivery db error: {exc}")

    if is_cron_scheduler_running():
        add("ok", "scheduler is running")
    else:
        add("warn", "scheduler is stopped; jobs will only run via manual cron tick")

    for job in list_jobs(include_disabled=False):
        if not job.get("next_run_at"):
            add("warn", f"active job {job.get('id')} has no next_run_at")

    exit_code = 2 if failures else (1 if warnings else 0)
    return CronCommandResult("\n".join(lines), exit_code=exit_code)
```

- [ ] **Step 4: Run CLI command tests**

Run: `pytest tests/test_agent_cli_cron_commands.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: add cron delivery diagnostics"
```

---

### Task 6: Test-Delivery Command and CLI Wiring

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/command_handlers/cron.py`
- Test: `tests/test_agent_cli_main.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Add command tests**

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_test_delivery_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands

    result = cron_commands.test_delivery(target="local")

    assert result.exit_code == 0
    assert "test-delivery" in result.text
    assert "delivered" in result.text
```

Append to `tests/test_agent_cli_main.py`:

```python
def test_main_cron_doctor(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.main as main_module
    from agent_cli.cron_commands import CronCommandResult

    monkeypatch.setattr(
        main_module.cron_commands,
        "cron_doctor",
        lambda: CronCommandResult("doctor ok", exit_code=1),
    )

    code = main_module.main(["cron", "doctor"])

    assert code == 1
    assert "doctor ok" in capsys.readouterr().out


def test_main_cron_test_delivery(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.main as main_module
    from agent_cli.cron_commands import CronCommandResult

    monkeypatch.setattr(
        main_module.cron_commands,
        "test_delivery",
        lambda **kwargs: CronCommandResult(f"target={kwargs['target']}", exit_code=0),
    )

    code = main_module.main(["cron", "test-delivery", "--target", "local"])

    assert code == 0
    assert "target=local" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_agent_cli_cron_commands.py::test_test_delivery_local tests/test_agent_cli_main.py::test_main_cron_doctor tests/test_agent_cli_main.py::test_main_cron_test_delivery -q`

Expected: FAIL because command wiring and helper are missing.

- [ ] **Step 3: Add `test_delivery()` helper**

In `agent_cli/cron_commands.py`:

```python
def test_delivery(*, target: str, session_id: str | None = None) -> CronCommandResult:
    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.runner import JobRunResult
    from cron.jobs import now

    job: dict[str, Any] = {
        "id": "test-delivery",
        "name": "test-delivery",
        "deliver": target,
    }
    if target == "origin":
        if not session_id:
            return CronCommandResult("origin test-delivery requires --session-id", exit_code=2)
        job["origin"] = {"thread_id": session_id}

    event = enqueue_result(
        job,
        JobRunResult(
            success=True,
            output_doc="# Test Delivery\n\nThis is a cron delivery test.",
            final_response="This is a cron delivery test.",
        ),
        output_path="",
        run_at=now(),
    )
    process_due(limit=20)
    if event is None:
        return CronCommandResult("No delivery event created.", exit_code=2)
    stored = DeliveryStore().get(event["id"])
    text = (
        f"test-delivery event={stored['id']} "
        f"target={stored['target']} status={stored['status']} "
        f"error={stored['last_error'] or '-'}"
    )
    if stored["status"] in {"delivered", "pending"}:
        return CronCommandResult(text, exit_code=0)
    if stored["status"] == "failed":
        return CronCommandResult(text, exit_code=1)
    return CronCommandResult(text, exit_code=2)
```

- [ ] **Step 4: Wire top-level CLI in `agent_cli/main.py`**

Add subparsers near existing `status` and `tick` cron subcommands:

```python
    cron_subparsers.add_parser("doctor", parents=[public_options])
    cron_test_delivery = cron_subparsers.add_parser("test-delivery", parents=[public_options])
    cron_test_delivery.add_argument("--target", required=True)
    cron_test_delivery.add_argument("--session-id")
```

Update `_run_cron_command()`:

```python
    if subcommand == "doctor":
        return cron_commands.cron_doctor()
    if subcommand == "test-delivery":
        return cron_commands.test_delivery(
            target=args.target,
            session_id=getattr(args, "session_id", None),
        )
```

- [ ] **Step 5: Wire `/cron` handler**

In `agent_cli/command_handlers/cron.py`, update `_usage()` with:

```python
"  /cron doctor",
"  /cron test-delivery --target local|origin|webhook:<url> [--session-id ID]",
```

Update `_parse_flags()` to include:

```python
"target": None,
"session_id": None,
```

Handle flags:

```python
if token in {"--target", "--session-id"}:
    if i + 1 >= len(tokens):
        return flags, positionals, f"{token} requires a value"
    flags[token[2:].replace("-", "_")] = tokens[i + 1]
    i += 2
```

Add in `handle_cron()`:

```python
    if subcommand == "doctor":
        return cron_commands.cron_doctor().text
    if subcommand == "test-delivery":
        target = flags["target"] or (positionals[0] if positionals else None)
        if not target:
            return _usage()
        session_id = flags["session_id"] or ctx.session_id
        return cron_commands.test_delivery(target=target, session_id=session_id).text
```

- [ ] **Step 6: Run command tests**

Run: `pytest tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_cli/cron_commands.py agent_cli/main.py agent_cli/command_handlers/cron.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py
git commit -m "feat: add cron delivery test command"
```

---

### Task 7: Full Verification and Regression Pass

**Files:**
- No planned source edits unless verification reveals failures.

- [ ] **Step 1: Run focused cron test suite**

Run:

```bash
pytest \
  tests/test_cron_delivery_store.py \
  tests/test_cron_delivery.py \
  tests/test_cron_notifications.py \
  tests/test_cron_scheduler.py \
  tests/test_cron_jobs.py \
  tests/test_agent_cli_cron_commands.py \
  tests/test_agent_cli_main.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run import/lazy-loading checks**

Run:

```bash
pytest tests/test_cron_lifecycle.py tests/test_agent_tools_public_imports.py -q
```

Expected: PASS. If lazy import tests fail because `cron.notifications` imports `cron.delivery_store` too early, move the import inside the public function body.

- [ ] **Step 3: Manual smoke commands**

Run:

```bash
AGENT_CRON_HOME=/tmp/agent-cron-smoke python -m agent_cli.main cron status
AGENT_CRON_HOME=/tmp/agent-cron-smoke python -m agent_cli.main cron doctor
AGENT_CRON_HOME=/tmp/agent-cron-smoke python -m agent_cli.main cron test-delivery --target local
```

Expected:

- `cron status` prints `Delivery queue:`.
- `cron doctor` prints a delivery db check.
- `cron test-delivery --target local` exits 0 and prints `status=delivered`.

- [ ] **Step 4: Commit verification fixes if needed**

If verification required any fixes:

```bash
git add <changed-files>
git commit -m "fix: stabilize cron delivery diagnostics"
```

If no fixes were needed, do not create an empty commit.


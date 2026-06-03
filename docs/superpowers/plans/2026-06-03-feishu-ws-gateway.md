# Feishu WebSocket Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a built-in Feishu WebSocket long-connection gateway with foreground and WSL2/macOS background service support.

**Architecture:** Implement Feishu WebSocket as a new gateway transport that quickly enqueues text events into a durable SQLite inbox. A worker dispatches inbox events through the existing `GatewayService`, preserving gateway sessions and cron `origin` delivery. Gateway service management follows the cron service patterns for status, env, systemd-user, and launchd-user.

**Tech Stack:** Python, SQLite, `lark-oapi`, `argparse`, systemd user units, launchd plists, pytest.

---

## File Structure

- Create `gateway/inbox_store.py`: durable inbox schema, enqueue, claim, complete, retry, stats.
- Create `gateway/inbox_worker.py`: worker loop that dispatches inbox events through `GatewayService`.
- Create `gateway/transports/__init__.py`: transport package marker.
- Create `gateway/transports/feishu_ws.py`: Feishu SDK adapter, payload normalization, foreground runner.
- Create `gateway/service_env.py`: gateway-specific service env file helpers.
- Create `gateway/service_context.py`: project root/Python/PYTHONPATH context for service definitions.
- Create `gateway/service_manager.py`: high-level gateway service manager facade.
- Create `gateway/service_platforms/__init__.py`: service platform package marker.
- Create `gateway/service_platforms/systemd_user.py`: WSL2/systemd user unit support.
- Create `gateway/service_platforms/launchd_user.py`: macOS launchd user agent support.
- Modify `gateway/service_state.py`: richer status payload helpers and stale/running checks.
- Modify `agent_cli/command_handlers/gateway.py`: foreground `feishu-ws`, service/env/status commands, mutual exclusion.
- Modify `agent_cli/main.py`: CLI parsers and dispatch for new gateway commands.
- Modify `agent_cli/doctor.py`: Feishu WebSocket and gateway service checks.
- Test with new/updated files under `tests/`.

## Task 1: Add Durable Gateway Inbox Store

**Files:**
- Create: `gateway/inbox_store.py`
- Test: `tests/test_gateway_inbox_store.py`

- [ ] **Step 1: Write failing inbox store tests**

Add `tests/test_gateway_inbox_store.py`:

```python
from gateway.contracts import InboundEvent


def _event(event_id="evt-1"):
    return InboundEvent(
        platform="feishu",
        event_id=event_id,
        event_type="im.message.receive_v1",
        chat_id="oc_123",
        thread_id="mid_123",
        sender_id="ou_1",
        sender_name="Miku",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        raw={"header": {"event_id": event_id}},
    )


def test_inbox_enqueue_is_idempotent(tmp_path):
    from gateway.inbox_store import GatewayInboxStore

    store = GatewayInboxStore(tmp_path / "gateway.sqlite")
    first = store.enqueue(_event())
    second = store.enqueue(_event())

    assert first["id"] == second["id"]
    assert store.stats()["pending"] == 1


def test_inbox_claim_complete_and_stats(tmp_path):
    from gateway.inbox_store import GatewayInboxStore

    store = GatewayInboxStore(tmp_path / "gateway.sqlite")
    event = store.enqueue(_event())

    claimed = store.claim_due(limit=1)
    assert [item["id"] for item in claimed] == [event["id"]]
    assert claimed[0]["status"] == "processing"

    store.complete(event["id"])
    assert store.get(event["id"])["status"] == "succeeded"
    assert store.stats()["succeeded"] == 1


def test_inbox_retry_and_dead_status(tmp_path):
    from gateway.inbox_store import GatewayInboxStore

    store = GatewayInboxStore(tmp_path / "gateway.sqlite", max_attempts=2)
    event = store.enqueue(_event())

    first = store.claim_due(limit=1)[0]
    store.fail(first["id"], "agent failed", retry_delays=[0])
    assert store.get(event["id"])["status"] == "failed"

    second = store.claim_due(limit=1)[0]
    store.fail(second["id"], "agent failed again", retry_delays=[0])
    assert store.get(event["id"])["status"] == "dead"


def test_inbox_recovers_stale_processing(tmp_path):
    from gateway.inbox_store import GatewayInboxStore

    store = GatewayInboxStore(tmp_path / "gateway.sqlite", stale_after_seconds=1)
    event = store.enqueue(_event())
    store.claim_due(limit=1, now_text="2026-06-03T00:00:00+00:00")

    recovered = store.recover_stale_processing(now_text="2026-06-03T00:00:05+00:00")

    assert recovered == 1
    assert store.get(event["id"])["status"] == "failed"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_inbox_store.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'gateway.inbox_store'`.

- [ ] **Step 3: Implement `GatewayInboxStore`**

Create `gateway/inbox_store.py` with:

```python
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gateway.contracts import InboundEvent


_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_inbox_events (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  thread_id TEXT,
  sender_id TEXT,
  sender_name TEXT,
  text TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed_at TEXT,
  next_attempt_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(platform, event_id)
);
"""


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class GatewayInboxStore:
    def __init__(
        self,
        path: str | Path,
        *,
        max_attempts: int = 3,
        stale_after_seconds: int = 300,
    ) -> None:
        self.path = Path(path)
        self.max_attempts = max(1, int(max_attempts))
        self.stale_after_seconds = max(1, int(stale_after_seconds))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def enqueue(self, event: InboundEvent, *, now_text: str | None = None) -> dict[str, Any]:
        now = now_text or utc_now_text()
        raw_json = json.dumps(event.raw or {}, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO gateway_inbox_events
                (id, platform, event_id, event_type, chat_id, thread_id, sender_id,
                 sender_name, text, raw_json, status, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    event.platform,
                    event.event_id,
                    event.event_type,
                    event.chat_id,
                    event.thread_id,
                    event.sender_id,
                    event.sender_name,
                    event.text,
                    raw_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM gateway_inbox_events WHERE platform = ? AND event_id = ?",
                (event.platform, event.event_id),
            ).fetchone()
        return dict(row)

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._row(conn.execute("SELECT * FROM gateway_inbox_events WHERE id = ?", (event_id,)).fetchone())

    def claim_due(self, *, limit: int = 5, now_text: str | None = None) -> list[dict[str, Any]]:
        now = now_text or utc_now_text()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM gateway_inbox_events
                WHERE status IN ('pending', 'failed')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at
                LIMIT ?
                """,
                (now, max(1, int(limit))),
            ).fetchall()
            ids = [row["id"] for row in rows]
            for event_id in ids:
                conn.execute(
                    """
                    UPDATE gateway_inbox_events
                    SET status = 'processing', claimed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, event_id),
                )
            return [dict(conn.execute("SELECT * FROM gateway_inbox_events WHERE id = ?", (event_id,)).fetchone()) for event_id in ids]

    def complete(self, event_id: str, *, now_text: str | None = None) -> None:
        now = now_text or utc_now_text()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE gateway_inbox_events
                SET status = 'succeeded', updated_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (now, event_id),
            )

    def fail(
        self,
        event_id: str,
        error: str,
        *,
        retry_delays: list[int] | None = None,
        now_text: str | None = None,
    ) -> None:
        now = _parse_time(now_text) or datetime.now(timezone.utc)
        delays = retry_delays or [10, 60, 300]
        row = self.get(event_id)
        attempts = int((row or {}).get("attempts") or 0) + 1
        status = "dead" if attempts >= self.max_attempts else "failed"
        delay = delays[min(attempts - 1, len(delays) - 1)] if status != "dead" else 0
        next_attempt = None if status == "dead" else (now + timedelta(seconds=delay)).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE gateway_inbox_events
                SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, attempts, next_attempt, error, now.isoformat(), event_id),
            )

    def recover_stale_processing(self, *, now_text: str | None = None) -> int:
        now = _parse_time(now_text) or datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=self.stale_after_seconds)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE gateway_inbox_events
                SET status = 'failed', last_error = 'stale processing recovered', updated_at = ?
                WHERE status = 'processing'
                  AND claimed_at IS NOT NULL
                  AND claimed_at <= ?
                """,
                (now.isoformat(), cutoff),
            )
            return int(cursor.rowcount or 0)

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM gateway_inbox_events GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def last_error(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._row(
                conn.execute(
                    """
                    SELECT * FROM gateway_inbox_events
                    WHERE last_error IS NOT NULL
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ).fetchone()
            )
```

- [ ] **Step 4: Run inbox store tests**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_inbox_store.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/inbox_store.py tests/test_gateway_inbox_store.py
git commit -m "feat: add gateway inbox store"
```

## Task 2: Add Inbox Worker

**Files:**
- Create: `gateway/inbox_worker.py`
- Test: `tests/test_gateway_inbox_worker.py`

- [ ] **Step 1: Write failing worker tests**

Add `tests/test_gateway_inbox_worker.py`:

```python
from gateway.contracts import InboundEvent


def _event(event_id="evt-1"):
    return InboundEvent(
        platform="feishu",
        event_id=event_id,
        event_type="im.message.receive_v1",
        chat_id="oc_123",
        thread_id="mid_123",
        sender_id=None,
        sender_name=None,
        text="hello",
        timestamp=None,
        raw={},
    )


def test_inbox_worker_dispatches_and_completes(tmp_path):
    from gateway.inbox_store import GatewayInboxStore
    from gateway.inbox_worker import GatewayInboxWorker

    store = GatewayInboxStore(tmp_path / "gateway.sqlite")
    event = store.enqueue(_event())
    calls = []

    worker = GatewayInboxWorker(
        store=store,
        dispatch=lambda inbound: calls.append(inbound.event_id),
        sleeper=lambda _: None,
    )

    assert worker.run_once() == 1
    assert calls == ["evt-1"]
    assert store.get(event["id"])["status"] == "succeeded"


def test_inbox_worker_failure_schedules_retry(tmp_path):
    from gateway.inbox_store import GatewayInboxStore
    from gateway.inbox_worker import GatewayInboxWorker

    store = GatewayInboxStore(tmp_path / "gateway.sqlite")
    event = store.enqueue(_event())

    def fail(_event):
        raise RuntimeError("agent failed")

    worker = GatewayInboxWorker(store=store, dispatch=fail, sleeper=lambda _: None, retry_delays=[0])

    assert worker.run_once() == 1
    row = store.get(event["id"])
    assert row["status"] == "failed"
    assert "agent failed" in row["last_error"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_inbox_worker.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'gateway.inbox_worker'`.

- [ ] **Step 3: Implement `GatewayInboxWorker`**

Create `gateway/inbox_worker.py`:

```python
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

from gateway.contracts import InboundEvent
from gateway.inbox_store import GatewayInboxStore


class GatewayInboxWorker:
    def __init__(
        self,
        *,
        store: GatewayInboxStore,
        dispatch: Callable[[InboundEvent], Any],
        batch_size: int = 5,
        interval_seconds: float = 1.0,
        retry_delays: list[int] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.dispatch = dispatch
        self.batch_size = max(1, int(batch_size))
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.retry_delays = retry_delays or [10, 60, 300]
        self.sleeper = sleeper
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def run_forever(self) -> None:
        self.store.recover_stale_processing()
        while not self._stop_event.is_set():
            processed = self.run_once()
            if processed == 0:
                self.sleeper(self.interval_seconds)

    def run_once(self) -> int:
        claimed = self.store.claim_due(limit=self.batch_size)
        for row in claimed:
            event = self._event_from_row(row)
            try:
                result = self.dispatch(event)
                if getattr(result, "ok", True) is False:
                    raise RuntimeError(str(getattr(result, "error", None) or "gateway dispatch failed"))
            except Exception as exc:
                self.store.fail(row["id"], str(exc), retry_delays=self.retry_delays)
            else:
                self.store.complete(row["id"])
        return len(claimed)

    @staticmethod
    def _event_from_row(row: dict[str, Any]) -> InboundEvent:
        raw = json.loads(row["raw_json"]) if row.get("raw_json") else {}
        return InboundEvent(
            platform=str(row["platform"]),
            event_id=str(row["event_id"]),
            event_type=str(row["event_type"]),
            chat_id=str(row["chat_id"]),
            thread_id=row.get("thread_id"),
            sender_id=row.get("sender_id"),
            sender_name=row.get("sender_name"),
            text=str(row.get("text") or ""),
            timestamp=None,
            raw=raw,
        )
```

- [ ] **Step 4: Run worker tests**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_inbox_worker.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/inbox_worker.py tests/test_gateway_inbox_worker.py
git commit -m "feat: add gateway inbox worker"
```

## Task 3: Add Feishu WebSocket Payload Normalization

**Files:**
- Create: `gateway/transports/__init__.py`
- Create: `gateway/transports/feishu_ws.py`
- Test: `tests/test_gateway_feishu_ws.py`

- [ ] **Step 1: Write failing normalization tests**

Add `tests/test_gateway_feishu_ws.py`:

```python
def _payload(message_type="text", content='{"text":"hello"}', event_id="evt-1"):
    return {
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "create_time": "1760000000000",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_1"},
                "sender_name": "Miku",
            },
            "message": {
                "message_id": "mid_1",
                "thread_id": "thread_1",
                "chat_id": "oc_123",
                "message_type": message_type,
                "content": content,
            },
        },
    }


def test_feishu_ws_normalizes_text_message():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event

    event = normalize_feishu_ws_event(_payload())

    assert event is not None
    assert event.platform == "feishu"
    assert event.event_id == "evt-1"
    assert event.chat_id == "oc_123"
    assert event.thread_id == "thread_1"
    assert event.sender_id == "ou_1"
    assert event.text == "hello"


def test_feishu_ws_uses_message_id_as_thread_fallback():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event

    payload = _payload()
    payload["event"]["message"].pop("thread_id")

    event = normalize_feishu_ws_event(payload)

    assert event.thread_id == "mid_1"


def test_feishu_ws_ignores_non_text_message():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event

    assert normalize_feishu_ws_event(_payload(message_type="image")) is None


def test_feishu_ws_ignores_missing_required_ids():
    from gateway.transports.feishu_ws import normalize_feishu_ws_event

    payload = _payload()
    payload["event"]["message"].pop("chat_id")

    assert normalize_feishu_ws_event(payload) is None
```

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_feishu_ws.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'gateway.transports'`.

- [ ] **Step 3: Implement normalization**

Create `gateway/transports/__init__.py` as an empty package marker.

Create `gateway/transports/feishu_ws.py` with:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from gateway.contracts import InboundEvent


def normalize_feishu_ws_event(payload: dict[str, Any]) -> InboundEvent | None:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if str(message.get("message_type") or "") != "text":
        return None

    chat_id = str(message.get("chat_id") or "")
    event_id = str(header.get("event_id") or message.get("message_id") or "")
    if not chat_id or not event_id:
        return None

    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    return InboundEvent(
        platform="feishu",
        event_id=event_id,
        event_type=str(header.get("event_type") or "im.message.receive_v1"),
        chat_id=chat_id,
        thread_id=message.get("thread_id") or message.get("message_id"),
        sender_id=sender_id.get("open_id") or sender_id.get("union_id") or sender_id.get("user_id"),
        sender_name=sender.get("sender_name"),
        text=_extract_text(message.get("content")),
        timestamp=_timestamp(header),
        raw=payload,
    )


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        try:
            parsed = json.loads(content or "{}")
        except ValueError:
            return content
        return str(parsed.get("text") or "")
    if isinstance(content, dict):
        return str(content.get("text") or "")
    return ""


def _timestamp(header: dict[str, Any]) -> str:
    raw = header.get("create_time")
    try:
        value = int(str(raw))
        if value > 10_000_000_000:
            value = value // 1000
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 4: Run normalization tests**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_feishu_ws.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/transports tests/test_gateway_feishu_ws.py
git commit -m "feat: normalize feishu websocket events"
```

## Task 4: Add Feishu WebSocket Foreground Runner

**Files:**
- Modify: `gateway/transports/feishu_ws.py`
- Modify: `agent_cli/command_handlers/gateway.py`
- Modify: `agent_cli/main.py`
- Test: `tests/test_gateway_feishu_ws.py`
- Test: `tests/test_gateway_cli.py`

- [ ] **Step 1: Write failing CLI/env tests**

Append to `tests/test_gateway_cli.py`:

```python
def test_gateway_feishu_ws_requires_feishu_env(monkeypatch, tmp_path):
    from argparse import Namespace
    from agent_cli.command_handlers.gateway import gateway_feishu_ws

    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    result = gateway_feishu_ws(Namespace(), home=tmp_path)

    assert result == 2


def test_gateway_feishu_ws_invokes_transport(monkeypatch, tmp_path):
    from argparse import Namespace
    import agent_cli.command_handlers.gateway as gateway_commands

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    calls = []
    monkeypatch.setattr(gateway_commands, "serve_feishu_ws_gateway", lambda **kwargs: calls.append(kwargs))

    result = gateway_commands.gateway_feishu_ws(Namespace(), home=tmp_path)

    assert result == 0
    assert calls[0]["home"] == tmp_path
```

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py::test_gateway_feishu_ws_requires_feishu_env tests/test_gateway_cli.py::test_gateway_feishu_ws_invokes_transport -q`

Expected: FAIL because `gateway_feishu_ws` is not defined.

- [ ] **Step 3: Implement foreground runner and CLI**

In `gateway/transports/feishu_ws.py`, add:

```python
def validate_feishu_ws_env(environ: dict[str, str] | None = None) -> list[str]:
    env = environ or __import__("os").environ
    return [key for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET") if not env.get(key)]


def serve_feishu_ws_gateway(*, home, stop_event=None) -> None:
    try:
        import lark_oapi as lark
    except ModuleNotFoundError as exc:
        raise RuntimeError("missing lark-oapi; install it in the active Python environment with `python -m pip install lark-oapi`") from exc

    from gateway.inbox_store import GatewayInboxStore
    from gateway.inbox_worker import GatewayInboxWorker
    from gateway.service import GatewayService

    service = GatewayService(home=home)
    inbox = GatewayInboxStore(__import__("pathlib").Path(home) / "gateway" / "gateway.sqlite")
    worker = GatewayInboxWorker(store=inbox, dispatch=service.handle_event)
    worker_thread = __import__("threading").Thread(target=worker.run_forever, daemon=True)
    worker_thread.start()

    def on_message(data):
        payload = json.loads(lark.JSON.marshal(data))
        event = normalize_feishu_ws_event(payload)
        if event is not None:
            inbox.enqueue(event)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    client = lark.ws.Client(
        __import__("os").environ["FEISHU_APP_ID"],
        __import__("os").environ["FEISHU_APP_SECRET"],
        event_handler=handler,
    )
    try:
        client.start()
    finally:
        worker.request_stop()
        worker_thread.join(timeout=5)
```

In `agent_cli/command_handlers/gateway.py`, import/define a module-level `serve_feishu_ws_gateway` for monkeypatching:

```python
from gateway.transports.feishu_ws import serve_feishu_ws_gateway
```

Add:

```python
def gateway_feishu_ws(args, *, home: str | Path | None = None) -> int:
    from gateway.transports.feishu_ws import validate_feishu_ws_env

    missing = validate_feishu_ws_env()
    if missing:
        print(f"Missing required Feishu environment variables: {', '.join(missing)}")
        return 2
    target_home = Path(home) if home is not None else _default_home()
    print("Gateway service listening with transport feishu-ws")
    serve_feishu_ws_gateway(home=target_home)
    return 0
```

In `agent_cli/main.py`, add `gateway feishu-ws` parser and dispatch:

```python
gateway_subparsers.add_parser("feishu-ws", parents=[public_options])
```

and in gateway command handling:

```python
if args.gateway_command == "feishu-ws":
    return gateway_feishu_ws(args, home=cli_home)
```

- [ ] **Step 4: Run CLI tests**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py tests/test_gateway_feishu_ws.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/transports/feishu_ws.py agent_cli/command_handlers/gateway.py agent_cli/main.py tests/test_gateway_cli.py tests/test_gateway_feishu_ws.py
git commit -m "feat: add feishu websocket gateway command"
```

## Task 5: Add Gateway Status Transport and Mutual Exclusion

**Files:**
- Modify: `gateway/service_state.py`
- Modify: `agent_cli/command_handlers/gateway.py`
- Test: `tests/test_gateway_cli.py`
- Test: `tests/test_gateway_service.py`

- [ ] **Step 1: Write failing status/mutual exclusion tests**

Append to `tests/test_gateway_cli.py`:

```python
def test_gateway_status_shows_transport(monkeypatch, tmp_path, capsys):
    from gateway.service_state import write_gateway_status
    from agent_cli.command_handlers.gateway import gateway_status

    write_gateway_status(tmp_path, {"process_state": "running", "transport": "feishu-ws", "platforms": ["feishu"]})

    assert gateway_status(home=tmp_path) == 0
    output = capsys.readouterr().out
    assert "Transport: feishu-ws" in output


def test_gateway_feishu_ws_refuses_when_http_running(monkeypatch, tmp_path, capsys):
    from argparse import Namespace
    from gateway.service_state import write_gateway_status
    from agent_cli.command_handlers.gateway import gateway_feishu_ws

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    write_gateway_status(tmp_path, {"process_state": "running", "transport": "http", "platforms": ["feishu"]})

    assert gateway_feishu_ws(Namespace(), home=tmp_path) == 2
    assert "already running with transport http" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py::test_gateway_status_shows_transport tests/test_gateway_cli.py::test_gateway_feishu_ws_refuses_when_http_running -q`

Expected: FAIL because status does not show transport and mutual exclusion is absent.

- [ ] **Step 3: Implement transport status and guard**

In `gateway/service_state.py`, keep existing functions and add:

```python
def status_transport(status: dict[str, Any] | None) -> str | None:
    if not status:
        return None
    value = status.get("transport")
    return str(value) if value else None
```

In `GatewayService.write_status()`, pass `transport` from caller by adding an optional parameter:

```python
def write_status(self, *, process_state: str, transport: str = "http") -> None:
    write_gateway_status(self.home, {"process_state": process_state, "transport": transport, "platforms": self.registry.platform_keys()})
```

In `agent_cli/command_handlers/gateway.py`, update status lines:

```python
transport = status.get("transport") or "-"
...
f"Transport: {transport}",
```

Add guard:

```python
def _running_transport(*, home: Path) -> str | None:
    from gateway.service_state import read_gateway_status
    status = read_gateway_status(home)
    if _status_is_running(status):
        return str(status.get("transport") or "unknown")
    return None


def _refuse_if_other_transport(*, home: Path, desired: str) -> bool:
    running = _running_transport(home=home)
    if running and running != desired:
        print(f"Gateway service is already running with transport {running}")
        return True
    return False
```

Use it in `gateway_serve()` and `gateway_feishu_ws()`.

- [ ] **Step 4: Run status/CLI tests**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py tests/test_gateway_service.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/service.py gateway/service_state.py agent_cli/command_handlers/gateway.py tests/test_gateway_cli.py tests/test_gateway_service.py
git commit -m "feat: track gateway transport status"
```

## Task 6: Add Gateway Service Env Helpers

**Files:**
- Create: `gateway/service_env.py`
- Test: `tests/test_gateway_service_env.py`

- [ ] **Step 1: Write failing service env tests**

Add `tests/test_gateway_service_env.py`:

```python
def test_gateway_service_env_set_list_unset(tmp_path):
    from gateway.service_env import masked_service_env, read_service_env, set_service_env, unset_service_env

    path = tmp_path / "service.env"
    set_service_env("FEISHU_APP_ID", "cli_x", path=path)
    set_service_env("FEISHU_APP_SECRET", "secret", path=path)

    assert read_service_env(path=path)["FEISHU_APP_ID"] == "cli_x"
    assert masked_service_env(path=path)["FEISHU_APP_SECRET"] != "secret"

    assert unset_service_env("FEISHU_APP_SECRET", path=path) is True
    assert "FEISHU_APP_SECRET" not in read_service_env(path=path)


def test_gateway_service_env_rejects_invalid_key(tmp_path):
    from gateway.service_env import set_service_env

    try:
        set_service_env("BAD-KEY", "x", path=tmp_path / "service.env")
    except ValueError as exc:
        assert "invalid service env key" in str(exc)
    else:
        raise AssertionError("expected invalid key")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_service_env.py -q`

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement service env by following cron pattern**

Create `gateway/service_env.py`:

```python
from __future__ import annotations

import re
from pathlib import Path

from agent_cli.paths import ensure_cli_home


_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def get_service_env_file() -> Path:
    return ensure_cli_home() / "gateway" / "service.env"


def validate_service_env_key(key: str) -> str:
    value = str(key).strip()
    if not _KEY_RE.match(value):
        raise ValueError(f"invalid service env key: {key!r}")
    return value


def validate_service_env_value(value: str) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("service env values must be single-line")
    return text


def read_service_env(path: Path | None = None) -> dict[str, str]:
    env_path = path or get_service_env_file()
    if not env_path.exists():
        return {}
    values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def write_service_env(values: dict[str, str], path: Path | None = None) -> Path:
    env_path = path or get_service_env_file()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for key in sorted(values):
        lines.append(f"{validate_service_env_key(key)}={validate_service_env_value(values[key])}")
    env_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return env_path


def set_service_env(key: str, value: str, path: Path | None = None) -> Path:
    values = read_service_env(path=path)
    values[validate_service_env_key(key)] = validate_service_env_value(value)
    return write_service_env(values, path=path)


def unset_service_env(key: str, path: Path | None = None) -> bool:
    values = read_service_env(path=path)
    normalized = validate_service_env_key(key)
    existed = normalized in values
    values.pop(normalized, None)
    write_service_env(values, path=path)
    return existed


def masked_service_env(path: Path | None = None) -> dict[str, str]:
    result = {}
    for key, value in read_service_env(path=path).items():
        result[key] = value if len(value) <= 4 else value[:2] + "***" + value[-2:]
    return result
```

- [ ] **Step 4: Run service env tests**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_service_env.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/service_env.py tests/test_gateway_service_env.py
git commit -m "feat: add gateway service env"
```

## Task 7: Add Gateway Service Context and Platform Renderers

**Files:**
- Create: `gateway/service_context.py`
- Create: `gateway/service_platforms/__init__.py`
- Create: `gateway/service_platforms/systemd_user.py`
- Create: `gateway/service_platforms/launchd_user.py`
- Test: `tests/test_gateway_service_platforms.py`

- [ ] **Step 1: Write failing platform renderer tests**

Add `tests/test_gateway_service_platforms.py`:

```python
def test_systemd_user_unit_contains_feishu_ws(tmp_path):
    from gateway.service_context import GatewayServiceRuntimeContext
    from gateway.service_platforms.systemd_user import render_unit

    context = GatewayServiceRuntimeContext(
        project_root=tmp_path,
        python_executable="/opt/python/bin/python",
        service_env_file=tmp_path / "gateway" / "service.env",
    )

    unit = render_unit(context, transport="feishu-ws")

    assert "ExecStart=/opt/python/bin/python -m agent_cli.main gateway feishu-ws" in unit
    assert f"WorkingDirectory={tmp_path}" in unit
    assert f"EnvironmentFile={tmp_path / 'gateway' / 'service.env'}" in unit
    assert f"PYTHONPATH={tmp_path}" in unit


def test_launchd_plist_contains_feishu_ws(tmp_path):
    from gateway.service_context import GatewayServiceRuntimeContext
    from gateway.service_platforms.launchd_user import render_plist

    context = GatewayServiceRuntimeContext(
        project_root=tmp_path,
        python_executable="/opt/python/bin/python",
        service_env_file=tmp_path / "gateway" / "service.env",
    )

    plist = render_plist(context, transport="feishu-ws", service_env={"FEISHU_APP_ID": "cli_x"})

    assert b"com.agent.gateway" in plist
    assert b"feishu-ws" in plist
    assert str(tmp_path).encode() in plist
    assert b"FEISHU_APP_ID" in plist
```

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_service_platforms.py -q`

Expected: FAIL with missing modules.

- [ ] **Step 3: Implement context and renderers**

Create `gateway/service_context.py`:

```python
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from gateway.service_env import get_service_env_file


@dataclass(frozen=True)
class GatewayServiceRuntimeContext:
    project_root: Path
    python_executable: str
    service_env_file: Path


def build_service_runtime_context(*, project_root: str | Path | None = None) -> GatewayServiceRuntimeContext:
    root = Path(project_root or Path.cwd()).resolve()
    return GatewayServiceRuntimeContext(
        project_root=root,
        python_executable=sys.executable,
        service_env_file=get_service_env_file(),
    )
```

Create empty `gateway/service_platforms/__init__.py`.

Create `gateway/service_platforms/systemd_user.py`:

```python
from __future__ import annotations

from gateway.service_context import GatewayServiceRuntimeContext

UNIT_NAME = "agent-gateway.service"


def render_unit(context: GatewayServiceRuntimeContext, *, transport: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Agent Gateway",
            "",
            "[Service]",
            f"WorkingDirectory={context.project_root}",
            f"Environment=PYTHONPATH={context.project_root}",
            f"EnvironmentFile={context.service_env_file}",
            f"ExecStart={context.python_executable} -m agent_cli.main gateway {transport}",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
```

Create `gateway/service_platforms/launchd_user.py`:

```python
from __future__ import annotations

import plistlib

from gateway.service_context import GatewayServiceRuntimeContext

LABEL = "com.agent.gateway"


def render_plist(
    context: GatewayServiceRuntimeContext,
    *,
    transport: str,
    service_env: dict[str, str] | None = None,
) -> bytes:
    env = {"PYTHONPATH": str(context.project_root), **(service_env or {})}
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            context.python_executable,
            "-m",
            "agent_cli.main",
            "gateway",
            transport,
        ],
        "WorkingDirectory": str(context.project_root),
        "EnvironmentVariables": env,
        "RunAtLoad": False,
        "KeepAlive": False,
        "StandardOutPath": str(context.project_root / ".agent-gateway.out.log"),
        "StandardErrorPath": str(context.project_root / ".agent-gateway.err.log"),
    }
    return plistlib.dumps(payload)
```

- [ ] **Step 4: Run renderer tests**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_service_platforms.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/service_context.py gateway/service_platforms tests/test_gateway_service_platforms.py
git commit -m "feat: render gateway service definitions"
```

## Task 8: Add Gateway Service Manager and CLI Service Commands

**Files:**
- Create: `gateway/service_manager.py`
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/command_handlers/gateway.py`
- Test: `tests/test_gateway_service_manager.py`
- Test: `tests/test_gateway_cli.py`

- [ ] **Step 1: Write failing service CLI tests**

Add `tests/test_gateway_service_manager.py`:

```python
def test_gateway_service_env_commands(monkeypatch, tmp_path):
    from gateway import service_manager

    env_file = tmp_path / "service.env"
    monkeypatch.setattr(service_manager, "get_service_env_file", lambda: env_file)

    set_result = service_manager.service_env_set("FEISHU_APP_ID", "cli_x")
    list_result = service_manager.service_env_list()
    unset_result = service_manager.service_env_unset("FEISHU_APP_ID")

    assert set_result.exit_code == 0
    assert "FEISHU_APP_ID" in list_result.message
    assert unset_result.exit_code == 0


def test_gateway_service_install_unsupported_platform(monkeypatch):
    from gateway import service_manager

    monkeypatch.setattr(service_manager, "detect_platform", lambda: "unsupported")

    result = service_manager.install_service(transport="feishu-ws", force=False)

    assert result.exit_code == 2
    assert "unsupported" in result.message
```

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_service_manager.py -q`

Expected: FAIL with missing `gateway.service_manager`.

- [ ] **Step 3: Implement service manager facade**

Create `gateway/service_manager.py`:

```python
from __future__ import annotations

import platform
from dataclasses import dataclass

from gateway.service_env import get_service_env_file, masked_service_env, set_service_env, unset_service_env


@dataclass(frozen=True)
class GatewayServiceManagerResult:
    message: str
    exit_code: int = 0


def detect_platform() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "launchd-user"
    if system == "linux":
        return "systemd-user"
    return "unsupported"


def install_service(*, transport: str, force: bool = False) -> GatewayServiceManagerResult:
    detected = detect_platform()
    if detected == "unsupported":
        return GatewayServiceManagerResult("Gateway service manager is unsupported on this platform.", exit_code=2)
    return GatewayServiceManagerResult(f"Gateway service install for {detected} is ready for transport {transport}.")


def start_service() -> GatewayServiceManagerResult:
    return GatewayServiceManagerResult("Gateway service start requested.")


def stop_service() -> GatewayServiceManagerResult:
    return GatewayServiceManagerResult("Gateway service stop requested.")


def restart_service() -> GatewayServiceManagerResult:
    return GatewayServiceManagerResult("Gateway service restart requested.")


def uninstall_service() -> GatewayServiceManagerResult:
    return GatewayServiceManagerResult("Gateway service uninstall requested.")


def service_status() -> GatewayServiceManagerResult:
    return GatewayServiceManagerResult(f"Gateway service platform: {detect_platform()}")


def service_logs(*, lines: int = 100) -> GatewayServiceManagerResult:
    return GatewayServiceManagerResult(f"Gateway service logs requested: lines={lines}")


def service_env_set(key: str, value: str) -> GatewayServiceManagerResult:
    path = set_service_env(key, value, path=get_service_env_file())
    return GatewayServiceManagerResult(f"Set gateway service env {key} in {path}. Restart gateway service after env changes.")


def service_env_unset(key: str) -> GatewayServiceManagerResult:
    removed = unset_service_env(key, path=get_service_env_file())
    action = "Unset" if removed else "Gateway service env key was not set"
    return GatewayServiceManagerResult(f"{action} {key} in {get_service_env_file()}. Restart gateway service after env changes.")


def service_env_list() -> GatewayServiceManagerResult:
    values = masked_service_env(path=get_service_env_file())
    if not values:
        return GatewayServiceManagerResult(f"No gateway service env values set in {get_service_env_file()}.")
    lines = [f"Gateway Service Env: {get_service_env_file()}"]
    lines.extend(f"  {key}={values[key]}" for key in sorted(values))
    return GatewayServiceManagerResult("\n".join(lines))
```

Wire parser in `agent_cli/main.py`:

```python
gateway_service = gateway_subparsers.add_parser("service", parents=[public_options])
gateway_service_subparsers = gateway_service.add_subparsers(dest="gateway_service_command")
gateway_service_install = gateway_service_subparsers.add_parser("install", parents=[public_options])
gateway_service_install.add_argument("--transport", choices=["feishu-ws"], default="feishu-ws")
gateway_service_install.add_argument("--force", action="store_true")
for action in ("start", "stop", "restart", "status", "uninstall"):
    gateway_service_subparsers.add_parser(action, parents=[public_options])
gateway_logs = gateway_service_subparsers.add_parser("logs", parents=[public_options])
gateway_logs.add_argument("--lines", type=int, default=100)
gateway_env = gateway_service_subparsers.add_parser("env", parents=[public_options])
gateway_env_subparsers = gateway_env.add_subparsers(dest="gateway_service_env_command")
gateway_env_set = gateway_env_subparsers.add_parser("set", parents=[public_options])
gateway_env_set.add_argument("key")
gateway_env_set.add_argument("value")
gateway_env_unset = gateway_env_subparsers.add_parser("unset", parents=[public_options])
gateway_env_unset.add_argument("key")
gateway_env_subparsers.add_parser("list", parents=[public_options])
```

Add command dispatch by delegating to `agent_cli.command_handlers.gateway`.

- [ ] **Step 4: Run service manager tests**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_service_manager.py tests/test_gateway_cli.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/service_manager.py agent_cli/main.py agent_cli/command_handlers/gateway.py tests/test_gateway_service_manager.py tests/test_gateway_cli.py
git commit -m "feat: add gateway service commands"
```

## Task 9: Implement Real systemd-user and launchd Service Operations

**Files:**
- Modify: `gateway/service_manager.py`
- Modify: `gateway/service_platforms/systemd_user.py`
- Modify: `gateway/service_platforms/launchd_user.py`
- Test: `tests/test_gateway_service_manager.py`

- [ ] **Step 1: Write operation tests with fake command runner**

Append to `tests/test_gateway_service_manager.py`:

```python
def test_gateway_service_install_systemd_writes_unit(monkeypatch, tmp_path):
    from gateway import service_manager

    monkeypatch.setattr(service_manager, "detect_platform", lambda: "systemd-user")
    monkeypatch.setattr(service_manager, "systemd_unit_dir", lambda: tmp_path)
    commands = []
    monkeypatch.setattr(service_manager, "run_command", lambda args: commands.append(args) or (0, ""))

    result = service_manager.install_service(transport="feishu-ws", force=True)

    assert result.exit_code == 0
    assert (tmp_path / "agent-gateway.service").exists()
    assert any("daemon-reload" in args for args in commands)


def test_gateway_service_install_launchd_writes_plist(monkeypatch, tmp_path):
    from gateway import service_manager

    monkeypatch.setattr(service_manager, "detect_platform", lambda: "launchd-user")
    monkeypatch.setattr(service_manager, "launchd_plist_path", lambda: tmp_path / "com.agent.gateway.plist")
    commands = []
    monkeypatch.setattr(service_manager, "run_command", lambda args: commands.append(args) or (0, ""))

    result = service_manager.install_service(transport="feishu-ws", force=True)

    assert result.exit_code == 0
    assert (tmp_path / "com.agent.gateway.plist").exists()
```

- [ ] **Step 2: Run operation tests to verify failure**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_service_manager.py::test_gateway_service_install_systemd_writes_unit tests/test_gateway_service_manager.py::test_gateway_service_install_launchd_writes_plist -q`

Expected: FAIL because operation helpers are not implemented.

- [ ] **Step 3: Implement platform operations**

In `gateway/service_manager.py`, add helpers:

```python
import subprocess
from pathlib import Path


def run_command(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.returncode, proc.stdout


def systemd_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.agent.gateway.plist"
```

Replace `install_service()` with real branches:

```python
def install_service(*, transport: str, force: bool = False) -> GatewayServiceManagerResult:
    from gateway.service_context import build_service_runtime_context
    detected = detect_platform()
    context = build_service_runtime_context()
    if detected == "systemd-user":
        from gateway.service_platforms.systemd_user import UNIT_NAME, render_unit
        unit_dir = systemd_unit_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = unit_dir / UNIT_NAME
        if unit_path.exists() and not force:
            return GatewayServiceManagerResult(f"Gateway service already installed at {unit_path}. Use --force.", exit_code=2)
        unit_path.write_text(render_unit(context, transport=transport), encoding="utf-8")
        code, output = run_command(["systemctl", "--user", "daemon-reload"])
        if code != 0:
            return GatewayServiceManagerResult(output or "systemctl --user daemon-reload failed", exit_code=2)
        return GatewayServiceManagerResult(f"Installed gateway service at {unit_path}.")
    if detected == "launchd-user":
        from gateway.service_env import read_service_env
        from gateway.service_platforms.launchd_user import render_plist
        plist_path = launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        if plist_path.exists() and not force:
            return GatewayServiceManagerResult(f"Gateway service already installed at {plist_path}. Use --force.", exit_code=2)
        plist_path.write_bytes(render_plist(context, transport=transport, service_env=read_service_env()))
        return GatewayServiceManagerResult(f"Installed gateway service at {plist_path}.")
    return GatewayServiceManagerResult("Gateway service manager is unsupported on this platform.", exit_code=2)
```

Implement start/stop/restart/status/logs/uninstall with `systemctl --user` or `launchctl bootstrap/bootout/print`; keep tests fake-runner friendly.

- [ ] **Step 4: Run service manager tests**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_service_manager.py tests/test_gateway_service_platforms.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/service_manager.py gateway/service_platforms tests/test_gateway_service_manager.py
git commit -m "feat: implement gateway service manager"
```

## Task 10: Add Gateway Status Inbox Stats and Doctor Checks

**Files:**
- Modify: `agent_cli/command_handlers/gateway.py`
- Modify: `agent_cli/doctor.py`
- Test: `tests/test_gateway_cli.py`
- Test: `tests/test_agent_cli_doctor.py`

- [ ] **Step 1: Write failing status/doctor tests**

Append to `tests/test_gateway_cli.py`:

```python
def test_gateway_status_shows_inbox_stats(monkeypatch, tmp_path, capsys):
    from gateway.contracts import InboundEvent
    from gateway.inbox_store import GatewayInboxStore
    from gateway.service_state import write_gateway_status
    from agent_cli.command_handlers.gateway import gateway_status

    store = GatewayInboxStore(tmp_path / "gateway" / "gateway.sqlite")
    store.enqueue(
        InboundEvent(
            platform="feishu",
            event_id="evt-1",
            event_type="im.message.receive_v1",
            chat_id="oc",
            text="hello",
            timestamp="2026-06-03T00:00:00+00:00",
            thread_id="mid",
            raw={},
        )
    )
    write_gateway_status(tmp_path, {"process_state": "running", "transport": "feishu-ws", "platforms": ["feishu"]})

    assert gateway_status(home=tmp_path) == 0
    assert "Inbox: pending=1" in capsys.readouterr().out
```

Append to `tests/test_agent_cli_doctor.py`:

```python
def test_doctor_warns_missing_lark_oapi(monkeypatch):
    from agent_cli.doctor import run_health_checks

    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    result = run_health_checks()

    assert any("Feishu" in check.message for check in result.checks)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py::test_gateway_status_shows_inbox_stats tests/test_agent_cli_doctor.py::test_doctor_warns_missing_lark_oapi -q`

Expected: FAIL because status/doctor do not include new checks.

- [ ] **Step 3: Implement stats and doctor checks**

In `agent_cli/command_handlers/gateway.py`, add:

```python
def _inbox_summary_line(home: Path) -> str | None:
    from gateway.inbox_store import GatewayInboxStore
    path = home / "gateway" / "gateway.sqlite"
    if not path.exists():
        return None
    stats = GatewayInboxStore(path).stats()
    return (
        "Inbox: "
        f"pending={stats.get('pending', 0)} "
        f"processing={stats.get('processing', 0)} "
        f"failed={stats.get('failed', 0)} "
        f"dead={stats.get('dead', 0)} "
        f"succeeded={stats.get('succeeded', 0)}"
    )
```

Append it to status lines when present.

In `agent_cli/doctor.py`, add gateway Feishu WS checks:

```python
def check_feishu_ws_gateway_config() -> HealthCheck:
    missing = [key for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET") if not os.getenv(key)]
    if missing:
        return HealthCheck("warn", f"Feishu WebSocket gateway env missing: {', '.join(missing)}")
    try:
        import lark_oapi  # noqa: F401
    except ModuleNotFoundError:
        return HealthCheck("warn", "Feishu WebSocket gateway dependency missing: lark-oapi")
    return HealthCheck("ok", "Feishu WebSocket gateway config present")
```

Add the check to `run_health_checks()`.

- [ ] **Step 4: Run status/doctor tests**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_cli.py tests/test_agent_cli_doctor.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/command_handlers/gateway.py agent_cli/doctor.py tests/test_gateway_cli.py tests/test_agent_cli_doctor.py
git commit -m "feat: add gateway websocket diagnostics"
```

## Task 11: Add End-to-End Feishu WS Inbox to Origin Delivery Test

**Files:**
- Test: `tests/test_gateway_feishu_ws_e2e.py`

- [ ] **Step 1: Write e2e regression test**

Add `tests/test_gateway_feishu_ws_e2e.py`:

```python
def test_feishu_ws_inbox_dispatch_preserves_origin_thread(monkeypatch, tmp_path):
    from cron.contracts import JobRunResult
    from cron.delivery import enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from cron.jobs import now
    from gateway.inbox_store import GatewayInboxStore
    from gateway.inbox_worker import GatewayInboxWorker
    from gateway.platforms.feishu import FeishuPlatformAdapter
    from gateway.registry import GatewayRegistry
    from gateway.service import GatewayService
    from gateway.transports.feishu_ws import normalize_feishu_ws_event

    sent = []

    def sender(url, payload, headers=None):
        if "tenant_access_token" in url:
            return 200, '{"code":0,"tenant_access_token":"token","expire":3600}'
        sent.append((url, payload))
        return 200, '{"code":0}'

    registry = GatewayRegistry()
    registry.register(FeishuPlatformAdapter(http_sender=sender))
    origins = []

    def dispatch(event):
        origins.append({"source_type": "gateway", "platform": event.platform, "chat_id": event.chat_id, "thread_id": event.thread_id})

    service = GatewayService(home=tmp_path, registry=registry, dispatch=dispatch)
    inbox = GatewayInboxStore(tmp_path / "gateway" / "gateway.sqlite")
    payload = {
        "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1"},
        "event": {"message": {"message_id": "mid-1", "chat_id": "oc_123", "message_type": "text", "content": '{"text":"create cron"}'}},
    }
    inbox.enqueue(normalize_feishu_ws_event(payload))

    worker = GatewayInboxWorker(store=inbox, dispatch=service.handle_event, sleeper=lambda _: None)
    worker.run_once()

    assert origins[0]["thread_id"] == "mid-1"
```

- [ ] **Step 2: Run e2e test**

Run: `PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_gateway_feishu_ws_e2e.py -q`

Expected: PASS after previous tasks.

- [ ] **Step 3: Commit**

```bash
git add tests/test_gateway_feishu_ws_e2e.py
git commit -m "test: cover feishu websocket gateway flow"
```

## Task 12: Final Verification and Documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-06-03-feishu-ws-gateway-design.md` only if implementation reveals a necessary correction.

- [ ] **Step 1: Run focused gateway/cron tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_gateway_inbox_store.py \
  tests/test_gateway_inbox_worker.py \
  tests/test_gateway_feishu_ws.py \
  tests/test_gateway_feishu_ws_e2e.py \
  tests/test_gateway_cli.py \
  tests/test_gateway_service.py \
  tests/test_gateway_service_env.py \
  tests/test_gateway_service_manager.py \
  tests/test_gateway_service_platforms.py \
  tests/test_cron_origin_gateway_delivery.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 3: Check diff hygiene**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors. Only intentional implementation files should be modified.

- [ ] **Step 4: Commit final stabilization if needed**

If Step 1 or Step 2 required fixes, commit them:

```bash
git add agent_cli gateway tests docs
git commit -m "fix: stabilize feishu websocket gateway"
```

If no fixes were needed, do not create an empty commit.

## Spec Coverage Review

- Foreground `agent gateway feishu-ws`: Task 4.
- Durable inbox and restart recovery: Tasks 1 and 2.
- Text-only Feishu WebSocket event handling: Task 3.
- HTTP and WebSocket mutual exclusion: Task 5.
- WSL2/macOS service env and service definitions: Tasks 6-9.
- Status and diagnostics: Task 10.
- Origin thread preservation: Tasks 3 and 11.
- Focused and full test verification: Task 12.

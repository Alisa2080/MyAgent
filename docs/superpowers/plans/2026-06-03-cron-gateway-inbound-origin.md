# Cron Gateway Inbound Origin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independent gateway service that receives Feishu callbacks, persists platform-origin sessions, dispatches inbound text through the agent layer, and lets cron deliver `origin` results back to Feishu through the existing delivery state machine.

**Architecture:** Keep `cron service` and `gateway service` as separate processes. Gateway owns callback HTTP handling, inbound adapter parsing, session persistence, and inbound dispatch; cron keeps scheduling, runner execution, delivery events, retries, and dead letters. Shared contracts connect them through `InboundEvent`, origin metadata, and `PlatformMessageTarget`.

**Tech Stack:** Python dataclasses, stdlib `http.server`, SQLite, existing `gateway` package, existing `cron` delivery adapters/store, pytest with fake adapters and fake HTTP senders.

---

## File Structure

Create:

- `gateway/session_store.py`: SQLite store for gateway sessions, gateway messages, and inbound event dedupe.
- `gateway/callback_server.py`: small HTTP server and request handler for `/health` and `/callback/<platform>`.
- `gateway/service.py`: `GatewayService` orchestration, status heartbeat, inbound event handling.
- `gateway/service_state.py`: paths and status read/write helpers for gateway service only.
- `gateway/dispatch.py`: session-scoped inbound dispatch that calls an injectable agent runner and outbound adapter.
- `agent_cli/command_handlers/gateway.py`: command handler for gateway CLI subcommands.
- `tests/test_gateway_session_store.py`: session identity, messages, event dedupe.
- `tests/test_gateway_callback_server.py`: route behavior, challenge handling, duplicate dispatch.
- `tests/test_gateway_service.py`: service inbound handling and status.
- `tests/test_gateway_dispatch.py`: session-scoped dispatch and outbound send.
- `tests/test_gateway_cli.py`: CLI command routing and status output.
- `tests/test_cron_origin_gateway_delivery.py`: origin delivery through gateway adapter.
- `tests/test_gateway_e2e.py`: fake Feishu callback to origin delivery path.

Modify:

- `gateway/contracts.py`: add inbound dataclasses and inbound-capable protocol methods.
- `gateway/registry.py`: keep outbound-only adapters working, allow inbound-capable adapters.
- `gateway/platforms/feishu.py`: add Feishu callback challenge, token validation, text event normalization.
- `cron/delivery_adapters.py`: make `OriginDeliveryAdapter` actively dispatch gateway origins.
- `cron/delivery_registry.py`: keep origin registered and active when gateway origin exists.
- `agent_cli/commands.py`: register `gateway` commands.
- `agent_cli/main.py`: route `agent gateway ...` command.
- `agent_cli/doctor.py`: include gateway/Feishu callback checks if doctor already centralizes cron diagnostics.

Do not modify:

- `cron/service.py`: cron service remains independent.
- `cron/scheduler.py`: scheduling flow remains unchanged.
- Existing delivery event table schema unless origin delivery requires a stored metadata read already absent from current payloads.

---

### Task 1: Gateway Inbound Contracts

**Files:**

- Modify: `gateway/contracts.py`
- Modify: `gateway/registry.py`
- Test: `tests/test_gateway_core.py`

- [ ] **Step 1: Add failing tests for inbound value objects and inbound-capable fake adapter**

Append to `tests/test_gateway_core.py`:

```python
def test_gateway_inbound_contracts_are_plain_values():
    from gateway.contracts import InboundEvent, InboundParseResult

    event = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        thread_id="thread-1",
        sender_id="ou_123",
        sender_name="Miku",
        raw={"event": {"message": {"text": "hello"}}},
    )
    result = InboundParseResult(ok=True, event=event, status_code=200, response_body={"ok": True})

    assert event.platform == "feishu"
    assert event.event_id == "evt-1"
    assert event.chat_id == "oc_123"
    assert event.thread_id == "thread-1"
    assert event.sender_id == "ou_123"
    assert result.ok is True
    assert result.event is event
    assert result.status_code == 200
    assert result.response_body == {"ok": True}


def test_gateway_registry_accepts_inbound_capable_adapter():
    from gateway.contracts import InboundParseResult, SendResult
    from gateway.registry import GatewayRegistry

    class FakeInboundAdapter:
        key = "fake"

        def validate_target(self, target):
            return SendResult(ok=True)

        def send_text(self, target, message):
            return SendResult(ok=True)

        def parse_callback(self, headers, body):
            return InboundParseResult(ok=True, response_body={"challenge": "ok"}, status_code=200)

    registry = GatewayRegistry()
    adapter = FakeInboundAdapter()
    registry.register(adapter)

    assert registry.get("fake") is adapter
    assert registry.platform_keys() == ["fake"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_gateway_core.py::test_gateway_inbound_contracts_are_plain_values tests/test_gateway_core.py::test_gateway_registry_accepts_inbound_capable_adapter -v
```

Expected: FAIL because `InboundEvent` and `InboundParseResult` are not defined.

- [ ] **Step 3: Add inbound contracts**

Modify `gateway/contracts.py` to include these dataclasses and protocol method:

```python
@dataclass(frozen=True)
class InboundEvent:
    platform: str
    event_id: str
    event_type: str
    chat_id: str
    text: str
    timestamp: str
    thread_id: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundParseResult:
    ok: bool
    event: InboundEvent | None = None
    response_body: dict[str, Any] | None = None
    status_code: int = 200
    error: str | None = None


class PlatformAdapter(Protocol):
    key: str

    def validate_target(self, target: PlatformMessageTarget) -> SendResult:
        ...

    def send_text(self, target: PlatformMessageTarget, message: OutboundMessage) -> SendResult:
        ...

    def parse_callback(self, headers: dict[str, str], body: dict[str, Any]) -> InboundParseResult:
        ...
```

If typing complains that outbound-only adapters do not implement `parse_callback`, use a second protocol named `InboundPlatformAdapter` and leave `PlatformAdapter` outbound-only:

```python
class InboundPlatformAdapter(Protocol):
    key: str

    def parse_callback(self, headers: dict[str, str], body: dict[str, Any]) -> InboundParseResult:
        ...
```

- [ ] **Step 4: Run gateway core tests**

Run:

```bash
pytest tests/test_gateway_core.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add gateway/contracts.py gateway/registry.py tests/test_gateway_core.py
git commit -m "feat: add gateway inbound contracts"
```

---

### Task 2: Gateway Session Store

**Files:**

- Create: `gateway/session_store.py`
- Test: `tests/test_gateway_session_store.py`

- [ ] **Step 1: Write failing session store tests**

Create `tests/test_gateway_session_store.py`:

```python
from __future__ import annotations


def test_gateway_session_identity_reuses_chat_thread(tmp_path):
    from gateway.session_store import GatewaySessionStore

    store = GatewaySessionStore(tmp_path / "gateway.sqlite")

    first = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id="thread-1",
        sender_id="ou_1",
        sender_name="Miku",
    )
    second = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id="thread-1",
        sender_id="ou_2",
        sender_name="Other",
    )
    third = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id="thread-2",
        sender_id="ou_1",
        sender_name="Miku",
    )

    assert first.session_id == second.session_id
    assert third.session_id != first.session_id
    assert second.sender_id == "ou_1"


def test_gateway_session_store_records_messages_in_order(tmp_path):
    from gateway.session_store import GatewaySessionStore

    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    session = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id=None,
        sender_id="ou_1",
        sender_name="Miku",
    )

    store.record_message(
        session.session_id,
        direction="inbound",
        platform="feishu",
        event_id="evt-1",
        text="hello",
        raw={"a": 1},
    )
    store.record_message(
        session.session_id,
        direction="outbound",
        platform="feishu",
        event_id=None,
        text="world",
        raw={},
    )

    messages = store.list_messages(session.session_id)
    assert [message.direction for message in messages] == ["inbound", "outbound"]
    assert [message.text for message in messages] == ["hello", "world"]


def test_gateway_event_dedupe_is_platform_scoped(tmp_path):
    from gateway.session_store import GatewaySessionStore

    store = GatewaySessionStore(tmp_path / "gateway.sqlite")

    assert store.claim_event("feishu", "evt-1") is True
    assert store.claim_event("feishu", "evt-1") is False
    assert store.claim_event("slack", "evt-1") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_gateway_session_store.py -v
```

Expected: FAIL because `gateway.session_store` does not exist.

- [ ] **Step 3: Implement minimal store**

Create `gateway/session_store.py` with:

```python
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_sessions (
  session_id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  thread_id TEXT,
  sender_id TEXT,
  sender_name TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  last_event_id TEXT,
  last_message_preview TEXT,
  UNIQUE(platform, chat_id, thread_id)
);

CREATE TABLE IF NOT EXISTS gateway_messages (
  message_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  platform TEXT NOT NULL,
  event_id TEXT,
  text TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_inbound_events (
  platform TEXT NOT NULL,
  event_id TEXT NOT NULL,
  claimed_at TEXT NOT NULL,
  PRIMARY KEY(platform, event_id)
);
"""


@dataclass(frozen=True)
class GatewaySession:
    session_id: str
    platform: str
    chat_id: str
    thread_id: str | None
    sender_id: str | None
    sender_name: str | None
    status: str
    last_event_id: str | None
    last_message_preview: str | None


@dataclass(frozen=True)
class GatewayMessage:
    message_id: str
    session_id: str
    direction: str
    platform: str
    event_id: str | None
    text: str
    raw: dict[str, Any]
    created_at: str


class GatewaySessionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    @staticmethod
    def _session_from_row(row: sqlite3.Row | tuple) -> GatewaySession:
        return GatewaySession(
            session_id=row[0],
            platform=row[1],
            chat_id=row[2],
            thread_id=row[3],
            sender_id=row[4],
            sender_name=row[5],
            status=row[6],
            last_event_id=row[7],
            last_message_preview=row[8],
        )

    def get_or_create_session(
        self,
        *,
        platform: str,
        chat_id: str,
        thread_id: str | None,
        sender_id: str | None,
        sender_name: str | None,
    ) -> GatewaySession:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, platform, chat_id, thread_id, sender_id, sender_name,
                       status, last_event_id, last_message_preview
                FROM gateway_sessions
                WHERE platform = ? AND chat_id = ? AND COALESCE(thread_id, '') = COALESCE(?, '')
                """,
                (platform, chat_id, thread_id),
            ).fetchone()
            if row:
                return self._session_from_row(row)
            now = self.now()
            session_id = self.new_id("gw")
            conn.execute(
                """
                INSERT INTO gateway_sessions (
                    session_id, platform, chat_id, thread_id, sender_id, sender_name,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, platform, chat_id, thread_id, sender_id, sender_name, now, now),
            )
        return self.get_or_create_session(
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            sender_id=sender_id,
            sender_name=sender_name,
        )

    def record_message(
        self,
        session_id: str,
        *,
        direction: str,
        platform: str,
        event_id: str | None,
        text: str,
        raw: dict[str, Any],
    ) -> GatewayMessage:
        if direction not in {"inbound", "outbound"}:
            raise ValueError("gateway message direction must be inbound or outbound")
        now = self.now()
        message_id = self.new_id("msg")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_messages (
                    message_id, session_id, direction, platform, event_id, text, raw_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, session_id, direction, platform, event_id, text, json.dumps(raw), now),
            )
            conn.execute(
                """
                UPDATE gateway_sessions
                SET updated_at = ?, last_event_id = COALESCE(?, last_event_id), last_message_preview = ?
                WHERE session_id = ?
                """,
                (now, event_id, text[:120], session_id),
            )
        return GatewayMessage(message_id, session_id, direction, platform, event_id, text, raw, now)

    def list_messages(self, session_id: str) -> list[GatewayMessage]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, session_id, direction, platform, event_id, text, raw_json, created_at
                FROM gateway_messages
                WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
        return [
            GatewayMessage(
                message_id=row[0],
                session_id=row[1],
                direction=row[2],
                platform=row[3],
                event_id=row[4],
                text=row[5],
                raw=json.loads(row[6]),
                created_at=row[7],
            )
            for row in rows
        ]

    def claim_event(self, platform: str, event_id: str) -> bool:
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO gateway_inbound_events (platform, event_id, claimed_at)
                    VALUES (?, ?, ?)
                    """,
                    (platform, event_id, self.now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_gateway_session_store.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add gateway/session_store.py tests/test_gateway_session_store.py
git commit -m "feat: add gateway session store"
```

---

### Task 3: Callback Server

**Files:**

- Create: `gateway/callback_server.py`
- Test: `tests/test_gateway_callback_server.py`

- [ ] **Step 1: Write failing callback server tests**

Create `tests/test_gateway_callback_server.py`:

```python
from __future__ import annotations

import json


class FakeAdapter:
    key = "fake"

    def parse_callback(self, headers, body):
        from gateway.contracts import InboundEvent, InboundParseResult

        if body.get("challenge"):
            return InboundParseResult(ok=True, response_body={"challenge": body["challenge"]}, status_code=200)
        return InboundParseResult(
            ok=True,
            event=InboundEvent(
                platform="fake",
                event_id=body["event_id"],
                event_type="message",
                chat_id=body["chat_id"],
                text=body["text"],
                timestamp="2026-06-03T00:00:00+00:00",
                raw=body,
            ),
        )


def test_callback_health_returns_platforms():
    from gateway.callback_server import CallbackApplication
    from gateway.registry import GatewayRegistry

    registry = GatewayRegistry()
    registry.register(FakeAdapter())
    app = CallbackApplication(registry=registry, dispatch=lambda event: None)

    response = app.handle("GET", "/health", {}, b"")

    assert response.status_code == 200
    assert response.body["ok"] is True
    assert response.body["platforms"] == ["fake"]


def test_callback_challenge_returns_adapter_body():
    from gateway.callback_server import CallbackApplication
    from gateway.registry import GatewayRegistry

    registry = GatewayRegistry()
    registry.register(FakeAdapter())
    app = CallbackApplication(registry=registry, dispatch=lambda event: None)

    response = app.handle("POST", "/callback/fake", {}, json.dumps({"challenge": "abc"}).encode())

    assert response.status_code == 200
    assert response.body == {"challenge": "abc"}


def test_callback_dispatches_inbound_event_once():
    from gateway.callback_server import CallbackApplication
    from gateway.registry import GatewayRegistry

    dispatched = []
    registry = GatewayRegistry()
    registry.register(FakeAdapter())
    app = CallbackApplication(registry=registry, dispatch=dispatched.append)

    body = json.dumps({"event_id": "evt-1", "chat_id": "chat-1", "text": "hello"}).encode()
    first = app.handle("POST", "/callback/fake", {}, body)
    second = app.handle("POST", "/callback/fake", {}, body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert [event.event_id for event in dispatched] == ["evt-1"]


def test_callback_unknown_platform_returns_404():
    from gateway.callback_server import CallbackApplication
    from gateway.registry import GatewayRegistry

    app = CallbackApplication(registry=GatewayRegistry(), dispatch=lambda event: None)

    response = app.handle("POST", "/callback/missing", {}, b"{}")

    assert response.status_code == 404
    assert "unsupported platform" in response.body["error"]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_gateway_callback_server.py -v
```

Expected: FAIL because `gateway.callback_server` does not exist.

- [ ] **Step 3: Implement callback application**

Create `gateway/callback_server.py` with:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from gateway.contracts import InboundEvent, InboundParseResult
from gateway.registry import GatewayRegistry


@dataclass(frozen=True)
class CallbackResponse:
    status_code: int
    body: dict[str, Any]


class CallbackApplication:
    def __init__(
        self,
        *,
        registry: GatewayRegistry,
        dispatch: Callable[[InboundEvent], None],
    ) -> None:
        self.registry = registry
        self.dispatch = dispatch
        self._seen_events: set[tuple[str, str]] = set()

    def handle(self, method: str, path: str, headers: dict[str, str], body_bytes: bytes) -> CallbackResponse:
        if method == "GET" and path == "/health":
            return CallbackResponse(200, {"ok": True, "platforms": self.registry.platform_keys()})
        if method != "POST" or not path.startswith("/callback/"):
            return CallbackResponse(404, {"error": "route not found"})

        platform = path.removeprefix("/callback/").strip("/")
        adapter = self.registry.get(platform)
        if adapter is None:
            return CallbackResponse(404, {"error": f"unsupported platform: {platform}"})
        if not hasattr(adapter, "parse_callback"):
            return CallbackResponse(400, {"error": f"platform does not support callbacks: {platform}"})

        try:
            body = json.loads(body_bytes.decode("utf-8") or "{}")
        except ValueError:
            return CallbackResponse(400, {"error": "callback body must be valid JSON"})
        if not isinstance(body, dict):
            return CallbackResponse(400, {"error": "callback body must be a JSON object"})

        result: InboundParseResult = adapter.parse_callback(headers, body)
        if not result.ok:
            return CallbackResponse(result.status_code, {"error": result.error or "callback rejected"})
        if result.response_body is not None:
            return CallbackResponse(result.status_code, result.response_body)
        if result.event is not None:
            key = (result.event.platform, result.event.event_id)
            if key not in self._seen_events:
                self._seen_events.add(key)
                self.dispatch(result.event)
        return CallbackResponse(result.status_code, {"ok": True})
```

Socket server can be added in Task 7 when CLI foreground serving is introduced.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_gateway_callback_server.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add gateway/callback_server.py tests/test_gateway_callback_server.py
git commit -m "feat: add gateway callback application"
```

---

### Task 4: Feishu Inbound Adapter

**Files:**

- Modify: `gateway/platforms/feishu.py`
- Test: `tests/test_gateway_feishu.py`

- [ ] **Step 1: Add failing Feishu inbound tests**

Append to `tests/test_gateway_feishu.py`:

```python
def test_feishu_callback_challenge_requires_matching_token(monkeypatch):
    monkeypatch.setenv("FEISHU_CALLBACK_TOKEN", "expected-token")
    adapter = FeishuPlatformAdapter(http_sender=FakeHttpSender([]))

    result = adapter.parse_callback(
        {},
        {"type": "url_verification", "token": "expected-token", "challenge": "challenge-value"},
    )

    assert result.ok is True
    assert result.status_code == 200
    assert result.response_body == {"challenge": "challenge-value"}


def test_feishu_callback_rejects_invalid_token(monkeypatch):
    monkeypatch.setenv("FEISHU_CALLBACK_TOKEN", "expected-token")
    adapter = FeishuPlatformAdapter(http_sender=FakeHttpSender([]))

    result = adapter.parse_callback(
        {},
        {"type": "url_verification", "token": "wrong-token", "challenge": "challenge-value"},
    )

    assert result.ok is False
    assert result.status_code == 403
    assert "token" in (result.error or "")


def test_feishu_text_message_event_normalizes_to_inbound_event(monkeypatch):
    monkeypatch.setenv("FEISHU_CALLBACK_TOKEN", "expected-token")
    adapter = FeishuPlatformAdapter(http_sender=FakeHttpSender([]))

    result = adapter.parse_callback(
        {},
        {
            "schema": "2.0",
            "header": {
                "event_id": "evt-1",
                "event_type": "im.message.receive_v1",
                "token": "expected-token",
                "create_time": "1780000000000",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou_1"},
                    "sender_type": "user",
                    "tenant_key": "tenant",
                },
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_123",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": "hello feishu"}),
                },
            },
        },
    )

    assert result.ok is True
    assert result.event is not None
    assert result.event.platform == "feishu"
    assert result.event.event_id == "evt-1"
    assert result.event.chat_id == "oc_123"
    assert result.event.sender_id == "ou_1"
    assert result.event.text == "hello feishu"
    assert result.response_body == {"ok": True}


def test_feishu_unsupported_message_type_is_acknowledged(monkeypatch):
    monkeypatch.setenv("FEISHU_CALLBACK_TOKEN", "expected-token")
    adapter = FeishuPlatformAdapter(http_sender=FakeHttpSender([]))

    result = adapter.parse_callback(
        {},
        {
            "schema": "2.0",
            "header": {"event_id": "evt-2", "event_type": "im.message.receive_v1", "token": "expected-token"},
            "event": {"message": {"chat_id": "oc_123", "message_type": "image", "content": "{}"}},
        },
    )

    assert result.ok is True
    assert result.event is None
    assert result.response_body == {"ok": True}
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_gateway_feishu.py::test_feishu_callback_challenge_requires_matching_token tests/test_gateway_feishu.py::test_feishu_text_message_event_normalizes_to_inbound_event -v
```

Expected: FAIL because `parse_callback` is not implemented.

- [ ] **Step 3: Implement Feishu callback parsing**

Modify `gateway/platforms/feishu.py`:

```python
from datetime import datetime, timezone
from gateway.contracts import InboundEvent, InboundParseResult
```

Add methods to `FeishuPlatformAdapter`:

```python
    def parse_callback(self, headers: dict[str, str], body: dict[str, Any]) -> InboundParseResult:
        token_result = self._validate_callback_token(body)
        if not token_result.ok:
            return token_result

        if body.get("type") == "url_verification":
            challenge = body.get("challenge")
            if not challenge:
                return InboundParseResult(False, status_code=400, error="feishu challenge is missing")
            return InboundParseResult(True, response_body={"challenge": str(challenge)}, status_code=200)

        header = body.get("header") if isinstance(body.get("header"), dict) else {}
        event = body.get("event") if isinstance(body.get("event"), dict) else {}
        event_type = str(header.get("event_type") or "")
        if event_type != "im.message.receive_v1":
            return InboundParseResult(True, response_body={"ok": True}, status_code=200)

        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        if message.get("message_type") != "text":
            return InboundParseResult(True, response_body={"ok": True}, status_code=200)

        chat_id = str(message.get("chat_id") or "")
        if not chat_id:
            return InboundParseResult(False, status_code=400, error="feishu message is missing chat_id")

        text = self._extract_text_content(message.get("content"))
        event_id = str(header.get("event_id") or message.get("message_id") or "")
        if not event_id:
            return InboundParseResult(False, status_code=400, error="feishu callback is missing event_id")

        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        inbound = InboundEvent(
            platform="feishu",
            event_id=event_id,
            event_type=event_type,
            chat_id=chat_id,
            thread_id=message.get("thread_id"),
            sender_id=sender_id.get("open_id") or sender_id.get("union_id") or sender_id.get("user_id"),
            sender_name=sender.get("sender_name"),
            text=text,
            timestamp=self._timestamp_from_header(header),
            raw=body,
        )
        return InboundParseResult(True, event=inbound, response_body={"ok": True}, status_code=200)

    def _validate_callback_token(self, body: dict[str, Any]) -> InboundParseResult:
        expected = os.environ.get("FEISHU_CALLBACK_TOKEN")
        if not expected:
            return InboundParseResult(False, status_code=403, error="missing FEISHU_CALLBACK_TOKEN")
        token = body.get("token")
        header = body.get("header") if isinstance(body.get("header"), dict) else {}
        if token is None:
            token = header.get("token")
        if token != expected:
            return InboundParseResult(False, status_code=403, error="invalid feishu callback token")
        return InboundParseResult(True)

    @staticmethod
    def _extract_text_content(content: Any) -> str:
        parsed = FeishuPlatformAdapter._parse_json(content or "{}") or {}
        return str(parsed.get("text") or "")

    @staticmethod
    def _timestamp_from_header(header: dict[str, Any]) -> str:
        raw = header.get("create_time")
        try:
            value = int(str(raw))
            if value > 10_000_000_000:
                value = value // 1000
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 4: Run Feishu tests**

Run:

```bash
pytest tests/test_gateway_feishu.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add gateway/platforms/feishu.py tests/test_gateway_feishu.py
git commit -m "feat: parse feishu inbound callbacks"
```

---

### Task 5: Gateway Dispatch And Service

**Files:**

- Create: `gateway/dispatch.py`
- Create: `gateway/service.py`
- Create: `gateway/service_state.py`
- Test: `tests/test_gateway_dispatch.py`
- Test: `tests/test_gateway_service.py`

- [ ] **Step 1: Write failing dispatch tests**

Create `tests/test_gateway_dispatch.py`:

```python
from __future__ import annotations

from gateway.contracts import InboundEvent, SendResult


class FakeAdapter:
    key = "feishu"

    def __init__(self):
        self.sent = []

    def send_text(self, target, message):
        self.sent.append((target, message))
        return SendResult(ok=True)


def test_gateway_dispatch_records_messages_and_sends_response(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    def fake_runner(event, session, origin):
        assert origin["source_type"] == "gateway"
        assert origin["platform"] == "feishu"
        assert origin["chat_id"] == "oc_123"
        return "agent response"

    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    dispatcher = GatewayDispatcher(store=store, registry=registry, runner=fake_runner)

    result = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-1",
            event_type="message",
            chat_id="oc_123",
            text="hello",
            timestamp="2026-06-03T00:00:00+00:00",
            raw={},
        )
    )

    assert result.ok is True
    assert adapter.sent[0][0].platform == "feishu"
    assert adapter.sent[0][0].target_id == "oc_123"
    assert adapter.sent[0][1].text == "agent response"
    messages = store.list_messages(result.session_id)
    assert [message.direction for message in messages] == ["inbound", "outbound"]


def test_gateway_dispatch_dedupes_event_before_runner(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    calls = []
    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    dispatcher = GatewayDispatcher(
        store=GatewaySessionStore(tmp_path / "gateway.sqlite"),
        registry=registry,
        runner=lambda event, session, origin: calls.append(event.event_id) or "ok",
    )
    event = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        raw={},
    )

    first = dispatcher.dispatch(event)
    second = dispatcher.dispatch(event)

    assert first.ok is True
    assert second.ok is True
    assert second.duplicate is True
    assert calls == ["evt-1"]
```

- [ ] **Step 2: Write failing service tests**

Create `tests/test_gateway_service.py`:

```python
from __future__ import annotations

from gateway.contracts import InboundEvent


def test_gateway_service_dispatches_event_and_writes_status(tmp_path):
    from gateway.service import GatewayService
    from gateway.service_state import read_gateway_status

    dispatched = []
    service = GatewayService(
        home=tmp_path,
        registry=None,
        dispatch=dispatched.append,
    )
    event = InboundEvent(
        platform="feishu",
        event_id="evt-1",
        event_type="message",
        chat_id="oc_123",
        text="hello",
        timestamp="2026-06-03T00:00:00+00:00",
        raw={},
    )

    service.handle_event(event)
    service.write_status(process_state="running")

    assert dispatched == [event]
    status = read_gateway_status(tmp_path)
    assert status["process_state"] == "running"
    assert status["service"] == "gateway"
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
pytest tests/test_gateway_dispatch.py tests/test_gateway_service.py -v
```

Expected: FAIL because dispatch and service modules do not exist.

- [ ] **Step 4: Implement dispatcher and service state**

Create `gateway/dispatch.py` with:

```python
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


class GatewayDispatcher:
    def __init__(self, *, store: GatewaySessionStore, registry: GatewayRegistry, runner: GatewayRunner) -> None:
        self.store = store
        self.registry = registry
        self.runner = runner

    def dispatch(self, event: InboundEvent) -> DispatchResult:
        session = self.store.get_or_create_session(
            platform=event.platform,
            chat_id=event.chat_id,
            thread_id=event.thread_id,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
        )
        if not self.store.claim_event(event.platform, event.event_id):
            return DispatchResult(True, session.session_id, duplicate=True)
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
                return DispatchResult(False, session.session_id, error=f"unsupported gateway platform: {event.platform}")
            target = PlatformMessageTarget(
                platform=event.platform,
                target_type="chat_id",
                target_id=event.chat_id,
                thread_id=event.thread_id,
            )
            send_result = adapter.send_text(target, OutboundMessage(text=response_text, metadata={"session_id": session.session_id}))
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
        return DispatchResult(True, session.session_id)
```

Create `gateway/service_state.py` with:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def gateway_state_dir(home: str | Path) -> Path:
    return Path(home) / "gateway"


def gateway_status_path(home: str | Path) -> Path:
    return gateway_state_dir(home) / "status.json"


def write_gateway_status(home: str | Path, status: dict[str, Any]) -> None:
    path = gateway_status_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"service": "gateway", "updated_at": datetime.now(timezone.utc).isoformat(), **status}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_gateway_status(home: str | Path) -> dict[str, Any] | None:
    path = gateway_status_path(home)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
```

Create `gateway/service.py` with:

```python
from __future__ import annotations

from pathlib import Path
from typing import Callable

from gateway.contracts import InboundEvent
from gateway.registry import GatewayRegistry, default_gateway_registry
from gateway.service_state import write_gateway_status


class GatewayService:
    def __init__(
        self,
        *,
        home: str | Path,
        registry: GatewayRegistry | None = None,
        dispatch: Callable[[InboundEvent], object] | None = None,
    ) -> None:
        self.home = Path(home)
        self.registry = registry or default_gateway_registry()
        self.dispatch = dispatch or (lambda event: None)

    def handle_event(self, event: InboundEvent) -> None:
        self.dispatch(event)

    def write_status(self, *, process_state: str) -> None:
        write_gateway_status(
            self.home,
            {
                "process_state": process_state,
                "platforms": self.registry.platform_keys(),
            },
        )
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_gateway_dispatch.py tests/test_gateway_service.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add gateway/dispatch.py gateway/service.py gateway/service_state.py tests/test_gateway_dispatch.py tests/test_gateway_service.py
git commit -m "feat: add gateway dispatch service"
```

---

### Task 6: Origin Delivery Through Gateway

**Files:**

- Modify: `cron/delivery_adapters.py`
- Test: `tests/test_cron_origin_gateway_delivery.py`

- [ ] **Step 1: Write failing origin delivery tests**

Create `tests/test_cron_origin_gateway_delivery.py`:

```python
from __future__ import annotations

import json


class FakeGatewayAdapter:
    key = "feishu"

    def __init__(self, *, ok=True, retryable=False, error=None):
        self.ok = ok
        self.retryable = retryable
        self.error = error
        self.sent = []

    def send_text(self, target, message):
        from gateway.contracts import SendResult

        self.sent.append((target, message))
        return SendResult(ok=self.ok, retryable=self.retryable, error=self.error)

    def validate_target(self, target):
        from gateway.contracts import SendResult

        return SendResult(ok=True)


def delivery_event():
    return {
        "id": "evt-1",
        "payload_json": json.dumps(
            {
                "job_id": "job-1",
                "job_name": "Daily",
                "run_id": "run-1",
                "status": "ok",
                "final_response": "done",
                "output_path": "/tmp/out.md",
            }
        ),
        "address": "oc_123",
        "thread_id": "thread-1",
    }


def test_origin_delivery_sends_gateway_origin(monkeypatch):
    import gateway.registry as gateway_registry
    from cron.delivery_adapters import OriginDeliveryAdapter

    fake = FakeGatewayAdapter()
    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: fake)
    try:
        adapter = OriginDeliveryAdapter()
        job = {
            "id": "job-1",
            "origin": {
                "source_type": "gateway",
                "platform": "feishu",
                "chat_id": "oc_123",
                "thread_id": "thread-1",
            },
        }

        result = adapter.deliver(delivery_event(), job, {"id": "run-1"})

        assert result.delivered is True
        target, message = fake.sent[0]
        assert target.platform == "feishu"
        assert target.target_type == "chat_id"
        assert target.target_id == "oc_123"
        assert target.thread_id == "thread-1"
        assert "Daily" in message.text
        assert "done" in message.text
    finally:
        gateway_registry.clear_gateway_adapter_factories()


def test_origin_delivery_maps_retryable_gateway_failure(monkeypatch):
    import gateway.registry as gateway_registry
    from cron.delivery_adapters import OriginDeliveryAdapter

    fake = FakeGatewayAdapter(ok=False, retryable=True, error="rate limit")
    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: fake)
    try:
        result = OriginDeliveryAdapter().deliver(
            delivery_event(),
            {
                "id": "job-1",
                "origin": {"source_type": "gateway", "platform": "feishu", "chat_id": "oc_123"},
            },
            {"id": "run-1"},
        )

        assert result.delivered is False
        assert result.retryable is True
        assert result.error == "rate limit"
    finally:
        gateway_registry.clear_gateway_adapter_factories()
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_cron_origin_gateway_delivery.py -v
```

Expected: FAIL because `OriginDeliveryAdapter.deliver()` still reports host bridge pickup.

- [ ] **Step 3: Implement origin delivery**

Modify `OriginDeliveryAdapter` in `cron/delivery_adapters.py`:

```python
class OriginDeliveryAdapter:
    key = "origin"
    active_dispatch = True

    def validate(self, target: Any, job: dict[str, Any]) -> AdapterValidation:
        origin = job.get("origin") or {}
        if origin.get("source_type") == "gateway" and origin.get("platform") and origin.get("chat_id"):
            return AdapterValidation(True)
        if not target.address:
            return AdapterValidation(False, "origin delivery requires session_id or chat_id")
        return AdapterValidation(True)

    def deliver(self, event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> DeliveryResult:
        origin = (job or {}).get("origin") or {}
        if origin.get("source_type") != "gateway":
            return DeliveryResult(False, retryable=False, error="origin delivery waits for origin poll or host bridge pickup")
        platform = str(origin.get("platform") or "")
        chat_id = str(origin.get("chat_id") or "")
        if not platform or not chat_id:
            return DeliveryResult(False, retryable=False, error="gateway origin delivery requires platform and chat_id")

        from gateway.contracts import OutboundMessage, PlatformMessageTarget
        from gateway.registry import default_gateway_registry

        gateway_adapter = default_gateway_registry().get(platform)
        if gateway_adapter is None:
            return DeliveryResult(False, retryable=False, error=f"unsupported gateway origin platform: {platform}")

        target = PlatformMessageTarget(
            platform=platform,
            target_type="chat_id",
            target_id=chat_id,
            thread_id=origin.get("thread_id"),
        )
        message = OutboundMessage(
            text=_format_origin_delivery_text(event, job, run),
            metadata={"event_id": event.get("id"), "job_id": (job or {}).get("id")},
        )
        result = gateway_adapter.send_text(target, message)
        return DeliveryResult(result.ok, retryable=result.retryable, error=result.error)
```

Add helper near existing delivery formatting helpers:

```python
def _format_origin_delivery_text(event: dict[str, Any], job: dict[str, Any] | None, run: dict[str, Any] | None) -> str:
    payload = json.loads(event.get("payload_json") or "{}")
    lines = [
        f"Cron job: {payload.get('job_name') or (job or {}).get('name') or (job or {}).get('id') or 'unknown'}",
        f"Job ID: {payload.get('job_id') or (job or {}).get('id') or 'unknown'}",
    ]
    run_id = payload.get("run_id") or (run or {}).get("id")
    if run_id:
        lines.append(f"Run ID: {run_id}")
    lines.append(f"Status: {payload.get('status') or 'unknown'}")
    if payload.get("final_response"):
        lines.extend(["", str(payload["final_response"])])
    if payload.get("error"):
        lines.extend(["", f"Error: {payload['error']}"])
    if payload.get("output_path"):
        lines.extend(["", f"Output: {payload['output_path']}"])
    return "\n".join(lines)
```

- [ ] **Step 4: Run origin delivery tests**

Run:

```bash
pytest tests/test_cron_origin_gateway_delivery.py tests/test_cron_delivery.py::test_failed_origin_result_enqueues_error_delivery -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add cron/delivery_adapters.py tests/test_cron_origin_gateway_delivery.py
git commit -m "feat: deliver gateway origins through adapters"
```

---

### Task 7: Gateway CLI, Status, And Doctor

**Files:**

- Create: `agent_cli/command_handlers/gateway.py`
- Modify: `agent_cli/commands.py`
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/doctor.py`
- Test: `tests/test_gateway_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_gateway_cli.py`:

```python
from __future__ import annotations


def test_gateway_status_reports_not_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.command_handlers.gateway import gateway_status

    exit_code = gateway_status(home=tmp_path)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Gateway service is not running" in output


def test_gateway_status_reports_running(tmp_path, capsys):
    from agent_cli.command_handlers.gateway import gateway_status
    from gateway.service_state import write_gateway_status

    write_gateway_status(tmp_path, {"process_state": "running", "platforms": ["feishu"]})

    exit_code = gateway_status(home=tmp_path)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Gateway service is running" in output
    assert "feishu" in output
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_gateway_cli.py -v
```

Expected: FAIL because gateway command handler does not exist.

- [ ] **Step 3: Implement status handler**

Create `agent_cli/command_handlers/gateway.py`:

```python
from __future__ import annotations

from pathlib import Path


def _default_home() -> Path:
    from cron.paths import cron_home

    return cron_home()


def gateway_status(*, home: str | Path | None = None) -> int:
    from gateway.service_state import read_gateway_status

    status = read_gateway_status(Path(home) if home is not None else _default_home())
    if not status or status.get("process_state") != "running":
        print("Gateway service is not running")
        return 1
    platforms = ", ".join(status.get("platforms") or []) or "-"
    print("Gateway service is running")
    print(f"Platforms: {platforms}")
    print(f"Updated: {status.get('updated_at')}")
    return 0


def gateway_serve(args) -> int:
    from gateway.service import GatewayService

    service = GatewayService(home=_default_home())
    service.write_status(process_state="running")
    print("Gateway service foreground mode is ready")
    return 0
```

Wire parser and dispatch following existing `agent_cli/command_handlers/cron.py` and `agent_cli/commands.py` patterns. Use subcommands:

```text
agent gateway status
agent gateway serve
```

The first `serve` implementation can write status and return for tests. Add blocking HTTP serving only after callback socket tests are added, so this task remains small.

- [ ] **Step 4: Add doctor checks**

Modify `agent_cli/doctor.py` only if cron delivery diagnostics are already centralized there. Add a function returning strings rather than printing directly:

```python
def check_feishu_gateway_config(env: dict[str, str] | None = None) -> list[str]:
    values = env or os.environ
    missing = [
        name
        for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CALLBACK_TOKEN")
        if not values.get(name)
    ]
    if not missing:
        return []
    return [f"missing Feishu gateway environment variables: {', '.join(missing)}"]
```

Add a focused test in `tests/test_gateway_cli.py`:

```python
def test_feishu_gateway_doctor_reports_missing_env():
    from agent_cli.doctor import check_feishu_gateway_config

    errors = check_feishu_gateway_config({})

    assert errors == [
        "missing Feishu gateway environment variables: FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CALLBACK_TOKEN"
    ]
```

- [ ] **Step 5: Run CLI tests**

Run:

```bash
pytest tests/test_gateway_cli.py tests/test_agent_cli_commands.py tests/test_agent_cli_main.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add agent_cli/command_handlers/gateway.py agent_cli/commands.py agent_cli/main.py agent_cli/doctor.py tests/test_gateway_cli.py
git commit -m "feat: add gateway cli status"
```

---

### Task 8: End-To-End Gateway Origin Flow

**Files:**

- Test: `tests/test_gateway_e2e.py`
- Modify: files from earlier tasks only if the E2E test exposes integration gaps.

- [ ] **Step 1: Write failing E2E test**

Create `tests/test_gateway_e2e.py`:

```python
from __future__ import annotations

import json


def test_fake_feishu_callback_to_origin_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_CALLBACK_TOKEN", "token")

    from cron.delivery import JobRunResult, enqueue_result, process_due
    from cron.delivery_store import DeliveryStore
    from gateway.callback_server import CallbackApplication
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore
    from gateway.platforms.feishu import FeishuPlatformAdapter

    sent = []

    class FakeFeishuAdapter(FeishuPlatformAdapter):
        def __init__(self):
            super().__init__(http_sender=lambda url, payload, headers=None: (200, "{}"))

        def send_text(self, target, message):
            from gateway.contracts import SendResult

            sent.append((target, message))
            return SendResult(ok=True)

    registry = GatewayRegistry()
    registry.register(FakeFeishuAdapter())
    origins = []

    def runner(event, session, origin):
        origins.append(origin)
        return "created cron job"

    dispatcher = GatewayDispatcher(
        store=GatewaySessionStore(tmp_path / "gateway.sqlite"),
        registry=registry,
        runner=runner,
    )
    app = CallbackApplication(registry=registry, dispatch=dispatcher.dispatch)
    body = {
        "schema": "2.0",
        "header": {
            "event_id": "evt-1",
            "event_type": "im.message.receive_v1",
            "token": "token",
            "create_time": "1780000000000",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_123",
                "message_type": "text",
                "content": json.dumps({"text": "run a cron report"}),
            },
        },
    }

    response = app.handle("POST", "/callback/feishu", {}, json.dumps(body).encode())

    assert response.status_code == 200
    assert origins[0]["platform"] == "feishu"
    assert sent[0][1].text == "created cron job"

    job = {
        "id": "job-1",
        "name": "Daily",
        "deliver": "origin",
        "origin": origins[0],
    }
    event = enqueue_result(
        job,
        JobRunResult(success=True, output_doc="# out", final_response="done"),
        "/tmp/out.md",
        "2026-06-03T00:00:00+00:00",
    )
    summary = process_due(limit=10)

    assert summary["delivered"] == 1
    assert DeliveryStore().get(event["id"])["status"] == "delivered"
    assert sent[-1][0].target_id == "oc_123"
    assert "done" in sent[-1][1].text
```

- [ ] **Step 2: Run E2E test to verify failure or pass**

Run:

```bash
pytest tests/test_gateway_e2e.py -v
```

Expected before integration fixes: FAIL if `process_due()` builds a fresh default gateway registry that does not include the fake adapter.

- [ ] **Step 3: Make registry injection stable for E2E**

If the test fails because origin delivery uses a separate default registry, register the fake adapter through the existing factory hook:

```python
import gateway.registry as gateway_registry

gateway_registry.clear_gateway_adapter_factories()
gateway_registry.register_gateway_adapter_factory(lambda **kwargs: fake_adapter)
try:
    summary = process_due(limit=10)
finally:
    gateway_registry.clear_gateway_adapter_factories()
```

If production code needs a fix, prefer adding an optional `gateway_registry` argument to `process_due()` and `DeliveryDispatcher` only if current dispatcher patterns already support dependency injection for adapters. Keep the default path unchanged.

- [ ] **Step 4: Run full gateway and cron focused tests**

Run:

```bash
pytest tests/test_gateway_core.py tests/test_gateway_feishu.py tests/test_gateway_session_store.py tests/test_gateway_callback_server.py tests/test_gateway_dispatch.py tests/test_gateway_service.py tests/test_cron_origin_gateway_delivery.py tests/test_gateway_e2e.py -v
```

Expected: PASS.

- [ ] **Step 5: Run broader cron delivery tests**

Run:

```bash
pytest tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_delivery_targets.py tests/test_cron_e2e.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add tests/test_gateway_e2e.py cron gateway agent_cli
git commit -m "test: cover gateway origin delivery e2e"
```

---

## Final Verification

- [ ] Run all gateway tests:

```bash
pytest tests/test_gateway_*.py -v
```

Expected: PASS.

- [ ] Run all cron tests:

```bash
pytest tests/test_cron_*.py -v
```

Expected: PASS.

- [ ] Run agent CLI tests touched by gateway command registration:

```bash
pytest tests/test_agent_cli_commands.py tests/test_agent_cli_main.py tests/test_agent_cli_doctor.py -v
```

Expected: PASS.

- [ ] Check worktree:

```bash
git status --short
```

Expected: only intentional files remain modified or untracked.

---

## Self-Review

Spec coverage:

- Independent cron and gateway services: Tasks 5 and 7.
- HTTP callback server: Task 3.
- Inbound event contracts: Task 1.
- Gateway session store and message history: Task 2.
- Feishu inbound event subscription callbacks: Task 4.
- Origin return path through cron delivery state machine: Task 6.
- CLI/status/doctor coverage: Task 7.
- E2E fake Feishu callback to origin delivery: Task 8.

Type consistency:

- `InboundEvent`, `InboundParseResult`, `PlatformMessageTarget`, `OutboundMessage`, and `SendResult` are introduced in Task 1 and reused consistently.
- `GatewaySessionStore.claim_event()` is defined in Task 2 and used by `GatewayDispatcher` in Task 5.
- `GatewayDispatcher.dispatch()` returns `DispatchResult`, used only by tests and service dispatch injection.

Scope check:

- This plan does not merge cron and gateway services.
- This plan does not implement encrypted Feishu callbacks, non-text Feishu messages, public tunnel setup, or extra platforms.

# Cron Scheduler Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `agent cron serve` as the only automatic cron scheduler entrypoint, with heartbeat/status JSON and explicit scheduler leader lease.

**Architecture:** Keep `cron.scheduler.tick()` as the execution engine. Add a process-level leader lease in the existing SQLite state database, a small foreground service loop that writes status JSON, and CLI/status integration. Remove REPL automatic ticker startup while leaving manual `cron tick` available for diagnostics.

**Tech Stack:** Python stdlib (`argparse`, `json`, `signal`, `socket`, `uuid`, `time`, `sqlite3` via existing `StateStore`), existing `cron.state_store.StateStore`, existing `cron.scheduler.tick`, pytest via `/home/miku/miniforge3/envs/langchain/bin/python -m pytest`.

---

## File Map

- Create `cron/service_state.py`: atomic read/write helpers for `<cron_home>/status.json`, plus freshness helpers for CLI rendering.
- Create `cron/leader.py`: scheduler leader lease API backed by `StateStore` methods.
- Create `cron/service.py`: foreground `CronService` loop and `serve()` entrypoint.
- Modify `cron/state_store.py`: add `scheduler_leases` schema and methods for acquire/renew/read/release.
- Modify `agent_cli/main.py`: add `agent cron serve` parser and dispatch.
- Modify `agent_cli/cron_commands.py`: add `serve_cron()` command wrapper; update `cron_status()`, `cron_doctor()`, and `run_tick()` to surface leader/service state.
- Modify `agent_cli/repl.py`: stop importing/starting/stopping cron lifecycle ticker.
- Modify `agent_cli/command_handlers/cron.py`: update `/cron status` help text; no `/cron serve` in chat.
- Test `tests/test_cron_leader.py`: lease acquire/renew/steal/release.
- Test `tests/test_cron_service_state.py`: status JSON atomic write/read and stale handling.
- Test `tests/test_cron_service.py`: `--once`, follower behavior, tick error recording, stop exit reason.
- Test `tests/test_agent_cli_cron_commands.py`: status/doctor/service command rendering.
- Test `tests/test_agent_cli_main.py`: parser and dispatch for `cron serve`.
- Test `tests/test_agent_cli_repl.py`: REPL no longer starts cron ticker.

## Task 1: SQLite Scheduler Leader Lease

**Files:**
- Modify: `cron/state_store.py`
- Create: `cron/leader.py`
- Test: `tests/test_cron_leader.py`

- [ ] **Step 1: Write failing leader lease tests**

Create `tests/test_cron_leader.py`:

```python
from __future__ import annotations


def test_leader_lease_acquire_renew_and_release(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.leader import SchedulerLeaderLease
    from cron.state_store import StateStore

    store = StateStore()
    lease = SchedulerLeaderLease(store=store, name="scheduler", lease_seconds=30)

    first = lease.try_acquire_or_renew(
        owner_id="host:1:first",
        pid=1,
        hostname="host",
        now_text="2026-05-29T10:00:00+00:00",
    )
    assert first.is_leader is True
    assert first.owner_id == "host:1:first"
    assert first.expires_at == "2026-05-29T10:00:30+00:00"

    renewed = lease.try_acquire_or_renew(
        owner_id="host:1:first",
        pid=1,
        hostname="host",
        now_text="2026-05-29T10:00:10+00:00",
    )
    assert renewed.is_leader is True
    assert renewed.owner_id == "host:1:first"
    assert renewed.expires_at == "2026-05-29T10:00:40+00:00"

    assert lease.release("host:2:other") is False
    assert lease.current().owner_id == "host:1:first"
    assert lease.release("host:1:first") is True
    assert lease.current() is None


def test_leader_lease_blocks_follower_until_expired(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.leader import SchedulerLeaderLease
    from cron.state_store import StateStore

    lease = SchedulerLeaderLease(store=StateStore(), name="scheduler", lease_seconds=30)

    lease.try_acquire_or_renew(
        owner_id="host:1:first",
        pid=1,
        hostname="host",
        now_text="2026-05-29T10:00:00+00:00",
    )
    follower = lease.try_acquire_or_renew(
        owner_id="host:2:second",
        pid=2,
        hostname="host",
        now_text="2026-05-29T10:00:10+00:00",
    )
    assert follower.is_leader is False
    assert follower.owner_id == "host:1:first"

    stolen = lease.try_acquire_or_renew(
        owner_id="host:2:second",
        pid=2,
        hostname="host",
        now_text="2026-05-29T10:00:31+00:00",
    )
    assert stolen.is_leader is True
    assert stolen.owner_id == "host:2:second"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_leader.py -q
```

Expected: FAIL because `cron.leader` does not exist.

- [ ] **Step 3: Add scheduler lease schema and StateStore methods**

In `cron/state_store.py`, add `scheduler_leases` creation next to the existing `jobs`, `runs`, and `delivery_events` tables:

```python
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS scheduler_leases (
        name TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        pid INTEGER,
        hostname TEXT,
        acquired_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """
)
```

Add methods near the run lease methods:

```python
def get_scheduler_lease(self, name: str = "scheduler") -> dict[str, Any] | None:
    with self._connect() as conn:
        row = conn.execute(
            "SELECT * FROM scheduler_leases WHERE name = ?",
            (name,),
        ).fetchone()
    return None if row is None else dict(row)

def try_acquire_scheduler_lease(
    self,
    *,
    name: str,
    owner_id: str,
    pid: int,
    hostname: str,
    now_text: str,
    expires_at: str,
) -> dict[str, Any]:
    with self._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM scheduler_leases WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO scheduler_leases (
                    name, owner_id, pid, hostname, acquired_at, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, owner_id, pid, hostname, now_text, now_text, expires_at),
            )
        else:
            current = dict(row)
            current_expires = _parse_time(current["expires_at"])
            now_dt = _parse_time(now_text)
            can_take = (
                current["owner_id"] == owner_id
                or current_expires is None
                or now_dt is None
                or current_expires <= now_dt
            )
            if can_take:
                acquired_at = current["acquired_at"] if current["owner_id"] == owner_id else now_text
                conn.execute(
                    """
                    UPDATE scheduler_leases
                    SET owner_id = ?, pid = ?, hostname = ?, acquired_at = ?,
                        heartbeat_at = ?, expires_at = ?
                    WHERE name = ?
                    """,
                    (owner_id, pid, hostname, acquired_at, now_text, expires_at, name),
                )
        updated = conn.execute(
            "SELECT * FROM scheduler_leases WHERE name = ?",
            (name,),
        ).fetchone()
    return dict(updated)

def release_scheduler_lease(self, *, name: str, owner_id: str) -> bool:
    with self._connect() as conn:
        cursor = conn.execute(
            "DELETE FROM scheduler_leases WHERE name = ? AND owner_id = ?",
            (name, owner_id),
        )
        return bool(cursor.rowcount)
```

- [ ] **Step 4: Add leader wrapper**

Create `cron/leader.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from cron.state_store import StateStore


@dataclass(frozen=True)
class LeaderLeaseState:
    is_leader: bool
    name: str
    owner_id: str | None
    pid: int | None
    hostname: str | None
    acquired_at: str | None
    heartbeat_at: str | None
    expires_at: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any] | None, *, requester: str | None = None) -> "LeaderLeaseState | None":
        if row is None:
            return None
        owner_id = str(row["owner_id"])
        return cls(
            is_leader=bool(requester is not None and owner_id == requester),
            name=str(row["name"]),
            owner_id=owner_id,
            pid=row.get("pid"),
            hostname=row.get("hostname"),
            acquired_at=row.get("acquired_at"),
            heartbeat_at=row.get("heartbeat_at"),
            expires_at=row.get("expires_at"),
        )


class SchedulerLeaderLease:
    def __init__(self, *, store: StateStore | None = None, name: str = "scheduler", lease_seconds: int = 180) -> None:
        self.store = store or StateStore()
        self.name = name
        self.lease_seconds = max(1, int(lease_seconds))

    def try_acquire_or_renew(
        self,
        *,
        owner_id: str,
        pid: int,
        hostname: str,
        now_text: str,
    ) -> LeaderLeaseState:
        now_dt = datetime.fromisoformat(now_text)
        expires_at = (now_dt + timedelta(seconds=self.lease_seconds)).isoformat()
        row = self.store.try_acquire_scheduler_lease(
            name=self.name,
            owner_id=owner_id,
            pid=pid,
            hostname=hostname,
            now_text=now_text,
            expires_at=expires_at,
        )
        state = LeaderLeaseState.from_row(row, requester=owner_id)
        assert state is not None
        return state

    def current(self) -> LeaderLeaseState | None:
        return LeaderLeaseState.from_row(self.store.get_scheduler_lease(self.name))

    def release(self, owner_id: str) -> bool:
        return self.store.release_scheduler_lease(name=self.name, owner_id=owner_id)
```

- [ ] **Step 5: Run leader tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_leader.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add cron/state_store.py cron/leader.py tests/test_cron_leader.py
git commit -m "feat: add cron scheduler leader lease"
```

## Task 2: Service Status JSON Helpers

**Files:**
- Create: `cron/service_state.py`
- Test: `tests/test_cron_service_state.py`

- [ ] **Step 1: Write failing status state tests**

Create `tests/test_cron_service_state.py`:

```python
from __future__ import annotations


def test_service_status_round_trips_json(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import read_service_status, service_status_path, write_service_status

    payload = {
        "version": 1,
        "service": "agent-cron",
        "owner_id": "host:1:abc",
        "pid": 1,
        "hostname": "host",
        "process_state": "running",
        "leader_state": "leader",
        "last_heartbeat_at": "2026-05-29T10:00:00+00:00",
        "last_error": None,
        "exit_reason": None,
    }

    path = service_status_path()
    assert path.name == "status.json"
    assert read_service_status() is None

    write_service_status(payload)

    assert read_service_status() == payload
    assert not path.with_suffix(".json.tmp").exists()


def test_service_status_freshness(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import is_status_fresh

    status = {"last_heartbeat_at": "2026-05-29T10:00:00+00:00"}

    assert is_status_fresh(status, now_text="2026-05-29T10:01:00+00:00", stale_after_seconds=120) is True
    assert is_status_fresh(status, now_text="2026-05-29T10:03:00+00:00", stale_after_seconds=120) is False
    assert is_status_fresh({}, now_text="2026-05-29T10:01:00+00:00", stale_after_seconds=120) is False
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_state.py -q
```

Expected: FAIL because `cron.service_state` does not exist.

- [ ] **Step 3: Implement status helpers**

Create `cron/service_state.py`:

```python
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from cron.paths import ensure_cron_dirs, get_cron_dir


def service_status_path() -> Path:
    ensure_cron_dirs()
    return get_cron_dir() / "status.json"


def write_service_status(status: dict[str, Any], *, path: Path | None = None) -> None:
    target = path or service_status_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def read_service_status(*, path: Path | None = None) -> dict[str, Any] | None:
    target = path or service_status_path()
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_status_fresh(status: dict[str, Any] | None, *, now_text: str, stale_after_seconds: int) -> bool:
    if not status:
        return False
    heartbeat = status.get("last_heartbeat_at")
    if not heartbeat:
        return False
    try:
        heartbeat_dt = datetime.fromisoformat(str(heartbeat))
        now_dt = datetime.fromisoformat(str(now_text))
    except ValueError:
        return False
    return (now_dt - heartbeat_dt).total_seconds() <= max(1, int(stale_after_seconds))
```

- [ ] **Step 4: Run status tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_state.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/service_state.py tests/test_cron_service_state.py
git commit -m "feat: add cron service status state"
```

## Task 3: Foreground Cron Service Loop

**Files:**
- Create: `cron/service.py`
- Test: `tests/test_cron_service.py`

- [ ] **Step 1: Write failing service tests**

Create `tests/test_cron_service.py`:

```python
from __future__ import annotations

from types import SimpleNamespace


def test_service_run_once_ticks_when_leader(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service import CronService
    from cron.service_state import read_service_status

    calls = []

    def fake_tick():
        calls.append("tick")
        return SimpleNamespace(due=1, ran=1, succeeded=1, failed=0, skipped=0)

    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:1:test",
        pid=1,
        hostname="host",
        tick_fn=fake_tick,
        clock=lambda: "2026-05-29T10:00:00+00:00",
        sleeper=lambda seconds: None,
    )

    assert service.run(once=True) == 0
    assert calls == ["tick"]
    status = read_service_status()
    assert status["process_state"] == "exited"
    assert status["leader_state"] == "leader"
    assert status["last_tick"]["ran"] == 1
    assert status["exit_reason"] == "once"


def test_service_run_once_skips_tick_when_follower(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.leader import SchedulerLeaderLease
    from cron.service import CronService
    from cron.service_state import read_service_status
    from cron.state_store import StateStore

    lease = SchedulerLeaderLease(store=StateStore(), lease_seconds=30)
    lease.try_acquire_or_renew(
        owner_id="host:1:other",
        pid=1,
        hostname="host",
        now_text="2026-05-29T10:00:00+00:00",
    )

    calls = []
    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:2:test",
        pid=2,
        hostname="host",
        tick_fn=lambda: calls.append("tick"),
        clock=lambda: "2026-05-29T10:00:10+00:00",
        sleeper=lambda seconds: None,
    )

    assert service.run(once=True) == 0
    assert calls == []
    status = read_service_status()
    assert status["leader_state"] == "follower"
    assert status["lease_owner"] == "host:1:other"


def test_service_records_tick_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service import CronService
    from cron.service_state import read_service_status

    def bad_tick():
        raise RuntimeError("tick exploded")

    service = CronService(
        interval_seconds=1,
        lease_seconds=30,
        owner_id="host:1:test",
        pid=1,
        hostname="host",
        tick_fn=bad_tick,
        clock=lambda: "2026-05-29T10:00:00+00:00",
        sleeper=lambda seconds: None,
    )

    assert service.run(once=True) == 1
    status = read_service_status()
    assert status["process_state"] == "exited"
    assert "tick exploded" in status["last_error"]
    assert status["exit_reason"] == "once"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service.py -q
```

Expected: FAIL because `cron.service` does not exist.

- [ ] **Step 3: Implement service loop**

Create `cron/service.py`:

```python
from __future__ import annotations

import os
import signal
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import Any

from cron.leader import SchedulerLeaderLease
from cron.service_state import write_service_status


def _now_text() -> str:
    return datetime.now().astimezone().isoformat()


def _default_owner_id(pid: int, hostname: str) -> str:
    return f"{hostname}:{pid}:{uuid.uuid4().hex}"


def _tick_summary(result: Any) -> dict[str, int]:
    return {
        "due": int(getattr(result, "due", 0) or 0),
        "ran": int(getattr(result, "ran", 0) or 0),
        "succeeded": int(getattr(result, "succeeded", 0) or 0),
        "failed": int(getattr(result, "failed", 0) or 0),
        "skipped": int(getattr(result, "skipped", 0) or 0),
    }


class CronService:
    def __init__(
        self,
        *,
        interval_seconds: float = 60,
        lease_seconds: int = 180,
        owner_id: str | None = None,
        pid: int | None = None,
        hostname: str | None = None,
        tick_fn: Callable[[], Any] | None = None,
        clock: Callable[[], str] = _now_text,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.lease_seconds = max(1, int(lease_seconds))
        self.pid = int(pid if pid is not None else os.getpid())
        self.hostname = hostname or socket.gethostname()
        self.owner_id = owner_id or _default_owner_id(self.pid, self.hostname)
        self.tick_fn = tick_fn or self._default_tick
        self.clock = clock
        self.sleeper = sleeper
        self.lease = SchedulerLeaderLease(lease_seconds=self.lease_seconds)
        self.stop_requested = False
        self.started_at = self.clock()
        self.status: dict[str, Any] = {
            "version": 1,
            "service": "agent-cron",
            "owner_id": self.owner_id,
            "pid": self.pid,
            "hostname": self.hostname,
            "process_state": "starting",
            "leader_state": "none",
            "lease_owner": None,
            "lease_expires_at": None,
            "started_at": self.started_at,
            "last_heartbeat_at": None,
            "last_tick_started_at": None,
            "last_tick_finished_at": None,
            "last_tick": None,
            "last_error": None,
            "exit_reason": None,
        }

    def _default_tick(self) -> Any:
        from cron.scheduler import tick

        return tick()

    def request_stop(self, reason: str = "signal") -> None:
        self.stop_requested = True
        self.status["process_state"] = "stopping"
        self.status["exit_reason"] = reason
        self._write_status()

    def install_signal_handlers(self) -> None:
        def handle(signum, frame):
            self.request_stop(f"signal:{signum}")

        signal.signal(signal.SIGINT, handle)
        signal.signal(signal.SIGTERM, handle)

    def run(self, *, once: bool = False) -> int:
        exit_code = 0
        self.status["process_state"] = "running"
        self._write_status()
        try:
            while not self.stop_requested:
                tick_error = self._run_one_loop()
                if tick_error:
                    exit_code = 1
                if once:
                    self.status["exit_reason"] = "once"
                    break
                self.sleeper(self.interval_seconds)
        finally:
            self.lease.release(self.owner_id)
            self.status["process_state"] = "exited"
            self.status.setdefault("exit_reason", "stopped")
            if self.status["exit_reason"] is None:
                self.status["exit_reason"] = "stopped"
            self._write_status()
        return exit_code

    def _run_one_loop(self) -> bool:
        now_text = self.clock()
        self.status["last_heartbeat_at"] = now_text
        state = self.lease.try_acquire_or_renew(
            owner_id=self.owner_id,
            pid=self.pid,
            hostname=self.hostname,
            now_text=now_text,
        )
        self.status["lease_owner"] = state.owner_id
        self.status["lease_expires_at"] = state.expires_at
        if not state.is_leader:
            self.status["leader_state"] = "follower"
            self._write_status()
            return False

        self.status["leader_state"] = "leader"
        self.status["last_tick_started_at"] = now_text
        self._write_status()
        try:
            result = self.tick_fn()
        except Exception as exc:
            self.status["last_error"] = str(exc)
            self.status["last_tick_finished_at"] = self.clock()
            self._write_status()
            return True
        self.status["last_error"] = None
        self.status["last_tick"] = _tick_summary(result)
        self.status["last_tick_finished_at"] = self.clock()
        self._write_status()
        return False

    def _write_status(self) -> None:
        write_service_status(dict(self.status))


def serve(*, interval_seconds: float = 60, lease_seconds: int = 180, once: bool = False) -> int:
    service = CronService(interval_seconds=interval_seconds, lease_seconds=lease_seconds)
    service.install_signal_handlers()
    return service.run(once=once)
```

- [ ] **Step 4: Run service tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/service.py tests/test_cron_service.py
git commit -m "feat: add cron scheduler service loop"
```

## Task 4: CLI `agent cron serve` and Service-Aware Status

**Files:**
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/cron_commands.py`
- Modify: `agent_cli/command_handlers/cron.py`
- Test: `tests/test_agent_cli_main.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing CLI tests**

Append to `tests/test_agent_cli_main.py`:

```python
def test_main_cron_serve_dispatches_service(monkeypatch, tmp_path, capsys):
    import agent_cli.main as main_module
    from agent_cli.cron_commands import CronCommandResult

    calls = []

    monkeypatch.setattr(
        main_module.cron_commands,
        "serve_cron",
        lambda interval_seconds=60, lease_seconds=180, once=False: calls.append(
            (interval_seconds, lease_seconds, once)
        ) or CronCommandResult("serve exited", exit_code=0),
    )

    code = main_module.main(["cron", "serve", "--interval", "5", "--lease-seconds", "20", "--once"])

    assert code == 0
    assert calls == [(5.0, 20, True)]
    assert "serve exited" in capsys.readouterr().out
```

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_serve_cron_calls_service(monkeypatch):
    from agent_cli import cron_commands

    calls = []

    monkeypatch.setattr(
        "cron.service.serve",
        lambda interval_seconds=60, lease_seconds=180, once=False: calls.append(
            (interval_seconds, lease_seconds, once)
        ) or 0,
    )

    result = cron_commands.serve_cron(interval_seconds=5, lease_seconds=20, once=True)

    assert result.exit_code == 0
    assert result.text == "Cron service exited."
    assert calls == [(5, 20, True)]


def test_cron_status_includes_service_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.service_state import write_service_status

    write_service_status(
        {
            "version": 1,
            "service": "agent-cron",
            "owner_id": "host:1:abc",
            "pid": 1,
            "hostname": "host",
            "process_state": "running",
            "leader_state": "leader",
            "lease_owner": "host:1:abc",
            "lease_expires_at": "2026-05-29T10:03:00+00:00",
            "started_at": "2026-05-29T10:00:00+00:00",
            "last_heartbeat_at": "2026-05-29T10:01:00+00:00",
            "last_tick_started_at": "2026-05-29T10:01:00+00:00",
            "last_tick_finished_at": "2026-05-29T10:01:01+00:00",
            "last_tick": {"due": 1, "ran": 1, "succeeded": 1, "failed": 0, "skipped": 0},
            "last_error": None,
            "exit_reason": None,
        }
    )
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [])

    result = cron_commands.cron_status()

    assert "Scheduler service: running" in result.text
    assert "Leader state: leader" in result.text
    assert "Last tick: due=1 ran=1 succeeded=1 failed=0 skipped=0" in result.text
```

- [ ] **Step 2: Run CLI tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_main_cron_serve_dispatches_service tests/test_agent_cli_cron_commands.py::test_serve_cron_calls_service tests/test_agent_cli_cron_commands.py::test_cron_status_includes_service_state -q
```

Expected: FAIL because `serve` parser/command/status rendering is not implemented.

- [ ] **Step 3: Add parser and dispatch**

In `agent_cli/main.py`, add after the existing `tick` parser:

```python
cron_serve = cron_subparsers.add_parser("serve", parents=[public_options])
cron_serve.add_argument("--interval", type=float, default=60.0)
cron_serve.add_argument("--lease-seconds", type=int, default=180)
cron_serve.add_argument("--once", action="store_true")
```

In `_run_cron_command()`, add before `doctor`:

```python
if subcommand == "serve":
    return cron_commands.serve_cron(
        interval_seconds=getattr(args, "interval", 60.0),
        lease_seconds=getattr(args, "lease_seconds", 180),
        once=bool(getattr(args, "once", False)),
    )
```

- [ ] **Step 4: Add command wrapper and status lines**

In `agent_cli/cron_commands.py`, add:

```python
def serve_cron(*, interval_seconds: float = 60, lease_seconds: int = 180, once: bool = False) -> CronCommandResult:
    from cron.service import serve

    exit_code = serve(
        interval_seconds=interval_seconds,
        lease_seconds=lease_seconds,
        once=once,
    )
    return CronCommandResult("Cron service exited.", exit_code=exit_code)
```

Add helper:

```python
def _service_status_lines() -> list[str]:
    from cron.leader import SchedulerLeaderLease
    from cron.service_state import read_service_status, service_status_path

    status = read_service_status()
    lease = SchedulerLeaderLease().current()
    lines = [f"Service status: {service_status_path()}"]
    if not status:
        lines.append("Scheduler service: unknown")
    else:
        lines.append(f"Scheduler service: {status.get('process_state') or 'unknown'}")
        lines.append(f"Service PID: {status.get('pid') or '-'}")
        lines.append(f"Leader state: {status.get('leader_state') or 'unknown'}")
        lines.append(f"Last heartbeat: {status.get('last_heartbeat_at') or '-'}")
        if status.get("last_tick"):
            tick = status["last_tick"]
            lines.append(
                "Last tick: "
                f"due={tick.get('due', 0)} ran={tick.get('ran', 0)} "
                f"succeeded={tick.get('succeeded', 0)} failed={tick.get('failed', 0)} "
                f"skipped={tick.get('skipped', 0)}"
            )
        if status.get("last_error"):
            lines.append(f"Last service error: {status['last_error']}")
        if status.get("exit_reason"):
            lines.append(f"Exit reason: {status['exit_reason']}")
    if lease is not None:
        lines.append(f"Lease owner: {lease.owner_id or '-'}")
        lines.append(f"Lease expires: {lease.expires_at or '-'}")
    return lines
```

Replace the old `Scheduler: running/stopped` line in `cron_status()` with `lines.extend(_service_status_lines())`. Keep existing cron home, sqlite, runner mode, job counts, delivery stats, and adapter lines.

- [ ] **Step 5: Update chat help**

In `agent_cli/command_handlers/cron.py`, change the status help around `/cron tick` to make automatic service explicit:

```python
"  /cron status",
"  /cron tick  # manual diagnostic; automatic scheduling uses `agent cron serve`",
```

- [ ] **Step 6: Run CLI tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_main_cron_serve_dispatches_service tests/test_agent_cli_cron_commands.py::test_serve_cron_calls_service tests/test_agent_cli_cron_commands.py::test_cron_status_includes_service_state tests/test_agent_cli_cron_commands.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_cli/main.py agent_cli/cron_commands.py agent_cli/command_handlers/cron.py tests/test_agent_cli_main.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: add cron serve command"
```

## Task 5: Disable REPL Automatic Cron Ticker

**Files:**
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_repl.py`
- Optional Modify: `agent_core/cron_lifecycle.py`
- Optional Test: `tests/test_cron_lifecycle.py`

- [ ] **Step 1: Write failing REPL test**

Find the existing REPL construction tests in `tests/test_agent_cli_repl.py` and add a focused test near them:

```python
def test_repl_does_not_start_cron_scheduler(monkeypatch, tmp_path):
    import agent_cli.repl as repl_module

    calls = []
    monkeypatch.setattr(repl_module, "start_cron_scheduler", lambda *args, **kwargs: calls.append((args, kwargs)))

    cli = repl_module.AgentCLI(
        session_store=FakeStore(),
        checkpointer=object(),
        agent_factory=lambda config: object(),
        runner=lambda agent, messages, config: {"messages": []},
        workdir=str(tmp_path),
        model_name=None,
        cron_enabled=True,
    )

    assert cli is not None
    assert calls == []
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_repl_does_not_start_cron_scheduler -q
```

Expected: FAIL because the REPL still starts the lifecycle ticker when `cron_enabled=True`.

- [ ] **Step 3: Remove REPL ticker startup/shutdown**

In `agent_cli/repl.py`:

- Remove `start_cron_scheduler` and `stop_cron_scheduler` import.
- Remove constructor or run-loop calls that start the cron scheduler.
- Remove shutdown calls that stop the cron scheduler.
- Keep `cron_enabled` and `cron_interval_seconds` constructor arguments if removing them causes broad public API churn; they can become ignored compatibility parameters for this phase.

The important end state is that no production path in `agent_cli/repl.py` calls `agent_core.cron_lifecycle.start_cron_scheduler()`.

- [ ] **Step 4: Run REPL and lifecycle tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py tests/test_cron_lifecycle.py -q
```

Expected: PASS after updating any tests that asserted REPL-owned ticker behavior. Keep `tests/test_cron_lifecycle.py` for direct compatibility behavior unless `agent_core/cron_lifecycle.py` is removed.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/repl.py tests/test_agent_cli_repl.py tests/test_cron_lifecycle.py
git commit -m "refactor: stop starting cron from repl"
```

## Task 6: Doctor, Manual Tick Guard, and Full Verification

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`
- Test: `tests/test_cron_scheduler.py`

- [ ] **Step 1: Add doctor stale-service tests**

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_doctor_warns_when_service_status_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands

    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])

    result = cron_commands.cron_doctor()

    assert "cron service heartbeat: missing" in result.text
    assert result.exit_code in {1, 2}


def test_cron_doctor_reports_fresh_service_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli import cron_commands
    from cron.service_state import write_service_status

    write_service_status(
        {
            "version": 1,
            "service": "agent-cron",
            "process_state": "running",
            "leader_state": "leader",
            "last_heartbeat_at": "2026-05-29T10:00:00+00:00",
        }
    )
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr("cron.jobs.now", lambda: __import__("datetime").datetime.fromisoformat("2026-05-29T10:01:00+00:00"))

    result = cron_commands.cron_doctor()

    assert "cron service heartbeat: fresh" in result.text
```

- [ ] **Step 2: Run doctor tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_when_service_status_missing tests/test_agent_cli_cron_commands.py::test_cron_doctor_reports_fresh_service_heartbeat -q
```

Expected: FAIL because doctor does not check service heartbeat.

- [ ] **Step 3: Implement doctor heartbeat checks**

In `agent_cli/cron_commands.py`, inside `cron_doctor()`, replace the old `is_cron_scheduler_running()` check with service status checks:

```python
try:
    from cron.jobs import now as cron_now
    from cron.service_state import is_status_fresh, read_service_status

    status = read_service_status()
    now_text = cron_now().isoformat()
    if not status:
        add("warn", "cron service heartbeat: missing; start automatic scheduling with `agent cron serve`")
    elif is_status_fresh(status, now_text=now_text, stale_after_seconds=180):
        add("ok", f"cron service heartbeat: fresh ({status.get('leader_state') or 'unknown'})")
    else:
        add("warn", "cron service heartbeat: stale; automatic scheduling may be stopped")
except Exception as exc:
    add("warn", f"cron service heartbeat: unreadable ({exc})")
```

- [ ] **Step 4: Decide and implement manual tick guard**

For this phase, keep `run_tick()` simple and rely on the existing `_TickLock` to prevent overlapping execution. Add one line to its output when a service lease exists so operators understand possible contention:

```python
from cron.leader import SchedulerLeaderLease

lease = SchedulerLeaderLease().current()
if lease is not None:
    lines.append(f"Scheduler lease owner: {lease.owner_id or '-'} expires={lease.expires_at or '-'}")
```

Do not fail manual tick solely because a lease exists; the service loop and `scheduler.tick()` lock remain authoritative for actual execution.

- [ ] **Step 5: Run targeted tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py tests/test_cron_leader.py tests/test_cron_service_state.py tests/test_cron_service.py -q
```

Expected: PASS.

- [ ] **Step 6: Run scheduler/lifecycle regression tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_scheduler.py tests/test_cron_state_store.py tests/test_cron_lifecycle.py tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 7: Run import health tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_import_health.py -q
```

Expected: PASS. This checks that lightweight cron imports still avoid pulling the heavy runner stack too early.

- [ ] **Step 8: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py tests/test_cron_scheduler.py
git commit -m "feat: report cron service health"
```

## Task 7: Final Verification

**Files:**
- No planned source changes.

- [ ] **Step 1: Run full cron-related tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_*.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 2: Run focused command smoke tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m agent_cli.main cron serve --once --interval 1 --lease-seconds 5
/home/miku/miniforge3/envs/langchain/bin/python -m agent_cli.main cron status
```

Expected: first command exits 0 and writes status JSON; second command prints scheduler service state, leader state, last heartbeat, and last tick summary.

- [ ] **Step 3: Inspect worktree**

Run:

```bash
git status --short
git log --oneline -5
```

Expected: only intentional tracked changes are present, and task commits are visible.

## Self-Review

Spec coverage:

- `agent cron serve` entrypoint: Task 4.
- REPL no longer starts ticker: Task 5.
- Heartbeat/status JSON: Task 2 and Task 3.
- `last_tick`, `last_error`, process state, leader state, exit reason: Task 3 and Task 4.
- Explicit leader lease: Task 1.
- Status/doctor management experience: Task 4 and Task 6.
- Manual `cron tick` remains diagnostic and safe: Task 6.

Scope check:

- The plan does not implement system service installation, missed-run policy, worker queues, timeout schema, origin delivery changes, or concurrency policy changes.

Type consistency:

- `SchedulerLeaderLease.try_acquire_or_renew()` returns `LeaderLeaseState`.
- `cron.service_state.write_service_status()` and `read_service_status()` operate on plain `dict[str, Any]`.
- `CronService.run(once=True)` returns an integer exit code for `serve_cron()`.

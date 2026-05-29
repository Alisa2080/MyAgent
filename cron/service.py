from __future__ import annotations

import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
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
        interval_seconds: int = 60,
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
        self.pid = os.getpid() if pid is None else pid
        self.hostname = hostname or socket.gethostname()
        self.owner_id = owner_id or _default_owner_id(self.pid, self.hostname)
        self.tick_fn = tick_fn or self._default_tick
        self.clock = clock
        self.sleeper = sleeper
        self.lease = SchedulerLeaderLease(lease_seconds=self.lease_seconds)
        self._stop_requested = False
        self._stop_event = threading.Event()

        started_at = self.clock()
        self.status: dict[str, Any] = {
            "version": 1,
            "service": "agent-cron",
            "owner_id": self.owner_id,
            "pid": self.pid,
            "hostname": self.hostname,
            "process_state": "initialized",
            "leader_state": "unknown",
            "lease_owner": None,
            "lease_expires_at": None,
            "started_at": started_at,
            "last_heartbeat_at": None,
            "last_tick_started_at": None,
            "last_tick_finished_at": None,
            "last_tick": None,
            "last_error": None,
            "exit_reason": None,
        }

    def install_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: object) -> None:
            self.request_stop(f"signal:{signum}")

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

    def request_stop(self, reason: str = "signal") -> None:
        self._stop_requested = True
        self._stop_event.set()
        self.status["process_state"] = "stopping"
        self.status["exit_reason"] = reason

    def _write_status(self) -> None:
        write_service_status(self.status)

    def run(self, once: bool = False) -> int:
        had_error = False
        self.status["process_state"] = "running"

        try:
            try:
                self._write_status()
                while not self._stop_requested:
                    had_error = self._run_one_loop() or had_error
                    if once:
                        break
                    if not self._stop_requested:
                        self._wait_interval()
            except Exception as exc:
                had_error = True
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"
                if self.status["exit_reason"] is None:
                    self.status["exit_reason"] = "error"
        finally:
            try:
                self.lease.release(self.owner_id)
            except Exception as exc:  # pragma: no cover - best-effort shutdown
                if not self.status["last_error"]:
                    self.status["last_error"] = f"{type(exc).__name__}: {exc}"

            self.status["process_state"] = "exited"
            if self.status["exit_reason"] is None:
                self.status["exit_reason"] = "once" if once else "stopped"
            try:
                self._write_status()
            except Exception as exc:
                had_error = True
                if not self.status["last_error"]:
                    self.status["last_error"] = f"{type(exc).__name__}: {exc}"
                if self.status["exit_reason"] is None:
                    self.status["exit_reason"] = "error"

        return 1 if had_error else 0

    def _wait_interval(self) -> None:
        if self.sleeper is time.sleep:
            self._stop_event.wait(self.interval_seconds)
        else:
            self.sleeper(self.interval_seconds)

    def _run_one_loop(self) -> bool:
        now_text = self.clock()
        self.status["last_heartbeat_at"] = now_text

        lease_state = self.lease.try_acquire_or_renew(
            owner_id=self.owner_id,
            pid=self.pid,
            hostname=self.hostname,
            now_text=now_text,
        )
        self.status["leader_state"] = "leader" if lease_state.is_leader else "follower"
        self.status["lease_owner"] = lease_state.owner_id
        self.status["lease_expires_at"] = lease_state.expires_at

        if not lease_state.is_leader:
            self._write_status()
            return False

        self.status["last_tick_started_at"] = now_text
        self._write_status()
        try:
            result = self.tick_fn()
        except Exception as exc:
            self.status["last_error"] = f"{type(exc).__name__}: {exc}"
            self.status["last_tick_finished_at"] = self.clock()
            self._write_status()
            return True

        self.status["last_tick"] = _tick_summary(result)
        self.status["last_tick_finished_at"] = self.clock()
        self.status["last_error"] = None
        self._write_status()
        return False

    @staticmethod
    def _default_tick() -> Any:
        from cron.scheduler import tick

        return tick()


def serve(interval_seconds: int = 60, lease_seconds: int = 180, once: bool = False) -> int:
    service = CronService(interval_seconds=interval_seconds, lease_seconds=lease_seconds)
    service.install_signal_handlers()
    return service.run(once=once)

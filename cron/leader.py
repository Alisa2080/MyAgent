from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from cron.state_store import StateStore


@dataclass(frozen=True)
class LeaderLeaseState:
    name: str
    owner_id: str
    pid: int | None
    hostname: str | None
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    is_leader: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any], *, owner_id: str | None = None) -> LeaderLeaseState:
        row_owner_id = str(row["owner_id"])
        return cls(
            name=str(row["name"]),
            owner_id=row_owner_id,
            pid=row["pid"],
            hostname=row["hostname"],
            acquired_at=str(row["acquired_at"]),
            heartbeat_at=str(row["heartbeat_at"]),
            expires_at=str(row["expires_at"]),
            is_leader=owner_id is not None and row_owner_id == owner_id,
        )


class SchedulerLeaderLease:
    def __init__(self, *, store: StateStore | None = None, name: str = "scheduler", lease_seconds: int = 30) -> None:
        self.store = store or StateStore()
        self.name = name
        self.lease_seconds = max(1, int(lease_seconds))

    def current(self) -> LeaderLeaseState | None:
        row = self.store.get_scheduler_lease(self.name)
        if row is None:
            return None
        return LeaderLeaseState.from_row(row)

    def try_acquire_or_renew(
        self,
        *,
        owner_id: str,
        pid: int | None,
        hostname: str | None,
        now_text: str,
    ) -> LeaderLeaseState:
        expires_at = (datetime.fromisoformat(now_text) + timedelta(seconds=self.lease_seconds)).isoformat()
        row = self.store.try_acquire_scheduler_lease(
            self.name,
            owner_id,
            pid,
            hostname,
            now_text,
            expires_at,
        )
        return LeaderLeaseState.from_row(row, owner_id=owner_id)

    def release(self, owner_id: str) -> bool:
        return self.store.release_scheduler_lease(self.name, owner_id)

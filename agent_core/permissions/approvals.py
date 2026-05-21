from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

from agent_core.permissions.models import RiskTag


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    decision_id: str
    task_id: str
    tool_call_id: str
    tool_name: str
    args_digest: str
    risk_tags: tuple[RiskTag, ...]
    allow_network_once: bool = False


_lock = threading.Lock()
_approvals: dict[tuple[str, str], ApprovalRecord] = {}


def make_args_digest(args: dict[str, Any]) -> str:
    payload = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_approval(record: ApprovalRecord) -> None:
    with _lock:
        _approvals[(record.task_id, record.tool_call_id)] = record


def consume_approval(
    *,
    task_id: str,
    tool_call_id: str | None,
    tool_name: str,
    args: dict[str, Any],
    required_risk_tags: tuple[RiskTag, ...],
) -> ApprovalRecord | None:
    if not tool_call_id:
        return None
    key = (task_id, tool_call_id)
    with _lock:
        record = _approvals.pop(key, None)
        if record is None:
            return None
        if record.tool_name != tool_name:
            return None
        if record.args_digest != make_args_digest(args):
            return None
        if not set(required_risk_tags).issubset(set(record.risk_tags)):
            return None
        return record


def clear_approvals() -> None:
    with _lock:
        _approvals.clear()

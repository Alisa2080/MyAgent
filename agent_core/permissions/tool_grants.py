from __future__ import annotations

import threading
from dataclasses import dataclass

from agent_core.permissions.models import RiskTag


@dataclass(frozen=True)
class ToolPolicyGrant:
    task_id: str
    tool_call_id: str
    tool_name: str
    risk_tags: tuple[RiskTag, ...]
    allow_network_once: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "risk_tags", tuple(self.risk_tags))


_lock = threading.Lock()
_grants: dict[tuple[str, str], ToolPolicyGrant] = {}


def record_tool_policy_grant(grant: ToolPolicyGrant) -> None:
    with _lock:
        _grants[(grant.task_id, grant.tool_call_id)] = grant


def consume_tool_policy_grant(
    *,
    task_id: str,
    tool_call_id: str | None,
    tool_name: str,
    required_risk_tags: tuple[RiskTag, ...] = (),
) -> ToolPolicyGrant | None:
    if not tool_call_id:
        return None
    key = (task_id, tool_call_id)
    with _lock:
        grant = _grants.get(key)
        if grant is None:
            return None
        if grant.tool_name != tool_name:
            return None
        if not set(required_risk_tags).issubset(set(grant.risk_tags)):
            return None
        return _grants.pop(key)


def clear_tool_policy_grants() -> None:
    with _lock:
        _grants.clear()

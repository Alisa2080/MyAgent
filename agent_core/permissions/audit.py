from __future__ import annotations

import logging
from typing import Any

from agent_core.permissions.models import PolicyDecision


logger = logging.getLogger(__name__)


def audit_policy_event(
    *,
    profile: str,
    tool_name: str,
    task_id: str,
    decision: PolicyDecision,
    preview: str = "",
    approved_by_human: bool = False,
    network_once: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    logger.info(
        "policy_event profile=%s tool=%s task=%s outcome=%s reason=%s risks=%s "
        "approved=%s network_once=%s preview=%r extra=%s",
        profile,
        tool_name,
        task_id,
        decision.outcome,
        decision.reason,
        ",".join(decision.risk_tags),
        approved_by_human,
        network_once,
        preview[:200],
        extra or {},
    )

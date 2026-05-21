from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


PolicyOutcome = Literal["allow", "review", "deny"]
RiskTag = str
RuntimeProfile = Literal["dev", "test", "hosted", "prod"]


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason: str
    risk_tags: tuple[RiskTag, ...] = ()
    message: str = ""
    requires_network: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, reason: str = "allowed", **kwargs: Any) -> "PolicyDecision":
        return cls(outcome="allow", reason=reason, **kwargs)

    @classmethod
    def review(
        cls,
        reason: str,
        *,
        risk_tags: tuple[RiskTag, ...],
        requires_network: bool = False,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> "PolicyDecision":
        return cls(
            outcome="review",
            reason=reason,
            risk_tags=risk_tags,
            requires_network=requires_network,
            message=message,
            data=data or {},
        )

    @classmethod
    def deny(
        cls,
        reason: str,
        *,
        risk_tags: tuple[RiskTag, ...] = (),
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> "PolicyDecision":
        return cls(
            outcome="deny",
            reason=reason,
            risk_tags=risk_tags,
            message=message,
            data=data or {},
        )

    @property
    def human_message(self) -> str:
        return self.message or self.reason.replace("_", " ")

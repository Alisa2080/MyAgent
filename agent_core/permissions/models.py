from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


PolicyOutcome = Literal["allow", "review", "deny"]
RiskTag = str
RuntimeProfile = Literal["dev", "test", "hosted", "prod"]


class FrozenDict(dict):
    """JSON-serializable immutable dict used for policy decision metadata."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(
            {key: self._freeze(value) for key, value in dict(*args, **kwargs).items()}
        )

    @classmethod
    def _freeze(cls, value: Any) -> Any:
        if isinstance(value, FrozenDict):
            return value
        if isinstance(value, dict):
            return FrozenDict(value)
        if isinstance(value, list):
            return tuple(cls._freeze(item) for item in value)
        if isinstance(value, tuple):
            return tuple(cls._freeze(item) for item in value)
        return value

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("PolicyDecision data is immutable.")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason: str
    risk_tags: tuple[RiskTag, ...] = ()
    message: str = ""
    requires_network: bool = False
    data: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", FrozenDict(self.data))

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

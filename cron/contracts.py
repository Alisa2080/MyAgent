from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class JobRunResult:
    success: bool
    output_doc: str | None = None
    final_response: str | None = None
    error: str | None = None
    exit_reason: str | None = None


class JobRunner(Protocol):
    def __call__(self, job: dict[str, Any]) -> JobRunResult:
        ...

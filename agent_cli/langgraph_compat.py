from __future__ import annotations

from typing import Any

try:
    from langgraph.types import Command
except ImportError:  # pragma: no cover - subprocess smoke path without langgraph
    class Command:
        def __init__(self, *, resume=None, **kwargs: Any) -> None:
            self.resume = resume
            for key, value in kwargs.items():
                setattr(self, key, value)

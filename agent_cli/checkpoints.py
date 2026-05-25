from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CheckpointDependencyError(RuntimeError):
    """Raised when the LangGraph SQLite checkpointer is unavailable."""


@dataclass
class CheckpointerHandle:
    checkpointer: Any
    _context: Any | None = None
    _connection: sqlite3.Connection | None = None

    def close(self) -> None:
        if self._context is not None:
            context = self._context
            self._context = None
            context.__exit__(None, None, None)
        if self._connection is not None:
            connection = self._connection
            self._connection = None
            connection.close()

    def __enter__(self) -> CheckpointerHandle:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def create_sqlite_checkpointer(db_path: str | Path) -> CheckpointerHandle:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ModuleNotFoundError as exc:
        expected_missing_modules = {
            "langgraph",
            "langgraph.checkpoint",
            "langgraph.checkpoint.sqlite",
        }
        if exc.name not in expected_missing_modules:
            raise
        raise CheckpointDependencyError(
            "SQLite checkpointing requires the LangGraph SQLite package. "
            "Install langgraph-checkpoint-sqlite in the project environment."
        ) from exc

    factory = getattr(SqliteSaver, "from_conn_string", None)
    if callable(factory):
        checkpointer = factory(str(path))
        if hasattr(checkpointer, "__enter__") and hasattr(checkpointer, "__exit__"):
            return CheckpointerHandle(
                checkpointer=checkpointer.__enter__(),
                _context=checkpointer,
            )
        return CheckpointerHandle(checkpointer=checkpointer)

    connection = sqlite3.connect(path)
    try:
        checkpointer = SqliteSaver(connection)
    except Exception:
        connection.close()
        raise
    return CheckpointerHandle(checkpointer=checkpointer, _connection=connection)

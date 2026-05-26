from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


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


def iter_checkpoints(checkpointer: Any, thread_id: str) -> Iterator[dict[str, Any]]:
    """Iterate over checkpoints for a given thread."""
    if checkpointer is None:
        return
    config = {"configurable": {"thread_id": thread_id}}
    try:
        if hasattr(checkpointer, "get_tuple"):
            checkpoint_tuple = checkpointer.get_tuple(config)
            if checkpoint_tuple is not None:
                yield checkpoint_tuple
                return
        if hasattr(checkpointer, "alist"):
            for state in checkpointer.alist(config, limit=100):
                yield state
        elif hasattr(checkpointer, "get_list"):
            for state in checkpointer.get_list(thread_id, limit=100):
                yield state
        elif hasattr(checkpointer, "list"):
            for state in checkpointer.list(config, limit=100):
                yield state
    except Exception:
        pass


def checkpoint_to_messages(checkpoint: Any) -> list[dict[str, Any]]:
    """Extract message list from a checkpoint."""
    if checkpoint is None:
        return []
    if hasattr(checkpoint, "checkpoint"):
        checkpoint = checkpoint.checkpoint
    if isinstance(checkpoint, dict):
        if "checkpoint" in checkpoint:
            return checkpoint_to_messages(checkpoint["checkpoint"])
        if "channel_values" in checkpoint:
            values = checkpoint["channel_values"]
            if "messages" in values:
                msgs = values["messages"]
                if isinstance(msgs, list):
                    return msgs
        if "messages" in checkpoint:
            msgs = checkpoint["messages"]
            if isinstance(msgs, list):
                return msgs
    return []


def extract_messages_from_checkpoints(
    checkpointer: Any, thread_id: str
) -> list[dict[str, Any]]:
    """Extract all messages from all checkpoints for a thread."""
    messages = []
    for checkpoint in iter_checkpoints(checkpointer, thread_id):
        msgs = checkpoint_to_messages(checkpoint)
        messages.extend(msgs)
    return messages

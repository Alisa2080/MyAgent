import sys
import types
from contextlib import AbstractContextManager
from dataclasses import dataclass

import pytest


def test_create_sqlite_checkpointer_uses_langgraph_factory(monkeypatch, tmp_path):
    calls = []

    class FakeSaver:
        @classmethod
        def from_conn_string(cls, conn_string):
            calls.append(conn_string)
            return "checkpointer"

    sqlite_mod = types.ModuleType("langgraph.checkpoint.sqlite")
    sqlite_mod.SqliteSaver = FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph", types.ModuleType("langgraph"))
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint", types.ModuleType("langgraph.checkpoint"))
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", sqlite_mod)

    from agent_cli.checkpoints import create_sqlite_checkpointer

    result = create_sqlite_checkpointer(tmp_path / "cli.sqlite")

    assert result.checkpointer == "checkpointer"
    assert calls == [str(tmp_path / "cli.sqlite")]
    assert not hasattr(result, "some_saver_method")


def test_create_sqlite_checkpointer_keeps_factory_context_alive(monkeypatch, tmp_path):
    events = []

    class FakeContext(AbstractContextManager):
        def __enter__(self):
            events.append("enter")
            return "entered-checkpointer"

        def __exit__(self, exc_type, exc, tb):
            events.append("exit")

    class FakeSaver:
        @classmethod
        def from_conn_string(cls, conn_string):
            events.append(("factory", conn_string))
            return FakeContext()

    sqlite_mod = types.ModuleType("langgraph.checkpoint.sqlite")
    sqlite_mod.SqliteSaver = FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph", types.ModuleType("langgraph"))
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint", types.ModuleType("langgraph.checkpoint"))
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", sqlite_mod)

    from agent_cli.checkpoints import CheckpointerHandle, create_sqlite_checkpointer

    result = create_sqlite_checkpointer(tmp_path / "cli.sqlite")

    assert isinstance(result, CheckpointerHandle)
    assert result.checkpointer == "entered-checkpointer"
    assert events == [("factory", str(tmp_path / "cli.sqlite")), "enter"]

    result.close()

    assert events == [("factory", str(tmp_path / "cli.sqlite")), "enter", "exit"]


def test_create_sqlite_checkpointer_fallback_uses_open_sqlite_connection(monkeypatch, tmp_path):
    calls = []

    class FakeSaver:
        def __init__(self, conn):
            calls.append(conn)
            self.conn = conn

    sqlite_mod = types.ModuleType("langgraph.checkpoint.sqlite")
    sqlite_mod.SqliteSaver = FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph", types.ModuleType("langgraph"))
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint", types.ModuleType("langgraph.checkpoint"))
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", sqlite_mod)

    from agent_cli.checkpoints import CheckpointerHandle, create_sqlite_checkpointer

    result = create_sqlite_checkpointer(tmp_path / "cli.sqlite")

    assert isinstance(result, CheckpointerHandle)
    assert result.checkpointer.conn is calls[0]
    calls[0].execute("select 1")

    result.close()

    with pytest.raises(Exception):
        calls[0].execute("select 1")


def test_create_sqlite_checkpointer_reports_missing_dependency(monkeypatch, tmp_path):
    for name in [
        "langgraph.checkpoint.sqlite",
        "langgraph.checkpoint",
        "langgraph",
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    original_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "langgraph.checkpoint.sqlite":
            raise ModuleNotFoundError("No module named 'langgraph.checkpoint.sqlite'", name=name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    from agent_cli.checkpoints import CheckpointDependencyError, create_sqlite_checkpointer

    with pytest.raises(CheckpointDependencyError) as exc:
        create_sqlite_checkpointer(tmp_path / "cli.sqlite")

    assert "langgraph-checkpoint-sqlite" in str(exc.value)


def test_create_sqlite_checkpointer_reraises_unexpected_transitive_import_error(
    monkeypatch, tmp_path
):
    original_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "langgraph.checkpoint.sqlite":
            raise ModuleNotFoundError("No module named 'missing_transitive'", name="missing_transitive")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    from agent_cli.checkpoints import create_sqlite_checkpointer

    with pytest.raises(ModuleNotFoundError) as exc:
        create_sqlite_checkpointer(tmp_path / "cli.sqlite")

    assert exc.value.name == "missing_transitive"


@dataclass
class FakeCheckpointTuple:
    checkpoint: dict


def test_extract_messages_from_checkpoints_reads_latest_checkpoint_tuple():
    class FakeSaver:
        def get_tuple(self, config):
            assert config == {"configurable": {"thread_id": "thread-1"}}
            return FakeCheckpointTuple(
                checkpoint={
                    "channel_values": {
                        "messages": [{"role": "user", "content": "hello"}]
                    }
                }
            )

    from agent_cli.checkpoints import extract_messages_from_checkpoints

    assert extract_messages_from_checkpoints(FakeSaver(), "thread-1") == [
        {"role": "user", "content": "hello"}
    ]


def test_extract_messages_from_checkpoints_reads_list_checkpoint_tuples():
    class FakeSaver:
        def list(self, config, *, limit=None):
            assert config == {"configurable": {"thread_id": "thread-1"}}
            assert limit == 100
            return [
                FakeCheckpointTuple(
                    checkpoint={
                        "channel_values": {
                            "messages": [{"role": "assistant", "content": "hi"}]
                        }
                    }
                )
            ]

    from agent_cli.checkpoints import extract_messages_from_checkpoints

    assert extract_messages_from_checkpoints(FakeSaver(), "thread-1") == [
        {"role": "assistant", "content": "hi"}
    ]

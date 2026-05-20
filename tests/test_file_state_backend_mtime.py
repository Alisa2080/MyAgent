from agent_tools.file_toolkit.file_state import FileStateRegistry


def test_explicit_unknown_mtime_records_sibling_writer_warning():
    registry = FileStateRegistry()
    path = "/workspace/notes.txt"

    registry.record_read("parent", path, mtime=None)
    registry.note_write("worker", path, mtime=None)

    warning = registry.check_stale("parent", path, current_mtime=None)

    assert warning is not None
    assert "modified by sibling subagent 'worker'" in warning
    assert path in warning


def test_explicit_unknown_mtime_preserves_partial_read_warning():
    registry = FileStateRegistry()
    path = "/workspace/notes.txt"

    registry.record_read("parent", path, partial=True, mtime=None)

    warning = registry.check_stale("parent", path, current_mtime=None)

    assert warning is not None
    assert "partial view" in warning


def test_explicit_unknown_mtime_skips_external_mtime_drift_warning():
    registry = FileStateRegistry()
    path = "/workspace/notes.txt"

    registry.record_read("parent", path, mtime=None)

    assert registry.check_stale("parent", path, current_mtime=None) is None


def test_explicit_current_mtime_is_normalized_before_comparison():
    registry = FileStateRegistry()
    path = "/workspace/notes.txt"

    registry.record_read("parent", path, mtime=1.0)

    assert registry.check_stale("parent", path, current_mtime="1.0") is None


def test_omitted_mtime_keeps_existing_host_getmtime_degradation(monkeypatch):
    registry = FileStateRegistry()
    path = "/workspace/not-on-host.txt"

    def missing(_path):
        raise OSError("not on host")

    monkeypatch.setattr("agent_tools.file_toolkit.file_state.os.path.getmtime", missing)

    registry.record_read("parent", path)

    assert registry.known_reads("parent") == []

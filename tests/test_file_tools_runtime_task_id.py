import json
from types import SimpleNamespace


def test_file_tool_schemas_do_not_expose_task_id():
    from agent_tools.public.files import patch, read_file, search_files, write_file

    assert "task_id" not in read_file.args
    assert "task_id" not in write_file.args
    assert "task_id" not in patch.args
    assert "task_id" not in search_files.args

    assert "path" in read_file.args
    assert "path" in write_file.args
    assert "mode" in patch.args
    assert "pattern" in search_files.args


def test_read_file_injects_runtime_thread_as_task_id(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []

    def fake_read_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "README.md", "content": "ok\n"})

    monkeypatch.setattr(file_tools, "read_file_tool", fake_read_file_tool)

    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="file-read-thread"),
        config={"configurable": {"thread_id": "ignored-config-thread"}},
    )

    raw = file_tools._read_file_impl(path="README.md", offset=1, limit=20, runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert "task_id" not in payload.get("meta", {})
    assert calls[0]["path"] == "README.md"
    assert calls[0]["offset"] == 1
    assert calls[0]["limit"] == 20
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("file-read-thread")


def test_write_file_injects_runtime_thread_as_task_id(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "notes.txt", "bytes_written": 3})

    monkeypatch.setattr(file_tools, "write_file_tool", fake_write_file_tool)

    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="file-write-thread"))

    raw = file_tools._write_file_impl(path="notes.txt", content="hello", runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert "task_id" not in payload.get("meta", {})
    assert calls[0]["path"] == "notes.txt"
    assert calls[0]["content"] == "hello"
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("file-write-thread")


def test_search_files_injects_runtime_thread_as_task_id(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []

    def fake_search_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"matches": []})

    monkeypatch.setattr(file_tools, "search_tool", fake_search_tool)

    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="file-search-thread"))

    raw = file_tools._search_files_impl(
        pattern="TODO",
        target="content",
        path=".",
        file_glob=None,
        limit=10,
        offset=0,
        output_mode="content",
        context=0,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert "task_id" not in payload.get("meta", {})
    assert calls[0]["pattern"] == "TODO"
    assert calls[0]["target"] == "content"
    assert calls[0]["path"] == "."
    assert calls[0]["file_glob"] is None
    assert calls[0]["limit"] == 10
    assert calls[0]["offset"] == 0
    assert calls[0]["output_mode"] == "content"
    assert calls[0]["context"] == 0
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("file-search-thread")


def test_patch_injects_runtime_thread_as_task_id(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []

    def fake_patch_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "notes.txt", "replacements": 1})

    monkeypatch.setattr(file_tools, "patch_tool", fake_patch_tool)

    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="file-patch-thread"))

    raw = file_tools._patch_impl(
        mode="replace",
        path="notes.txt",
        old_string="old",
        new_string="new",
        replace_all=False,
        patch=None,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert "task_id" not in payload.get("meta", {})
    assert calls[0]["mode"] == "replace"
    assert calls[0]["path"] == "notes.txt"
    assert calls[0]["old_string"] == "old"
    assert calls[0]["new_string"] == "new"
    assert calls[0]["replace_all"] is False
    assert calls[0]["patch"] is None
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("file-patch-thread")

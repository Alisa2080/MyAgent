import json
from types import SimpleNamespace


class FakeEnv:
    def __init__(self, cwd, env_type, configured_cwd=None):
        self.cwd = cwd
        self._hermes_env_type = env_type
        self._hermes_configured_cwd = (
            configured_cwd if configured_cwd is not None else cwd
        )
        self._hermes_host_cwd = None


def _invoke_toolnode(tool, tool_name, args, thread_id):
    from langchain_core.messages import AIMessage
    from langgraph.graph import MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([tool]))
    graph.set_entry_point("tools")
    graph.set_finish_point("tools")
    app = graph.compile()

    result = app.invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": tool_name,
                            "args": args,
                            "id": f"call-{tool_name}",
                        }
                    ],
                )
            ]
        },
        config={"configurable": {"thread_id": thread_id}},
    )

    return json.loads(result["messages"][-1].content)


def test_file_tool_schemas_do_not_expose_task_id():
    from agent_tools.public.files import patch, read_file, search_files, write_file

    assert "task_id" not in read_file.args
    assert "task_id" not in write_file.args
    assert "task_id" not in patch.args
    assert "task_id" not in search_files.args
    assert "runtime" not in read_file.args
    assert "runtime" not in write_file.args
    assert "runtime" not in patch.args
    assert "runtime" not in search_files.args

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


def test_read_file_toolnode_injects_runtime_thread(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.public.files import read_file

    calls = []

    def fake_read_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "README.md", "content": "toolnode-ok\n"})

    monkeypatch.setattr(file_tools, "read_file_tool", fake_read_file_tool)

    payload = _invoke_toolnode(
        read_file,
        "read_file",
        {"path": "README.md", "offset": 2, "limit": 5},
        "toolnode-read-thread",
    )

    assert payload["ok"] is True
    assert payload["data"]["content"] == "toolnode-ok\n"
    assert "task_id" not in payload.get("meta", {})
    assert calls[0]["path"] == "README.md"
    assert calls[0]["offset"] == 2
    assert calls[0]["limit"] == 5
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("toolnode-read-thread")


def test_file_tool_impl_falls_back_to_default_task_id_without_runtime(monkeypatch):
    import agent_tools.public.files as file_tools

    calls = []

    def fake_read_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"content": "1|fallback\n", "total_lines": 1})

    monkeypatch.setattr(file_tools, "read_file_tool", fake_read_file_tool)

    raw = file_tools._read_file_impl(path="README.md", offset=1, limit=5, runtime=None)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["task_id"] == "default"


def test_read_file_rejects_live_cwd_outside_workspace(monkeypatch, tmp_path):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []
    resolve_calls = []
    outside_path = tmp_path / "leak.txt"
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="outside-read"))
    expected_task_id = hermes_task_id_from_thread_id("outside-read")

    def fake_resolve_path_for_policy(path, task_id):
        resolve_calls.append((path, task_id))
        return outside_path

    def fake_read_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "leak.txt", "content": "secret\n"})

    monkeypatch.setattr(
        file_tools,
        "resolve_path_for_policy",
        fake_resolve_path_for_policy,
        raising=False,
    )
    monkeypatch.setattr(file_tools, "read_file_tool", fake_read_file_tool)

    raw = file_tools._read_file_impl(path="leak.txt", offset=1, limit=20, runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_path"
    assert calls == []
    assert resolve_calls == [("leak.txt", expected_task_id)]


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


def test_write_file_rejects_live_cwd_outside_workspace(monkeypatch, tmp_path):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []
    resolve_calls = []
    outside_path = tmp_path / "leak.txt"
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="outside-write"))
    expected_task_id = hermes_task_id_from_thread_id("outside-write")

    def fake_resolve_path_for_policy(path, task_id):
        resolve_calls.append((path, task_id))
        return outside_path

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "leak.txt", "bytes_written": 4})

    monkeypatch.setattr(
        file_tools,
        "resolve_path_for_policy",
        fake_resolve_path_for_policy,
        raising=False,
    )
    monkeypatch.setattr(file_tools, "write_file_tool", fake_write_file_tool)

    raw = file_tools._write_file_impl(path="leak.txt", content="leak", runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_path"
    assert calls == []
    assert resolve_calls == [("leak.txt", expected_task_id)]


def test_write_file_denies_local_backend_configured_cwd_outside_workspace(
    monkeypatch, tmp_path
):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    calls = []
    resolve_calls = []
    outside_path = tmp_path / "local-live-cwd" / "leak.txt"
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="local-outside"))
    expected_task_id = hermes_task_id_from_thread_id("local-outside")

    def fake_resolve_path_for_policy(path, task_id):
        resolve_calls.append((path, task_id))
        return outside_path

    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "local", "cwd": str(outside_path.parent)},
    )
    monkeypatch.setattr(
        file_tools,
        "resolve_path_for_policy",
        fake_resolve_path_for_policy,
        raising=False,
    )
    monkeypatch.setattr(
        file_tools,
        "write_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"bytes_written": 4}),
    )

    raw = file_tools._write_file_impl(path="leak.txt", content="leak", runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_path"
    assert calls == []
    assert resolve_calls == [("leak.txt", expected_task_id)]


def test_write_file_allows_live_cwd_inside_workspace_and_forwards_original_path(
    monkeypatch,
):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []
    resolve_calls = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="inside-write"))
    expected_task_id = hermes_task_id_from_thread_id("inside-write")

    def fake_resolve_path_for_policy(path, task_id):
        resolve_calls.append((path, task_id))
        return file_tools.WORKDIR / "subdir" / path

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "notes.txt", "bytes_written": 5})

    monkeypatch.setattr(
        file_tools,
        "resolve_path_for_policy",
        fake_resolve_path_for_policy,
        raising=False,
    )
    monkeypatch.setattr(file_tools, "write_file_tool", fake_write_file_tool)

    raw = file_tools._write_file_impl(path="notes.txt", content="hello", runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert resolve_calls == [("notes.txt", expected_task_id)]
    assert calls == [
        {"path": "notes.txt", "content": "hello", "task_id": expected_task_id}
    ]


def test_write_file_allows_docker_workspace_resolved_path_and_forwards_original_path(
    monkeypatch,
):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    calls = []
    resolve_calls = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="docker-write"))
    expected_task_id = hermes_task_id_from_thread_id("docker-write")

    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/root"},
    )

    def fake_resolve_path_for_policy(path, task_id):
        resolve_calls.append((path, task_id))
        return f"/workspace/project/{path}"

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "notes.txt", "bytes_written": 5})

    monkeypatch.setattr(
        file_tools,
        "resolve_path_for_policy",
        fake_resolve_path_for_policy,
        raising=False,
    )
    monkeypatch.setattr(file_tools, "write_file_tool", fake_write_file_tool)

    raw = file_tools._write_file_impl(path="notes.txt", content="hello", runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert resolve_calls == [("notes.txt", expected_task_id)]
    assert calls == [
        {"path": "notes.txt", "content": "hello", "task_id": expected_task_id}
    ]


def test_write_file_allows_active_ssh_cwd_path_and_forwards_original_path(
    monkeypatch,
):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    calls = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="ssh-write"))
    expected_task_id = hermes_task_id_from_thread_id("ssh-write")
    active = FakeEnv("/home/remote/project", "ssh", configured_cwd="~")

    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "~", "host_cwd": None},
    )

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "/home/remote/project/notes.txt", "bytes_written": 5})

    monkeypatch.setattr(file_tools, "write_file_tool", fake_write_file_tool)

    raw = file_tools._write_file_impl(
        path="/home/remote/project/notes.txt",
        content="hello",
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls == [
        {
            "path": "/home/remote/project/notes.txt",
            "content": "hello",
            "task_id": expected_task_id,
        }
    ]


def test_write_file_rejects_ssh_absolute_path_outside_active_cwd(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    calls = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="ssh-reject"))
    active = FakeEnv("/home/remote/project", "ssh", configured_cwd="~")

    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "~", "host_cwd": None},
    )
    monkeypatch.setattr(
        file_tools,
        "write_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"bytes_written": 4}),
    )

    raw = file_tools._write_file_impl(
        path="/home/remote/other/leak.txt",
        content="leak",
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_path"
    assert calls == []


def test_read_file_allows_docker_workspace_resolved_path_and_forwards_original_path(
    monkeypatch,
):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    calls = []
    resolve_calls = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="docker-read"))
    expected_task_id = hermes_task_id_from_thread_id("docker-read")

    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/root"},
    )

    def fake_resolve_path_for_policy(path, task_id):
        resolve_calls.append((path, task_id))
        return f"/workspace/project/{path}"

    def fake_read_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "notes.txt", "content": "hello\n"})

    monkeypatch.setattr(
        file_tools,
        "resolve_path_for_policy",
        fake_resolve_path_for_policy,
        raising=False,
    )
    monkeypatch.setattr(file_tools, "read_file_tool", fake_read_file_tool)

    raw = file_tools._read_file_impl(path="notes.txt", offset=1, limit=20, runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert resolve_calls == [("notes.txt", expected_task_id)]
    assert calls == [
        {"path": "notes.txt", "offset": 1, "limit": 20, "task_id": expected_task_id}
    ]


def test_read_file_allows_singularity_active_cwd_path(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    calls = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="singularity-read"))
    expected_task_id = hermes_task_id_from_thread_id("singularity-read")
    active = FakeEnv("/analysis/project", "singularity")

    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "singularity", "cwd": "/root", "host_cwd": None},
    )

    def fake_read_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "/analysis/project/notes.txt", "content": "hello\n"})

    monkeypatch.setattr(file_tools, "read_file_tool", fake_read_file_tool)

    raw = file_tools._read_file_impl(
        path="/analysis/project/notes.txt",
        offset=1,
        limit=20,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls == [
        {
            "path": "/analysis/project/notes.txt",
            "offset": 1,
            "limit": 20,
            "task_id": expected_task_id,
        }
    ]


def test_read_file_rejects_docker_skill_cache_path(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    calls = []
    resolve_calls = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="docker-cache-read"))
    expected_task_id = hermes_task_id_from_thread_id("docker-cache-read")

    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/root"},
    )

    def fake_resolve_path_for_policy(path, task_id):
        resolve_calls.append((path, task_id))
        return "/workspace/skills/.hub/index-cache/prompt.md"

    monkeypatch.setattr(
        file_tools,
        "resolve_path_for_policy",
        fake_resolve_path_for_policy,
        raising=False,
    )
    monkeypatch.setattr(
        file_tools,
        "read_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"content": "blocked\n"}),
    )

    raw = file_tools._read_file_impl(
        path="skills/.hub/index-cache/prompt.md",
        offset=1,
        limit=20,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "access_denied"
    assert calls == []
    assert resolve_calls == [
        ("skills/.hub/index-cache/prompt.md", expected_task_id)
    ]


def test_write_file_toolnode_injects_runtime_thread(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.public.files import write_file

    calls = []

    def fake_write_file_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "notes.txt", "bytes_written": 18})

    monkeypatch.setattr(file_tools, "write_file_tool", fake_write_file_tool)

    payload = _invoke_toolnode(
        write_file,
        "write_file",
        {"path": "notes.txt", "content": "hello from toolnode"},
        "toolnode-write-thread",
    )

    assert payload["ok"] is True
    assert payload["data"]["path"] == "notes.txt"
    assert "task_id" not in payload.get("meta", {})
    assert calls[0]["path"] == "notes.txt"
    assert calls[0]["content"] == "hello from toolnode"
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("toolnode-write-thread")


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


def test_search_files_rejects_internal_skill_cache_path(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    calls = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="cache-search"))

    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/root"},
    )
    monkeypatch.setattr(
        file_tools,
        "resolve_path_for_policy",
        lambda path, task_id: "/workspace/skills/.hub/index-cache",
        raising=False,
    )
    monkeypatch.setattr(
        file_tools,
        "search_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"matches": []}),
    )

    raw = file_tools._search_files_impl(
        pattern="system",
        target="content",
        path="skills/.hub/index-cache",
        file_glob=None,
        limit=10,
        offset=0,
        output_mode="content",
        context=0,
        runtime=runtime,
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "access_denied"
    assert calls == []


def test_search_files_toolnode_injects_runtime_thread(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.public.files import search_files

    calls = []

    def fake_search_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"matches": [{"path": "README.md", "line": 1, "text": "TODO"}]})

    monkeypatch.setattr(file_tools, "search_tool", fake_search_tool)

    payload = _invoke_toolnode(
        search_files,
        "search_files",
        {
            "pattern": "TODO",
            "target": "content",
            "path": ".",
            "file_glob": "*.md",
            "limit": 7,
            "offset": 1,
            "output_mode": "content",
            "context": 2,
        },
        "toolnode-search-thread",
    )

    assert payload["ok"] is True
    assert payload["data"]["matches"][0]["path"] == "README.md"
    assert "task_id" not in payload.get("meta", {})
    assert calls[0]["pattern"] == "TODO"
    assert calls[0]["target"] == "content"
    assert calls[0]["path"] == "."
    assert calls[0]["file_glob"] == "*.md"
    assert calls[0]["limit"] == 7
    assert calls[0]["offset"] == 1
    assert calls[0]["output_mode"] == "content"
    assert calls[0]["context"] == 2
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("toolnode-search-thread")


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


def test_patch_move_file_rejects_live_cwd_outside_workspace_source(
    monkeypatch, tmp_path
):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []
    resolve_calls = []
    outside_path = tmp_path / "source.txt"
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="move-source"))
    expected_task_id = hermes_task_id_from_thread_id("move-source")
    patch_content = "\n".join(
        [
            "*** Begin Patch",
            "*** Move File: source.txt -> dest.txt",
            "*** End Patch",
        ]
    )

    def fake_resolve_path_for_policy(path, task_id):
        resolve_calls.append((path, task_id))
        if path == "source.txt":
            return outside_path
        return file_tools.WORKDIR / path

    def fake_patch_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"status": "success"})

    monkeypatch.setattr(
        file_tools,
        "resolve_path_for_policy",
        fake_resolve_path_for_policy,
        raising=False,
    )
    monkeypatch.setattr(file_tools, "patch_tool", fake_patch_tool)

    raw = file_tools._patch_impl(mode="patch", patch=patch_content, runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_path"
    assert calls == []
    assert resolve_calls == [("source.txt", expected_task_id)]


def test_patch_move_file_rejects_live_cwd_outside_workspace_destination(
    monkeypatch, tmp_path
):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id

    calls = []
    resolve_calls = []
    outside_path = tmp_path / "dest.txt"
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="move-dest"))
    expected_task_id = hermes_task_id_from_thread_id("move-dest")
    patch_content = "\n".join(
        [
            "*** Begin Patch",
            "*** Move File: source.txt -> dest.txt",
            "*** End Patch",
        ]
    )

    def fake_resolve_path_for_policy(path, task_id):
        resolve_calls.append((path, task_id))
        if path == "dest.txt":
            return outside_path
        return file_tools.WORKDIR / path

    def fake_patch_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"status": "success"})

    monkeypatch.setattr(
        file_tools,
        "resolve_path_for_policy",
        fake_resolve_path_for_policy,
        raising=False,
    )
    monkeypatch.setattr(file_tools, "patch_tool", fake_patch_tool)

    raw = file_tools._patch_impl(mode="patch", patch=patch_content, runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_path"
    assert calls == []
    assert resolve_calls == [
        ("source.txt", expected_task_id),
        ("dest.txt", expected_task_id),
    ]


def test_patch_toolnode_injects_runtime_thread(monkeypatch):
    import agent_tools.public.files as file_tools
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.public.files import patch

    calls = []

    def fake_patch_tool(**kwargs):
        calls.append(kwargs)
        return json.dumps({"path": "notes.txt", "replacements": 1})

    monkeypatch.setattr(file_tools, "patch_tool", fake_patch_tool)

    payload = _invoke_toolnode(
        patch,
        "patch",
        {
            "mode": "replace",
            "path": "notes.txt",
            "old_string": "old",
            "new_string": "new",
            "replace_all": False,
        },
        "toolnode-patch-thread",
    )

    assert payload["ok"] is True
    assert payload["data"]["replacements"] == 1
    assert "task_id" not in payload.get("meta", {})
    assert calls[0]["mode"] == "replace"
    assert calls[0]["path"] == "notes.txt"
    assert calls[0]["old_string"] == "old"
    assert calls[0]["new_string"] == "new"
    assert calls[0]["replace_all"] is False
    assert calls[0]["patch"] is None
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("toolnode-patch-thread")

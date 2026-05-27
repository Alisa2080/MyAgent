from langchain_core.messages import ToolMessage
from types import SimpleNamespace


def _runtime(tool_call_id: str = "call-public-tool"):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="public-tool-thread"),
        tool_call_id=tool_call_id,
    )


def _assert_tool_result(result: ToolMessage, tool: str, ok: bool):
    assert isinstance(result, ToolMessage)
    assert result.content
    assert result.artifact["ok"] is ok
    assert result.artifact["tool"] == tool
    assert result.status == ("success" if ok else "error")


def test_web_search_error_returns_tool_message(monkeypatch):
    import agent_tools.public.web as web

    monkeypatch.setattr(web, "_get_client", lambda: (_ for _ in ()).throw(RuntimeError("missing key")))

    result = web.web_search.func("langchain", limit=3)

    _assert_tool_result(result, "web_search", False)
    assert result.artifact["error"]["code"] == "tool_error"
    assert "missing key" in result.content


def test_skills_list_returns_tool_message():
    from agent_tools.public.skills import skills_list

    result = skills_list.func()

    _assert_tool_result(result, "skills_list", True)
    assert "skills" in result.artifact["data"]
    assert "categories" in result.artifact["data"]


def test_task_empty_summary_returns_tool_message(monkeypatch):
    import agent_core.delegation as delegation

    class FakeAgent:
        def invoke(self, *args, **kwargs):
            return {"messages": []}

    monkeypatch.setattr(delegation, "build_task_subagent", lambda: FakeAgent())

    result = delegation.task.func("inspect repo", "empty")

    _assert_tool_result(result, "task", True)
    assert result.artifact["data"]["description"] == "empty"


def test_web_search_preserves_runtime_tool_call_id(monkeypatch):
    import agent_tools.public.web as web

    monkeypatch.setattr(web, "_get_client", lambda: (_ for _ in ()).throw(RuntimeError("missing key")))

    result = web.web_search.func("langchain", limit=3, runtime=_runtime("call-web"))

    _assert_tool_result(result, "web_search", False)
    assert result.tool_call_id == "call-web"


def test_skills_list_preserves_runtime_tool_call_id():
    from agent_tools.public.skills import skills_list

    result = skills_list.func(runtime=_runtime("call-skills-list"))

    _assert_tool_result(result, "skills_list", True)
    assert result.tool_call_id == "call-skills-list"


def test_skill_view_failure_preserves_runtime_tool_call_id():
    from agent_tools.public.skills import skill_view

    result = skill_view.func("missing-skill", runtime=_runtime("call-skill-view"))

    _assert_tool_result(result, "skill_view", False)
    assert result.tool_call_id == "call-skill-view"


def test_task_preserves_runtime_tool_call_id(monkeypatch):
    import agent_core.delegation as delegation

    class FakeAgent:
        def invoke(self, *args, **kwargs):
            return {"messages": []}

    monkeypatch.setattr(delegation, "build_task_subagent", lambda: FakeAgent())

    result = delegation.task.func(
        "inspect repo",
        "empty",
        runtime=_runtime("call-task"),
    )

    _assert_tool_result(result, "task", True)
    assert result.tool_call_id == "call-task"


def test_web_fetch_preserves_runtime_tool_call_id(monkeypatch):
    import agent_tools.public.web as web

    monkeypatch.setattr(web, "_get_client", lambda: (_ for _ in ()).throw(RuntimeError("missing key")))

    result = web.web_fetch.func(["https://example.com"], runtime=_runtime("call-fetch"))

    _assert_tool_result(result, "web_fetch", False)
    assert result.tool_call_id == "call-fetch"


def test_memory_manage_preserves_runtime_tool_call_id(monkeypatch):
    import agent_tools.public.memory as memory

    monkeypatch.setattr(memory, "run_memory_tool", lambda **kwargs: '{"error": "nope"}')

    result = memory.memory_manage.func(
        "add",
        "memory",
        content="remember this",
        runtime=_runtime("call-memory"),
    )

    _assert_tool_result(result, "memory_manage", False)
    assert result.tool_call_id == "call-memory"


def test_skill_manage_failure_preserves_runtime_tool_call_id():
    from agent_tools.public.skill_manage_impl import skill_manage

    result = skill_manage.func(
        "create",
        "bad/name",
        runtime=_runtime("call-skill-manage"),
    )

    _assert_tool_result(result, "skill_manage", False)
    assert result.tool_call_id == "call-skill-manage"


def test_read_file_content_includes_file_body(monkeypatch):
    from agent_tools.public import files

    raw = '{"path": "README.md", "content": "alpha\\nbeta", "start_line": 1, "end_line": 2}'
    monkeypatch.setattr(files, "_ensure_read_allowed_for_task", lambda path, task_id: None)
    monkeypatch.setattr(files, "read_file_tool", lambda **kwargs: raw)

    result = files._read_file_impl("README.md", runtime=_runtime("call-read-body"))

    _assert_tool_result(result, "read_file", True)
    assert result.tool_call_id == "call-read-body"
    assert "README.md" in result.content
    assert "alpha" in result.content
    assert "beta" in result.content


def test_search_files_content_includes_matches(monkeypatch):
    from agent_tools.public import files

    raw = (
        '{"matches": ['
        '{"path": "agent_cli/repl.py", "line": 10, "content": "class AgentCLI:"}'
        '], "total_count": 1}'
    )
    monkeypatch.setattr(files, "_ensure_read_allowed_for_task", lambda path, task_id: None)
    monkeypatch.setattr(files, "search_tool", lambda **kwargs: raw)

    result = files._search_files_impl("AgentCLI", runtime=_runtime("call-search-body"))

    _assert_tool_result(result, "search_files", True)
    assert result.tool_call_id == "call-search-body"
    assert "Search results for 'AgentCLI':" in result.content
    assert "agent_cli/repl.py" in result.content
    assert "class AgentCLI:" in result.content


def test_search_files_content_includes_files_only_results(monkeypatch):
    from agent_tools.public import files

    raw = '{"files": ["agent_cli/main.py", "agent_cli/repl.py"], "total_count": 2}'
    monkeypatch.setattr(files, "_ensure_read_allowed_for_task", lambda path, task_id: None)
    monkeypatch.setattr(files, "search_tool", lambda **kwargs: raw)

    result = files._search_files_impl(
        "*.py",
        target="files",
        path="agent_cli",
        runtime=_runtime("call-search-files"),
    )

    _assert_tool_result(result, "search_files", True)
    assert "Search results for '*.py':" in result.content
    assert "Target: files" in result.content
    assert "Path: agent_cli" in result.content
    assert "agent_cli/main.py" in result.content
    assert "agent_cli/repl.py" in result.content


def test_list_directory_content_includes_entries(tmp_path, monkeypatch):
    from agent_tools.public import files

    root = tmp_path / "repo"
    root.mkdir()
    (root / "agent_cli").mkdir()
    (root / "README.md").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(files, "WORKDIR", root)
    monkeypatch.setattr(files, "safe_path", lambda path: root / path)
    monkeypatch.setattr(
        files,
        "relative_path",
        lambda path: path.relative_to(root).as_posix(),
    )

    result = files._list_directory_impl(".", runtime=_runtime("call-list-body"))

    _assert_tool_result(result, "list_directory", True)
    assert result.tool_call_id == "call-list-body"
    assert "agent_cli" in result.content
    assert "README.md" in result.content


def test_skill_view_content_includes_skill_body(monkeypatch, tmp_path):
    import agent_tools.public.skills as skills

    skill_dir = tmp_path / "example"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Example Skill\nUse this skill.", encoding="utf-8")
    monkeypatch.setattr(
        skills,
        "_find_skill",
        lambda name: {
            "name": "example",
            "title": "Example",
            "description": "Demo.",
            "category": "local",
            "dir": skill_dir,
            "path": skill_file,
        },
    )

    result = skills.skill_view.func("example", runtime=_runtime("call-skill-body"))

    _assert_tool_result(result, "skill_view", True)
    assert result.tool_call_id == "call-skill-body"
    assert "# Example Skill" in result.content
    assert "Use this skill." in result.content

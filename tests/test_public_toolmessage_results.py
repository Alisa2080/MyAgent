from langchain_core.messages import ToolMessage


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

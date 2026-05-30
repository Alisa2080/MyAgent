from __future__ import annotations

import sys
import types
import importlib.util
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _fake_tool(name=None, *tool_args, **tool_kwargs):
    def decorate(func):
        func.name = str(name or getattr(func, "__name__", "tool"))
        func.func = func
        return func

    if callable(name) and not tool_args and not tool_kwargs:
        func = name
        name = getattr(func, "__name__", "tool")
        return decorate(func)
    return decorate


def _install_langchain_stubs(monkeypatch=None):
    import types as types_

    setter = monkeypatch.setitem if monkeypatch is not None else sys.modules.setdefault
    mock_langchain = types_.ModuleType("langchain")
    mock_langchain_tools = types_.ModuleType("langchain.tools")
    mock_langchain_tools.tool = _fake_tool
    mock_langchain_tools.ToolRuntime = MagicMock()
    mock_langchain.tools = mock_langchain_tools
    mock_langchain_agents = types_.ModuleType("langchain.agents")
    mock_langchain_agents.create_agent = MagicMock(side_effect=lambda **kwargs: kwargs)
    mock_langchain_agents.middleware = types_.ModuleType("langchain.agents.middleware")
    mock_langchain_agents.middleware.__path__ = []

    class AgentMiddleware:
        def __init__(self, *args, **kwargs):
            pass

    mock_langchain_agents.middleware.AgentMiddleware = AgentMiddleware
    for name in (
        "ToolCallRequest",
        "ToolCallLimitMiddleware",
        "SummarizationMiddleware",
        "TodoListMiddleware",
        "ModelRetryMiddleware",
        "ToolRetryMiddleware",
        "ModelCallLimitMiddleware",
    ):
        setattr(mock_langchain_agents.middleware, name, MagicMock())
    human_loop = types_.ModuleType("langchain.agents.middleware.human_in_the_loop")

    class HumanInTheLoopMiddleware:
        def __init__(self, *args, **kwargs):
            pass

    human_loop.HumanInTheLoopMiddleware = HumanInTheLoopMiddleware
    mock_langchain.agents = mock_langchain_agents
    mock_langchain_core = types_.ModuleType("langchain_core")
    mock_langchain_core.messages = types_.ModuleType("langchain_core.messages")
    mock_langchain_core.messages.AIMessage = MagicMock()
    mock_langchain_core.messages.ToolCall = dict
    mock_langchain_core.messages.ToolMessage = lambda **kwargs: SimpleNamespace(**kwargs)
    if monkeypatch is not None:
        setter(sys.modules, "langchain", mock_langchain)
        setter(sys.modules, "langchain.agents", mock_langchain_agents)
        setter(sys.modules, "langchain.agents.middleware", mock_langchain_agents.middleware)
        setter(sys.modules, "langchain.agents.middleware.human_in_the_loop", human_loop)
        setter(sys.modules, "langchain.tools", mock_langchain_tools)
        setter(sys.modules, "langchain_core", mock_langchain_core)
        setter(sys.modules, "langchain_core.messages", mock_langchain_core.messages)
    else:
        setter("langchain", mock_langchain)
        setter("langchain.agents", mock_langchain_agents)
        setter("langchain.agents.middleware", mock_langchain_agents.middleware)
        setter("langchain.agents.middleware.human_in_the_loop", human_loop)
        setter("langchain.tools", mock_langchain_tools)
        setter("langchain_core", mock_langchain_core)
        setter("langchain_core.messages", mock_langchain_core.messages)


def _install_optional_dep_stubs(monkeypatch=None):
    setter = monkeypatch.setitem if monkeypatch is not None else sys.modules.setdefault
    if importlib.util.find_spec("dateutil") is None:
        if monkeypatch is not None:
            setter(sys.modules, "dateutil", MagicMock())
            setter(sys.modules, "dateutil.parser", MagicMock())
        else:
            setter("dateutil", MagicMock())
            setter("dateutil.parser", MagicMock())


@pytest.fixture(autouse=True)
def stub_heavy_deps(monkeypatch):
    import types as types_

    _install_langchain_stubs(monkeypatch)

    # langgraph stubs
    mock_langgraph = types_.ModuleType("langgraph")
    mock_langgraph.__path__ = []
    mock_langgraph_runtime = types_.ModuleType("langgraph.runtime")
    mock_langgraph_runtime.Runtime = MagicMock()
    mock_langgraph_types = types_.ModuleType("langgraph.types")
    mock_langgraph_types.Command = MagicMock()
    mock_langgraph_types.interrupt = MagicMock()
    mock_langgraph.runtime = mock_langgraph_runtime
    mock_langgraph.types = mock_langgraph_types
    monkeypatch.setitem(sys.modules, "langgraph", mock_langgraph)
    monkeypatch.setitem(sys.modules, "langgraph.runtime", mock_langgraph_runtime)
    monkeypatch.setitem(sys.modules, "langgraph.types", mock_langgraph_types)

    monkeypatch.setitem(sys.modules, "dotenv", MagicMock())
    monkeypatch.setitem(sys.modules, "pydantic", MagicMock())
    monkeypatch.setitem(sys.modules, "tinyfish", MagicMock())
    monkeypatch.setitem(sys.modules, "langchain_anthropic", MagicMock())
    monkeypatch.setitem(sys.modules, "langchain_openai", MagicMock())
    _install_optional_dep_stubs(monkeypatch)


def pytest_configure(config):
    import types as types_

    # Stub heavy deps before any imports happen
    _install_langchain_stubs()

    mock_langgraph = types_.ModuleType("langgraph")
    mock_langgraph.__path__ = []
    mock_langgraph_runtime = types_.ModuleType("langgraph.runtime")
    mock_langgraph_runtime.Runtime = MagicMock()
    mock_langgraph_types = types_.ModuleType("langgraph.types")
    mock_langgraph_types.Command = MagicMock()
    mock_langgraph_types.interrupt = MagicMock()
    mock_langgraph.runtime = mock_langgraph_runtime
    mock_langgraph.types = mock_langgraph_types
    sys.modules.setdefault("langgraph", mock_langgraph)
    sys.modules.setdefault("langgraph.runtime", mock_langgraph_runtime)
    sys.modules.setdefault("langgraph.types", mock_langgraph_types)

    sys.modules.setdefault("dotenv", MagicMock())
    sys.modules.setdefault("pydantic", MagicMock())
    sys.modules.setdefault("tinyfish", MagicMock())
    sys.modules.setdefault("langchain_anthropic", MagicMock())
    sys.modules.setdefault("langchain_openai", MagicMock())
    _install_optional_dep_stubs()

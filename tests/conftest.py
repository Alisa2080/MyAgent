from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def stub_heavy_deps(monkeypatch):
    import types as types_

    # langchain stubs - must use types.ModuleType so sub-imports work
    mock_langchain = types_.ModuleType("langchain")
    mock_langchain.tools = MagicMock()
    mock_langchain_agents = types_.ModuleType("langchain.agents")
    mock_langchain_agents.middleware = types_.ModuleType("langchain.agents.middleware")
    mock_langchain.agents = mock_langchain_agents
    mock_langchain_core = types_.ModuleType("langchain_core")
    mock_langchain_core.messages = MagicMock()
    monkeypatch.setitem(sys.modules, "langchain", mock_langchain)
    monkeypatch.setitem(sys.modules, "langchain.agents", mock_langchain_agents)
    monkeypatch.setitem(sys.modules, "langchain.agents.middleware", mock_langchain_agents.middleware)
    monkeypatch.setitem(sys.modules, "langchain.tools", mock_langchain.tools)
    monkeypatch.setitem(sys.modules, "langchain_core", mock_langchain_core)
    monkeypatch.setitem(sys.modules, "langchain_core.messages", mock_langchain_core.messages)

    # langgraph stubs
    mock_langgraph = types_.ModuleType("langgraph")
    mock_langgraph_types = types_.ModuleType("langgraph.types")
    mock_langgraph_types.Command = MagicMock()
    mock_langgraph.types = mock_langgraph_types
    monkeypatch.setitem(sys.modules, "langgraph", mock_langgraph)
    monkeypatch.setitem(sys.modules, "langgraph.types", mock_langgraph_types)

    monkeypatch.setitem(sys.modules, "dotenv", MagicMock())
    monkeypatch.setitem(sys.modules, "pydantic", MagicMock())
    monkeypatch.setitem(sys.modules, "tinyfish", MagicMock())
    monkeypatch.setitem(sys.modules, "langchain_anthropic", MagicMock())
    monkeypatch.setitem(sys.modules, "langchain_openai", MagicMock())
    monkeypatch.setitem(sys.modules, "dateutil", MagicMock())
    monkeypatch.setitem(sys.modules, "dateutil.parser", MagicMock())


def pytest_configure(config):
    import types as types_

    # Stub heavy deps before any imports happen
    mock_langchain = types_.ModuleType("langchain")
    mock_langchain.tools = MagicMock()
    mock_langchain_agents = types_.ModuleType("langchain.agents")
    mock_langchain_agents.middleware = types_.ModuleType("langchain.agents.middleware")
    mock_langchain.agents = mock_langchain_agents
    mock_langchain_core = types_.ModuleType("langchain_core")
    mock_langchain_core.messages = MagicMock()
    sys.modules.setdefault("langchain", mock_langchain)
    sys.modules.setdefault("langchain.agents", mock_langchain_agents)
    sys.modules.setdefault("langchain.agents.middleware", mock_langchain_agents.middleware)
    sys.modules.setdefault("langchain.tools", mock_langchain.tools)
    sys.modules.setdefault("langchain_core", mock_langchain_core)
    sys.modules.setdefault("langchain_core.messages", mock_langchain_core.messages)

    mock_langgraph = types_.ModuleType("langgraph")
    mock_langgraph_types = types_.ModuleType("langgraph.types")
    mock_langgraph_types.Command = MagicMock()
    mock_langgraph.types = mock_langgraph_types
    sys.modules.setdefault("langgraph", mock_langgraph)
    sys.modules.setdefault("langgraph.types", mock_langgraph_types)

    sys.modules.setdefault("dotenv", MagicMock())
    sys.modules.setdefault("pydantic", MagicMock())
    sys.modules.setdefault("tinyfish", MagicMock())
    sys.modules.setdefault("langchain_anthropic", MagicMock())
    sys.modules.setdefault("langchain_openai", MagicMock())
    sys.modules.setdefault("dateutil", MagicMock())
    sys.modules.setdefault("dateutil.parser", MagicMock())

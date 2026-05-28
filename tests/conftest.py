from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def stub_heavy_deps(monkeypatch):
    mock_langchain = MagicMock()
    mock_langchain.agents = MagicMock()
    mock_langchain.tools = MagicMock()
    mock_langchain_core = MagicMock()
    mock_langchain_core.messages = MagicMock()
    monkeypatch.setitem(sys.modules, "langchain", mock_langchain)
    monkeypatch.setitem(sys.modules, "langchain.agents", mock_langchain.agents)
    monkeypatch.setitem(sys.modules, "langchain.tools", mock_langchain.tools)
    monkeypatch.setitem(sys.modules, "langchain_core", mock_langchain_core)
    monkeypatch.setitem(sys.modules, "langchain_core.messages", mock_langchain_core.messages)
    monkeypatch.setitem(sys.modules, "dotenv", MagicMock())

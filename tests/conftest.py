from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def stub_langchain(monkeypatch):
    mock = MagicMock()
    mock.agents = MagicMock()
    monkeypatch.setitem(sys.modules, "langchain", mock)
    monkeypatch.setitem(sys.modules, "langchain.agents", mock.agents)

from __future__ import annotations

import sys
import types
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


import os

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")


def _fake_tool(name=None, *tool_args, **tool_kwargs):
    def decorate(func):
        func.name = str(name or getattr(func, "__name__", "tool"))
        func.func = func
        signature = inspect.signature(func)
        injected = {
            param_name
            for param_name in signature.parameters
            if param_name == "runtime"
        }
        func._injected_args_keys = injected
        func.args = {
            param_name: {}
            for param_name in signature.parameters
            if param_name not in injected
        }
        return func

    if callable(name) and not tool_args and not tool_kwargs:
        func = name
        name = getattr(func, "__name__", "tool")
        return decorate(func)
    return decorate


def _install_langchain_stubs(monkeypatch=None):
    import types as types_

    if _is_real_package_installed("langchain") and _is_real_package_installed("langchain_core"):
        _clear_stubbed_modules("langchain")
        _clear_stubbed_modules("langchain_core")
        return

    existing_tools = sys.modules.get("langchain.tools")
    existing_messages = sys.modules.get("langchain_core.messages")
    if (
        existing_tools is not None
        and existing_messages is not None
        and hasattr(existing_tools, "tool")
        and hasattr(existing_messages, "ToolMessage")
    ):
        return

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
    class ToolMessage(SimpleNamespace):
        pass

    mock_langchain_core.messages.ToolMessage = ToolMessage
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


def _is_real_package_installed(name: str) -> bool:
    """Return True if *name* is a real importable package (not a MagicMock stub).

    ``importlib.util.find_spec`` raises ``ValueError`` when the module is
    already in ``sys.modules`` but its ``__spec__`` is ``None`` — which is
    the case for MagicMock stubs.  We guard against that here.
    """
    import importlib.util
    existing = sys.modules.get(name)
    if existing is not None:
        spec = getattr(existing, "__spec__", None)
        if spec is not None and getattr(spec, "origin", None) is not None:
            return True
        del sys.modules[name]
        real_spec = None
        try:
            real_spec = importlib.util.find_spec(name)
        finally:
            if real_spec is None:
                sys.modules[name] = existing
        return real_spec is not None
    return importlib.util.find_spec(name) is not None


def _clear_stubbed_modules(prefix: str) -> None:
    for name, module in list(sys.modules.items()):
        if name != prefix and not name.startswith(prefix + "."):
            continue
        spec = getattr(module, "__spec__", None)
        if spec is None or getattr(spec, "origin", None) is None:
            sys.modules.pop(name, None)


def _install_optional_dep_stubs(monkeypatch=None):
    """Install lightweight MagicMock stubs for optional packages that are not
    installed in the current environment.  Skips stubbing when the real package
    is already installed so that tests which exercise the actual functionality
    (e.g. cron-expression parsing via ``croniter``) continue to work.
    """
    setter = monkeypatch.setitem if monkeypatch is not None else sys.modules.setdefault
    if not _is_real_package_installed("dateutil"):
        if monkeypatch is not None:
            setter(sys.modules, "dateutil", MagicMock())
            setter(sys.modules, "dateutil.parser", MagicMock())
        else:
            setter("dateutil", MagicMock())
            setter("dateutil.parser", MagicMock())


def _install_langgraph_stubs(monkeypatch=None):
    if _is_real_package_installed("langgraph"):
        _clear_stubbed_modules("langgraph")
        return

    import types as types_

    setter = monkeypatch.setitem if monkeypatch is not None else sys.modules.setdefault
    mock_langgraph = types_.ModuleType("langgraph")
    mock_langgraph.__path__ = []
    mock_langgraph_runtime = types_.ModuleType("langgraph.runtime")
    mock_langgraph_runtime.Runtime = MagicMock()
    mock_langgraph_types = types_.ModuleType("langgraph.types")

    class Command:
        def __init__(self, *, resume=None, **kwargs):
            self.resume = resume
            for key, value in kwargs.items():
                setattr(self, key, value)

    mock_langgraph_types.Command = Command
    mock_langgraph_types.interrupt = MagicMock()
    mock_langgraph.runtime = mock_langgraph_runtime
    mock_langgraph.types = mock_langgraph_types
    if monkeypatch is not None:
        setter(sys.modules, "langgraph", mock_langgraph)
        setter(sys.modules, "langgraph.runtime", mock_langgraph_runtime)
        setter(sys.modules, "langgraph.types", mock_langgraph_types)
    else:
        setter("langgraph", mock_langgraph)
        setter("langgraph.runtime", mock_langgraph_runtime)
        setter("langgraph.types", mock_langgraph_types)


def _install_module_stub(name: str, monkeypatch=None) -> None:
    if _is_real_package_installed(name):
        _clear_stubbed_modules(name)
        return
    setter = monkeypatch.setitem if monkeypatch is not None else sys.modules.setdefault
    if monkeypatch is not None:
        setter(sys.modules, name, MagicMock())
    else:
        setter(name, MagicMock())


@pytest.fixture(autouse=True)
def stub_heavy_deps(monkeypatch):
    _install_langchain_stubs(monkeypatch)
    _install_langgraph_stubs(monkeypatch)
    for module_name in (
        "dotenv",
        "pydantic",
        "tinyfish",
        "langchain_anthropic",
        "langchain_openai",
    ):
        _install_module_stub(module_name, monkeypatch)
    _install_optional_dep_stubs(monkeypatch)


def pytest_configure(config):
    # Stub heavy deps before any imports happen
    _install_langchain_stubs()
    _install_langgraph_stubs()
    for module_name in (
        "dotenv",
        "pydantic",
        "tinyfish",
        "langchain_anthropic",
        "langchain_openai",
    ):
        _install_module_stub(module_name)
    _install_optional_dep_stubs()

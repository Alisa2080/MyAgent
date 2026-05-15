import importlib
import sys
import types
from types import SimpleNamespace

from agent_core.session_context import hermes_task_id_from_thread_id


def _install_langchain_and_pydantic_stubs(monkeypatch):
    langchain = types.ModuleType("langchain")
    tools = types.ModuleType("langchain.tools")

    def tool(name=None, args_schema=None):
        def decorate(func):
            func.name = name or func.__name__
            func.args_schema = args_schema
            return func
        return decorate

    class ToolRuntime:
        pass

    tools.tool = tool
    tools.ToolRuntime = ToolRuntime
    langchain.tools = tools

    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        pass

    def Field(*args, **kwargs):
        return None

    pydantic.BaseModel = BaseModel
    pydantic.Field = Field

    monkeypatch.setitem(sys.modules, "langchain", langchain)
    monkeypatch.setitem(sys.modules, "langchain.tools", tools)
    monkeypatch.setitem(sys.modules, "pydantic", pydantic)


def _import_shell(monkeypatch):
    _install_langchain_and_pydantic_stubs(monkeypatch)
    sys.modules.pop("agent_tools.shell", None)
    return importlib.import_module("agent_tools.shell")


def test_execute_command_forwards_runtime_thread_as_task_id(monkeypatch):
    shell = _import_shell(monkeypatch)
    calls = []

    def fake_run_foreground_command(command, *, workdir, timeout=120, task_id="default"):
        calls.append(
            {
                "command": command,
                "workdir": workdir,
                "timeout": timeout,
                "task_id": task_id,
            }
        )
        return {"output": "ok\n", "exit_code": 0, "error": None}

    monkeypatch.setattr(shell, "run_foreground_command", fake_run_foreground_command)
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-abc"),
        config={"configurable": {"thread_id": "ignored-config-thread"}},
    )

    result = shell._execute_command_impl("python -c \"print('ok')\"", runtime=runtime)

    assert '"ok": true' in result
    assert calls[0]["task_id"] == hermes_task_id_from_thread_id("thread-abc")


def test_execute_command_falls_back_to_default_without_runtime(monkeypatch):
    shell = _import_shell(monkeypatch)
    calls = []

    def fake_run_foreground_command(command, *, workdir, timeout=120, task_id="default"):
        calls.append(task_id)
        return {"output": "ok\n", "exit_code": 0, "error": None}

    monkeypatch.setattr(shell, "run_foreground_command", fake_run_foreground_command)

    result = shell._execute_command_impl("python -c \"print('ok')\"")

    assert '"ok": true' in result
    assert calls == ["default"]

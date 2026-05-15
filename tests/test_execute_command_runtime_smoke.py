import json
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from agent_core.session_context import hermes_task_id_from_thread_id

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None or importlib.util.find_spec("pydantic") is None,
    reason="LangChain and Pydantic are required for real execute_command smoke tests.",
)


def _decode_tool_result(raw: str) -> dict:
    return json.loads(raw)


def test_execute_command_uses_runtime_thread_with_real_adapter():
    from agent_tools.shell import execute_command

    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="smoke-thread-1"),
        config={"configurable": {"thread_id": "ignored"}},
    )

    raw = execute_command(
        f"{sys.executable} -c \"print('shell-smoke-ok')\"",
        runtime=runtime,
    )
    payload = _decode_tool_result(raw)

    assert payload["ok"] is True
    assert payload["data"]["exit_code"] == 0
    assert "shell-smoke-ok" in payload["data"]["output"]
    assert hermes_task_id_from_thread_id("smoke-thread-1").startswith("lg_")


def test_execute_command_still_blocks_dangerous_commands_before_hermes():
    from agent_tools.shell import execute_command

    raw = execute_command("sudo ls")
    payload = _decode_tool_result(raw)

    assert payload["ok"] is False
    assert payload["code"] == "blocked_command"

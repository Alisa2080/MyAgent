from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.session_context import RuntimeContext

DEFAULT_DOCKER_IMAGE = "nikolaik/python-nodejs:python3.11-nodejs20"


def _docker_binary() -> str | None:
    return shutil.which("docker") or shutil.which("podman")


def _docker_ready() -> bool:
    binary = _docker_binary()
    if not binary:
        return False
    image = DEFAULT_DOCKER_IMAGE
    try:
        info = subprocess.run(
            [binary, "info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if info.returncode != 0:
            return False
        inspect = subprocess.run(
            [binary, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if inspect.returncode != 0:
            return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _runtime(thread_id: str):
    return SimpleNamespace(
        config={"configurable": {"thread_id": thread_id}},
        tool_call_id=f"{thread_id}-call",
    )


@pytest.fixture
def docker_runtime(monkeypatch, tmp_path):
    if os.environ.get("CODE_EXECUTION_DOCKER_TESTS") != "1":
        pytest.skip("Set CODE_EXECUTION_DOCKER_TESTS=1 to run Docker integration tests")
    if not _docker_ready():
        pytest.skip("Docker or Podman daemon is required")

    runtime = _runtime(f"code-execution-docker-{tmp_path.name}")
    task_id = RuntimeContext.from_runtime(runtime).task_id
    sandbox_dir = tmp_path / "sandboxes"

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(sandbox_dir))
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE)

    yield runtime, task_id, sandbox_dir

    from agent_tools.terminal_toolkit.terminal_tool import cleanup_vm

    cleanup_vm(task_id)


def test_execute_code_docker_simple_script(docker_runtime):
    from agent_tools.public.code_execution import execute_code_impl

    runtime, _, _ = docker_runtime
    result = execute_code_impl(
        code='print("docker ok")',
        runtime=runtime,
        enabled_tools=[],
        include_web=False,
        timeout_seconds=60,
    )

    assert result.artifact["ok"] is True
    assert "docker ok" in result.artifact["data"]["stdout"]
    assert result.artifact["meta"]["backend"] == "docker"


def test_execute_code_docker_file_rpc_read_file(docker_runtime):
    from agent_tools.public.code_execution import execute_code_impl

    runtime, task_id, sandbox_dir = docker_runtime
    host_workspace = sandbox_dir / "docker" / task_id / "workspace"
    host_workspace.mkdir(parents=True, exist_ok=True)
    (host_workspace / "rpc-source.txt").write_text("file rpc works\n", encoding="utf-8")

    result = execute_code_impl(
        code=(
            "from hermes_tools import read_file\n"
            "result = read_file('/workspace/rpc-source.txt', limit=5)\n"
            "print(result['ok'])\n"
            "print(result['data'])\n"
        ),
        runtime=runtime,
        enabled_tools=["read_file"],
        include_web=False,
        timeout_seconds=90,
    )

    assert result.artifact["ok"] is True
    assert "True" in result.artifact["data"]["stdout"]
    assert "file rpc works" in result.artifact["data"]["stdout"]


def test_execute_code_docker_timeout_cleans_run_dir(docker_runtime):
    from agent_tools.public.code_execution import execute_code_impl

    runtime, task_id, sandbox_dir = docker_runtime
    result = execute_code_impl(
        code="import time\ntime.sleep(30)",
        runtime=runtime,
        enabled_tools=[],
        include_web=False,
        timeout_seconds=1,
    )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "timeout"
    run_root = sandbox_dir / "docker" / task_id / "workspace" / ".code_execution"
    assert not list(run_root.glob("*"))

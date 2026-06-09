from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_core.session_context import RuntimeContext

from agent_tools.code_execution.config import CodeExecutionConfig
from agent_tools.code_execution.dispatch import CodeExecutionDispatcher, failure_payload
from agent_tools.code_execution.local_rpc import CodeExecutionRpcServer
from agent_tools.code_execution.stubs import generate_uds_tools_module, visible_sandbox_tools

SAFE_ENV_EXACT = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SHELL",
    "LOGNAME",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
})
SAFE_ENV_PREFIXES = ("LC_", "XDG_", "CONDA_")
SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PASSWD", "AUTH")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_CHILD_POLICY_SITE_CUSTOMIZE = r'''
from __future__ import annotations

import builtins
import os
import socket
import sys
import sysconfig

_SANDBOX_ROOT = os.path.realpath(os.environ.get("CODE_EXECUTION_SANDBOX_ROOT", ""))
_RPC_SOCKET = os.environ.get("CODE_EXECUTION_RPC_SOCKET", "")


def _roots_from_env(name):
    roots = []
    for raw in os.environ.get(name, "").split(os.pathsep):
        if raw:
            roots.append(os.path.realpath(raw))
    return roots


_READ_ROOTS = _roots_from_env("CODE_EXECUTION_ALLOWED_READ_ROOTS")
for _path in (
    sys.prefix,
    getattr(sys, "base_prefix", ""),
    sysconfig.get_path("stdlib") or "",
    sysconfig.get_path("platstdlib") or "",
    sysconfig.get_path("purelib") or "",
    sysconfig.get_path("platlib") or "",
):
    if _path:
        _READ_ROOTS.append(os.path.realpath(_path))


def _under(path, roots):
    real = os.path.realpath(path)
    for root in roots:
        if real == root or real.startswith(root + os.sep):
            return True
    return False


def _deny(action):
    raise PermissionError(
        f"execute_code policy denied {action}; use hermes_tools RPC wrappers for project files, terminal, and web access."
    )


def _is_write_open(mode, flags):
    mode_text = str(mode or "")
    if any(m in mode_text for m in ("w", "a", "x", "+")):
        return True
    if isinstance(flags, int):
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    return False


def _guard_open(file, mode="r", flags=0):
    if isinstance(file, int):
        return
    if not isinstance(file, (str, bytes, os.PathLike)):
        return
    path_text = os.fsdecode(file)
    if not path_text:
        return
    if not os.path.isabs(path_text):
        path_text = os.path.join(os.getcwd(), path_text)
    if _is_write_open(mode, flags):
        if _SANDBOX_ROOT and _under(path_text, (_SANDBOX_ROOT,)):
            return
        _deny(f"direct file write to {path_text}")
    if _under(path_text, _READ_ROOTS):
        return
    _deny(f"direct file read from {path_text}")


_original_open = builtins.open


def _policy_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
    _guard_open(file, mode=mode)
    return _original_open(file, mode, buffering, encoding, errors, newline, closefd, opener)


builtins.open = _policy_open


def _audit(event, args):
    if event == "open":
        path, mode, flags = (args + (None, None, 0))[:3]
        _guard_open(path, mode=mode, flags=flags)
    elif event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.posix_spawnp", "pty.spawn"}:
        _deny(event)
    elif event == "socket.connect":
        sock, address = args
        if getattr(sock, "family", None) == socket.AF_UNIX and address == _RPC_SOCKET:
            return
        _deny("direct socket connect")


sys.addaudithook(_audit)
'''


def _is_safe_env_name(name: str) -> bool:
    upper = name.upper()
    if any(marker in upper for marker in SECRET_SUBSTRINGS):
        return False
    return name in SAFE_ENV_EXACT or any(name.startswith(prefix) for prefix in SAFE_ENV_PREFIXES)


def safe_child_env(config: CodeExecutionConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if _is_safe_env_name(name)}
    if config.env_allowlist:
        for name in config.env_allowlist:
            if name not in env and name in os.environ:
                env[name] = os.environ[name]
    env.update(extra or {})
    return env


def sanitize_output(text: str, *, limit: int) -> tuple[str, bool]:
    from agent_tools.public.code_execution import redact_sensitive_text
    cleaned = ANSI_RE.sub("", str(text or ""))
    cleaned = redact_sensitive_text(cleaned)
    if len(cleaned) <= limit:
        return cleaned, False
    return cleaned[:limit] + "\n[truncated]", True


def _result_data(stdout: str, stderr: str, returncode: int, *, stdout_truncated: bool, stderr_truncated: bool) -> dict:
    return {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


def docker_host_workspace_dir(env: object) -> str | None:
    if not bool(getattr(env, "_persistent", False)):
        return None
    workspace = getattr(env, "host_workspace_dir", None)
    if workspace:
        return str(workspace)
    workspace = getattr(env, "_workspace_dir", None)
    if workspace:
        return str(workspace)
    return None


def _backend_for_runtime(runtime: object) -> tuple[str, str]:
    from agent_tools.public.code_execution import get_backend_path_context
    runtime_context = RuntimeContext.from_runtime(runtime)
    task_id = runtime_context.task_id or "default"
    backend = get_backend_path_context(task_id)
    return str(backend.env_type or "local"), task_id


class LocalUdsRunner:
    def run(
        self,
        *,
        code: str,
        runtime: object,
        enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
        include_web: bool,
        config: CodeExecutionConfig,
        task_id: str,
    ) -> object:
        from agent_tools.shared.tool_result import tool_failure, tool_success

        if platform.system() == "Windows":
            return tool_failure("execute_code", "execute_code is not available on Windows.", code="unsupported_platform", runtime=runtime)

        runtime_env_type, _ = _backend_for_runtime(runtime)
        if runtime_env_type != "local":
            return tool_failure(
                "execute_code",
                f"execute_code only supports the local terminal backend; current backend is {runtime_env_type}.",
                code="unsupported_backend",
                runtime=runtime,
                data={"env_type": runtime_env_type},
            )

        visible_tools = visible_sandbox_tools(enabled_tools, include_web=include_web)
        temp_dir = Path(tempfile.mkdtemp(prefix="code-execution-"))
        server: CodeExecutionRpcServer | None = None

        try:
            script_path = temp_dir / "script.py"
            stub_path = temp_dir / "hermes_tools.py"
            policy_path = temp_dir / "sitecustomize.py"
            socket_path = str(temp_dir / "rpc.sock")

            script_path.write_text(code, encoding="utf-8")
            stub_path.write_text(generate_uds_tools_module(visible_tools), encoding="utf-8")
            policy_path.write_text(_CHILD_POLICY_SITE_CUSTOMIZE, encoding="utf-8")

            dispatcher = CodeExecutionDispatcher(runtime=runtime, visible_tools=visible_tools, max_tool_calls=config.max_tool_calls)
            server = CodeExecutionRpcServer(socket_path=socket_path, dispatcher=dispatcher)
            server.start()

            cwd = os.getcwd() if config.mode == "project" else str(temp_dir)
            env = safe_child_env(config, {
                "CODE_EXECUTION_RPC_SOCKET": socket_path,
                "CODE_EXECUTION_SANDBOX_ROOT": str(temp_dir),
                "CODE_EXECUTION_ALLOWED_READ_ROOTS": str(temp_dir),
                "PYTHONPATH": str(temp_dir),
            })

            proc = subprocess.Popen(
                [sys.executable, str(script_path)],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )

            try:
                stdout_raw, stderr_raw = proc.communicate(timeout=config.timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    proc.kill()
                stdout_raw, stderr_raw = proc.communicate(timeout=1)
                stdout, stdout_truncated = sanitize_output(stdout_raw, limit=config.stdout_limit_chars)
                stderr, stderr_truncated = sanitize_output(stderr_raw, limit=config.stderr_limit_chars)
                return tool_failure(
                    "execute_code",
                    f"execute_code timed out after {config.timeout_seconds} seconds.",
                    code="timeout",
                    data=_result_data(stdout, stderr, -1, stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated),
                    runtime=runtime,
                )

            stdout, stdout_truncated = sanitize_output(stdout_raw, limit=config.stdout_limit_chars)
            stderr, stderr_truncated = sanitize_output(stderr_raw, limit=config.stderr_limit_chars)
            data = _result_data(stdout, stderr, int(proc.returncode or 0), stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated)

            if proc.returncode:
                return tool_failure("execute_code", f"Python script exited with code {proc.returncode}.", code="child_failed", data=data, runtime=runtime)

            return tool_success("execute_code", message="Code executed.", data=data, runtime=runtime, content=stdout or "Code executed.")

        finally:
            if server is not None:
                server.close()
            shutil.rmtree(temp_dir, ignore_errors=True)


def execute_code_with_backend(
    *,
    code: str,
    runtime: object,
    enabled_tools: list[str] | tuple[str, ...] | set[str] | None,
    include_web: bool,
    backend_env_type: str | None = None,
    config: CodeExecutionConfig | None = None,
) -> object:
    resolved_config = config or CodeExecutionConfig.from_sources()
    env_type, task_id = _backend_for_runtime(runtime)
    if backend_env_type is not None:
        env_type = backend_env_type
    if env_type == "local":
        return LocalUdsRunner().run(
            code=code,
            runtime=runtime,
            enabled_tools=enabled_tools,
            include_web=include_web,
            config=resolved_config,
            task_id=task_id,
        )
    if env_type == "docker":
        from agent_tools.shared.tool_result import tool_failure
        return tool_failure(
            "execute_code",
            "Docker execute_code support is not implemented yet.",
            code="unsupported_backend",
            data={"env_type": "docker"},
            runtime=runtime,
        )
    from agent_tools.shared.tool_result import tool_failure
    return tool_failure(
        "execute_code",
        f"execute_code does not support the {env_type} terminal backend.",
        code="unsupported_backend",
        data={"env_type": env_type},
        runtime=runtime,
    )

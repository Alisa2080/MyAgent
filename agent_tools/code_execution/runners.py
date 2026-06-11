from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_core.session_context import RuntimeContext

from agent_tools.code_execution.config import CodeExecutionConfig
from agent_tools.code_execution.child_policy import CHILD_POLICY_SITE_CUSTOMIZE as _CHILD_POLICY_SITE_CUSTOMIZE
from agent_tools.code_execution.dispatch import CodeExecutionDispatcher
from agent_tools.code_execution.local_rpc import CodeExecutionRpcServer
from agent_tools.code_execution.runner_env import (
    SAFE_ENV_EXACT,
    SAFE_ENV_PREFIXES,
    SECRET_SUBSTRINGS,
    _is_safe_env_name,
    _join_roots,
    safe_child_env,
)
from agent_tools.code_execution.runner_output import (
    ANSI_RE,
    _result_data,
    sanitize_output,
    sanitize_process_output,
)
from agent_tools.code_execution.safety import redact_code_execution_text
from agent_tools.code_execution.stubs import generate_uds_tools_module, visible_sandbox_tools

def _execution_scope_for_runtime(runtime: object):
    """Return a context manager that registers the current thread as the active execution for the runtime's thread_id."""
    try:
        context = RuntimeContext.from_runtime(runtime)
        thread_id = getattr(context, "thread_id", None)
    except Exception:
        thread_id = None
    if not thread_id:
        from contextlib import nullcontext
        return nullcontext()
    from agent_core.terminal_lifecycle import terminal_execution_scope
    return terminal_execution_scope(thread_id)


def _terminate_local_process_group(proc: subprocess.Popen, *, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        if sig == 9:
            proc.kill()
        else:
            proc.terminate()


def _communicate_local_process(
    proc: subprocess.Popen,
    *,
    timeout_seconds: int,
) -> tuple[str, str, str | None]:
    """Wait for a local child while honoring terminal toolkit interruption."""
    try:
        from agent_tools.terminal_toolkit.interrupt import is_interrupted
    except Exception:
        is_interrupted = lambda: False

    deadline = time_monotonic() + timeout_seconds
    while True:
        if is_interrupted():
            _terminate_local_process_group(proc, sig=15)
            try:
                stdout_raw, stderr_raw = proc.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                _terminate_local_process_group(proc, sig=9)
                stdout_raw, stderr_raw = proc.communicate(timeout=1)
            return stdout_raw, stderr_raw, "interrupted"

        remaining = deadline - time_monotonic()
        if remaining <= 0:
            _terminate_local_process_group(proc, sig=9)
            stdout_raw, stderr_raw = proc.communicate(timeout=1)
            return stdout_raw, stderr_raw, "timeout"

        try:
            stdout_raw, stderr_raw = proc.communicate(timeout=min(0.2, remaining))
            return stdout_raw, stderr_raw, None
        except subprocess.TimeoutExpired:
            continue


def time_monotonic() -> float:
    import time

    return time.monotonic()


def _project_python_for_cwd(cwd: str) -> str:
    for env_var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        prefix = os.environ.get(env_var)
        if prefix:
            candidate = Path(prefix) / "bin" / "python"
            if candidate.exists():
                return str(candidate)
    for dirname in (".venv", "venv"):
        candidate = Path(cwd) / dirname / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    return sys.executable


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


def _docker_child_env(
    config: CodeExecutionConfig,
    *,
    container_run_dir: str,
    container_rpc_dir: str,
    allowed_read_roots: str,
) -> dict[str, str]:
    env = {
        "HOME": "/root",
        "USER": "root",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    for name in config.env_allowlist:
        if config.env_name_allowed(name) and name in os.environ:
            env[name] = os.environ[name]
    env.update(
        {
            "CODE_EXECUTION_RPC_DIR": container_rpc_dir,
            "CODE_EXECUTION_RPC_TIMEOUT_SECONDS": str(min(30, config.timeout_seconds)),
            "CODE_EXECUTION_RPC_MAX_RESPONSE_BYTES": "1000000",
            "CODE_EXECUTION_SANDBOX_ROOT": container_run_dir,
            "CODE_EXECUTION_ALLOWED_READ_ROOTS": allowed_read_roots,
            "PYTHONPATH": container_run_dir,
        }
    )
    return env


def _terminate_docker_child(env: object, container_pid_path: str) -> None:
    quoted_pid_path = shlex.quote(container_pid_path)
    command = (
        f"if [ -f {quoted_pid_path} ]; then "
        f"pid=$(cat {quoted_pid_path}); "
        "kill -TERM -- -\"$pid\" 2>/dev/null || true; "
        "sleep 0.2; "
        "kill -KILL -- -\"$pid\" 2>/dev/null || true; "
        f"rm -f {quoted_pid_path}; "
        "fi"
    )
    result = env.execute(command, timeout=5)
    if int(result.get("returncode", 0) or 0) != 0:
        raise RuntimeError(str(result.get("output") or "Docker child cleanup failed."))


def _backend_for_runtime(runtime: object) -> tuple[str, str]:
    from agent_tools.file_toolkit.backend_paths import get_backend_path_context

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
                meta={"backend": "local", "mode": config.mode, "warnings": list(config.warnings)},
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

            backend_ctx = None
            try:
                from agent_tools.file_toolkit.backend_paths import get_backend_path_context
                backend_ctx = get_backend_path_context(task_id)
            except Exception:
                backend_ctx = None
            cwd = (
                backend_ctx.cwd
                if config.mode == "project" and backend_ctx is not None
                else str(temp_dir)
            )
            python_executable = (
                _project_python_for_cwd(cwd)
                if config.mode == "project" and backend_ctx is not None and backend_ctx.env_type == "local"
                else sys.executable
            )
            allowed_read_roots = _join_roots(
                str(temp_dir),
                cwd if config.mode == "project" else None,
            )
            dispatcher = CodeExecutionDispatcher(
                runtime=runtime,
                visible_tools=visible_tools,
                max_tool_calls=config.max_tool_calls,
                terminal_default_workdir=cwd,
            )
            server = CodeExecutionRpcServer(socket_path=socket_path, dispatcher=dispatcher)
            server.start()
            env = safe_child_env(config, {
                "CODE_EXECUTION_RPC_SOCKET": socket_path,
                "CODE_EXECUTION_SANDBOX_ROOT": str(temp_dir),
                "CODE_EXECUTION_ALLOWED_READ_ROOTS": allowed_read_roots,
                "PYTHONPATH": str(temp_dir),
            })

            proc = subprocess.Popen(
                [python_executable, str(script_path)],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )

            try:
                with _execution_scope_for_runtime(runtime):
                    stdout_raw, stderr_raw, stop_reason = _communicate_local_process(
                        proc,
                        timeout_seconds=config.timeout_seconds,
                    )
            except (KeyboardInterrupt, SystemExit):
                _terminate_local_process_group(proc, sig=9)
                raise

            if stop_reason == "timeout":
                data = sanitize_process_output(
                    stdout_raw=stdout_raw,
                    stderr_raw=stderr_raw,
                    returncode=-1,
                    config=config,
                )
                return tool_failure(
                    "execute_code",
                    f"execute_code timed out after {config.timeout_seconds} seconds.",
                    code="timeout",
                    data=data,
                    runtime=runtime,
                    meta={"backend": "local", "mode": config.mode, "warnings": list(config.warnings)},
                )
            if stop_reason == "interrupted":
                data = sanitize_process_output(
                    stdout_raw=stdout_raw,
                    stderr_raw=stderr_raw,
                    returncode=130,
                    config=config,
                )
                return tool_failure(
                    "execute_code",
                    "execute_code was interrupted.",
                    code="interrupted",
                    data=data,
                    runtime=runtime,
                    meta={"backend": "local", "mode": config.mode, "warnings": list(config.warnings)},
                )

            data = sanitize_process_output(
                stdout_raw=stdout_raw,
                stderr_raw=stderr_raw,
                returncode=int(proc.returncode or 0),
                config=config,
            )

            if proc.returncode:
                return tool_failure("execute_code", f"Python script exited with code {proc.returncode}.", code="child_failed", data=data, runtime=runtime, meta={"backend": "local", "mode": config.mode, "warnings": list(config.warnings)})

            return tool_success(
                "execute_code",
                message="Code executed.",
                data=data,
                runtime=runtime,
                content=data["stdout"] or "Code executed.",
                meta={
                    "backend": "local",
                    "mode": config.mode,
                    "warnings": list(config.warnings),
                },
            )

        finally:
            if server is not None:
                server.close()
            shutil.rmtree(temp_dir, ignore_errors=True)


class DockerFileRpcRunner:
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

        import uuid

        from agent_tools.code_execution.file_rpc import FileRpcBridge
        from agent_tools.code_execution.stubs import generate_file_rpc_tools_module, visible_sandbox_tools
        from agent_tools.terminal_toolkit.terminal_tool import get_or_create_active_env

        if platform.system() == "Windows":
            return tool_failure(
                "execute_code",
                "execute_code is not available on Windows.",
                code="unsupported_platform",
                runtime=runtime,
            )

        run_id = uuid.uuid4().hex
        try:
            env = get_or_create_active_env(task_id=task_id, timeout=config.timeout_seconds)
        except Exception as exc:
            return tool_failure(
                "execute_code",
                redact_code_execution_text(
                    f"Failed to acquire Docker execution environment: {type(exc).__name__}: {exc}"
                ),
                code="docker_execution_failed",
                data={"env_type": "docker"},
                meta={"backend": "docker", "mode": config.mode, "warnings": list(config.warnings)},
                runtime=runtime,
            )

        host_workspace = docker_host_workspace_dir(env)
        if host_workspace is None:
            return tool_failure(
                "execute_code",
                (
                    "Docker execute_code requires terminal toolkit persistent /workspace. "
                    "Configure TERMINAL_CONTAINER_PERSISTENT=true and restart the terminal environment."
                ),
                code="docker_workspace_unavailable",
                data={"env_type": "docker"},
                meta={"backend": "docker", "mode": config.mode, "warnings": list(config.warnings)},
                runtime=runtime,
            )

        container_workspace = str(getattr(env, "container_workspace_dir", "/workspace") or "/workspace")
        host_run_dir = Path(host_workspace) / ".code_execution" / run_id
        container_run_dir = f"{container_workspace.rstrip('/')}/.code_execution/{run_id}"
        host_rpc_dir = host_run_dir / "rpc"
        container_rpc_dir = f"{container_run_dir}/rpc"
        container_pid_path = f"{container_run_dir}/child.pid"
        host_stdout_path = host_run_dir / "stdout.txt"
        host_stderr_path = host_run_dir / "stderr.txt"
        container_stdout_path = f"{container_run_dir}/stdout.txt"
        container_stderr_path = f"{container_run_dir}/stderr.txt"

        visible_tools = visible_sandbox_tools(enabled_tools, include_web=include_web)

        bridge: FileRpcBridge | None = None
        outcome = None
        cleanup_warnings: list[str] = []
        try:
            host_rpc_dir.mkdir(parents=True, exist_ok=True)
            (host_run_dir / "script.py").write_text(code, encoding="utf-8")
            (host_run_dir / "hermes_tools.py").write_text(
                generate_file_rpc_tools_module(visible_tools),
                encoding="utf-8",
            )
            (host_run_dir / "sitecustomize.py").write_text(
                _CHILD_POLICY_SITE_CUSTOMIZE,
                encoding="utf-8",
            )

            python_exec = env.execute(
                "command -v python || command -v python3",
                timeout=10,
            )
            if int(python_exec.get("returncode", 0) or 0) != 0:
                raise RuntimeError("No Python interpreter is available in the Docker environment.")
            python_bin = ""
            for line in (python_exec.get("output") or "").strip().splitlines():
                candidate = line.strip()
                if candidate.startswith("/"):
                    python_bin = candidate
                    break
            if not python_bin:
                raise RuntimeError("Docker Python interpreter path could not be resolved.")

            cwd = container_run_dir if config.mode == "strict" else str(getattr(env, "cwd", "") or container_workspace)
            allowed_read_roots = _join_roots(
                container_run_dir,
                cwd if config.mode == "project" else None,
            )
            dispatcher = CodeExecutionDispatcher(
                runtime=runtime,
                visible_tools=visible_tools,
                max_tool_calls=config.max_tool_calls,
                terminal_default_workdir=cwd,
            )
            bridge = FileRpcBridge(rpc_dir=host_rpc_dir, dispatcher=dispatcher, poll_interval_seconds=0.05)
            bridge.start()

            child_env = _docker_child_env(
                config,
                container_run_dir=container_run_dir,
                container_rpc_dir=container_rpc_dir,
                allowed_read_roots=allowed_read_roots,
            )
            env_args = " ".join(
                shlex.quote(f"{name}={value}")
                for name, value in sorted(child_env.items())
            )
            python_command = " ".join(
                [
                    shlex.quote(python_bin),
                    shlex.quote(f"{container_run_dir}/script.py"),
                ]
            )
            script_cmd = (
                f"setsid env -i {env_args} {python_command} "
                f"> {shlex.quote(container_stdout_path)} "
                f"2> {shlex.quote(container_stderr_path)} & "
                f"child_pid=$!; "
                f"printf '%s' \"$child_pid\" > {shlex.quote(container_pid_path)}; "
                f"wait \"$child_pid\"; status=$?; "
                f"rm -f {shlex.quote(container_pid_path)}; "
                f"exit \"$status\""
            )
            with _execution_scope_for_runtime(runtime):
                execution = env.execute(
                    script_cmd,
                    cwd=cwd,
                    timeout=config.timeout_seconds,
                )

            returncode = int(execution.get("returncode", 0) or 0)
            stdout_raw = host_stdout_path.read_text(encoding="utf-8") if host_stdout_path.exists() else ""
            stderr_raw = host_stderr_path.read_text(encoding="utf-8") if host_stderr_path.exists() else ""
            wrapper_output = str(execution.get("output") or "")
            if wrapper_output:
                if stdout_raw or stderr_raw:
                    if returncode in {124, 130}:
                        stderr_raw = f"{stderr_raw}\n{wrapper_output}" if stderr_raw else wrapper_output
                else:
                    stdout_raw = wrapper_output

            data = sanitize_process_output(
                stdout_raw=stdout_raw,
                stderr_raw=stderr_raw,
                returncode=returncode,
                config=config,
            )
            meta = {
                "backend": "docker",
                "mode": config.mode,
                "warnings": list(config.warnings),
            }

            if returncode == 124:
                outcome = tool_failure(
                    "execute_code",
                    f"execute_code timed out after {config.timeout_seconds} seconds.",
                    code="timeout",
                    data=data,
                    meta=meta,
                    runtime=runtime,
                )
            elif returncode == 130:
                outcome = tool_failure(
                    "execute_code",
                    "execute_code was interrupted.",
                    code="interrupted",
                    data=data,
                    meta=meta,
                    runtime=runtime,
                )
            elif returncode != 0:
                outcome = tool_failure(
                    "execute_code",
                    f"Python script exited with code {returncode}.",
                    code="child_failed",
                    data=data,
                    meta=meta,
                    runtime=runtime,
                )
            else:
                outcome = tool_success(
                    "execute_code",
                    message="Code executed.",
                    data=data,
                    meta=meta,
                    runtime=runtime,
                    content=data["stdout"] or "Code executed.",
                )
        except Exception as exc:
            outcome = tool_failure(
                "execute_code",
                redact_code_execution_text(f"Docker execute_code failed: {type(exc).__name__}: {exc}"),
                code="docker_execution_failed",
                data={"env_type": "docker"},
                meta={"backend": "docker", "mode": config.mode, "warnings": list(config.warnings)},
                runtime=runtime,
            )

        finally:
            try:
                _terminate_docker_child(env, container_pid_path)
            except Exception as exc:
                cleanup_warnings.append(
                    redact_code_execution_text(f"Failed to terminate Docker child process: {exc}")
                )
            if bridge is not None:
                bridge.close()
                if bridge.is_alive():
                    cleanup_warnings.append("File RPC bridge did not stop within the cleanup timeout.")
                if bridge.has_active_dispatches():
                    cleanup_warnings.append("One or more RPC tool calls were still active during cleanup.")
            try:
                shutil.rmtree(host_run_dir)
            except FileNotFoundError:
                pass
            except Exception as exc:
                try:
                    fallback = env.execute(
                        f"rm -rf {shlex.quote(container_run_dir)}",
                        timeout=10,
                    )
                    if int(fallback.get("returncode", 0) or 0) != 0:
                        cleanup_warnings.append(
                            redact_code_execution_text(
                                "Failed to remove code execution directory: "
                                f"host cleanup failed with {exc}; "
                                f"container cleanup failed with {fallback.get('output') or fallback.get('returncode')}"
                            )
                        )
                except Exception as fallback_exc:
                    cleanup_warnings.append(
                        redact_code_execution_text(
                            "Failed to remove code execution directory: "
                            f"host cleanup failed with {exc}; "
                            f"container cleanup failed with {fallback_exc}"
                        )
                    )

        if outcome is None:
            outcome = tool_failure(
                "execute_code",
                "Docker execute_code ended without a result.",
                code="docker_execution_failed",
                runtime=runtime,
            )
        if cleanup_warnings and isinstance(getattr(outcome, "artifact", None), dict):
            meta = outcome.artifact.setdefault("meta", {})
            warnings = meta.setdefault("warnings", [])
            warnings.extend(cleanup_warnings)
        return outcome


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
        return DockerFileRpcRunner().run(
            code=code,
            runtime=runtime,
            enabled_tools=enabled_tools,
            include_web=include_web,
            config=resolved_config,
            task_id=task_id,
        )
    from agent_tools.shared.tool_result import tool_failure
    return tool_failure(
        "execute_code",
        f"execute_code does not support the {env_type} terminal backend.",
        code="unsupported_backend",
        data={"env_type": env_type},
        runtime=runtime,
        meta={"backend": env_type, "mode": resolved_config.mode, "warnings": list(resolved_config.warnings)},
    )

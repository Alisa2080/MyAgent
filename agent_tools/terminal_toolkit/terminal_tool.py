"""Standalone standalone terminal tool implementation."""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from .ansi_strip import strip_ansi
from .approval import check_all_command_guards
from .command_utils import (
    safe_command_preview,
    set_sudo_password_callback,
)
from .environments.docker import DockerEnvironment, find_docker
from .environments.local import LocalEnvironment
from .environments.singularity import (
    SingularityEnvironment,
    _get_scratch_dir,
)
from .environments.ssh import SSHEnvironment
from .process_registry import process_registry
from .redact import redact_sensitive_text
from .terminal_config import (
    _container_config_from_terminal_config,
    _get_env_config,
    _image_for_env_type,
    _parse_env_var,
    _resolve_container_task_id,
    _safe_parse_import_env,
    _ssh_config_from_terminal_config,
)
from .terminal_guidance import (
    INLINE_BACKGROUND_AMP_RE as _INLINE_BACKGROUND_AMP_RE,
    LONG_LIVED_FOREGROUND_PATTERNS as _LONG_LIVED_FOREGROUND_PATTERNS,
    SHELL_LEVEL_BACKGROUND_RE as _SHELL_LEVEL_BACKGROUND_RE,
    TRAILING_BACKGROUND_AMP_RE as _TRAILING_BACKGROUND_AMP_RE,
    WORKDIR_SAFE_RE as _WORKDIR_SAFE_RE,
    _command_requires_pipe_stdin,
    _foreground_background_guidance,
    _handle_sudo_failure,
    _interpret_exit_code,
    _looks_like_help_or_version_command,
    _resolve_notification_flag_conflict,
    _validate_workdir,
)
from .tool_output_limits import get_max_bytes

logger = logging.getLogger(__name__)


FOREGROUND_MAX_TIMEOUT = _safe_parse_import_env("TERMINAL_MAX_FOREGROUND_TIMEOUT", 600, int, "integer")
DISK_USAGE_WARNING_THRESHOLD_GB = _safe_parse_import_env("TERMINAL_DISK_WARNING_GB", 500.0, float, "number")

TERMINAL_TOOL_DESCRIPTION = """Execute shell commands on a Linux environment. Filesystem usually persists between calls.

Prefer dedicated file tools for file reads/writes/searches when your agent has them.
Reserve terminal for builds, installs, git, processes, scripts, network, package managers, and anything that needs a shell.

Foreground (default): commands return immediately when they finish, even if timeout is high.
Background: set background=true to get a session_id for long-running tasks or servers.
Use process(action="poll") for progress checks and process(action="wait") to block until done.
Set pty=true for interactive CLI tools.
"""

_active_environments: Dict[str, Any] = {}
_last_activity: Dict[str, float] = {}
_env_lock = threading.Lock()
_creation_locks: Dict[str, threading.Lock] = {}
_creation_locks_lock = threading.Lock()
_cleanup_thread = None
_cleanup_running = False
_callback_tls = threading.local()


def _get_approval_callback():
    return getattr(_callback_tls, "approval", None)


def set_approval_callback(cb):
    _callback_tls.approval = cb


def _clear_file_ops_cache_for_task(task_id: str):
    try:
        from agent_tools.file_toolkit.file_tools import clear_file_ops_cache

        clear_file_ops_cache(task_id)
    except Exception:
        logger.debug(
            "Failed to clear file operations cache for task %s",
            task_id,
            exc_info=True,
        )


def _check_disk_usage_warning():
    try:
        scratch_dir = _get_scratch_dir()
        total_bytes = 0
        import glob
        from pathlib import Path

        for path in glob.glob(str(scratch_dir / "terminal-*")):
            for f in Path(path).rglob("*"):
                if f.is_file():
                    try:
                        total_bytes += f.stat().st_size
                    except OSError:
                        pass
        total_gb = total_bytes / (1024 ** 3)
        if total_gb > DISK_USAGE_WARNING_THRESHOLD_GB:
            logger.warning(
                "Disk usage (%.1fGB) exceeds threshold (%.0fGB).",
                total_gb,
                DISK_USAGE_WARNING_THRESHOLD_GB,
            )
            return True
        return False
    except Exception as e:
        logger.debug("Disk usage warning check failed: %s", e, exc_info=True)
        return False


def _check_all_guards(command: str, env_type: str) -> dict:
    return check_all_command_guards(command, env_type, approval_callback=_get_approval_callback())


def _tag_environment(env, *, env_type: str, configured_cwd: str, host_cwd: str | None = None):
    """Attach terminal toolkit backend metadata used by file-tool path policy."""
    try:
        setattr(env, "_backend_env_type", env_type)
        setattr(env, "_backend_configured_cwd", configured_cwd)
        setattr(env, "_backend_host_cwd", host_cwd)
    except Exception:
        logger.debug("Failed to tag environment metadata", exc_info=True)
    return env


def _create_environment(
    env_type: str,
    image: str,
    cwd: str,
    timeout: int,
    ssh_config: dict = None,
    container_config: dict = None,
    task_id: str = "default",
    host_cwd: str = None,
):
    cc = container_config or {}
    cpu = cc.get("container_cpu", 1)
    memory = cc.get("container_memory", 5120)
    disk = cc.get("container_disk", 51200)
    persistent = cc.get("container_persistent", True)
    network = cc.get("container_network", True)
    volumes = cc.get("docker_volumes", [])
    docker_forward_env = cc.get("docker_forward_env", [])
    docker_env = cc.get("docker_env", {})

    if env_type == "local":
        return _tag_environment(
            LocalEnvironment(cwd=cwd, timeout=timeout),
            env_type=env_type,
            configured_cwd=cwd,
            host_cwd=host_cwd,
        )
    if env_type == "docker":
        return _tag_environment(
            DockerEnvironment(
                image=image,
                cwd=cwd,
                timeout=timeout,
                cpu=cpu,
                memory=memory,
                disk=disk,
                persistent_filesystem=persistent,
                task_id=task_id,
                volumes=volumes,
                host_cwd=host_cwd,
                auto_mount_cwd=cc.get("docker_mount_cwd_to_workspace", False),
                forward_env=docker_forward_env,
                env=docker_env,
                network=network,
                run_as_host_user=cc.get("docker_run_as_host_user", False),
            ),
            env_type=env_type,
            configured_cwd=cwd,
            host_cwd=host_cwd,
        )
    if env_type == "singularity":
        return _tag_environment(
            SingularityEnvironment(
                image=image,
                cwd=cwd,
                timeout=timeout,
                cpu=cpu,
                memory=memory,
                disk=disk,
                persistent_filesystem=persistent,
                task_id=task_id,
            ),
            env_type=env_type,
            configured_cwd=cwd,
            host_cwd=host_cwd,
        )
    if env_type == "ssh":
        if not ssh_config or not ssh_config.get("host") or not ssh_config.get("user"):
            raise ValueError("SSH environment requires ssh_host and ssh_user to be configured")
        return _tag_environment(
            SSHEnvironment(
                host=ssh_config["host"],
                user=ssh_config["user"],
                port=ssh_config.get("port", 22),
                key_path=ssh_config.get("key", ""),
                cwd=cwd,
                timeout=timeout,
            ),
            env_type=env_type,
            configured_cwd=cwd,
            host_cwd=host_cwd,
        )
    raise ValueError(f"Unknown environment type: {env_type}. Use 'local', 'docker', 'singularity', or 'ssh'")


def _cleanup_inactive_envs(lifetime_seconds: int = 300):
    current_time = time.time()
    for task_id in list(_last_activity.keys()):
        if process_registry.has_active_processes(task_id):
            _last_activity[task_id] = current_time

    envs_to_stop = []
    with _env_lock:
        for task_id, last_time in list(_last_activity.items()):
            if current_time - last_time > lifetime_seconds:
                env = _active_environments.pop(task_id, None)
                _last_activity.pop(task_id, None)
                if env is not None:
                    envs_to_stop.append((task_id, env))
        with _creation_locks_lock:
            for task_id, _ in envs_to_stop:
                _creation_locks.pop(task_id, None)

    for task_id, env in envs_to_stop:
        _clear_file_ops_cache_for_task(task_id)
        try:
            if hasattr(env, "cleanup"):
                env.cleanup()
            elif hasattr(env, "stop"):
                env.stop()
            logger.info("Cleaned up inactive environment for task: %s", task_id)
        except Exception as e:
            logger.warning("Error cleaning up environment for task %s: %s", task_id, e)


def _cleanup_thread_worker():
    while _cleanup_running:
        try:
            config = _get_env_config()
            _cleanup_inactive_envs(config["lifetime_seconds"])
        except Exception as e:
            logger.warning("Error in cleanup thread: %s", e, exc_info=True)
        for _ in range(60):
            if not _cleanup_running:
                break
            time.sleep(1)


def get_or_create_active_env(
    task_id: Optional[str],
    workdir: Optional[str] = None,
    timeout: Optional[int] = None,
):
    """Return the active terminal toolkit environment for *task_id*, creating it if needed.

    This is the single owner of environment config resolution, environment
    creation locks, active-env reuse, and last-activity tracking. ``workdir``
    is accepted for call-site symmetry; environment creation intentionally uses
    the configured backend cwd so existing terminal behavior does not change.
    Callers can pass per-command cwd to ``env.execute(...)`` after acquiring
    the env.
    """
    config = _get_env_config()
    env_type = config["env_type"]
    effective_task_id = _resolve_container_task_id(task_id)
    effective_timeout = timeout if timeout is not None else config["timeout"]
    cwd = config["cwd"]

    _start_cleanup_thread()

    with _env_lock:
        env = _active_environments.get(effective_task_id)
        if env is not None:
            _last_activity[effective_task_id] = time.time()
            return env

    with _creation_locks_lock:
        if effective_task_id not in _creation_locks:
            _creation_locks[effective_task_id] = threading.Lock()
        task_lock = _creation_locks[effective_task_id]

    with task_lock:
        with _env_lock:
            env = _active_environments.get(effective_task_id)
            if env is not None:
                _last_activity[effective_task_id] = time.time()
                return env

        if env_type == "singularity":
            _check_disk_usage_warning()

        new_env = _create_environment(
            env_type=env_type,
            image=_image_for_env_type(config, env_type),
            cwd=cwd,
            timeout=effective_timeout,
            ssh_config=_ssh_config_from_terminal_config(config),
            container_config=_container_config_from_terminal_config(config),
            task_id=effective_task_id,
            host_cwd=config.get("host_cwd"),
        )

        env_to_cleanup = None
        env_to_return = None
        with _env_lock:
            existing = _active_environments.get(effective_task_id)
            if existing is not None:
                _last_activity[effective_task_id] = time.time()
                env_to_cleanup = new_env
                env_to_return = existing
            else:
                _active_environments[effective_task_id] = new_env
                _last_activity[effective_task_id] = time.time()
                return new_env

        if env_to_cleanup is not None:
            try:
                if hasattr(env_to_cleanup, "cleanup"):
                    env_to_cleanup.cleanup()
                elif hasattr(env_to_cleanup, "stop"):
                    env_to_cleanup.stop()
            except Exception:
                logger.debug("Failed to clean redundant environment for task %s", effective_task_id, exc_info=True)
        return env_to_return


def _start_cleanup_thread():
    global _cleanup_thread, _cleanup_running
    with _env_lock:
        if _cleanup_thread is None or not _cleanup_thread.is_alive():
            _cleanup_running = True
            _cleanup_thread = threading.Thread(target=_cleanup_thread_worker, daemon=True)
            _cleanup_thread.start()


def _stop_cleanup_thread():
    global _cleanup_running
    _cleanup_running = False
    if _cleanup_thread is not None:
        try:
            _cleanup_thread.join(timeout=5)
        except (SystemExit, KeyboardInterrupt):
            pass


def get_active_env(task_id: str):
    with _env_lock:
        return _active_environments.get(_resolve_container_task_id(task_id)) or _active_environments.get(task_id)


def is_persistent_env(task_id: str) -> bool:
    env = get_active_env(task_id)
    if env is None:
        return False
    return bool(getattr(env, "_persistent", False))


def cleanup_vm(task_id: str):
    effective_task_id = _resolve_container_task_id(task_id)
    env = None
    with _env_lock:
        env = _active_environments.pop(effective_task_id, None)
        _last_activity.pop(effective_task_id, None)
    with _creation_locks_lock:
        _creation_locks.pop(effective_task_id, None)
    _clear_file_ops_cache_for_task(effective_task_id)
    if env is None:
        return
    if hasattr(env, "cleanup"):
        env.cleanup()
    elif hasattr(env, "stop"):
        env.stop()


def cleanup_all_environments():
    task_ids = list(_active_environments.keys())
    cleaned = 0
    for task_id in task_ids:
        try:
            cleanup_vm(task_id)
            cleaned += 1
        except Exception as e:
            logger.error("Error cleaning %s: %s", task_id, e, exc_info=True)
    try:
        scratch_dir = _get_scratch_dir()
        import glob

        for path in glob.glob(str(scratch_dir / "terminal-*")):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
    except Exception:
        pass
    return cleaned


atexit.register(_stop_cleanup_thread)
atexit.register(cleanup_all_environments)


def terminal_tool(
    command: str,
    background: bool = False,
    timeout: Optional[int] = None,
    task_id: Optional[str] = None,
    force: bool = False,
    workdir: Optional[str] = None,
    pty: bool = False,
    notify_on_complete: bool = False,
    watch_patterns: Optional[List[str]] = None,
    allow_network_once: bool = False,
) -> str:
    try:
        if not isinstance(command, str):
            logger.warning("Rejected invalid terminal command value: %s", type(command).__name__)
            return json.dumps(
                {
                    "output": "",
                    "exit_code": -1,
                    "error": f"Invalid command: expected string, got {type(command).__name__}",
                    "status": "error",
                },
                ensure_ascii=False,
            )

        config = _get_env_config()
        env_type = config["env_type"]
        effective_task_id = _resolve_container_task_id(task_id)

        cwd = config["cwd"]
        default_timeout = config["timeout"]
        effective_timeout = timeout or default_timeout

        if not background and timeout and timeout > FOREGROUND_MAX_TIMEOUT:
            return json.dumps(
                {
                    "error": (
                        f"Foreground timeout {timeout}s exceeds the maximum of "
                        f"{FOREGROUND_MAX_TIMEOUT}s. Use background=true with "
                        "notify_on_complete=true for long-running commands."
                    )
                },
                ensure_ascii=False,
            )

        if not background:
            guidance = _foreground_background_guidance(command)
            if guidance:
                return json.dumps({"output": "", "exit_code": -1, "error": guidance, "status": "error"}, ensure_ascii=False)

        env = get_or_create_active_env(
            effective_task_id,
            workdir=workdir,
            timeout=effective_timeout,
        )
        temporary_network = getattr(env, "temporary_network", None) if allow_network_once else None
        if allow_network_once and temporary_network is None and env_type != "local":
            return json.dumps(
                {
                    "output": "",
                    "exit_code": -1,
                    "error": "One-shot network access is only supported by backends with temporary_network().",
                    "status": "blocked",
                },
                ensure_ascii=False,
            )

        approval_note = None
        if not force:
            approval = _check_all_guards(command, env_type)
            if not approval["approved"]:
                desc = approval.get("description", "command flagged")
                fallback_msg = f"Command denied: {desc}. Pass force=True only when you explicitly trust the command."
                return json.dumps(
                    {"output": "", "exit_code": -1, "error": approval.get("message", fallback_msg), "status": "blocked"},
                    ensure_ascii=False,
                )
            if approval.get("user_approved"):
                desc = approval.get("description", "flagged as dangerous")
                approval_note = f"Command required approval ({desc}) and was approved by the user."

        if workdir:
            workdir_error = _validate_workdir(workdir)
            if workdir_error:
                logger.warning("Blocked dangerous workdir: %s (command: %s)", workdir[:200], safe_command_preview(command))
                return json.dumps({"output": "", "exit_code": -1, "error": workdir_error, "status": "blocked"}, ensure_ascii=False)

        pty_disabled_reason = None
        effective_pty = pty
        if pty and _command_requires_pipe_stdin(command):
            effective_pty = False
            pty_disabled_reason = (
                "PTY disabled for this command because it expects piped stdin/EOF "
                "(for example gh auth login --with-token). For local background "
                "processes, call process(action='close') after writing so it receives EOF."
            )

        if background:
            effective_cwd = workdir or cwd
            network_release = None
            try:
                if env_type == "local":
                    proc_session = process_registry.spawn_local(
                        command=command,
                        cwd=effective_cwd,
                        task_id=effective_task_id,
                        session_key="",
                        env_vars=env.env if hasattr(env, "env") else None,
                        use_pty=effective_pty,
                    )
                else:
                    if temporary_network is not None:
                        lease = temporary_network()
                        lease.__enter__()

                        def network_release(lease=lease):
                            lease.__exit__(None, None, None)

                    proc_session = process_registry.spawn_via_env(
                        env=env,
                        command=command,
                        cwd=effective_cwd,
                        task_id=effective_task_id,
                        session_key="",
                        network_release=network_release,
                    )
                    network_release = None

                result_data = {
                    "output": "Background process started",
                    "session_id": proc_session.id,
                    "pid": proc_session.pid,
                    "exit_code": 0,
                    "error": None,
                }
                if approval_note:
                    result_data["approval"] = approval_note
                if pty_disabled_reason:
                    result_data["pty_note"] = pty_disabled_reason

                watch_patterns, conflict_note = _resolve_notification_flag_conflict(
                    notify_on_complete=bool(notify_on_complete),
                    watch_patterns=watch_patterns,
                    background=bool(background),
                )
                if conflict_note:
                    result_data["watch_patterns_ignored"] = conflict_note
                if notify_on_complete and background:
                    proc_session.notify_on_complete = True
                    result_data["notify_on_complete"] = True
                if watch_patterns and background:
                    proc_session.watch_patterns = list(watch_patterns)
                    result_data["watch_patterns"] = proc_session.watch_patterns
                return json.dumps(result_data, ensure_ascii=False)
            except Exception as e:
                if network_release is not None:
                    try:
                        network_release()
                    except Exception:
                        logger.warning("Failed to release network lease after spawn failure", exc_info=True)
                return json.dumps({"output": "", "exit_code": -1, "error": f"Failed to start background process: {str(e)}"}, ensure_ascii=False)

        max_retries = 3
        retry_count = 0
        result = None
        network_warning = None
        while retry_count <= max_retries:
            try:
                execute_kwargs = {"timeout": effective_timeout}
                if workdir:
                    execute_kwargs["cwd"] = workdir
                if temporary_network is None:
                    result = env.execute(command, **execute_kwargs)
                else:
                    try:
                        with temporary_network():
                            result = env.execute(command, **execute_kwargs)
                    except Exception as network_exc:
                        if result is not None and "disconnect" in str(network_exc).lower():
                            network_warning = f"Network cleanup failed after command execution: {network_exc}"
                            try:
                                cleanup_vm(effective_task_id)
                            except Exception:
                                logger.debug("Failed to cleanup env after network warning", exc_info=True)
                        else:
                            raise
            except Exception as e:
                error_str = str(e).lower()
                if "timeout" in error_str:
                    return json.dumps({"output": "", "exit_code": 124, "error": f"Command timed out after {effective_timeout} seconds"}, ensure_ascii=False)
                if retry_count < max_retries:
                    retry_count += 1
                    wait_time = 2 ** retry_count
                    logger.warning(
                        "Execution error, retrying in %ds (attempt %d/%d) - Command: %s - Error: %s: %s - Task: %s, Backend: %s",
                        wait_time,
                        retry_count,
                        max_retries,
                        safe_command_preview(command),
                        type(e).__name__,
                        e,
                        effective_task_id,
                        env_type,
                    )
                    time.sleep(wait_time)
                    continue
                logger.error(
                    "Execution failed after %d retries - Command: %s - Error: %s: %s - Task: %s, Backend: %s",
                    max_retries,
                    safe_command_preview(command),
                    type(e).__name__,
                    e,
                    effective_task_id,
                    env_type,
                )
                return json.dumps(
                    {"output": "", "exit_code": -1, "error": f"Command execution failed: {type(e).__name__}: {str(e)}"},
                    ensure_ascii=False,
                )
            break

        output = result.get("output", "")
        returncode = result.get("returncode", 0)
        output = _handle_sudo_failure(output)

        max_output_chars = get_max_bytes()
        if len(output) > max_output_chars:
            head_chars = int(max_output_chars * 0.4)
            tail_chars = max_output_chars - head_chars
            omitted = len(output) - head_chars - tail_chars
            truncated_notice = (
                f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted "
                f"out of {len(output)} total] ...\n\n"
            )
            output = output[:head_chars] + truncated_notice + output[-tail_chars:]

        output = strip_ansi(output)
        output = redact_sensitive_text(output.strip()) if output else ""
        exit_note = _interpret_exit_code(command, returncode)

        result_dict = {"output": output, "exit_code": returncode, "error": None}
        if approval_note:
            result_dict["approval"] = approval_note
        if exit_note:
            result_dict["exit_code_meaning"] = exit_note
        if network_warning:
            result_dict["network_warning"] = network_warning
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        import traceback

        tb_str = traceback.format_exc()
        logger.error("terminal_tool exception:\n%s", tb_str)
        return json.dumps(
            {
                "output": "",
                "exit_code": -1,
                "error": f"Failed to execute command: {str(e)}",
                "traceback": tb_str,
                "status": "error",
            },
            ensure_ascii=False,
        )


def check_terminal_requirements() -> bool:
    config = _get_env_config()
    env_type = config["env_type"]
    try:
        if env_type == "local":
            return True
        if env_type == "docker":
            docker = find_docker()
            if not docker:
                logger.error("Docker executable not found in PATH or common install locations")
                return False
            result = subprocess.run([docker, "version"], capture_output=True, timeout=5)
            return result.returncode == 0
        if env_type == "singularity":
            executable = shutil.which("apptainer") or shutil.which("singularity")
            if executable:
                result = subprocess.run([executable, "--version"], capture_output=True, timeout=5)
                return result.returncode == 0
            return False
        if env_type == "ssh":
            if not config.get("ssh_host") or not config.get("ssh_user"):
                logger.error(
                    "SSH backend selected but TERMINAL_SSH_HOST and TERMINAL_SSH_USER are not both set."
                )
                return False
            return True
        logger.error("Unknown TERMINAL_ENV '%s'. Use one of: local, docker, singularity, ssh.", env_type)
        return False
    except Exception as e:
        logger.error("Terminal requirements check failed: %s", e, exc_info=True)
        return False

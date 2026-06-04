from __future__ import annotations

import platform
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cron.paths import atomic_replace, secure_file
from gateway.service_env import get_service_env_file, masked_service_env, set_service_env, unset_service_env


@dataclass(frozen=True)
class GatewayServiceManagerResult:
    message: str
    exit_code: int = 0


def detect_platform() -> str:
    system = platform.system().lower()
    if system == "darwin":
        if shutil.which("launchctl") is None:
            return "unsupported"
        return "launchd-user" if run_command(["launchctl", "print", _launchd_domain()])[0] == 0 else "unsupported"
    if system == "linux":
        if shutil.which("systemctl") is None:
            return "unsupported"
        return "systemd-user" if run_command(["systemctl", "--user", "show-environment"])[0] == 0 else "unsupported"
    return "unsupported"


def run_command(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout


def systemd_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.agent.gateway.plist"


def _missing_install_result() -> GatewayServiceManagerResult:
    return GatewayServiceManagerResult(
        "Gateway service is not installed; run `agent gateway service install`.",
        exit_code=2,
    )


def _unsupported_result() -> GatewayServiceManagerResult:
    return GatewayServiceManagerResult(
        "Gateway service manager is unsupported on this platform.",
        exit_code=2,
    )


def _command_result(code: int, output: str, *, fallback: str = "ok") -> GatewayServiceManagerResult:
    text = output.strip() or fallback
    return GatewayServiceManagerResult(text, exit_code=0 if code == 0 else 1)


def _systemd_unit_path() -> Path:
    return systemd_unit_dir() / "agent-gateway.service"


def _launchd_service_ref() -> str:
    return f"gui/{_uid()}/com.agent.gateway"


def _uid() -> int:
    import os

    return os.getuid()


def _launchd_domain() -> str:
    return f"gui/{_uid()}"


def _launchd_log_paths() -> tuple[Path, Path]:
    path = launchd_plist_path()
    try:
        payload = plistlib.loads(path.read_bytes())
        stdout = payload.get("StandardOutPath")
        stderr = payload.get("StandardErrorPath")
        if stdout and stderr:
            return Path(str(stdout)), Path(str(stderr))
    except Exception:
        pass
    return path.parent / "agent-gateway.out.log", path.parent / "agent-gateway.err.log"


def _restore_file(path: Path, previous_content: str | bytes | None) -> None:
    try:
        if previous_content is None:
            path.unlink(missing_ok=True)
        elif isinstance(previous_content, bytes):
            path.write_bytes(previous_content)
            secure_file(path)
        else:
            path.write_text(previous_content, encoding="utf-8")
            secure_file(path)
    except OSError:
        pass


def _cleanup_tmp(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def install_service(*, transport: str, force: bool = False) -> GatewayServiceManagerResult:
    from gateway.service_context import build_service_runtime_context

    detected = detect_platform()
    context = build_service_runtime_context()
    if detected == "systemd-user":
        from gateway.service_platforms.systemd_user import UNIT_NAME, render_unit

        unit_dir = systemd_unit_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = unit_dir / UNIT_NAME
        if unit_path.exists() and not force:
            return GatewayServiceManagerResult(
                f"Gateway service already installed at {unit_path}. Use --force.",
                exit_code=2,
            )
        previous_content = unit_path.read_text(encoding="utf-8") if unit_path.exists() else None
        tmp_path = unit_path.with_name(f".{unit_path.name}.tmp")
        try:
            tmp_path.write_text(render_unit(context, transport=transport), encoding="utf-8")
            atomic_replace(tmp_path, unit_path)
            secure_file(unit_path)
        except OSError as exc:
            _cleanup_tmp(tmp_path)
            return GatewayServiceManagerResult(f"Could not write {unit_path}: {exc}", exit_code=2)
        for args in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "agent-gateway.service"],
        ):
            code, output = run_command(args)
            if code != 0:
                _restore_file(unit_path, previous_content)
                run_command(["systemctl", "--user", "daemon-reload"])
                return GatewayServiceManagerResult(
                    output or f"{' '.join(args)} failed",
                    exit_code=2,
                )
        return GatewayServiceManagerResult(f"Installed gateway service at {unit_path}.")
    if detected == "launchd-user":
        from gateway.service_env import read_service_env
        from gateway.service_platforms.launchd_user import render_plist

        plist_path = launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        if plist_path.exists() and not force:
            return GatewayServiceManagerResult(
                f"Gateway service already installed at {plist_path}. Use --force.",
                exit_code=2,
            )
        previous_content = plist_path.read_bytes() if plist_path.exists() else None
        tmp_path = plist_path.with_name(f".{plist_path.name}.tmp")
        try:
            tmp_path.write_bytes(
                render_plist(context, transport=transport, service_env=read_service_env())
            )
            atomic_replace(tmp_path, plist_path)
            secure_file(plist_path)
        except OSError as exc:
            _cleanup_tmp(tmp_path)
            _restore_file(plist_path, previous_content)
            return GatewayServiceManagerResult(f"Could not write {plist_path}: {exc}", exit_code=2)
        return GatewayServiceManagerResult(f"Installed gateway service at {plist_path}.")
    return _unsupported_result()


def start_service() -> GatewayServiceManagerResult:
    detected = detect_platform()
    if detected == "systemd-user":
        if not _systemd_unit_path().exists():
            return _missing_install_result()
        return _command_result(
            *run_command(["systemctl", "--user", "start", "agent-gateway.service"]),
            fallback="started",
        )
    if detected == "launchd-user":
        path = launchd_plist_path()
        if not path.exists():
            return _missing_install_result()
        code, output = run_command(["launchctl", "bootstrap", _launchd_domain(), str(path)])
        if code == 5 and run_command(["launchctl", "print", _launchd_service_ref()])[0] == 0:
            pass
        elif code != 0:
            return _command_result(code, output, fallback="bootstrap failed")
        return _command_result(
            *run_command(["launchctl", "kickstart", "-k", _launchd_service_ref()]),
            fallback="started",
        )
    return _unsupported_result()


def stop_service() -> GatewayServiceManagerResult:
    detected = detect_platform()
    if detected == "systemd-user":
        if not _systemd_unit_path().exists():
            return _missing_install_result()
        return _command_result(
            *run_command(["systemctl", "--user", "stop", "agent-gateway.service"]),
            fallback="stopped",
        )
    if detected == "launchd-user":
        path = launchd_plist_path()
        if not path.exists():
            return _missing_install_result()
        return _command_result(
            *run_command(["launchctl", "bootout", _launchd_domain(), str(path)]),
            fallback="stopped",
        )
    return _unsupported_result()


def restart_service() -> GatewayServiceManagerResult:
    result = stop_service()
    if result.exit_code:
        return result
    return start_service()


def uninstall_service() -> GatewayServiceManagerResult:
    detected = detect_platform()
    if detected == "systemd-user":
        unit_path = _systemd_unit_path()
        if not unit_path.exists():
            return _missing_install_result()
        failures = []
        for args in (
            ["systemctl", "--user", "disable", "agent-gateway.service"],
            ["systemctl", "--user", "stop", "agent-gateway.service"],
        ):
            result = _command_result(*run_command(args))
            if result.exit_code:
                failures.append(result.message)
        try:
            unit_path.unlink()
        except OSError as exc:
            failures.append(str(exc))
        result = _command_result(*run_command(["systemctl", "--user", "daemon-reload"]))
        if result.exit_code:
            failures.append(result.message)
        if failures:
            return GatewayServiceManagerResult("; ".join(failures), exit_code=1)
        return GatewayServiceManagerResult("Uninstalled agent-gateway.service.")
    if detected == "launchd-user":
        path = launchd_plist_path()
        if not path.exists():
            return _missing_install_result()
        stop_service()
        try:
            path.unlink()
        except OSError as exc:
            return GatewayServiceManagerResult(str(exc), exit_code=1)
        return GatewayServiceManagerResult("Uninstalled com.agent.gateway.")
    return _unsupported_result()


def service_status() -> GatewayServiceManagerResult:
    detected = detect_platform()
    if detected == "systemd-user":
        if not _systemd_unit_path().exists():
            return _missing_install_result()
        return _command_result(
            *run_command(["systemctl", "--user", "status", "agent-gateway.service"]),
            fallback="installed",
        )
    if detected == "launchd-user":
        if not launchd_plist_path().exists():
            return _missing_install_result()
        return _command_result(
            *run_command(["launchctl", "print", _launchd_service_ref()]),
            fallback="installed",
        )
    return _unsupported_result()


def service_logs(*, lines: int = 100) -> GatewayServiceManagerResult:
    detected = detect_platform()
    if detected == "systemd-user":
        if not _systemd_unit_path().exists():
            return _missing_install_result()
        return _command_result(
            *run_command(
                ["journalctl", "--user-unit", "agent-gateway.service", "-n", str(lines), "--no-pager"]
            ),
            fallback="no logs",
        )
    if detected == "launchd-user":
        if not launchd_plist_path().exists():
            return _missing_install_result()
        stdout_path, stderr_path = _launchd_log_paths()
        return _command_result(
            *run_command(["tail", "-n", str(lines), str(stdout_path), str(stderr_path)]),
            fallback="no logs",
        )
    return _unsupported_result()


def service_env_set(key: str, value: str) -> GatewayServiceManagerResult:
    try:
        path = set_service_env(key, value, path=get_service_env_file())
    except ValueError as exc:
        return GatewayServiceManagerResult(str(exc), exit_code=2)
    return GatewayServiceManagerResult(
        f"Set gateway service env {key} in {path}. Restart gateway service after env changes. "
        "On macOS launchd, rerun `agent gateway service install --force` so embedded env values are refreshed."
    )


def service_env_unset(key: str) -> GatewayServiceManagerResult:
    try:
        removed = unset_service_env(key, path=get_service_env_file())
    except ValueError as exc:
        return GatewayServiceManagerResult(str(exc), exit_code=2)
    action = "Unset" if removed else "Gateway service env key was not set"
    return GatewayServiceManagerResult(
        f"{action} {key} in {get_service_env_file()}. Restart gateway service after env changes. "
        "On macOS launchd, rerun `agent gateway service install --force` so embedded env values are refreshed."
    )


def service_env_list() -> GatewayServiceManagerResult:
    values = masked_service_env(path=get_service_env_file())
    if not values:
        return GatewayServiceManagerResult(f"No gateway service env values set in {get_service_env_file()}.")
    lines = [f"Gateway Service Env: {get_service_env_file()}"]
    lines.extend(f"  {key}={values[key]}" for key in sorted(values))
    return GatewayServiceManagerResult("\n".join(lines))

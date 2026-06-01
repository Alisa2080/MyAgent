from __future__ import annotations

import importlib.util
import os
import platform as platform_module
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from cron.service_state import is_status_fresh, read_service_status


@dataclass(frozen=True)
class ServiceCommandResult:
    message: str
    exit_code: int = 0


@dataclass(frozen=True)
class ServiceInstallConfig:
    interval_seconds: float
    lease_seconds: int
    force: bool = False
    python_executable: str = sys.executable


@dataclass(frozen=True)
class ServiceRuntimeStatus:
    platform: str
    supported: bool
    installed: bool | None
    enabled: bool | None
    active: bool | None
    pid: int | None = None
    detail: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ServiceStatus:
    platform: str
    supported: bool
    installed: bool | None
    enabled: bool | None
    active: bool | None
    pid: int | None
    detail: str | None
    error: str | None
    heartbeat_fresh: bool
    process_state: str | None
    leader_state: str | None
    last_heartbeat_at: str | None
    last_tick: dict[str, Any] | None
    last_error: str | None
    exit_reason: str | None


class CronServicePlatform(Protocol):
    key: str

    def supported(self) -> bool:
        raise NotImplementedError

    def install(self, config: ServiceInstallConfig) -> ServiceCommandResult:
        raise NotImplementedError

    def uninstall(self) -> ServiceCommandResult:
        raise NotImplementedError

    def start(self) -> ServiceCommandResult:
        raise NotImplementedError

    def stop(self) -> ServiceCommandResult:
        raise NotImplementedError

    def restart(self) -> ServiceCommandResult:
        raise NotImplementedError

    def status(self) -> ServiceRuntimeStatus:
        raise NotImplementedError

    def logs(self, lines: int = 100) -> ServiceCommandResult:
        raise NotImplementedError


def service_environment() -> dict[str, str]:
    keys = (
        "PATH",
        "AGENT_CLI_HOME",
        "AGENT_CRON_HOME",
        "AGENT_RUNTIME_PROFILE",
        "AGENT_CRON_RUNNER_MODE",
        "AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT",
        "AGENT_CRON_MAX_PARALLEL",
    )
    return {key: value for key in keys if (value := os.getenv(key))}


def serve_command(config: ServiceInstallConfig) -> list[str]:
    return [
        config.python_executable,
        "-m",
        "agent_cli.main",
        "cron",
        "serve",
        "--interval",
        str(config.interval_seconds),
        "--lease-seconds",
        str(config.lease_seconds),
    ]


def detect_platform() -> CronServicePlatform | None:
    system = platform_module.system().lower()
    if system == "linux":
        if importlib.util.find_spec("cron.service_platforms.systemd_user") is None:
            return None
        from cron.service_platforms.systemd_user import SystemdUserCronService

        platform = SystemdUserCronService()
        return platform if platform.supported() else None
    if system == "darwin":
        if importlib.util.find_spec("cron.service_platforms.launchd_user") is None:
            return None
        from cron.service_platforms.launchd_user import LaunchdUserCronService

        platform = LaunchdUserCronService()
        return platform if platform.supported() else None
    return None


def unsupported_result() -> ServiceCommandResult:
    return ServiceCommandResult(
        "user-level cron service is not supported on this platform; "
        "run `agent cron serve` to keep cron scheduling in the foreground.",
        exit_code=2,
    )


def _platform_or_none() -> CronServicePlatform | None:
    return detect_platform()


def install_service(
    *,
    interval_seconds: float,
    lease_seconds: int,
    force: bool,
) -> ServiceCommandResult:
    platform = _platform_or_none()
    if platform is None:
        return unsupported_result()
    return platform.install(
        ServiceInstallConfig(
            interval_seconds=interval_seconds,
            lease_seconds=lease_seconds,
            force=force,
        )
    )


def uninstall_service() -> ServiceCommandResult:
    platform = _platform_or_none()
    return unsupported_result() if platform is None else platform.uninstall()


def start_service() -> ServiceCommandResult:
    platform = _platform_or_none()
    return unsupported_result() if platform is None else platform.start()


def stop_service() -> ServiceCommandResult:
    platform = _platform_or_none()
    return unsupported_result() if platform is None else platform.stop()


def restart_service() -> ServiceCommandResult:
    platform = _platform_or_none()
    return unsupported_result() if platform is None else platform.restart()


def service_logs(*, lines: int = 100) -> ServiceCommandResult:
    platform = _platform_or_none()
    return unsupported_result() if platform is None else platform.logs(lines=lines)


def service_runtime_status() -> ServiceRuntimeStatus:
    platform = _platform_or_none()
    if platform is None:
        return ServiceRuntimeStatus(
            platform="unsupported",
            supported=False,
            installed=False,
            enabled=False,
            active=False,
            detail="run `agent cron serve` for foreground scheduling",
        )
    return platform.status()


def compose_service_status(
    runtime: ServiceRuntimeStatus | None = None,
    *,
    now_text: str | None = None,
    stale_after_seconds: int = 180,
) -> ServiceStatus:
    runtime = runtime or service_runtime_status()
    status = read_service_status()
    if now_text is None:
        from cron.jobs import now

        now_text = now().isoformat()
    fresh = is_status_fresh(
        status,
        now_text=now_text,
        stale_after_seconds=stale_after_seconds,
    )
    last_tick = status.get("last_tick") if isinstance(status, dict) else None
    return ServiceStatus(
        platform=runtime.platform,
        supported=runtime.supported,
        installed=runtime.installed,
        enabled=runtime.enabled,
        active=runtime.active,
        pid=runtime.pid,
        detail=runtime.detail,
        error=runtime.error,
        heartbeat_fresh=fresh,
        process_state=status.get("process_state") if isinstance(status, dict) else None,
        leader_state=status.get("leader_state") if isinstance(status, dict) else None,
        last_heartbeat_at=status.get("last_heartbeat_at") if isinstance(status, dict) else None,
        last_tick=last_tick if isinstance(last_tick, dict) else None,
        last_error=status.get("last_error") if isinstance(status, dict) else None,
        exit_reason=status.get("exit_reason") if isinstance(status, dict) else None,
    )

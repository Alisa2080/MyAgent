from __future__ import annotations

import importlib.util
import os
import platform as platform_module
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol

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
    environment_overrides: Mapping[str, str] = field(default_factory=dict)
    working_directory: Path | None = None
    pythonpath: str | None = None
    service_env_file: Path | None = None


@dataclass(frozen=True)
class ServiceInstallDetail:
    effective_profile: str
    profile_source: str
    runner_mode: str
    runner_mode_source: str
    production_recommended: bool


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
    status_pid: int | None = None


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


PRODUCTION_RUNTIME_PROFILES = frozenset({"hosted", "prod"})


def _clean_profile(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def effective_runtime_profile(*, cli_profile: str | None = None) -> tuple[str, str]:
    runtime_profile = _clean_profile(os.getenv("AGENT_RUNTIME_PROFILE"))
    if runtime_profile:
        return runtime_profile, "runtime"
    cli = _clean_profile(cli_profile)
    if cli in PRODUCTION_RUNTIME_PROFILES:
        return cli, "cli"
    return "dev", "default"


def _explicit_runner_mode() -> str | None:
    raw = os.getenv("AGENT_CRON_RUNNER_MODE", "").strip().lower()
    return raw or None


def build_service_install_config(
    *,
    interval_seconds: float,
    lease_seconds: int,
    force: bool,
    cli_profile: str | None = None,
) -> tuple[ServiceInstallConfig, ServiceInstallDetail]:
    profile, profile_source = effective_runtime_profile(cli_profile=cli_profile)
    explicit_mode = _explicit_runner_mode()
    overrides: dict[str, str] = {}
    if explicit_mode:
        runner_mode = "subprocess" if explicit_mode == "process" else explicit_mode
        runner_source = "explicit"
    elif profile in PRODUCTION_RUNTIME_PROFILES:
        runner_mode = "subprocess"
        runner_source = "auto"
        overrides["AGENT_CRON_RUNNER_MODE"] = "subprocess"
    else:
        runner_mode = "inprocess"
        runner_source = "default"

    detail = ServiceInstallDetail(
        effective_profile=profile,
        profile_source=profile_source,
        runner_mode=runner_mode,
        runner_mode_source=runner_source,
        production_recommended=(
            profile not in PRODUCTION_RUNTIME_PROFILES or runner_mode == "subprocess"
        ),
    )
    from cron.service_context import build_service_runtime_context

    runtime_context = build_service_runtime_context()
    config = ServiceInstallConfig(
        interval_seconds=interval_seconds,
        lease_seconds=lease_seconds,
        force=force,
        environment_overrides=MappingProxyType(overrides),
        working_directory=runtime_context.working_directory,
        pythonpath=runtime_context.pythonpath,
        service_env_file=runtime_context.service_env_file,
    )
    return config, detail


def service_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    keys = (
        "PATH",
        "AGENT_CLI_HOME",
        "AGENT_CRON_HOME",
        "AGENT_RUNTIME_PROFILE",
        "AGENT_CRON_RUNNER_MODE",
        "AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT",
        "AGENT_CRON_MAX_PARALLEL",
    )
    env = {key: value for key in keys if (value := os.getenv(key))}
    if overrides:
        env.update({key: value for key, value in overrides.items() if value})
    return env


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
    cli_profile: str | None = None,
) -> ServiceCommandResult:
    platform = _platform_or_none()
    if platform is None:
        return unsupported_result()
    config, detail = build_service_install_config(
        interval_seconds=interval_seconds,
        lease_seconds=lease_seconds,
        force=force,
        cli_profile=cli_profile,
    )
    result = platform.install(config)
    if result.exit_code:
        return result
    if detail.runner_mode_source == "auto":
        suffix = f" Runner mode: subprocess for {detail.effective_profile} profile."
    else:
        suffix = f" Runner mode: {detail.runner_mode}."
    return ServiceCommandResult(result.message + suffix, exit_code=result.exit_code)


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
    raw_status_pid = status.get("pid") if isinstance(status, dict) else None
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
        status_pid=raw_status_pid if type(raw_status_pid) is int else None,
    )

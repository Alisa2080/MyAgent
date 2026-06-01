from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from cron.paths import atomic_replace, secure_file
from cron.service_manager import (
    ServiceCommandResult,
    ServiceInstallConfig,
    ServiceRuntimeStatus,
    serve_command,
    service_environment,
)

SERVICE_NAME = "langchain-agent-cron.service"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def _run_command(args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    return result.returncode, result.stdout or "", result.stderr or ""


def _quote_systemd(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _format_environment() -> str:
    return "\n".join(
        f'Environment="{key}={_quote_systemd(value)}"'
        for key, value in sorted(service_environment().items())
    )


def render_unit(config: ServiceInstallConfig) -> str:
    command = " ".join(serve_command(config))
    environment = _format_environment()
    environment_block = f"{environment}\n" if environment else ""
    return (
        "[Unit]\n"
        "Description=LangChain Agent Cron Service\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{environment_block}"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


class SystemdUserCronService:
    key = "systemd-user"

    def __init__(self, command_runner=_run_command) -> None:
        self.command_runner = command_runner

    def supported(self) -> bool:
        return shutil.which("systemctl") is not None

    def _run_raw(self, args: list[str]) -> tuple[int, str, str]:
        try:
            return self.command_runner(args)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", str(exc)

    def _run(self, args: list[str]) -> ServiceCommandResult:
        code, stdout, stderr = self._run_raw(args)
        text = (stderr or stdout).strip()
        return ServiceCommandResult(text or "ok", exit_code=0 if code == 0 else 1)

    def install(self, config: ServiceInstallConfig) -> ServiceCommandResult:
        path = systemd_unit_path()
        if path.exists() and not config.force:
            return ServiceCommandResult(
                f"{path} already exists; rerun with --force to overwrite.",
                exit_code=2,
            )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(render_unit(config), encoding="utf-8")
            atomic_replace(tmp, path)
            secure_file(path)
        except OSError as exc:
            return ServiceCommandResult(
                "user-level cron service is not supported on this platform; "
                "run `agent cron serve` to keep cron scheduling in the foreground. "
                f"Could not write {path}: {exc}",
                exit_code=2,
            )

        for args in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", SERVICE_NAME],
        ):
            result = self._run(args)
            if result.exit_code:
                return result

        return ServiceCommandResult(
            f"Installed {SERVICE_NAME}. Run `agent cron service start` to start it."
        )

    def uninstall(self) -> ServiceCommandResult:
        for args in (
            ["systemctl", "--user", "disable", SERVICE_NAME],
            ["systemctl", "--user", "stop", SERVICE_NAME],
        ):
            result = self._run(args)
            if result.exit_code:
                return result

        path = systemd_unit_path()
        if path.exists():
            path.unlink()

        result = self._run(["systemctl", "--user", "daemon-reload"])
        if result.exit_code:
            return result
        return ServiceCommandResult(f"Uninstalled {SERVICE_NAME}.")

    def start(self) -> ServiceCommandResult:
        return self._run(["systemctl", "--user", "start", SERVICE_NAME])

    def stop(self) -> ServiceCommandResult:
        return self._run(["systemctl", "--user", "stop", SERVICE_NAME])

    def restart(self) -> ServiceCommandResult:
        return self._run(["systemctl", "--user", "restart", SERVICE_NAME])

    def status(self) -> ServiceRuntimeStatus:
        enabled_code, enabled_out, enabled_err = self._run_raw(
            ["systemctl", "--user", "is-enabled", SERVICE_NAME]
        )
        active_code, active_out, active_err = self._run_raw(
            ["systemctl", "--user", "is-active", SERVICE_NAME]
        )
        show_code, show_out, show_err = self._run_raw(
            [
                "systemctl",
                "--user",
                "show",
                SERVICE_NAME,
                "--property=MainPID,ExecMainStatus,Result",
            ]
        )

        props = {}
        for line in show_out.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                props[key] = value

        pid_text = props.get("MainPID") or "0"
        pid = int(pid_text) if pid_text.isdigit() and int(pid_text) > 0 else None
        enabled_text = enabled_out.strip()
        active_text = active_out.strip()
        error = (show_err or active_err or enabled_err).strip() or None

        return ServiceRuntimeStatus(
            platform=self.key,
            supported=True,
            installed=enabled_code == 0 or active_code == 0 or show_code == 0,
            enabled=enabled_text == "enabled" if enabled_code == 0 else False,
            active=active_text == "active" if active_code == 0 else False,
            pid=pid,
            detail=active_text or enabled_text or None,
            error=error,
        )

    def logs(self, lines: int = 100) -> ServiceCommandResult:
        count = str(max(1, int(lines)))
        code, stdout, stderr = self._run_raw(
            ["journalctl", "--user", "-u", SERVICE_NAME, "-n", count, "--no-pager"]
        )
        text = (stdout or stderr).strip() or "No cron service logs yet."
        return ServiceCommandResult(text, exit_code=0 if code == 0 else 1)

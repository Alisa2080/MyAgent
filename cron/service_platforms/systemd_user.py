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
MISSING_INSTALL_MESSAGE = (
    "cron service is not installed; run `agent cron service install`."
)


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def read_installed_unit() -> str | None:
    path = systemd_unit_path()
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _run_command(args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    return result.returncode, result.stdout or "", result.stderr or ""


def _quote_systemd(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def _quote_systemd_arg(value: str) -> str:
    escaped = _quote_systemd(value).replace("$", "$$")
    if not value or any(char.isspace() or char in {'"', "\\"} for char in value):
        return f'"{escaped}"'
    return escaped


def _format_working_directory(config: ServiceInstallConfig) -> str:
    if config.working_directory is None:
        return ""
    return f"WorkingDirectory={_quote_systemd(str(config.working_directory))}\n"


def _format_environment_file(config: ServiceInstallConfig) -> str:
    if config.service_env_file is None:
        return ""
    return f"EnvironmentFile=-{_quote_systemd(str(config.service_env_file))}\n"


def _format_environment(config: ServiceInstallConfig) -> str:
    values = service_environment(config.environment_overrides)
    if config.pythonpath:
        values["PYTHONPATH"] = config.pythonpath
    for secret_key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET"):
        values.pop(secret_key, None)
    return "\n".join(
        f'Environment="{key}={_quote_systemd(value)}"'
        for key, value in sorted(values.items())
        if "\n" not in value and "\r" not in value
    )


def render_unit(config: ServiceInstallConfig) -> str:
    command = " ".join(_quote_systemd_arg(arg) for arg in serve_command(config))
    working_directory = _format_working_directory(config)
    environment_block = _format_environment(config)
    environment_file = _format_environment_file(config)
    environment_block_line = f"{environment_block}\n" if environment_block else ""
    return (
        "[Unit]\n"
        "Description=LangChain Agent Cron Service\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{working_directory}"
        f"{environment_block_line}"
        f"{environment_file}"
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
        if shutil.which("systemctl") is None:
            return False
        code, _stdout, _stderr = self._run_raw(
            ["systemctl", "--user", "show-environment"]
        )
        return code == 0

    def _run_raw(self, args: list[str]) -> tuple[int, str, str]:
        try:
            return self.command_runner(args)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", str(exc)

    def _run(self, args: list[str]) -> ServiceCommandResult:
        code, stdout, stderr = self._run_raw(args)
        text = (stderr or stdout).strip()
        return ServiceCommandResult(text or "ok", exit_code=0 if code == 0 else 1)

    def _restore_unit(self, path: Path, previous_content: str | None) -> None:
        try:
            if previous_content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(previous_content, encoding="utf-8")
                secure_file(path)
        except OSError:
            pass

    def _cleanup_tmp(self, tmp: Path) -> None:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    def _rollback_install(self, path: Path, previous_content: str | None) -> None:
        self._restore_unit(path, previous_content)
        self._run_raw(["systemctl", "--user", "daemon-reload"])

    def install(self, config: ServiceInstallConfig) -> ServiceCommandResult:
        path = systemd_unit_path()
        if path.exists() and not config.force:
            return ServiceCommandResult(
                f"{path} already exists; rerun with --force to overwrite.",
                exit_code=2,
            )

        previous_content = path.read_text(encoding="utf-8") if path.exists() else None
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(render_unit(config), encoding="utf-8")
            atomic_replace(tmp, path)
            secure_file(path)
        except OSError as exc:
            self._cleanup_tmp(tmp)
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
                self._rollback_install(path, previous_content)
                return result

        return ServiceCommandResult(
            f"Installed {SERVICE_NAME}. Run `agent cron service start` to start it."
        )

    def uninstall(self) -> ServiceCommandResult:
        failures = []
        for args in (
            ["systemctl", "--user", "disable", SERVICE_NAME],
            ["systemctl", "--user", "stop", SERVICE_NAME],
        ):
            result = self._run(args)
            if result.exit_code:
                if not _is_tolerable_uninstall_failure(result.message):
                    failures.append(result.message)

        path = systemd_unit_path()
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            failures.append(str(exc))

        result = self._run(["systemctl", "--user", "daemon-reload"])
        if result.exit_code:
            failures.append(result.message)
        if failures:
            return ServiceCommandResult("; ".join(failures), exit_code=1)
        return ServiceCommandResult(f"Uninstalled {SERVICE_NAME}.")

    def start(self) -> ServiceCommandResult:
        if not systemd_unit_path().exists():
            return ServiceCommandResult(MISSING_INSTALL_MESSAGE, exit_code=2)
        return self._run(["systemctl", "--user", "start", SERVICE_NAME])

    def stop(self) -> ServiceCommandResult:
        if not systemd_unit_path().exists():
            return ServiceCommandResult(MISSING_INSTALL_MESSAGE, exit_code=2)
        return self._run(["systemctl", "--user", "stop", SERVICE_NAME])

    def restart(self) -> ServiceCommandResult:
        if not systemd_unit_path().exists():
            return ServiceCommandResult(MISSING_INSTALL_MESSAGE, exit_code=2)
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
                "--property=LoadState,UnitFileState,MainPID,ExecMainStatus,Result",
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
        load_state = props.get("LoadState", "")
        unit_file_state = props.get("UnitFileState", "")
        result = props.get("Result", "")
        exec_status = props.get("ExecMainStatus", "")
        error = (show_err or active_err or enabled_err).strip() or None
        enabled = enabled_text == "enabled" or unit_file_state == "enabled"
        active = active_text == "active"
        installed = enabled or active or (
            load_state not in {"", "not-found"} and show_code == 0
        )
        detail_parts = [active_text or load_state or enabled_text]
        if result and result != "success":
            detail_parts.append(f"result={result}")
        if exec_status and exec_status != "0":
            detail_parts.append(f"exit_status={exec_status}")

        return ServiceRuntimeStatus(
            platform=self.key,
            supported=True,
            installed=installed,
            enabled=enabled,
            active=active,
            pid=pid,
            detail="; ".join(part for part in detail_parts if part) or None,
            error=error,
        )

    def logs(self, lines: int = 100) -> ServiceCommandResult:
        count = str(max(1, int(lines)))
        code, stdout, stderr = self._run_raw(
            ["journalctl", "--user", "-u", SERVICE_NAME, "-n", count, "--no-pager"]
        )
        text = (stdout or stderr).strip() or "No cron service logs yet."
        return ServiceCommandResult(text, exit_code=0 if code == 0 else 1)


def _is_tolerable_uninstall_failure(message: str) -> bool:
    text = message.lower()
    return any(
        phrase in text
        for phrase in (
            "not loaded",
            "not-found",
            "not found",
            "no such",
            "not enabled",
            "disabled",
            "inactive",
            "not running",
        )
    )

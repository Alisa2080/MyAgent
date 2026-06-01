from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

from cron.paths import atomic_replace, get_cron_dir, secure_dir, secure_file
from cron.service_manager import (
    ServiceCommandResult,
    ServiceInstallConfig,
    ServiceRuntimeStatus,
    serve_command,
    service_environment,
)

LABEL = "ai.langchain.agent.cron"
MISSING_INSTALL_MESSAGE = (
    "cron service is not installed; run `agent cron service install`."
)


def _uid() -> int:
    return os.getuid()


def launchd_domain() -> str:
    return f"gui/{_uid()}"


def launchd_service_ref() -> str:
    return f"{launchd_domain()}/{LABEL}"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchd_log_paths() -> tuple[Path, Path]:
    log_dir = get_cron_dir() / "logs"
    return log_dir / f"{LABEL}.out.log", log_dir / f"{LABEL}.err.log"


def _run_command(args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    return result.returncode, result.stdout or "", result.stderr or ""


def _environment_variables() -> dict[str, str]:
    return {
        key: value
        for key, value in sorted(service_environment().items())
        if "\n" not in value and "\r" not in value
    }


def render_plist(config: ServiceInstallConfig) -> bytes:
    stdout_path, stderr_path = launchd_log_paths()
    payload: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": serve_command(config),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    environment = _environment_variables()
    if environment:
        payload["EnvironmentVariables"] = environment
    return plistlib.dumps(payload, sort_keys=True)


class LaunchdUserCronService:
    key = "launchd-user"

    def __init__(self, command_runner=_run_command) -> None:
        self.command_runner = command_runner

    def supported(self) -> bool:
        if shutil.which("launchctl") is None:
            return False
        code, _stdout, _stderr = self._run_raw(["launchctl", "print", launchd_domain()])
        return code == 0

    def _run_raw(self, args: list[str]) -> tuple[int, str, str]:
        try:
            return self.command_runner(args)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", str(exc)

    def _result(
        self,
        args: list[str],
        *,
        ok_codes: tuple[int, ...] = (0,),
        fallback: str = "ok",
    ) -> ServiceCommandResult:
        code, stdout, stderr = self._run_raw(args)
        text = (stderr or stdout).strip()
        return ServiceCommandResult(
            text or fallback,
            exit_code=0 if code in ok_codes else 1,
        )

    def _cleanup_tmp(self, tmp: Path) -> None:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    def install(self, config: ServiceInstallConfig) -> ServiceCommandResult:
        path = launchd_plist_path()
        if path.exists() and not config.force:
            return ServiceCommandResult(
                f"{path} already exists; rerun with --force to overwrite.",
                exit_code=2,
            )

        stdout_path, stderr_path = launchd_log_paths()
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            secure_dir(stdout_path.parent)
            tmp.write_bytes(render_plist(config))
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

        return ServiceCommandResult(
            f"Installed {LABEL}. Run `agent cron service start` to start it."
        )

    def uninstall(self) -> ServiceCommandResult:
        failures = []
        result = self.stop()
        if result.exit_code:
            failures.append(result.message)

        path = launchd_plist_path()
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            failures.append(str(exc))

        if failures:
            return ServiceCommandResult("; ".join(failures), exit_code=1)
        return ServiceCommandResult(f"Uninstalled {LABEL}.")

    def start(self) -> ServiceCommandResult:
        path = launchd_plist_path()
        if not path.exists():
            return ServiceCommandResult(
                MISSING_INSTALL_MESSAGE,
                exit_code=2,
            )

        code, stdout, stderr = self._run_raw(
            ["launchctl", "bootstrap", launchd_domain(), str(path)]
        )
        if code == 5 and self._print_service_code() == 0:
            pass
        elif code != 0:
            return _command_result(code, stdout, stderr, fallback="bootstrap failed")
        return self._result(
            ["launchctl", "kickstart", "-k", launchd_service_ref()],
            fallback="started",
        )

    def stop(self) -> ServiceCommandResult:
        path = launchd_plist_path()
        if not path.exists():
            return ServiceCommandResult(MISSING_INSTALL_MESSAGE, exit_code=2)

        code, stdout, stderr = self._run_raw(
            ["launchctl", "bootout", launchd_domain(), str(path)]
        )
        if code == 5 and self._print_service_code() != 0:
            pass
        elif code != 0:
            return _command_result(code, stdout, stderr, fallback="bootout failed")
        return ServiceCommandResult((stderr or stdout).strip() or "stopped")

    def restart(self) -> ServiceCommandResult:
        result = self.stop()
        if result.exit_code:
            return result
        return self.start()

    def status(self) -> ServiceRuntimeStatus:
        path = launchd_plist_path()
        installed = path.exists()
        code, stdout, stderr = self._run_raw(
            ["launchctl", "print", launchd_service_ref()]
        )
        pid = _parse_pid(stdout)
        active = pid is not None
        if code != 0:
            detail = "not loaded"
        elif active:
            detail = "loaded"
        else:
            detail = _parse_state_detail(stdout) or "loaded"
        error = None if code == 0 else (stderr or stdout).strip() or None
        return ServiceRuntimeStatus(
            platform=self.key,
            supported=True,
            installed=installed,
            enabled=installed,
            active=active,
            pid=pid,
            detail=detail,
            error=error,
        )

    def logs(self, lines: int = 100) -> ServiceCommandResult:
        count = max(1, int(lines))
        stdout_path, stderr_path = launchd_log_paths()
        sections = []
        for label, path in (("stdout", stdout_path), ("stderr", stderr_path)):
            text = _tail_file(path, count)
            if text:
                sections.append(f"==> {label} <==\n{text}")
        return ServiceCommandResult(
            "\n\n".join(sections) if sections else "No cron service logs yet."
        )

    def _print_service_code(self) -> int:
        code, _stdout, _stderr = self._run_raw(
            ["launchctl", "print", launchd_service_ref()]
        )
        return code


def _parse_pid(output: str) -> int | None:
    match = re.search(r"\bpid\s*=\s*(\d+)\b", output)
    if not match:
        return None
    pid = int(match.group(1))
    return pid if pid > 0 else None


def _parse_state_detail(output: str) -> str | None:
    for line in output.splitlines():
        text = line.strip()
        if text.startswith("state = "):
            return text
    return None


def _command_result(
    code: int,
    stdout: str,
    stderr: str,
    *,
    fallback: str,
) -> ServiceCommandResult:
    text = (stderr or stdout).strip()
    return ServiceCommandResult(text or fallback, exit_code=0 if code == 0 else 1)


def _tail_file(path: Path, lines: int) -> str | None:
    max_bytes = min(max(8192, lines * 4096), 1024 * 1024)
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            content_bytes = handle.read(size - start)
    except OSError:
        return None
    content = content_bytes.decode("utf-8", errors="replace")
    selected = content.splitlines()[-lines:]
    return "\n".join(selected).strip() or None

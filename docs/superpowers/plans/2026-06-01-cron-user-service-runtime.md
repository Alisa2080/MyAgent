# Cron User Service Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local user-level cron service runtime so users can install, start, stop, inspect, and uninstall automatic scheduling through the `agent cron service` command group.

**Architecture:** Add a thin `cron.service_manager` layer above the existing `cron.service.serve()` foreground loop. Platform adapters under `cron/service_platforms/` own systemd-user and launchd command construction/status parsing, while `agent_cli/cron_commands.py` owns user-facing rendering. The service manager never mutates jobs, runs, delivery events, or leases.

**Tech Stack:** Python 3.11+, argparse, pytest, pathlib, plistlib, subprocess, existing `CronCommandResult`, existing `cron.service_state`, `cron.state_store`, and `cron.leader`.

---

## File Structure

- Create `cron/service_manager.py`: platform-neutral dataclasses, platform detection, command dispatch, status composition, environment capture, heartbeat summary.
- Create `cron/service_platforms/__init__.py`: adapter exports.
- Create `cron/service_platforms/systemd_user.py`: Linux user systemd unit rendering, command construction, status parsing, journal logs.
- Create `cron/service_platforms/launchd_user.py`: macOS LaunchAgent plist rendering, command construction, status parsing, stdout/stderr log reading.
- Create `tests/test_cron_service_manager.py`: platform-neutral manager and unsupported-platform tests.
- Create `tests/test_cron_service_platforms.py`: systemd/launchd adapter unit tests with fake command runners.
- Modify `agent_cli/main.py`: add the `agent cron service` parser and dispatch.
- Modify `agent_cli/cron_commands.py`: add service command renderers, include service manager summary in `cron status`, include install/start/restart/fallback repair suggestions in `cron doctor`.
- Modify `tests/test_agent_cli_main.py`: parser and dispatch tests for service subcommands.
- Modify `tests/test_agent_cli_cron_commands.py`: rendering/status/doctor tests.
- Modify `README.md`: document user-level service install/start/status/logs and foreground fallback.

---

### Task 1: Service Manager Core and Unsupported Platform

**Files:**
- Create: `cron/service_manager.py`
- Create: `cron/service_platforms/__init__.py`
- Test: `tests/test_cron_service_manager.py`

- [ ] **Step 1: Write failing tests for unsupported platform and heartbeat composition**

Append this file:

```python
from __future__ import annotations


def test_unsupported_platform_install_returns_exit_code_2(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_manager import install_service

    monkeypatch.setattr("cron.service_manager.detect_platform", lambda: None)

    result = install_service(interval_seconds=60, lease_seconds=180, force=False)

    assert result.exit_code == 2
    assert "user-level cron service is not supported" in result.message
    assert "agent cron serve" in result.message


def test_service_status_combines_platform_and_fresh_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import write_service_status
    from cron.service_manager import ServiceRuntimeStatus, compose_service_status

    write_service_status(
        {
            "process_state": "running",
            "pid": 123,
            "leader_state": "leader",
            "last_heartbeat_at": "2026-06-01T10:00:00+00:00",
            "last_tick": {"due": 1, "ran": 1, "succeeded": 1, "failed": 0, "skipped": 0},
            "last_error": None,
        }
    )

    status = compose_service_status(
        ServiceRuntimeStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
        ),
        now_text="2026-06-01T10:01:00+00:00",
    )

    assert status.platform == "systemd-user"
    assert status.installed is True
    assert status.active is True
    assert status.heartbeat_fresh is True
    assert status.process_state == "running"
    assert status.leader_state == "leader"
    assert status.last_tick == {"due": 1, "ran": 1, "succeeded": 1, "failed": 0, "skipped": 0}


def test_service_status_marks_stale_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import write_service_status
    from cron.service_manager import ServiceRuntimeStatus, compose_service_status

    write_service_status(
        {
            "process_state": "running",
            "pid": 123,
            "leader_state": "leader",
            "last_heartbeat_at": "2026-06-01T09:00:00+00:00",
        }
    )

    status = compose_service_status(
        ServiceRuntimeStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
        ),
        now_text="2026-06-01T10:01:00+00:00",
    )

    assert status.heartbeat_fresh is False
    assert status.last_heartbeat_at == "2026-06-01T09:00:00+00:00"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_manager.py -q
```

Expected: FAIL because `cron.service_manager` does not exist.

- [ ] **Step 3: Implement manager dataclasses and unsupported dispatch**

Create `cron/service_platforms/__init__.py`:

```python
from __future__ import annotations

__all__ = ["launchd_user", "systemd_user"]
```

Create `cron/service_manager.py` with this shape:

```python
from __future__ import annotations

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
        from cron.service_platforms.systemd_user import SystemdUserCronService

        platform = SystemdUserCronService()
        return platform if platform.supported() else None
    if system == "darwin":
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


def install_service(*, interval_seconds: float, lease_seconds: int, force: bool) -> ServiceCommandResult:
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
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_manager.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/service_manager.py cron/service_platforms/__init__.py tests/test_cron_service_manager.py
git commit -m "feat: add cron service manager core"
```

---

### Task 2: Linux User Systemd Adapter

**Files:**
- Create: `cron/service_platforms/systemd_user.py`
- Modify: `tests/test_cron_service_platforms.py`

- [ ] **Step 1: Write failing systemd adapter tests**

Create `tests/test_cron_service_platforms.py` with:

```python
from __future__ import annotations


def test_systemd_install_writes_unit_and_enables(monkeypatch, tmp_path):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import SystemdUserCronService

    calls = []

    def fake_run(args):
        calls.append(args)
        return 0, "ok", ""

    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: tmp_path / "langchain-agent-cron.service",
    )
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "cron-home"))

    service = SystemdUserCronService(command_runner=fake_run)
    result = service.install(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            force=False,
            python_executable="/usr/bin/python3",
        )
    )

    unit = (tmp_path / "langchain-agent-cron.service").read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert "ExecStart=/usr/bin/python3 -m agent_cli.main cron serve --interval 30 --lease-seconds 90" in unit
    assert 'Environment="AGENT_CRON_HOME=' in unit
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "langchain-agent-cron.service"],
    ]


def test_systemd_install_existing_requires_force(monkeypatch, tmp_path):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import SystemdUserCronService

    unit_path = tmp_path / "langchain-agent-cron.service"
    unit_path.write_text("existing", encoding="utf-8")
    monkeypatch.setattr("cron.service_platforms.systemd_user.systemd_unit_path", lambda: unit_path)

    result = SystemdUserCronService(command_runner=lambda args: (0, "", "")).install(
        ServiceInstallConfig(interval_seconds=60, lease_seconds=180, force=False)
    )

    assert result.exit_code == 2
    assert "--force" in result.message


def test_systemd_status_parses_active_enabled(monkeypatch):
    from cron.service_platforms.systemd_user import SystemdUserCronService

    def fake_run(args):
        if args[:3] == ["systemctl", "--user", "is-enabled"]:
            return 0, "enabled\n", ""
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return 0, "active\n", ""
        if args[:3] == ["systemctl", "--user", "show"]:
            return 0, "MainPID=456\nExecMainStatus=0\nResult=success\n", ""
        raise AssertionError(args)

    status = SystemdUserCronService(command_runner=fake_run).status()

    assert status.platform == "systemd-user"
    assert status.supported is True
    assert status.installed is True
    assert status.enabled is True
    assert status.active is True
    assert status.pid == 456


def test_systemd_logs_use_journalctl():
    from cron.service_platforms.systemd_user import SystemdUserCronService

    calls = []

    def fake_run(args):
        calls.append(args)
        return 0, "line1\nline2\n", ""

    result = SystemdUserCronService(command_runner=fake_run).logs(lines=2)

    assert result.exit_code == 0
    assert result.message == "line1\nline2"
    assert calls == [["journalctl", "--user", "-u", "langchain-agent-cron.service", "-n", "2", "--no-pager"]]
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_platforms.py -q
```

Expected: FAIL because `systemd_user.py` does not exist.

- [ ] **Step 3: Implement systemd adapter**

Create `cron/service_platforms/systemd_user.py`:

```python
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


def _format_env() -> str:
    lines = []
    for key, value in sorted(service_environment().items()):
        lines.append(f'Environment="{key}={_quote_systemd(value)}"')
    return "\n".join(lines)


def render_unit(config: ServiceInstallConfig) -> str:
    command = " ".join(serve_command(config))
    env = _format_env()
    env_block = f"{env}\n" if env else ""
    return (
        "[Unit]\n"
        "Description=LangChain Agent Cron Service\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{env_block}"
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

    def _run(self, args: list[str]) -> ServiceCommandResult:
        code, stdout, stderr = self.command_runner(args)
        text = (stderr or stdout).strip()
        return ServiceCommandResult(text or "ok", exit_code=0 if code == 0 else 1)

    def install(self, config: ServiceInstallConfig) -> ServiceCommandResult:
        path = systemd_unit_path()
        if path.exists() and not config.force:
            return ServiceCommandResult(
                f"{path} already exists; rerun with --force to overwrite.",
                exit_code=2,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(render_unit(config), encoding="utf-8")
        atomic_replace(tmp, path)
        secure_file(path)
        for args in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", SERVICE_NAME],
        ):
            result = self._run(args)
            if result.exit_code:
                return result
        return ServiceCommandResult(f"Installed {SERVICE_NAME}. Run `agent cron service start` to start it.")

    def uninstall(self) -> ServiceCommandResult:
        self.command_runner(["systemctl", "--user", "disable", SERVICE_NAME])
        self.command_runner(["systemctl", "--user", "stop", SERVICE_NAME])
        path = systemd_unit_path()
        if path.exists():
            path.unlink()
        self.command_runner(["systemctl", "--user", "daemon-reload"])
        return ServiceCommandResult(f"Uninstalled {SERVICE_NAME}.")

    def start(self) -> ServiceCommandResult:
        return self._run(["systemctl", "--user", "start", SERVICE_NAME])

    def stop(self) -> ServiceCommandResult:
        return self._run(["systemctl", "--user", "stop", SERVICE_NAME])

    def restart(self) -> ServiceCommandResult:
        return self._run(["systemctl", "--user", "restart", SERVICE_NAME])

    def status(self) -> ServiceRuntimeStatus:
        enabled_code, enabled_out, enabled_err = self.command_runner(["systemctl", "--user", "is-enabled", SERVICE_NAME])
        active_code, active_out, active_err = self.command_runner(["systemctl", "--user", "is-active", SERVICE_NAME])
        show_code, show_out, show_err = self.command_runner(
            ["systemctl", "--user", "show", SERVICE_NAME, "--property=MainPID,ExecMainStatus,Result"]
        )
        props = {}
        for line in show_out.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                props[key] = value
        pid_text = props.get("MainPID") or "0"
        pid = int(pid_text) if pid_text.isdigit() and int(pid_text) > 0 else None
        detail = (active_out or active_err or enabled_err or show_err).strip() or None
        return ServiceRuntimeStatus(
            platform=self.key,
            supported=True,
            installed=enabled_code == 0 or active_code == 0 or show_code == 0,
            enabled=enabled_out.strip() == "enabled" if enabled_code == 0 else False,
            active=active_out.strip() == "active" if active_code == 0 else False,
            pid=pid,
            detail=detail,
            error=None if show_code == 0 else (show_err.strip() or None),
        )

    def logs(self, lines: int = 100) -> ServiceCommandResult:
        count = str(max(1, int(lines)))
        code, stdout, stderr = self.command_runner(
            ["journalctl", "--user", "-u", SERVICE_NAME, "-n", count, "--no-pager"]
        )
        return ServiceCommandResult((stdout or stderr).strip() or "No cron service logs yet.", exit_code=0 if code == 0 else 1)
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_platforms.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/service_platforms/systemd_user.py tests/test_cron_service_platforms.py
git commit -m "feat: add cron systemd user service adapter"
```

---

### Task 3: macOS Launchd Adapter

**Files:**
- Create: `cron/service_platforms/launchd_user.py`
- Modify: `tests/test_cron_service_platforms.py`

- [ ] **Step 1: Add failing launchd adapter tests**

Append to `tests/test_cron_service_platforms.py`:

```python
def test_launchd_install_writes_plist_with_run_at_load(monkeypatch, tmp_path):
    import plistlib

    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    plist_path = tmp_path / "ai.langchain.agent.cron.plist"
    monkeypatch.setattr("cron.service_platforms.launchd_user.launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr("cron.service_platforms.launchd_user.launchd_log_paths", lambda: (tmp_path / "out.log", tmp_path / "err.log"))

    result = LaunchdUserCronService(command_runner=lambda args: (0, "", "")).install(
        ServiceInstallConfig(
            interval_seconds=45,
            lease_seconds=120,
            force=False,
            python_executable="/usr/bin/python3",
        )
    )

    payload = plistlib.loads(plist_path.read_bytes())
    assert result.exit_code == 0
    assert payload["Label"] == "ai.langchain.agent.cron"
    assert payload["RunAtLoad"] is True
    assert payload["ProgramArguments"] == [
        "/usr/bin/python3",
        "-m",
        "agent_cli.main",
        "cron",
        "serve",
        "--interval",
        "45",
        "--lease-seconds",
        "120",
    ]
    assert payload["StandardOutPath"] == str(tmp_path / "out.log")
    assert payload["StandardErrorPath"] == str(tmp_path / "err.log")


def test_launchd_start_bootstraps_and_kickstarts(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    calls = []
    plist_path = tmp_path / "agent.plist"
    plist_path.write_text("<plist />", encoding="utf-8")
    monkeypatch.setattr("cron.service_platforms.launchd_user.launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    def fake_run(args):
        calls.append(args)
        return 0, "", ""

    result = LaunchdUserCronService(command_runner=fake_run).start()

    assert result.exit_code == 0
    assert calls == [
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
        ["launchctl", "kickstart", "-k", "gui/501/ai.langchain.agent.cron"],
    ]


def test_launchd_logs_read_stdout_and_stderr(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    out = tmp_path / "out.log"
    err = tmp_path / "err.log"
    out.write_text("out1\nout2\n", encoding="utf-8")
    err.write_text("err1\n", encoding="utf-8")
    monkeypatch.setattr("cron.service_platforms.launchd_user.launchd_log_paths", lambda: (out, err))

    result = LaunchdUserCronService(command_runner=lambda args: (0, "", "")).logs(lines=2)

    assert result.exit_code == 0
    assert "== stdout ==" in result.message
    assert "out2" in result.message
    assert "== stderr ==" in result.message
    assert "err1" in result.message
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_platforms.py -q
```

Expected: FAIL because `launchd_user.py` does not exist.

- [ ] **Step 3: Implement launchd adapter**

Create `cron/service_platforms/launchd_user.py`:

```python
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from cron.paths import atomic_replace, get_cron_dir, secure_file
from cron.service_manager import (
    ServiceCommandResult,
    ServiceInstallConfig,
    ServiceRuntimeStatus,
    serve_command,
    service_environment,
)

LABEL = "ai.langchain.agent.cron"


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
    return log_dir / "service.out.log", log_dir / "service.err.log"


def _run_command(args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    return result.returncode, result.stdout or "", result.stderr or ""


def render_plist(config: ServiceInstallConfig) -> bytes:
    stdout_path, stderr_path = launchd_log_paths()
    env = service_environment()
    payload = {
        "Label": LABEL,
        "ProgramArguments": serve_command(config),
        "RunAtLoad": True,
        "KeepAlive": {"Crashed": True, "SuccessfulExit": False},
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    if env:
        payload["EnvironmentVariables"] = env
    return plistlib.dumps(payload, sort_keys=True)


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    items = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(items[-max(1, int(lines)):])


class LaunchdUserCronService:
    key = "launchd-user"

    def __init__(self, command_runner=_run_command) -> None:
        self.command_runner = command_runner

    def supported(self) -> bool:
        return shutil.which("launchctl") is not None

    def _run(self, args: list[str], *, ok_codes: set[int] | None = None) -> ServiceCommandResult:
        ok_codes = ok_codes or {0}
        code, stdout, stderr = self.command_runner(args)
        text = (stderr or stdout).strip()
        return ServiceCommandResult(text or "ok", exit_code=0 if code in ok_codes else 1)

    def install(self, config: ServiceInstallConfig) -> ServiceCommandResult:
        path = launchd_plist_path()
        if path.exists() and not config.force:
            return ServiceCommandResult(f"{path} already exists; rerun with --force to overwrite.", exit_code=2)
        path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path = launchd_log_paths()
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(render_plist(config))
        atomic_replace(tmp, path)
        secure_file(path)
        return ServiceCommandResult(f"Installed {LABEL}. Run `agent cron service start` to start it.")

    def uninstall(self) -> ServiceCommandResult:
        self.command_runner(["launchctl", "bootout", launchd_domain(), str(launchd_plist_path())])
        path = launchd_plist_path()
        if path.exists():
            path.unlink()
        return ServiceCommandResult(f"Uninstalled {LABEL}.")

    def start(self) -> ServiceCommandResult:
        path = launchd_plist_path()
        if not path.exists():
            return ServiceCommandResult("cron service is not installed; run `agent cron service install`.", exit_code=2)
        first = self._run(["launchctl", "bootstrap", launchd_domain(), str(path)], ok_codes={0, 5})
        if first.exit_code:
            return first
        return self._run(["launchctl", "kickstart", "-k", launchd_service_ref()])

    def stop(self) -> ServiceCommandResult:
        return self._run(["launchctl", "bootout", launchd_domain(), str(launchd_plist_path())], ok_codes={0, 5})

    def restart(self) -> ServiceCommandResult:
        stopped = self.stop()
        if stopped.exit_code:
            return stopped
        return self.start()

    def status(self) -> ServiceRuntimeStatus:
        path = launchd_plist_path()
        code, stdout, stderr = self.command_runner(["launchctl", "print", launchd_service_ref()])
        active = code == 0
        pid = None
        for line in stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("pid = "):
                value = stripped.split("=", 1)[1].strip()
                if value.isdigit():
                    pid = int(value)
        return ServiceRuntimeStatus(
            platform=self.key,
            supported=True,
            installed=path.exists(),
            enabled=path.exists(),
            active=active,
            pid=pid,
            detail="loaded" if active else "not loaded",
            error=None if active else (stderr.strip() or None),
        )

    def logs(self, lines: int = 100) -> ServiceCommandResult:
        stdout_path, stderr_path = launchd_log_paths()
        stdout_text = _tail(stdout_path, lines)
        stderr_text = _tail(stderr_path, lines)
        if not stdout_text and not stderr_text:
            return ServiceCommandResult("No cron service logs yet.")
        sections = []
        if stdout_text:
            sections.append(f"== stdout ==\n{stdout_text}")
        if stderr_text:
            sections.append(f"== stderr ==\n{stderr_text}")
        return ServiceCommandResult("\n".join(sections))
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_platforms.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/service_platforms/launchd_user.py tests/test_cron_service_platforms.py
git commit -m "feat: add cron launchd user service adapter"
```

---

### Task 4: CLI Parser and Service Command Rendering

**Files:**
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/cron_commands.py`
- Modify: `tests/test_agent_cli_main.py`
- Modify: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing CLI dispatch tests**

Append to `tests/test_agent_cli_main.py`:

```python
def test_main_cron_service_install_dispatches(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.main as main_module
    from agent_cli.cron_commands import CronCommandResult

    captured = {}

    def fake_install(**kwargs):
        captured.update(kwargs)
        return CronCommandResult("installed", exit_code=0)

    monkeypatch.setattr(main_module.cron_commands, "install_cron_service", fake_install)

    code = main_module.main(["cron", "service", "install", "--interval", "30", "--lease-seconds", "90", "--force"])

    assert code == 0
    assert captured == {"interval_seconds": 30.0, "lease_seconds": 90, "force": True}
    assert "installed" in capsys.readouterr().out


def test_main_cron_service_status_dispatches(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.main as main_module
    from agent_cli.cron_commands import CronCommandResult

    monkeypatch.setattr(
        main_module.cron_commands,
        "cron_service_status",
        lambda: CronCommandResult("service status", exit_code=0),
    )

    code = main_module.main(["cron", "service", "status"])

    assert code == 0
    assert "service status" in capsys.readouterr().out
```

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_install_cron_service_renders_manager_result(monkeypatch):
    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceCommandResult

    captured = {}

    def fake_install(**kwargs):
        captured.update(kwargs)
        return ServiceCommandResult("installed ok", exit_code=0)

    monkeypatch.setattr("cron.service_manager.install_service", fake_install)

    result = cron_commands.install_cron_service(interval_seconds=30, lease_seconds=90, force=True)

    assert result.exit_code == 0
    assert result.text == "installed ok"
    assert captured == {"interval_seconds": 30, "lease_seconds": 90, "force": True}


def test_cron_service_status_renders_composed_status(monkeypatch):
    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick={"due": 1, "ran": 1, "succeeded": 1, "failed": 0, "skipped": 0},
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_service_status()

    assert result.exit_code == 0
    assert "Service manager: systemd-user installed active enabled" in result.text
    assert "PID: 123" in result.text
    assert "Heartbeat: fresh" in result.text
    assert "Last tick: due=1 ran=1 succeeded=1 failed=0 skipped=0" in result.text
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_main_cron_service_install_dispatches tests/test_agent_cli_main.py::test_main_cron_service_status_dispatches tests/test_agent_cli_cron_commands.py::test_install_cron_service_renders_manager_result tests/test_agent_cli_cron_commands.py::test_cron_service_status_renders_composed_status -q
```

Expected: FAIL because parser and command functions are missing.

- [ ] **Step 3: Add parser and command functions**

Modify `agent_cli/main.py` near the existing cron parser:

```python
    cron_service = cron_subparsers.add_parser("service", parents=[public_options])
    cron_service_subparsers = cron_service.add_subparsers(dest="cron_service_command")

    cron_service_install = cron_service_subparsers.add_parser("install", parents=[public_options])
    cron_service_install.add_argument("--interval", type=float, default=60.0)
    cron_service_install.add_argument("--lease-seconds", type=int, default=180)
    cron_service_install.add_argument("--force", action="store_true")

    for service_action in ("uninstall", "start", "stop", "restart", "status"):
        cron_service_subparsers.add_parser(service_action, parents=[public_options])

    cron_service_logs = cron_service_subparsers.add_parser("logs", parents=[public_options])
    cron_service_logs.add_argument("--lines", type=int, default=100)
```

Modify `_handle_cron_command` in `agent_cli/main.py` before the normal cron command dispatch returns unknown:

```python
    if subcommand == "service":
        service_command = getattr(args, "cron_service_command", None)
        if service_command == "install":
            return cron_commands.install_cron_service(
                interval_seconds=args.interval,
                lease_seconds=args.lease_seconds,
                force=bool(getattr(args, "force", False)),
            )
        if service_command == "uninstall":
            return cron_commands.uninstall_cron_service()
        if service_command == "start":
            return cron_commands.start_cron_service()
        if service_command == "stop":
            return cron_commands.stop_cron_service()
        if service_command == "restart":
            return cron_commands.restart_cron_service()
        if service_command == "status":
            return cron_commands.cron_service_status()
        if service_command == "logs":
            return cron_commands.cron_service_logs(lines=args.lines)
```

Modify `agent_cli/cron_commands.py` with:

```python
def _cron_service_result(result) -> CronCommandResult:
    return CronCommandResult(result.message, exit_code=result.exit_code)


def install_cron_service(*, interval_seconds: float, lease_seconds: int, force: bool = False) -> CronCommandResult:
    from cron.service_manager import install_service

    return _cron_service_result(
        install_service(
            interval_seconds=interval_seconds,
            lease_seconds=lease_seconds,
            force=force,
        )
    )


def uninstall_cron_service() -> CronCommandResult:
    from cron.service_manager import uninstall_service

    return _cron_service_result(uninstall_service())


def start_cron_service() -> CronCommandResult:
    from cron.service_manager import start_service

    return _cron_service_result(start_service())


def stop_cron_service() -> CronCommandResult:
    from cron.service_manager import stop_service

    return _cron_service_result(stop_service())


def restart_cron_service() -> CronCommandResult:
    from cron.service_manager import restart_service

    return _cron_service_result(restart_service())


def cron_service_logs(*, lines: int = 100) -> CronCommandResult:
    from cron.service_manager import service_logs

    return _cron_service_result(service_logs(lines=lines))


def _service_manager_summary_line(status) -> str:
    if not status.supported:
        return "Service manager: unsupported"
    installed = "installed" if status.installed else "not-installed"
    active = "active" if status.active else "inactive"
    enabled = "enabled" if status.enabled else "disabled"
    return f"Service manager: {status.platform} {installed} {active} {enabled}"


def _last_tick_line(last_tick: dict[str, Any]) -> str:
    return (
        "Last tick: "
        f"due={last_tick.get('due', 0)} "
        f"ran={last_tick.get('ran', 0)} "
        f"succeeded={last_tick.get('succeeded', 0)} "
        f"failed={last_tick.get('failed', 0)} "
        f"skipped={last_tick.get('skipped', 0)}"
    )


def cron_service_status() -> CronCommandResult:
    from cron.service_manager import compose_service_status

    status = compose_service_status()
    lines = [
        _service_manager_summary_line(status),
        f"Automatic scheduling: {'enabled' if status.supported and status.installed and status.active and status.heartbeat_fresh else 'not-ready'}",
    ]
    if status.pid:
        lines.append(f"PID: {status.pid}")
    lines.append(f"Heartbeat: {'fresh' if status.heartbeat_fresh else 'stale-or-missing'}")
    if status.process_state:
        lines.append(f"Process state: {status.process_state}")
    if status.leader_state:
        lines.append(f"Leader state: {status.leader_state}")
    if status.last_heartbeat_at:
        lines.append(f"Last heartbeat: {status.last_heartbeat_at}")
    if status.last_tick:
        lines.append(_last_tick_line(status.last_tick))
    if status.last_error:
        lines.append(f"Last service error: {status.last_error}")
    if status.exit_reason:
        lines.append(f"Exit reason: {status.exit_reason}")
    if status.error:
        lines.append(f"Platform error: {status.error}")
    return CronCommandResult("\n".join(lines), exit_code=0 if status.supported else 2)
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_main_cron_service_install_dispatches tests/test_agent_cli_main.py::test_main_cron_service_status_dispatches tests/test_agent_cli_cron_commands.py::test_install_cron_service_renders_manager_result tests/test_agent_cli_cron_commands.py::test_cron_service_status_renders_composed_status -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/main.py agent_cli/cron_commands.py tests/test_agent_cli_main.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: add cron service cli commands"
```

---

### Task 5: Integrate Service Runtime into Cron Status and Doctor

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Modify: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing status and doctor tests**

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_status_includes_automatic_scheduling_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T10:00:00+00:00",
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_status()

    assert "Service manager: systemd-user installed active enabled" in result.text
    assert "Automatic scheduling: enabled" in result.text


def test_cron_doctor_suggests_service_install_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=False,
            enabled=False,
            active=False,
            pid=None,
            detail="not installed",
            error=None,
            heartbeat_fresh=False,
            process_state=None,
            leader_state=None,
            last_heartbeat_at=None,
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_doctor()

    assert result.exit_code == 1
    assert "[warn] cron service: not installed; run `agent cron service install`" in result.text


def test_cron_doctor_suggests_restart_when_heartbeat_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.cron_commands as cron_commands
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
            error=None,
            heartbeat_fresh=False,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-01T09:00:00+00:00",
            last_tick=None,
            last_error=None,
            exit_reason=None,
        ),
    )

    result = cron_commands.cron_doctor()

    assert "[warn] cron service heartbeat: stale; run `agent cron service restart`" in result.text
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_status_includes_automatic_scheduling_summary tests/test_agent_cli_cron_commands.py::test_cron_doctor_suggests_service_install_when_missing tests/test_agent_cli_cron_commands.py::test_cron_doctor_suggests_restart_when_heartbeat_stale -q
```

Expected: FAIL because `cron_status` and `cron_doctor` do not use `compose_service_status()`.

- [ ] **Step 3: Add helper and integrate status/doctor**

Modify `agent_cli/cron_commands.py`.

Add:

```python
def _automatic_scheduling_line(status) -> str:
    ready = status.supported and status.installed and status.active and status.heartbeat_fresh
    return f"Automatic scheduling: {'enabled' if ready else 'not-ready'}"
```

In `cron_status()`, after `Runner mode`, add:

```python
    from cron.service_manager import compose_service_status

    service_status = compose_service_status()
```

And include these lines in the existing `lines` list:

```python
        _service_manager_summary_line(service_status),
        _automatic_scheduling_line(service_status),
```

In `cron_doctor()`, after the existing `_add_service_heartbeat_check(add)` call or replacing that older heartbeat-only call, add:

```python
    from cron.service_manager import compose_service_status

    service_status = compose_service_status()
    if not service_status.supported:
        add("warn", "cron service: unsupported platform; use `agent cron serve`")
    elif not service_status.installed:
        add("warn", "cron service: not installed; run `agent cron service install`")
    elif not service_status.active:
        add("warn", "cron service: installed but inactive; run `agent cron service start`")
    elif not service_status.heartbeat_fresh:
        add("warn", "cron service heartbeat: stale; run `agent cron service restart`")
    else:
        add("ok", f"cron service: running ({service_status.platform})")
```

Remove `_add_service_heartbeat_check(add)` from `cron_doctor()` if it creates duplicate heartbeat warnings. Keep the helper itself until no tests import it; deleting it is optional.

- [ ] **Step 4: Run tests and verify they pass**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_status_includes_automatic_scheduling_summary tests/test_agent_cli_cron_commands.py::test_cron_doctor_suggests_service_install_when_missing tests/test_agent_cli_cron_commands.py::test_cron_doctor_suggests_restart_when_heartbeat_stale -q
```

Expected: PASS.

- [ ] **Step 5: Run broader cron CLI tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: surface cron service health in status and doctor"
```

---

### Task 6: Documentation and Final Verification

**Files:**
- Modify: `README.md`
- Test: existing cron and CLI test suites

- [ ] **Step 1: Update README cron scheduling section**

Modify the `## Cron Scheduling` section in `README.md` to include:

```markdown
## Cron Scheduling

Cron jobs can run through a local user-level service or through an embedding
application.

For local automatic scheduling:

```bash
python -m agent_cli cron service install
python -m agent_cli cron service start
python -m agent_cli cron service status
python -m agent_cli cron service logs
```

Linux uses user systemd when available. macOS uses a user LaunchAgent. These
services run the existing foreground service command:

```bash
python -m agent_cli cron serve --interval 60 --lease-seconds 180
```

Use `python -m agent_cli cron doctor` when jobs are not firing automatically.
If user-level services are not supported on the current platform, keep the
foreground service running with `python -m agent_cli cron serve`.

Embedding applications that want to own scheduling can still call:

```python
from agent_core.cron_lifecycle import start_cron_scheduler, stop_cron_scheduler

start_cron_scheduler(interval_seconds=60)
```
```

Keep the existing `build_agent(include_cron_tools=True)` and delivery paragraph after this text.

- [ ] **Step 2: Run service-focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_manager.py tests/test_cron_service_platforms.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py -q
```

Expected: PASS.

- [ ] **Step 3: Run existing cron service and scheduler tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service.py tests/test_cron_service_state.py tests/test_cron_scheduler.py tests/test_cron_state_store.py -q
```

Expected: PASS.

- [ ] **Step 4: Run foreground smoke command**

Run:

```bash
AGENT_CRON_HOME=/tmp/agent-cron-user-service-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli.main cron serve --once --interval 1 --lease-seconds 5
```

Expected: exit code 0 and output containing:

```text
Cron service exited.
```

- [ ] **Step 5: Inspect git status**

Run:

```bash
git status --short
```

Expected: only `README.md` and files intentionally changed by this plan are modified. Pre-existing untracked files such as `docs/superpowers/plans/2026-06-01-cron-concurrency-cli-management.md` may remain untracked.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: document cron user service runtime"
```

---

## Final Verification

- [ ] Run the combined regression command:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_manager.py tests/test_cron_service_platforms.py tests/test_cron_service.py tests/test_cron_service_state.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py -q
```

Expected: PASS.

- [ ] Run `git log --oneline -n 6` and verify the task commits are present.

Expected: commits for service manager core, systemd adapter, launchd adapter, CLI commands, status/doctor integration, and docs.

## Self-Review Notes

- Spec coverage: this plan covers user-level install/start/stop/restart/uninstall/status/logs, Linux systemd-user, macOS launchd-user, status/doctor integration, recovery boundaries, and README rollout notes.
- Scope control: it does not implement system-level services, Windows service support, multi-profile service naming, runner default changes, log rotation, or cron state mutation from service manager commands.
- Type consistency: the shared dataclasses are defined in Task 1 and reused by adapters and CLI tasks. The CLI wrapper returns existing `CronCommandResult` objects.

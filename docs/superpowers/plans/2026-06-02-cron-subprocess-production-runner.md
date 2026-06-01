# Cron Subprocess Production Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make subprocess execution the recommended production cron service path while keeping development defaults in-process.

**Architecture:** Add a small profile/runner policy layer in `cron.service_manager`, pass CLI profile context through `agent_cli.main`, and keep platform renderers focused on rendering the resolved service environment. Add a worker protocol smoke and runner temp maintenance helpers to `cron.runner_subprocess`, with `cron.runner_worker --smoke` handling a no-agent child-process check.

**Tech Stack:** Python stdlib, argparse, subprocess, pathlib, pytest, user systemd units, launchd plists.

---

## File Map

- Modify `cron/service_manager.py`: add profile policy, service install environment overrides, and install result detail.
- Modify `cron/service_platforms/systemd_user.py`: render environment from install config instead of only process env.
- Modify `cron/service_platforms/launchd_user.py`: render environment from install config instead of only process env.
- Modify `agent_cli/main.py`: pass parsed CLI profile into cron status, doctor, and service install; add `--cleanup-runner-tmp`.
- Modify `agent_cli/cron_commands.py`: accept CLI profile context, show profile/runner diagnostics, call smoke and tmp helpers.
- Modify `cron/runner_subprocess.py`: add worker smoke and runner tmp residual inspection/cleanup helpers.
- Modify `cron/runner_worker.py`: add `--smoke` path that validates protocol and writes a success result without importing the real runner.
- Modify `README.md`: document prod/hosted subprocess recommendation and doctor cleanup flag.
- Modify tests:
  - `tests/test_cron_service_manager.py`
  - `tests/test_cron_service_platforms.py`
  - `tests/test_agent_cli_main.py`
  - `tests/test_agent_cli_cron_commands.py`
  - `tests/test_cron_runner_subprocess.py`
  - `tests/test_cron_runner_worker.py`

## Task 1: Profile and Runner Policy

**Files:**
- Modify: `cron/service_manager.py`
- Test: `tests/test_cron_service_manager.py`

- [ ] **Step 1: Write failing policy tests**

Add these tests to `tests/test_cron_service_manager.py`:

```python
def test_service_install_config_injects_subprocess_for_runtime_prod(monkeypatch):
    monkeypatch.delenv("AGENT_CRON_RUNNER_MODE", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")

    from cron.service_manager import build_service_install_config

    config, detail = build_service_install_config(
        interval_seconds=60,
        lease_seconds=180,
        force=False,
        cli_profile=None,
    )

    assert config.environment_overrides["AGENT_CRON_RUNNER_MODE"] == "subprocess"
    assert detail.effective_profile == "prod"
    assert detail.runner_mode == "subprocess"
    assert detail.runner_mode_source == "auto"
    assert detail.production_recommended is True


def test_service_install_config_injects_subprocess_for_cli_prod(monkeypatch):
    monkeypatch.delenv("AGENT_CRON_RUNNER_MODE", raising=False)
    monkeypatch.delenv("AGENT_RUNTIME_PROFILE", raising=False)

    from cron.service_manager import build_service_install_config

    config, detail = build_service_install_config(
        interval_seconds=60,
        lease_seconds=180,
        force=False,
        cli_profile="prod",
    )

    assert config.environment_overrides["AGENT_CRON_RUNNER_MODE"] == "subprocess"
    assert detail.effective_profile == "prod"
    assert detail.profile_source == "cli"
    assert detail.runner_mode_source == "auto"


def test_service_install_config_preserves_explicit_inprocess_for_prod(monkeypatch):
    monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "inprocess")
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")

    from cron.service_manager import build_service_install_config

    config, detail = build_service_install_config(
        interval_seconds=60,
        lease_seconds=180,
        force=False,
        cli_profile=None,
    )

    assert config.environment_overrides == {}
    assert detail.runner_mode == "inprocess"
    assert detail.runner_mode_source == "explicit"
    assert detail.production_recommended is False


def test_service_install_config_keeps_dev_inprocess(monkeypatch):
    monkeypatch.delenv("AGENT_CRON_RUNNER_MODE", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "dev")

    from cron.service_manager import build_service_install_config

    config, detail = build_service_install_config(
        interval_seconds=60,
        lease_seconds=180,
        force=False,
        cli_profile="dev",
    )

    assert config.environment_overrides == {}
    assert detail.effective_profile == "dev"
    assert detail.runner_mode == "inprocess"
    assert detail.runner_mode_source == "default"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_cron_service_manager.py -q
```

Expected: FAIL because `build_service_install_config`, `environment_overrides`, and install detail fields do not exist.

- [ ] **Step 3: Implement policy helpers**

In `cron/service_manager.py`, extend imports and dataclasses:

```python
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping
```

Replace the current `ServiceInstallConfig` definition with:

```python
@dataclass(frozen=True)
class ServiceInstallConfig:
    interval_seconds: float
    lease_seconds: int
    force: bool = False
    python_executable: str = sys.executable
    environment_overrides: Mapping[str, str] = field(default_factory=dict)
```

Add this dataclass near `ServiceInstallConfig`:

```python
@dataclass(frozen=True)
class ServiceInstallDetail:
    effective_profile: str
    profile_source: str
    runner_mode: str
    runner_mode_source: str
    production_recommended: bool
```

Add these helpers in `cron/service_manager.py`:

```python
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
        production_recommended=(profile not in PRODUCTION_RUNTIME_PROFILES or runner_mode == "subprocess"),
    )
    config = ServiceInstallConfig(
        interval_seconds=interval_seconds,
        lease_seconds=lease_seconds,
        force=force,
        environment_overrides=MappingProxyType(overrides),
    )
    return config, detail
```

Update `service_environment` to accept overrides:

```python
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
```

Update `install_service` signature and body:

```python
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
        suffix = (
            f" Runner mode: subprocess for {detail.effective_profile} profile."
        )
    else:
        suffix = f" Runner mode: {detail.runner_mode}."
    return ServiceCommandResult(result.message + suffix, exit_code=result.exit_code)
```

- [ ] **Step 4: Run tests to verify Task 1 passes**

Run:

```bash
python -m pytest tests/test_cron_service_manager.py -q
```

Expected: PASS for `tests/test_cron_service_manager.py`.

- [ ] **Step 5: Commit**

```bash
git add cron/service_manager.py tests/test_cron_service_manager.py
git commit -m "feat: resolve cron service runner profile policy"
```

## Task 2: Service Install Environment Rendering

**Files:**
- Modify: `cron/service_platforms/systemd_user.py`
- Modify: `cron/service_platforms/launchd_user.py`
- Test: `tests/test_cron_service_platforms.py`

- [ ] **Step 1: Write failing renderer tests**

Add these tests to `tests/test_cron_service_platforms.py`:

```python
def test_systemd_install_auto_injects_subprocess_for_prod(monkeypatch, tmp_path):
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
    monkeypatch.delenv("AGENT_CRON_RUNNER_MODE", raising=False)

    service = SystemdUserCronService(command_runner=fake_run)
    result = service.install(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            force=False,
            python_executable="/usr/bin/python3",
            environment_overrides={"AGENT_CRON_RUNNER_MODE": "subprocess"},
        )
    )

    unit = (tmp_path / "langchain-agent-cron.service").read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert 'Environment="AGENT_CRON_RUNNER_MODE=subprocess"' in unit


def test_launchd_install_auto_injects_subprocess_for_prod(monkeypatch, tmp_path):
    import plistlib

    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: tmp_path / "ai.langchain.agent.cron.plist",
    )
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_log_paths",
        lambda: (tmp_path / "out.log", tmp_path / "err.log"),
    )
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "cron-home"))
    monkeypatch.delenv("AGENT_CRON_RUNNER_MODE", raising=False)

    service = LaunchdUserCronService(command_runner=lambda args: (0, "ok", ""))
    result = service.install(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            force=False,
            python_executable="/usr/bin/python3",
            environment_overrides={"AGENT_CRON_RUNNER_MODE": "subprocess"},
        )
    )

    payload = plistlib.loads(
        (tmp_path / "ai.langchain.agent.cron.plist").read_bytes()
    )
    assert result.exit_code == 0
    assert payload["EnvironmentVariables"]["AGENT_CRON_RUNNER_MODE"] == "subprocess"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_cron_service_platforms.py -q
```

Expected: FAIL because platform renderers ignore `config.environment_overrides`.

- [ ] **Step 3: Implement renderer support**

In `cron/service_platforms/systemd_user.py`, change `_format_environment`:

```python
def _format_environment(config: ServiceInstallConfig) -> str:
    return "\n".join(
        f'Environment="{key}={_quote_systemd(value)}"'
        for key, value in sorted(service_environment(config.environment_overrides).items())
        if "\n" not in value and "\r" not in value
    )
```

Update `render_unit`:

```python
def render_unit(config: ServiceInstallConfig) -> str:
    command = " ".join(_quote_systemd_arg(arg) for arg in serve_command(config))
    environment = _format_environment(config)
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
```

In `cron/service_platforms/launchd_user.py`, change `_environment_variables`:

```python
def _environment_variables(config: ServiceInstallConfig) -> dict[str, str]:
    return {
        key: value
        for key, value in sorted(service_environment(config.environment_overrides).items())
        if "\n" not in value and "\r" not in value
    }
```

Update `render_plist`:

```python
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
    environment = _environment_variables(config)
    if environment:
        payload["EnvironmentVariables"] = environment
    return plistlib.dumps(payload, sort_keys=True)
```

- [ ] **Step 4: Run renderer tests**

Run:

```bash
python -m pytest tests/test_cron_service_platforms.py tests/test_cron_service_manager.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cron/service_platforms/systemd_user.py cron/service_platforms/launchd_user.py tests/test_cron_service_platforms.py
git commit -m "feat: install prod cron service with subprocess runner"
```

## Task 3: Worker Smoke and Runner Temp Maintenance

**Files:**
- Modify: `cron/runner_worker.py`
- Modify: `cron/runner_subprocess.py`
- Test: `tests/test_cron_runner_worker.py`
- Test: `tests/test_cron_runner_subprocess.py`

- [ ] **Step 1: Write failing worker smoke tests**

Add this test to `tests/test_cron_runner_worker.py`:

```python
class TestWorkerSmoke:
    def test_smoke_writes_success_without_real_runner(self, tmp_path):
        output_path = tmp_path / "result.json"
        input_path = tmp_path / "input.json"
        input_path.write_text(
            json.dumps({"version": 1, "smoke": True}),
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cron.runner_worker",
                "--smoke",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={**subprocess.os.environ, "AGENT_CRON_HOME": str(tmp_path)},
        )

        assert result.returncode == 0, result.stderr
        data = json.loads(output_path.read_text(encoding="utf-8"))
        assert data == {
            "version": 1,
            "success": True,
            "output_doc": "runner_worker smoke ok",
            "final_response": "runner_worker smoke ok",
            "error": None,
            "exit_reason": None,
        }
```

- [ ] **Step 2: Write failing parent smoke and temp tests**

Add these tests to `tests/test_cron_runner_subprocess.py`:

```python
def test_worker_protocol_smoke_succeeds_and_cleans_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.paths import get_runner_tmp_dir
    from cron.runner_subprocess import worker_protocol_smoke

    result = worker_protocol_smoke(timeout_seconds=10)

    assert result.ok is True
    assert result.error is None
    assert list(get_runner_tmp_dir().iterdir()) == []


def test_runner_tmp_residuals_report_old_directory(monkeypatch, tmp_path):
    import os
    import time

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.paths import get_runner_tmp_dir
    from cron.runner_subprocess import inspect_runner_tmp

    old_dir = get_runner_tmp_dir() / "abcdef1234567890"
    old_dir.mkdir(parents=True)
    old_time = time.time() - (25 * 60 * 60)
    os.utime(old_dir, (old_time, old_time))

    summary = inspect_runner_tmp(stale_after_seconds=24 * 60 * 60)

    assert summary.total == 1
    assert summary.stale == 1
    assert summary.oldest_age_seconds is not None
    assert summary.oldest_age_seconds >= 24 * 60 * 60


def test_cleanup_runner_tmp_removes_only_old_valid_dirs(monkeypatch, tmp_path):
    import os
    import time

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    from cron.paths import get_runner_tmp_dir
    from cron.runner_subprocess import cleanup_runner_tmp

    root = get_runner_tmp_dir()
    old_valid = root / "abcdef1234567890"
    fresh_valid = root / "1234567890abcdef"
    invalid = root / "not-a-run-dir"
    for path in (old_valid, fresh_valid, invalid):
        path.mkdir(parents=True, exist_ok=True)
    old_time = time.time() - (25 * 60 * 60)
    os.utime(old_valid, (old_time, old_time))

    result = cleanup_runner_tmp(stale_after_seconds=24 * 60 * 60)

    assert result.removed == 1
    assert result.failed == 0
    assert not old_valid.exists()
    assert fresh_valid.exists()
    assert invalid.exists()
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_cron_runner_worker.py tests/test_cron_runner_subprocess.py -q
```

Expected: FAIL because `--smoke`, `worker_protocol_smoke`, `inspect_runner_tmp`, and `cleanup_runner_tmp` do not exist.

- [ ] **Step 4: Implement `--smoke` in worker**

In `cron/runner_worker.py`, add:

```python
def _success_result(message: str) -> dict[str, Any]:
    return {
        "version": _PROTOCOL_VERSION,
        "success": True,
        "output_doc": message,
        "final_response": message,
        "error": None,
        "exit_reason": None,
    }


def _run_smoke(input_path: Path, output_path: Path) -> int:
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _write_result(output_path, _failure_result(f"Smoke input error: {exc}"))
        return 0
    if payload.get("version") != _PROTOCOL_VERSION or payload.get("smoke") is not True:
        _write_result(output_path, _failure_result("Smoke input payload is invalid"))
        return 0
    _write_result(output_path, _success_result("runner_worker smoke ok"))
    return 0
```

Update argument parsing in `main()`:

```python
parser.add_argument("--smoke", action="store_true", help="Run protocol smoke without executing a job")
```

After `input_path` and `output_path` are assigned, add:

```python
if args.smoke:
    return _run_smoke(input_path, output_path)
```

- [ ] **Step 5: Implement parent smoke and temp helpers**

In `cron/runner_subprocess.py`, add imports:

```python
import re
import time
from dataclasses import dataclass
```

Add constants near temp management:

```python
_SMOKE_TIMEOUT_DEFAULT = 10
_TMP_STALE_AFTER_DEFAULT = 24 * 60 * 60
_RUN_DIR_RE = re.compile(r"^[0-9a-f]{16}$")
_SMOKE_DIR_PREFIX = "smoke-"
```

Add dataclasses and helpers before `run_job_subprocess`:

```python
@dataclass(frozen=True)
class WorkerSmokeResult:
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class RunnerTmpSummary:
    total: int
    stale: int
    oldest_age_seconds: int | None
    path: Path


@dataclass(frozen=True)
class RunnerTmpCleanupResult:
    removed: int
    failed: int
    remaining: int
    path: Path


def _is_runner_tmp_child(path: Path) -> bool:
    return path.is_dir() and (
        _RUN_DIR_RE.match(path.name) is not None or path.name.startswith(_SMOKE_DIR_PREFIX)
    )


def _runner_tmp_children() -> list[Path]:
    root = get_runner_tmp_dir()
    if not root.exists():
        return []
    return [path for path in root.iterdir() if _is_runner_tmp_child(path)]


def inspect_runner_tmp(*, stale_after_seconds: int = _TMP_STALE_AFTER_DEFAULT) -> RunnerTmpSummary:
    root = get_runner_tmp_dir()
    now = time.time()
    children = _runner_tmp_children()
    ages = [max(0, int(now - child.stat().st_mtime)) for child in children]
    stale_count = sum(1 for age in ages if age >= stale_after_seconds)
    return RunnerTmpSummary(
        total=len(children),
        stale=stale_count,
        oldest_age_seconds=max(ages) if ages else None,
        path=root,
    )


def cleanup_runner_tmp(*, stale_after_seconds: int = _TMP_STALE_AFTER_DEFAULT) -> RunnerTmpCleanupResult:
    root = get_runner_tmp_dir()
    now = time.time()
    removed = 0
    failed = 0
    for child in _runner_tmp_children():
        try:
            age = max(0, int(now - child.stat().st_mtime))
            if age < stale_after_seconds:
                continue
            shutil.rmtree(child)
            removed += 1
        except OSError:
            failed += 1
    remaining = len(_runner_tmp_children())
    return RunnerTmpCleanupResult(removed=removed, failed=failed, remaining=remaining, path=root)


def worker_protocol_smoke(*, timeout_seconds: int = _SMOKE_TIMEOUT_DEFAULT) -> WorkerSmokeResult:
    tmp_root = get_runner_tmp_dir()
    tmp_root.mkdir(parents=True, exist_ok=True)
    secure_dir(tmp_root)
    smoke_dir = tmp_root / f"{_SMOKE_DIR_PREFIX}{uuid.uuid4().hex[:16]}"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    secure_dir(smoke_dir)
    input_path = smoke_dir / "input.json"
    output_path = smoke_dir / "result.json"
    try:
        input_path.write_text(json.dumps({"version": _RESULT_VERSION, "smoke": True}), encoding="utf-8")
        secure_file(input_path)
        cmd = [
            sys.executable,
            "-m",
            "cron.runner_worker",
            "--smoke",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            return WorkerSmokeResult(False, f"runner_worker smoke exited with code {proc.returncode}{suffix}")
        parsed = _parse_result_file(output_path)
        if not parsed.success:
            return WorkerSmokeResult(False, parsed.error or "runner_worker smoke failed")
        return WorkerSmokeResult(True)
    except subprocess.TimeoutExpired:
        return WorkerSmokeResult(False, f"runner_worker smoke timed out after {timeout_seconds}s")
    except Exception as exc:
        return WorkerSmokeResult(False, str(exc))
    finally:
        _cleanup_temp_dir(smoke_dir)
```

- [ ] **Step 6: Run runner tests**

Run:

```bash
python -m pytest tests/test_cron_runner_worker.py tests/test_cron_runner_subprocess.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cron/runner_worker.py cron/runner_subprocess.py tests/test_cron_runner_worker.py tests/test_cron_runner_subprocess.py
git commit -m "feat: smoke test cron runner subprocess protocol"
```

## Task 4: CLI Doctor and Status Integration

**Files:**
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_main.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing main dispatch tests**

Add tests to `tests/test_agent_cli_main.py`:

```python
def test_main_cron_service_install_passes_cli_profile(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.main as main_module
    from agent_cli.cron_commands import CronCommandResult

    captured = {}

    def fake_install(**kwargs):
        captured.update(kwargs)
        return CronCommandResult("installed", exit_code=0)

    monkeypatch.setattr(main_module.cron_commands, "install_cron_service", fake_install)

    code = main_module.main(
        ["--profile", "prod", "cron", "service", "install", "--interval", "30"]
    )

    assert code == 0
    assert captured["cli_profile"] == "prod"


def test_main_cron_doctor_passes_cleanup_and_profile(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.main as main_module
    from agent_cli.cron_commands import CronCommandResult

    captured = {}

    def fake_doctor(**kwargs):
        captured.update(kwargs)
        return CronCommandResult("doctor", exit_code=0)

    monkeypatch.setattr(main_module.cron_commands, "cron_doctor", fake_doctor)

    code = main_module.main(
        ["--profile", "prod", "cron", "doctor", "--cleanup-runner-tmp"]
    )

    assert code == 0
    assert captured == {"cli_profile": "prod", "cleanup_runner_tmp": True}
```

- [ ] **Step 2: Write failing doctor/status tests**

Add tests to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_doctor_warns_for_prod_inprocess(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")
    monkeypatch.delenv("AGENT_CRON_RUNNER_MODE", raising=False)

    import agent_cli.cron_commands as cron_commands

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])

    result = cron_commands.cron_doctor(cli_profile=None)

    assert result.exit_code == 1
    assert "[ok] effective profile: prod (runtime)" in result.text
    assert "[ok] runner mode: inprocess" in result.text
    assert "[warn] prod/hosted cron service should use AGENT_CRON_RUNNER_MODE=subprocess" in result.text


def test_cron_doctor_runs_worker_smoke_for_subprocess(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_CRON_RUNNER_MODE", "subprocess")

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import WorkerSmokeResult, RunnerTmpSummary

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr("cron.runner_subprocess.worker_protocol_smoke", lambda: WorkerSmokeResult(True))
    monkeypatch.setattr(
        "cron.runner_subprocess.inspect_runner_tmp",
        lambda: RunnerTmpSummary(total=0, stale=0, oldest_age_seconds=None, path=tmp_path),
    )

    result = cron_commands.cron_doctor(cli_profile=None)

    assert result.exit_code == 0
    assert "[ok] runner_worker smoke: ok" in result.text


def test_cron_doctor_cleanup_runner_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import RunnerTmpCleanupResult

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "_add_service_heartbeat_check", lambda add: None)
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=False: [])
    monkeypatch.setattr(
        "cron.runner_subprocess.cleanup_runner_tmp",
        lambda: RunnerTmpCleanupResult(removed=2, failed=0, remaining=1, path=tmp_path),
    )

    result = cron_commands.cron_doctor(cli_profile=None, cleanup_runner_tmp=True)

    assert "runner tmp cleanup: removed=2 remaining=1" in result.text


def test_cron_status_renders_effective_profile_and_tmp_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")

    import agent_cli.cron_commands as cron_commands
    from cron.runner_subprocess import RunnerTmpSummary

    monkeypatch.setattr(cron_commands, "display_cron_home", lambda: str(tmp_path))
    monkeypatch.setattr(cron_commands, "get_jobs_file", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [])
    monkeypatch.setattr(cron_commands, "_delivery_stats_lines", lambda: [])
    monkeypatch.setattr(
        "cron.runner_subprocess.inspect_runner_tmp",
        lambda: RunnerTmpSummary(total=3, stale=1, oldest_age_seconds=90000, path=tmp_path),
    )

    result = cron_commands.cron_status(cli_profile=None)

    assert "Effective profile: prod (runtime)" in result.text
    assert "Runner tmp: total=3 stale=1 oldest=90000s" in result.text
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_agent_cli_main.py tests/test_agent_cli_cron_commands.py -q
```

Expected: FAIL because CLI signatures and `--cleanup-runner-tmp` do not exist.

- [ ] **Step 4: Implement CLI argument passing**

In `agent_cli/main.py`, add the cron doctor argument where cron subcommands are defined:

```python
cron_doctor_parser = cron_subparsers.add_parser(
    "doctor",
    help="Run cron health checks.",
    parents=[public_options],
)
cron_doctor_parser.add_argument(
    "--cleanup-runner-tmp",
    action="store_true",
    help="Delete stale cron runner temp directories older than 24 hours.",
)
```

If a bare `cron_subparsers.add_parser("doctor", ...)` already exists, replace it with the block above.

In the cron dispatch function, update these calls:

```python
if subcommand == "status":
    return cron_commands.cron_status(cli_profile=getattr(args, "profile", None))
if subcommand == "doctor":
    return cron_commands.cron_doctor(
        cli_profile=getattr(args, "profile", None),
        cleanup_runner_tmp=bool(getattr(args, "cleanup_runner_tmp", False)),
    )
```

Update service install dispatch:

```python
return cron_commands.install_cron_service(
    interval_seconds=args.interval,
    lease_seconds=args.lease_seconds,
    force=bool(getattr(args, "force", False)),
    cli_profile=getattr(args, "profile", None),
)
```

- [ ] **Step 5: Implement cron command diagnostics**

In `agent_cli/cron_commands.py`, import no new top-level heavy modules. Update `install_cron_service`:

```python
def install_cron_service(
    *,
    interval_seconds: float,
    lease_seconds: int,
    force: bool = False,
    cli_profile: str | None = None,
) -> CronCommandResult:
    from cron.service_manager import install_service

    return _cron_service_result(
        install_service(
            interval_seconds=interval_seconds,
            lease_seconds=lease_seconds,
            force=force,
            cli_profile=cli_profile,
        )
    )
```

Add helper functions near `_check_line`:

```python
def _profile_line(cli_profile: str | None) -> tuple[str, str, str]:
    from cron.service_manager import effective_runtime_profile

    profile, source = effective_runtime_profile(cli_profile=cli_profile)
    return profile, source, f"effective profile: {profile} ({source})"


def _tmp_summary_line(summary) -> str:
    oldest = "-" if summary.oldest_age_seconds is None else f"{summary.oldest_age_seconds}s"
    return (
        "runner tmp: "
        f"total={summary.total} stale={summary.stale} oldest={oldest}"
    )
```

Change `cron_status` signature and body:

```python
def cron_status(*, cli_profile: str | None = None) -> CronCommandResult:
    from cron.delivery_registry import default_delivery_registry
    from cron.runner_client import runner_mode_diagnostic
    from cron.runner_subprocess import inspect_runner_tmp
    from cron.service_manager import compose_service_status
    from cron.state_store import StateStore

    jobs = list_jobs(include_disabled=True)
    state_store = StateStore()
    service_status = compose_service_status()
    profile, profile_source, profile_text = _profile_line(cli_profile)
    mode, mode_ok = runner_mode_diagnostic()
    mode_label = f"{'ok' if mode_ok else 'UNSUPPORTED'} ({mode})"
    counts = state_store.job_counts_by_state()
    count_text = ", ".join(f"{state}={count}" for state, count in sorted(counts.items())) or "-"
    tmp_summary = inspect_runner_tmp()
    lines = [
        *_service_status_lines(),
        _service_manager_summary_line(service_status),
        _automatic_scheduling_line(service_status),
        f"Effective profile: {profile} ({profile_source})",
        f"Cron home: {display_cron_home()}",
        f"Cron sqlite: {state_store.path}",
        f"Runner mode: {mode_label}",
        f"Runner tmp: total={tmp_summary.total} stale={tmp_summary.stale} oldest={'-' if tmp_summary.oldest_age_seconds is None else str(tmp_summary.oldest_age_seconds) + 's'}",
        f"Jobs file: {get_jobs_file()}",
        f"Output dir: {get_output_dir()}",
        f"Scripts dir: {get_scripts_dir()}",
        f"Jobs: {len(jobs)}",
        f"Job states: {count_text}",
        f"Delivery adapters: {', '.join(default_delivery_registry().adapter_keys())}",
    ]
```

Preserve the existing `cron_status` sections after this initialization: next due,
running, queued, stale, latest failed run, subprocess timeout, delivery stats,
and the final `return CronCommandResult("\n".join(lines))`.

Change `cron_doctor` signature:

```python
def cron_doctor(
    *,
    cli_profile: str | None = None,
    cleanup_runner_tmp: bool = False,
) -> CronCommandResult:
```

At the top of `cron_doctor`, import:

```python
from cron.service_manager import PRODUCTION_RUNTIME_PROFILES, effective_runtime_profile
```

Replace the current runner-mode block with:

```python
profile, profile_source = effective_runtime_profile(cli_profile=cli_profile)
add("ok", f"effective profile: {profile} ({profile_source})")

mode, mode_ok = runner_mode_diagnostic()
if mode_ok:
    add("ok", f"runner mode: {mode}")
else:
    add("fail", f"runner mode: {mode!r} (unsupported)")
if profile in PRODUCTION_RUNTIME_PROFILES and mode != "subprocess" and mode_ok:
    add("warn", "prod/hosted cron service should use AGENT_CRON_RUNNER_MODE=subprocess")
```

For subprocess mode, replace the `--help` entrypoint check with:

```python
timeout_val, timeout_ok = subprocess_timeout_diagnostic()
if timeout_ok:
    add("ok", f"subprocess timeout: {timeout_val}s")
    if timeout_val is not None and timeout_val < 30:
        add("warn", "subprocess timeout is very small; use at least 30s for production cron jobs")
else:
    add("fail", "subprocess timeout: invalid (set AGENT_CRON_RUNNER_SUBPROCESS_TIMEOUT to a positive integer)")

from cron.runner_subprocess import (
    cleanup_runner_tmp as _cleanup_runner_tmp,
    inspect_runner_tmp,
    worker_protocol_smoke,
)

smoke = worker_protocol_smoke()
if smoke.ok:
    add("ok", "runner_worker smoke: ok")
else:
    add("fail", f"runner_worker smoke: {smoke.error or 'failed'}")
```

After the runner tmp writable check, add:

```python
if cleanup_runner_tmp:
    cleanup = _cleanup_runner_tmp()
    if cleanup.failed:
        add(
            "warn",
            f"runner tmp cleanup: removed={cleanup.removed} failed={cleanup.failed} remaining={cleanup.remaining}",
        )
    else:
        add(
            "ok",
            f"runner tmp cleanup: removed={cleanup.removed} remaining={cleanup.remaining}",
        )
summary = inspect_runner_tmp()
if summary.stale:
    oldest = "-" if summary.oldest_age_seconds is None else f"{summary.oldest_age_seconds}s"
    add("warn", f"runner tmp residuals: stale={summary.stale} total={summary.total} oldest={oldest}")
else:
    add("ok", f"runner tmp residuals: total={summary.total} stale=0")
```

- [ ] **Step 6: Run CLI tests**

Run:

```bash
python -m pytest tests/test_agent_cli_main.py tests/test_agent_cli_cron_commands.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_cli/main.py agent_cli/cron_commands.py tests/test_agent_cli_main.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: expose cron subprocess production diagnostics"
```

## Task 5: Documentation and Full Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README**

In `README.md`, replace the paragraph after the local service command block with:

~~~markdown
Linux uses user systemd when it is available. macOS uses a user LaunchAgent. The
installed service runs the existing foreground command:

```bash
python -m agent_cli cron serve --interval 60 --lease-seconds 180
```

Development installs keep the default in-process cron runner. For hosted or
production cron services, set `AGENT_RUNTIME_PROFILE=hosted` or
`AGENT_RUNTIME_PROFILE=prod` before installing the service; if
`AGENT_CRON_RUNNER_MODE` is not already set, the installed user service will use
`AGENT_CRON_RUNNER_MODE=subprocess`. This keeps the long-lived scheduler process
separate from each job's agent execution.
~~~

Then add this paragraph after the doctor command:

~~~markdown
`agent cron doctor` checks the effective profile, runner mode, subprocess
worker smoke, timeout configuration, and runner temp directory. It reports stale
runner temp directories by default. To explicitly remove stale runner temp
directories older than 24 hours, run:

```bash
python -m agent_cli cron doctor --cleanup-runner-tmp
```
~~~

- [ ] **Step 2: Run focused verification**

Run:

```bash
python -m pytest tests/test_cron_service_manager.py tests/test_cron_service_platforms.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py -q
python -m pytest tests/test_cron_runner_subprocess.py tests/test_cron_runner_worker.py tests/test_cron_import_health.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Check git status**

Run:

```bash
git status --short
```

Expected: modified files are only the implementation files for this feature plus the pre-existing untracked `docs/superpowers/plans/2026-06-01-cron-concurrency-cli-management.md`.

- [ ] **Step 4: Commit docs**

```bash
git add README.md
git commit -m "docs: recommend subprocess cron runner for production"
```

## Final Verification Before Merge

- [ ] Run the full focused suite:

```bash
python -m pytest tests/test_cron_service_manager.py tests/test_cron_service_platforms.py tests/test_agent_cli_cron_commands.py tests/test_agent_cli_main.py tests/test_cron_runner_subprocess.py tests/test_cron_runner_worker.py tests/test_cron_import_health.py -q
```

Expected: all tests pass.

- [ ] Run import health separately if failures are noisy:

```bash
python -m pytest tests/test_cron_import_health.py -q
```

Expected: all tests pass.

- [ ] Confirm history:

```bash
git log --oneline --max-count=8
```

Expected: implementation commits are on the feature branch in task order.

## Plan Self-Review

- Spec coverage: tasks cover profile policy, service install injection, systemd/launchd rendering, worker smoke, tmp observation/cleanup, doctor/status diagnostics, README, and verification.
- Plan scan: no incomplete tokens are required for implementation; all new functions and tests are named explicitly.
- Type consistency: `ServiceInstallConfig.environment_overrides`, `ServiceInstallDetail`, `WorkerSmokeResult`, `RunnerTmpSummary`, and `RunnerTmpCleanupResult` are introduced before downstream tasks use them.

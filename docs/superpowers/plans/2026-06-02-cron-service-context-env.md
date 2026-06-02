# Cron Service Context Env Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make cron user services run from a development checkout and manage durable service environment credentials through `cron service env`.

**Architecture:** Add small runtime-context and service-env modules under `cron/`, then wire them into systemd/launchd renderers, CLI commands, and doctor checks. Keep service installation separate from secret mutation: systemd references `service.env`, launchd expands it during install, and doctor reports mismatches.

**Tech Stack:** Python stdlib, argparse CLI, user systemd unit rendering, launchd plist rendering, pytest.

---

## File Structure

- Create `cron/service_context.py`: project-root detection, `PYTHONPATH` composition, service env path, and installed-service definition inspection helpers.
- Create `cron/service_env.py`: dotenv-like read/write/set/unset/list, key/value validation, sensitive masking, file permission checks.
- Modify `cron/service_manager.py`: extend `ServiceInstallConfig` with runtime context fields and build them in `build_service_install_config()`.
- Modify `cron/service_platforms/systemd_user.py`: render `WorkingDirectory`, `PYTHONPATH`, and optional `EnvironmentFile`.
- Modify `cron/service_platforms/launchd_user.py`: render `WorkingDirectory`, `PYTHONPATH`, and expand service env file into plist environment variables.
- Modify `agent_cli/main.py`: add `cron service env set|unset|list` parser and dispatch.
- Modify `agent_cli/cron_commands.py`: add service env command handlers and doctor checks.
- Modify `tests/test_cron_service_platforms.py`: cover service context fields in generated systemd/launchd definitions.
- Modify `tests/test_agent_cli_cron_commands.py`: cover CLI env management and doctor warnings.
- Create `tests/test_cron_service_env.py`: cover env file parser/writer, masking, validation, and permissions.
- Create `tests/test_cron_service_context.py`: cover project root and `PYTHONPATH` composition.

---

### Task 1: Service Context Model

**Files:**
- Create: `cron/service_context.py`
- Modify: `cron/service_manager.py`
- Test: `tests/test_cron_service_context.py`

- [ ] **Step 1: Write failing service context tests**

Create `tests/test_cron_service_context.py`:

```python
from __future__ import annotations

from pathlib import Path


def test_detect_project_root_walks_to_pyproject(tmp_path):
    from cron.service_context import detect_project_root

    root = tmp_path / "repo"
    package = root / "agent_cli"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    module_path = package / "main.py"
    module_path.write_text("x = 1\n", encoding="utf-8")

    assert detect_project_root(module_path) == root


def test_build_service_runtime_context_includes_project_pythonpath(monkeypatch, tmp_path):
    from cron.service_context import build_service_runtime_context

    root = tmp_path / "repo"
    (root / "agent_cli").mkdir(parents=True)
    (root / "cron").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    module_path = root / "agent_cli" / "main.py"
    module_path.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PYTHONPATH", "/extra/path")

    context = build_service_runtime_context(module_path=module_path)

    assert context.project_root == root
    assert context.working_directory == root
    assert context.service_env_file == (tmp_path / "home" / "cron" / "service.env").resolve()
    assert context.pythonpath == f"{root}:/extra/path"


def test_build_service_install_config_carries_runtime_context(monkeypatch, tmp_path):
    from cron import service_context
    from cron.service_manager import build_service_install_config

    root = tmp_path / "repo"
    env_file = tmp_path / "home" / "cron" / "service.env"
    monkeypatch.setattr(
        service_context,
        "build_service_runtime_context",
        lambda: service_context.ServiceRuntimeContext(
            project_root=root,
            working_directory=root,
            pythonpath=str(root),
            service_env_file=env_file,
        ),
    )

    config, _detail = build_service_install_config(
        interval_seconds=30,
        lease_seconds=90,
        force=True,
    )

    assert config.working_directory == root
    assert config.pythonpath == str(root)
    assert config.service_env_file == env_file
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_context.py -q
```

Expected: fails because `cron.service_context` does not exist.

- [ ] **Step 3: Implement service context**

Create `cron/service_context.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cron.paths import get_cron_dir


@dataclass(frozen=True)
class ServiceRuntimeContext:
    project_root: Path | None
    working_directory: Path | None
    pythonpath: str | None
    service_env_file: Path


def detect_project_root(start: str | Path | None = None) -> Path | None:
    if start is None:
        start_path = Path(__file__).resolve()
    else:
        start_path = Path(start).resolve()
    current = start_path.parent if start_path.is_file() else start_path
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    if (Path.cwd() / "pyproject.toml").exists() and (Path.cwd() / "agent_cli").exists():
        return Path.cwd().resolve()
    return None


def compose_pythonpath(project_root: Path | None, existing: str | None = None) -> str | None:
    parts: list[str] = []
    if project_root is not None:
        parts.append(str(project_root))
    existing_value = os.getenv("PYTHONPATH") if existing is None else existing
    if existing_value:
        parts.extend(part for part in existing_value.split(os.pathsep) if part)
    deduped: list[str] = []
    for part in parts:
        if part not in deduped:
            deduped.append(part)
    return os.pathsep.join(deduped) or None


def get_service_env_file() -> Path:
    return (get_cron_dir() / "service.env").resolve()


def build_service_runtime_context(*, module_path: str | Path | None = None) -> ServiceRuntimeContext:
    project_root = detect_project_root(module_path)
    return ServiceRuntimeContext(
        project_root=project_root,
        working_directory=project_root,
        pythonpath=compose_pythonpath(project_root),
        service_env_file=get_service_env_file(),
    )
```

Modify `cron/service_manager.py`:

```python
from pathlib import Path
```

Extend `ServiceInstallConfig`:

```python
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
```

In `build_service_install_config()`, import and use context:

```python
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
```

- [ ] **Step 4: Run service context tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_context.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add cron/service_context.py cron/service_manager.py tests/test_cron_service_context.py
git commit -m "feat: add cron service runtime context"
```

---

### Task 2: Service Env File Model

**Files:**
- Create: `cron/service_env.py`
- Test: `tests/test_cron_service_env.py`

- [ ] **Step 1: Write failing service env tests**

Create `tests/test_cron_service_env.py`:

```python
from __future__ import annotations

import os


def test_service_env_set_list_unset_masks_secrets(monkeypatch, tmp_path):
    from cron import service_env

    path = tmp_path / "service.env"
    monkeypatch.setattr(service_env, "get_service_env_file", lambda: path)

    service_env.set_service_env("FEISHU_APP_ID", "cli_123")
    service_env.set_service_env("FEISHU_APP_SECRET", "secret-value")

    values = service_env.read_service_env()
    assert values == {
        "FEISHU_APP_ID": "cli_123",
        "FEISHU_APP_SECRET": "secret-value",
    }
    assert service_env.masked_service_env() == {
        "FEISHU_APP_ID": "********",
        "FEISHU_APP_SECRET": "********",
    }

    removed = service_env.unset_service_env("FEISHU_APP_SECRET")

    assert removed is True
    assert service_env.read_service_env() == {"FEISHU_APP_ID": "cli_123"}


def test_service_env_rejects_invalid_key_and_multiline_value(monkeypatch, tmp_path):
    from cron import service_env

    monkeypatch.setattr(service_env, "get_service_env_file", lambda: tmp_path / "service.env")

    for key in ("feishu", "FEISHU-APP", "1FEISHU"):
        try:
            service_env.set_service_env(key, "value")
        except ValueError as exc:
            assert "invalid service env key" in str(exc)
        else:
            raise AssertionError(f"{key} should be rejected")

    try:
        service_env.set_service_env("FEISHU_APP_SECRET", "a\nb")
    except ValueError as exc:
        assert "single-line" in str(exc)
    else:
        raise AssertionError("multiline values should be rejected")


def test_service_env_file_permissions_are_restricted(monkeypatch, tmp_path):
    from cron import service_env

    path = tmp_path / "service.env"
    monkeypatch.setattr(service_env, "get_service_env_file", lambda: path)

    service_env.set_service_env("FEISHU_APP_ID", "cli_123")

    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_service_env_permission_status_detects_broad_file(monkeypatch, tmp_path):
    from cron import service_env

    path = tmp_path / "service.env"
    path.write_text("FEISHU_APP_ID=cli_123\n", encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(service_env, "get_service_env_file", lambda: path)

    status = service_env.inspect_service_env_file()

    assert status.exists is True
    if os.name != "nt":
        assert status.permissions_ok is False
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_env.py -q
```

Expected: fails because `cron.service_env` does not exist.

- [ ] **Step 3: Implement env file model**

Create `cron/service_env.py`:

```python
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cron.paths import atomic_replace, secure_dir, secure_file
from cron.service_context import get_service_env_file

_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SENSITIVE_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "KEY")


@dataclass(frozen=True)
class ServiceEnvFileStatus:
    path: Path
    exists: bool
    readable: bool
    permissions_ok: bool
    error: str | None = None


def validate_service_env_key(key: str) -> str:
    normalized = str(key or "").strip()
    if not _KEY_RE.match(normalized):
        raise ValueError(f"invalid service env key: {key!r}")
    return normalized


def validate_service_env_value(value: str) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("service env values must be single-line")
    return text


def read_service_env(path: Path | None = None) -> dict[str, str]:
    env_path = path or get_service_env_file()
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if _KEY_RE.match(key):
            result[key] = value.strip()
    return result


def write_service_env(values: dict[str, str], path: Path | None = None) -> Path:
    env_path = path or get_service_env_file()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    secure_dir(env_path.parent)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(env_path.parent),
        prefix=f".{env_path.name}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for key in sorted(values):
                normalized_key = validate_service_env_key(key)
                normalized_value = validate_service_env_value(values[key])
                handle.write(f"{normalized_key}={normalized_value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, env_path)
        secure_file(env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return env_path


def set_service_env(key: str, value: str) -> Path:
    values = read_service_env()
    values[validate_service_env_key(key)] = validate_service_env_value(value)
    return write_service_env(values)


def unset_service_env(key: str) -> bool:
    normalized = validate_service_env_key(key)
    values = read_service_env()
    existed = normalized in values
    values.pop(normalized, None)
    write_service_env(values)
    return existed


def is_sensitive_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _SENSITIVE_MARKERS)


def mask_value(key: str, value: str) -> str:
    if is_sensitive_key(key):
        return "********"
    return value


def masked_service_env() -> dict[str, str]:
    return {key: mask_value(key, value) for key, value in read_service_env().items()}


def inspect_service_env_file(path: Path | None = None) -> ServiceEnvFileStatus:
    env_path = path or get_service_env_file()
    if not env_path.exists():
        return ServiceEnvFileStatus(env_path, exists=False, readable=False, permissions_ok=True)
    try:
        env_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ServiceEnvFileStatus(env_path, exists=True, readable=False, permissions_ok=False, error=str(exc))
    permissions_ok = True
    if os.name != "nt":
        permissions_ok = (env_path.stat().st_mode & 0o077) == 0
    return ServiceEnvFileStatus(env_path, exists=True, readable=True, permissions_ok=permissions_ok)
```

- [ ] **Step 4: Run env file tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_env.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add cron/service_env.py tests/test_cron_service_env.py
git commit -m "feat: add cron service env file"
```

---

### Task 3: Render Runtime Context In User Services

**Files:**
- Modify: `cron/service_platforms/systemd_user.py`
- Modify: `cron/service_platforms/launchd_user.py`
- Test: `tests/test_cron_service_platforms.py`

- [ ] **Step 1: Add failing platform rendering tests**

Append to `tests/test_cron_service_platforms.py`:

```python
def test_systemd_unit_includes_workdir_pythonpath_and_env_file(tmp_path):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import render_unit

    unit = render_unit(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            python_executable="/usr/bin/python3",
            working_directory=tmp_path / "repo",
            pythonpath=f"{tmp_path / 'repo'}:/extra",
            service_env_file=tmp_path / "home" / "cron" / "service.env",
            environment_overrides={"FEISHU_APP_SECRET": "must-not-leak"},
        )
    )

    assert f"WorkingDirectory={tmp_path / 'repo'}" in unit
    assert f'Environment="PYTHONPATH={tmp_path / "repo"}:/extra"' in unit
    assert f"EnvironmentFile=-{tmp_path / 'home' / 'cron' / 'service.env'}" in unit
    assert "FEISHU_APP_SECRET" not in unit


def test_launchd_plist_includes_workdir_pythonpath_and_service_env(monkeypatch, tmp_path):
    import plistlib

    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.launchd_user import render_plist

    env_path = tmp_path / "home" / "cron" / "service.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("FEISHU_APP_ID=cli_123\nFEISHU_APP_SECRET=secret\n", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_log_paths",
        lambda: (tmp_path / "out.log", tmp_path / "err.log"),
    )

    plist = plistlib.loads(
        render_plist(
            ServiceInstallConfig(
                interval_seconds=30,
                lease_seconds=90,
                working_directory=tmp_path / "repo",
                pythonpath=f"{tmp_path / 'repo'}:/extra",
                service_env_file=env_path,
            )
        )
    )

    assert plist["WorkingDirectory"] == str(tmp_path / "repo")
    env = plist["EnvironmentVariables"]
    assert env["PYTHONPATH"] == f"{tmp_path / 'repo'}:/extra"
    assert env["FEISHU_APP_ID"] == "cli_123"
    assert env["FEISHU_APP_SECRET"] == "secret"
```

- [ ] **Step 2: Run failing platform tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_platforms.py::test_systemd_unit_includes_workdir_pythonpath_and_env_file tests/test_cron_service_platforms.py::test_launchd_plist_includes_workdir_pythonpath_and_service_env -q
```

Expected: fails because renderers do not include these fields.

- [ ] **Step 3: Update systemd renderer**

In `cron/service_platforms/systemd_user.py`, add helpers:

```python
def _format_working_directory(config: ServiceInstallConfig) -> str:
    if config.working_directory is None:
        return ""
    return f"WorkingDirectory={_quote_systemd(str(config.working_directory))}\n"


def _format_environment_file(config: ServiceInstallConfig) -> str:
    if config.service_env_file is None:
        return ""
    return f"EnvironmentFile=-{_quote_systemd(str(config.service_env_file))}\n"
```

Update `_format_environment()` so it injects `PYTHONPATH` and filters Feishu secrets from inline unit env:

```python
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
```

Update `render_unit()` service block:

```python
    working_directory = _format_working_directory(config)
    environment_file = _format_environment_file(config)
    return (
        "[Unit]\n"
        "Description=LangChain Agent Cron Service\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{working_directory}"
        f"{environment_block}"
        f"{environment_file}"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
```

- [ ] **Step 4: Update launchd renderer**

In `cron/service_platforms/launchd_user.py`, import `read_service_env`:

```python
from cron.service_env import read_service_env
```

Update `_environment_variables()`:

```python
def _environment_variables(config: ServiceInstallConfig) -> dict[str, str]:
    values = service_environment(config.environment_overrides)
    if config.pythonpath:
        values["PYTHONPATH"] = config.pythonpath
    if config.service_env_file is not None:
        values.update(read_service_env(config.service_env_file))
    return {
        key: value
        for key, value in sorted(values.items())
        if "\n" not in value and "\r" not in value
    }
```

Update `render_plist()`:

```python
    if config.working_directory is not None:
        payload["WorkingDirectory"] = str(config.working_directory)
```

- [ ] **Step 5: Run platform tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_platforms.py -q
```

Expected: all service platform tests pass.

- [ ] **Step 6: Commit**

```bash
git add cron/service_platforms/systemd_user.py cron/service_platforms/launchd_user.py tests/test_cron_service_platforms.py
git commit -m "feat: write cron service runtime context"
```

---

### Task 4: CLI Commands For Service Env

**Files:**
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Add failing command handler tests**

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_service_env_set_list_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.main import run_cli

    set_result = run_cli(["cron", "service", "env", "set", "FEISHU_APP_SECRET", "secret"])
    assert set_result.exit_code == 0
    assert "Set service env FEISHU_APP_SECRET" in set_result.text
    assert "cron service restart" in set_result.text

    list_result = run_cli(["cron", "service", "env", "list"])
    assert list_result.exit_code == 0
    assert "FEISHU_APP_SECRET=********" in list_result.text
    assert "secret" not in list_result.text

    unset_result = run_cli(["cron", "service", "env", "unset", "FEISHU_APP_SECRET"])
    assert unset_result.exit_code == 0
    assert "Unset service env FEISHU_APP_SECRET" in unset_result.text


def test_cron_service_env_set_invalid_key_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.main import run_cli

    result = run_cli(["cron", "service", "env", "set", "feishu-secret", "secret"])

    assert result.exit_code == 2
    assert "invalid service env key" in result.text
```

- [ ] **Step 2: Run failing CLI command tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_service_env_set_list_unset tests/test_agent_cli_cron_commands.py::test_cron_service_env_set_invalid_key_fails -q
```

Expected: fails because parser does not know `cron service env`.

- [ ] **Step 3: Add parser and dispatch**

In `agent_cli/main.py`, under `cron_service_subparsers`, add:

```python
    cron_service_env = cron_service_subparsers.add_parser("env", parents=[public_options])
    cron_service_env_subparsers = cron_service_env.add_subparsers(dest="cron_service_env_command")

    cron_service_env_set = cron_service_env_subparsers.add_parser("set", parents=[public_options])
    cron_service_env_set.add_argument("key")
    cron_service_env_set.add_argument("value")

    cron_service_env_unset = cron_service_env_subparsers.add_parser("unset", parents=[public_options])
    cron_service_env_unset.add_argument("key")

    cron_service_env_subparsers.add_parser("list", parents=[public_options])
```

In service dispatch:

```python
        if service_command == "env":
            env_command = getattr(args, "cron_service_env_command", None)
            if env_command == "set":
                return cron_commands.cron_service_env_set(args.key, args.value)
            if env_command == "unset":
                return cron_commands.cron_service_env_unset(args.key)
            if env_command == "list":
                return cron_commands.cron_service_env_list()
            return cron_commands.CronCommandResult(
                "Missing cron service env command. Use one of: set, unset, list.",
                exit_code=2,
            )
```

Update the missing service command text to include `env`.

- [ ] **Step 4: Add command handlers**

In `agent_cli/cron_commands.py`, add:

```python
def cron_service_env_set(key: str, value: str) -> CronCommandResult:
    from cron.service_env import set_service_env

    try:
        path = set_service_env(key, value)
    except ValueError as exc:
        return CronCommandResult(str(exc), exit_code=2)
    return CronCommandResult(
        f"Set service env {key} in {path}.\n"
        "Restart an already-running service with `agent cron service restart`."
    )


def cron_service_env_unset(key: str) -> CronCommandResult:
    from cron.service_env import unset_service_env, get_service_env_file

    try:
        removed = unset_service_env(key)
    except ValueError as exc:
        return CronCommandResult(str(exc), exit_code=2)
    action = "Unset" if removed else "Service env key was not set"
    return CronCommandResult(
        f"{action} {key} in {get_service_env_file()}.\n"
        "Restart an already-running service with `agent cron service restart`."
    )


def cron_service_env_list() -> CronCommandResult:
    from cron.service_env import get_service_env_file, masked_service_env

    values = masked_service_env()
    if not values:
        return CronCommandResult(f"No service env values set in {get_service_env_file()}.")
    lines = [f"Service Env: {get_service_env_file()}"]
    lines.extend(f"  {key}={values[key]}" for key in sorted(values))
    return CronCommandResult("\n".join(lines))
```

- [ ] **Step 5: Run CLI command tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_service_env_set_list_unset tests/test_agent_cli_cron_commands.py::test_cron_service_env_set_invalid_key_fails -q
```

Expected: both tests pass.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/main.py agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: add cron service env commands"
```

---

### Task 5: Doctor Checks For Service Context And Feishu Service Env

**Files:**
- Modify: `cron/service_context.py`
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Add failing doctor tests**

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_doctor_warns_active_feishu_job_missing_service_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "shell-app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "shell-secret")

    import gateway.registry as gateway_registry
    import agent_cli.cron_commands as cron_commands
    from agent_cli.cron_commands import cron_doctor
    from cron.jobs import create_job
    from gateway.contracts import SendResult

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)

    class FakeFeishuAdapter:
        key = "feishu"

        def validate_target(self, target):
            return SendResult(True)

        def send_text(self, target, message):
            raise AssertionError("doctor must not send a Feishu message")

        def token_smoke(self):
            return SendResult(True)

    gateway_registry.clear_gateway_adapter_factories()
    gateway_registry.register_gateway_adapter_factory(lambda **kwargs: FakeFeishuAdapter())
    try:
        create_job(prompt="write report", schedule="30m", deliver="feishu:oc_123")
        result = cron_doctor()
    finally:
        gateway_registry.clear_gateway_adapter_factories()

    assert result.exit_code == 1
    assert "service env missing FEISHU_APP_ID, FEISHU_APP_SECRET" in result.text
    assert "current shell Feishu env is set but cron service env is missing" in result.text
    assert "cron service env set FEISHU_APP_ID" in result.text


def test_cron_doctor_reports_service_env_permissions(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    env_file = tmp_path / "cron" / "service.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("FEISHU_APP_ID=cli_123\n", encoding="utf-8")
    env_file.chmod(0o644)

    import agent_cli.cron_commands as cron_commands
    from agent_cli.cron_commands import cron_doctor

    monkeypatch.setattr(cron_commands, "_add_service_manager_check", lambda add: None)

    result = cron_doctor()

    assert "service env permissions are too broad" in result.text
```

- [ ] **Step 2: Run failing doctor tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_active_feishu_job_missing_service_env tests/test_agent_cli_cron_commands.py::test_cron_doctor_reports_service_env_permissions -q
```

Expected: fails because doctor does not inspect service env.

- [ ] **Step 3: Add service definition inspection helpers**

In `cron/service_context.py`, add:

```python
@dataclass(frozen=True)
class InstalledServiceContextStatus:
    installed: bool
    working_directory_ok: bool
    pythonpath_ok: bool
    service_env_linked: bool
    detail: str | None = None


def inspect_text_service_context(
    text: str,
    *,
    project_root: Path | None,
    service_env_file: Path,
    platform: str,
) -> InstalledServiceContextStatus:
    if not text:
        return InstalledServiceContextStatus(False, False, False, False, "service definition missing")
    expected_root = "" if project_root is None else str(project_root)
    working_ok = bool(expected_root and expected_root in text and "WorkingDirectory" in text)
    pythonpath_ok = bool(expected_root and "PYTHONPATH" in text and expected_root in text)
    if platform == "systemd-user":
        env_linked = f"EnvironmentFile=-{service_env_file}" in text
    else:
        env_linked = True
    return InstalledServiceContextStatus(True, working_ok, pythonpath_ok, env_linked)
```

This helper is intentionally text-based for systemd unit checks. launchd-specific plist parsing can be added in the implementation if needed, but the first doctor check can warn through renderer tests and platform status.

- [ ] **Step 4: Add doctor service env checks**

In `agent_cli/cron_commands.py`, import service env functions inside `cron_doctor()` after service manager checks:

```python
    from cron.service_env import inspect_service_env_file, read_service_env

    service_env_status = inspect_service_env_file()
    service_env_values = read_service_env()
    if not service_env_status.exists:
        add("ok", f"service env file: not configured ({service_env_status.path})")
    elif not service_env_status.readable:
        add("fail", f"service env file unreadable: {service_env_status.error or service_env_status.path}")
    elif not service_env_status.permissions_ok:
        add("warn", f"service env permissions are too broad: {service_env_status.path}")
    else:
        add("ok", f"service env file: {service_env_status.path}")
```

Track active Feishu delivery in the existing active-job loop:

```python
    active_feishu_jobs: set[str] = set()
```

Inside `if adapter_key == "feishu":` before validation:

```python
                active_feishu_jobs.add(str(job.get("id")))
```

After the active-job loop:

```python
    if active_feishu_jobs:
        required = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
        missing_service_env = [key for key in required if not service_env_values.get(key)]
        shell_present = [key for key in required if os.getenv(key)]
        if missing_service_env:
            add(
                "warn",
                "active Feishu cron jobs require service env; "
                f"service env missing {', '.join(missing_service_env)}. "
                "Set with `agent cron service env set FEISHU_APP_ID <app_id>` and "
                "`agent cron service env set FEISHU_APP_SECRET <app_secret>`.",
            )
            if shell_present:
                add(
                    "warn",
                    "current shell Feishu env is set but cron service env is missing; "
                    "background service delivery uses service.env.",
                )
        else:
            add("ok", "service env has Feishu credentials for active Feishu jobs")
```

If exact placement is awkward because `service_env_values` is needed before the loop, initialize it before loading active jobs.

- [ ] **Step 5: Run doctor tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_active_feishu_job_missing_service_env tests/test_agent_cli_cron_commands.py::test_cron_doctor_reports_service_env_permissions -q
```

Expected: both tests pass.

- [ ] **Step 6: Commit**

```bash
git add cron/service_context.py agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: diagnose cron service env readiness"
```

---

### Task 6: Doctor Checks For Installed Service Definition

**Files:**
- Modify: `agent_cli/cron_commands.py`
- Modify: `cron/service_platforms/systemd_user.py`
- Modify: `cron/service_platforms/launchd_user.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Add failing installed definition doctor tests**

Append to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_doctor_warns_installed_systemd_unit_missing_runtime_context(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "home"))
    unit_path = tmp_path / "langchain-agent-cron.service"
    unit_path.write_text(
        "[Service]\nExecStart=/usr/bin/python -m agent_cli.main cron serve\n",
        encoding="utf-8",
    )

    import agent_cli.cron_commands as cron_commands
    from agent_cli.cron_commands import cron_doctor
    from cron.service_manager import ServiceRuntimeStatus

    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: unit_path,
    )
    monkeypatch.setattr(
        "cron.service_manager.compose_service_status",
        lambda: ServiceRuntimeStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            heartbeat_fresh=True,
        ),
    )
    monkeypatch.setattr(cron_commands, "_pid_is_running", lambda pid: True)

    result = cron_doctor()

    assert "cron service definition missing WorkingDirectory" in result.text
    assert "cron service definition missing project PYTHONPATH" in result.text
    assert "cron service definition missing service.env reference" in result.text
    assert "cron service install --force" in result.text
```

- [ ] **Step 2: Run failing installed definition test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_installed_systemd_unit_missing_runtime_context -q
```

Expected: fails because doctor does not inspect installed unit content.

- [ ] **Step 3: Expose service definition readers**

In `cron/service_platforms/systemd_user.py`, add:

```python
def read_installed_unit() -> str | None:
    path = systemd_unit_path()
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")
```

In `cron/service_platforms/launchd_user.py`, add:

```python
def read_installed_plist() -> bytes | None:
    path = launchd_plist_path()
    if not path.exists():
        return None
    return path.read_bytes()
```

- [ ] **Step 4: Add doctor installed definition check**

In `agent_cli/cron_commands.py`, add helper near `_add_service_manager_check()`:

```python
def _add_service_definition_context_check(add, platform: str) -> None:
    from cron.service_context import build_service_runtime_context, inspect_text_service_context

    context = build_service_runtime_context()
    text = None
    if platform == "systemd-user":
        from cron.service_platforms.systemd_user import read_installed_unit

        text = read_installed_unit()
    elif platform == "launchd-user":
        from cron.service_platforms.launchd_user import read_installed_plist

        raw = read_installed_plist()
        text = raw.decode("utf-8", errors="replace") if raw is not None else None
    else:
        return
    status = inspect_text_service_context(
        text or "",
        project_root=context.project_root,
        service_env_file=context.service_env_file,
        platform=platform,
    )
    if not status.installed:
        return
    if not status.working_directory_ok:
        add("warn", "cron service definition missing WorkingDirectory; run `agent cron service install --force`")
    if not status.pythonpath_ok:
        add("warn", "cron service definition missing project PYTHONPATH; run `agent cron service install --force`")
    if platform == "systemd-user" and not status.service_env_linked:
        add("warn", "cron service definition missing service.env reference; run `agent cron service install --force`")
```

Update `_add_service_manager_check()` after the existing running-service line branches so installed services invoke:

```python
    if status.installed:
        _add_service_definition_context_check(add, status.platform)
```

Use a structure that does not skip the existing service health warning. The implementation can call this helper before the final `else` if clearer.

- [ ] **Step 5: Run installed definition doctor test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_cron_commands.py::test_cron_doctor_warns_installed_systemd_unit_missing_runtime_context -q
```

Expected: test passes.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/cron_commands.py cron/service_platforms/systemd_user.py cron/service_platforms/launchd_user.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: diagnose cron service install context"
```

---

### Task 7: Full Regression And Manual Smoke

**Files:**
- Modify only files needed for failures found in this task.

- [ ] **Step 1: Run focused cron suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_cron_service_context.py tests/test_cron_service_env.py tests/test_cron_service_platforms.py tests/test_agent_cli_cron_commands.py tests/test_gateway_core.py tests/test_gateway_feishu.py tests/test_cron_delivery.py tests/test_cron_delivery_dispatcher.py tests/test_cron_service.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run local CLI smoke for env commands**

Run:

```bash
AGENT_CRON_HOME=/tmp/cron-service-env-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli.main cron service env set FEISHU_APP_ID cli_smoke
AGENT_CRON_HOME=/tmp/cron-service-env-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli.main cron service env set FEISHU_APP_SECRET smoke_secret
AGENT_CRON_HOME=/tmp/cron-service-env-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli.main cron service env list
```

Expected output includes masked values:

```text
FEISHU_APP_ID=********
FEISHU_APP_SECRET=********
```

- [ ] **Step 3: Inspect generated systemd unit without leaking secrets**

Run:

```bash
AGENT_CRON_HOME=/tmp/cron-service-install-smoke /home/miku/miniforge3/envs/langchain/bin/python - <<'PY'
from cron.service_manager import build_service_install_config
from cron.service_platforms.systemd_user import render_unit

config, _ = build_service_install_config(
    interval_seconds=30,
    lease_seconds=90,
    force=True,
    cli_profile="prod",
)
unit = render_unit(config)
assert "WorkingDirectory=" in unit
assert "PYTHONPATH=" in unit
assert "EnvironmentFile=-" in unit
assert "FEISHU_APP_SECRET" not in unit
print("ok")
PY
```

Expected:

```text
ok
```

- [ ] **Step 4: Commit final fixes if needed**

If Step 1, 2, or 3 required fixes, commit them:

```bash
git add <changed-files>
git commit -m "fix: stabilize cron service env diagnostics"
```

If no files changed, do not create an empty commit.

- [ ] **Step 5: Final status**

Run:

```bash
git status --short
```

Expected: only pre-existing unrelated untracked files remain, especially:

```text
?? docs/superpowers/plans/2026-06-01-cron-concurrency-cli-management.md
```

Do not remove or modify unrelated untracked files.

---

## Self-Review

- Spec coverage: Tasks 1 and 3 cover checkout importability via `WorkingDirectory`/`PYTHONPATH`; Task 2 and 4 cover persistent service env management; Task 5 covers Feishu service env readiness and shell-vs-service distinction; Task 6 covers installed service definition diagnostics; Task 7 covers regression and smoke verification.
- Placeholder scan: no unfinished marker or cross-reference shortcut steps are intentionally left.
- Type consistency: `ServiceRuntimeContext`, `ServiceEnvFileStatus`, and `ServiceInstallConfig` fields are introduced before later tasks consume them.

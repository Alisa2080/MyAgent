# Hermes Backend-Aware Path Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make public file-tool path validation and low-level safe-root enforcement understand Docker `/workspace`, Singularity active/configured cwd, and SSH active/configured cwd without host-path expansion leaks.

**Architecture:** Add one small backend path policy module that derives backend type, live cwd, configured cwd, and allowed roots from the Hermes active environment plus `_get_env_config()`. Public file tools use that module for read/write/search/patch path admission; `ShellFileOperations` uses the same policy shape for safe-root exceptions while keeping local writes constrained to `AGENT_WRITE_SAFE_ROOT`.

**Tech Stack:** Python 3.11, pytest, LangChain tool wrappers, Hermes terminal toolkit environments.

---

## File Structure

- Create `agent_tools/file_toolkit/backend_paths.py`
  - Owns backend path context discovery and path/root normalization.
  - Avoids host `~` expansion for Docker/Singularity/SSH paths.
  - Exposes helpers for public tool validation and safe-root extra roots.

- Modify `agent_tools/hermes_terminal_toolkit/terminal_tool.py`
  - Tags created environments with `_hermes_env_type`, `_hermes_configured_cwd`, and `_hermes_host_cwd`.
  - This makes fake class-name inference unnecessary for real Hermes envs.

- Modify `agent_tools/public/files.py`
  - Replaces local `_allowed_workspace_roots_for_task()` internals with backend-aware helper calls.
  - Keeps current public API and error shapes.

- Modify `agent_tools/file_toolkit/file_tools.py`
  - Reuses backend-aware path resolver for bookkeeping paths.
  - Keeps read dedup/staleness host mtime checks best-effort: if backend paths are remote/container-only and cannot be statted on host, current graceful fallback remains.

- Modify `agent_tools/file_toolkit/file_safety.py`
  - Extends `is_write_denied()` to accept `extra_allowed_roots`.
  - Keeps existing default behavior unchanged when no extra roots are supplied.

- Modify `agent_tools/file_toolkit/file_operations.py`
  - Uses the shared file-safety helper.
  - Allows Docker `/workspace`, Docker effective cwd, Singularity effective/configured cwd, and SSH effective/configured cwd as extra roots.
  - Does not globally allow `/workspace` for Singularity or SSH.

- Add/modify tests:
  - `tests/test_backend_path_policy.py`
  - `tests/test_hermes_active_env.py`
  - `tests/test_file_tools_runtime_task_id.py`
  - `tests/test_file_tools_hermes_env.py`

---

### Task 1: Tag Hermes Environments With Backend Metadata

**Files:**
- Modify: `agent_tools/hermes_terminal_toolkit/terminal_tool.py`
- Test: `tests/test_hermes_active_env.py`

- [ ] **Step 1: Write failing tests for environment metadata**

Append these tests to `tests/test_hermes_active_env.py`:

```python
def test_create_environment_tags_local_env_metadata(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class FakeLocalEnv(FakeEnv):
        def __init__(self, cwd, timeout):
            super().__init__(cwd=cwd)
            self.timeout = timeout

    monkeypatch.setattr(
        terminal_tool,
        "LocalEnvironment",
        lambda cwd, timeout: FakeLocalEnv(cwd=cwd, timeout=timeout),
    )

    env = terminal_tool._create_environment(
        env_type="local",
        image="",
        cwd="/repo",
        timeout=33,
    )

    assert env._hermes_env_type == "local"
    assert env._hermes_configured_cwd == "/repo"
    assert env._hermes_host_cwd is None


def test_create_environment_tags_docker_env_metadata(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class FakeDockerEnv(FakeEnv):
        def __init__(self, **kwargs):
            super().__init__(cwd=kwargs["cwd"])
            self.kwargs = kwargs

    monkeypatch.setattr(
        terminal_tool,
        "DockerEnvironment",
        lambda **kwargs: FakeDockerEnv(**kwargs),
    )

    env = terminal_tool._create_environment(
        env_type="docker",
        image="fake-image",
        cwd="/workspace",
        timeout=44,
        container_config={"container_persistent": True},
        task_id="task-docker",
        host_cwd="/home/miku/projects/langchain",
    )

    assert env._hermes_env_type == "docker"
    assert env._hermes_configured_cwd == "/workspace"
    assert env._hermes_host_cwd == "/home/miku/projects/langchain"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_hermes_active_env.py::test_create_environment_tags_local_env_metadata \
  tests/test_hermes_active_env.py::test_create_environment_tags_docker_env_metadata -q
```

Expected: FAIL with `AttributeError` for `_hermes_env_type`.

- [ ] **Step 3: Implement environment tagging**

In `agent_tools/hermes_terminal_toolkit/terminal_tool.py`, add this helper above `_create_environment()`:

```python
def _tag_environment(env, *, env_type: str, configured_cwd: str, host_cwd: str | None = None):
    """Attach Hermes backend metadata used by file-tool path policy."""
    try:
        setattr(env, "_hermes_env_type", env_type)
        setattr(env, "_hermes_configured_cwd", configured_cwd)
        setattr(env, "_hermes_host_cwd", host_cwd)
    except Exception:
        logger.debug("Failed to tag environment metadata", exc_info=True)
    return env
```

Then wrap every `_create_environment()` return. For example:

```python
if env_type == "local":
    return _tag_environment(
        LocalEnvironment(cwd=cwd, timeout=timeout),
        env_type=env_type,
        configured_cwd=cwd,
        host_cwd=host_cwd,
    )
```

Apply the same pattern to Docker, Singularity, and SSH returns:

```python
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
        run_as_host_user=cc.get("docker_run_as_host_user", False),
    ),
    env_type=env_type,
    configured_cwd=cwd,
    host_cwd=host_cwd,
)
```

```python
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
```

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_hermes_active_env.py::test_create_environment_tags_local_env_metadata \
  tests/test_hermes_active_env.py::test_create_environment_tags_docker_env_metadata -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/hermes_terminal_toolkit/terminal_tool.py tests/test_hermes_active_env.py
git commit -m "feat: tag Hermes environments with backend metadata"
```

---

### Task 2: Add Backend Path Policy Helper

**Files:**
- Create: `agent_tools/file_toolkit/backend_paths.py`
- Test: `tests/test_backend_path_policy.py`

- [ ] **Step 1: Write failing tests for backend path policy**

Create `tests/test_backend_path_policy.py` with:

```python
from pathlib import Path


class FakeEnv:
    def __init__(self, cwd, env_type):
        self.cwd = cwd
        self._hermes_env_type = env_type
        self._hermes_configured_cwd = cwd
        self._hermes_host_cwd = None


def test_policy_uses_active_ssh_cwd_without_host_expanding_tilde(monkeypatch):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/home/remote/project", "ssh")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "~", "host_cwd": None},
    )

    ctx = backend_paths.get_backend_path_context("task-ssh")

    assert ctx.env_type == "ssh"
    assert ctx.cwd == "/home/remote/project"
    assert backend_paths.resolve_path_for_policy("notes.txt", "task-ssh") == "/home/remote/project/notes.txt"
    assert "/home/remote/project" in backend_paths.allowed_workspace_roots_for_task("task-ssh")
    assert str(Path.home()) not in backend_paths.allowed_workspace_roots_for_task("task-ssh")


def test_policy_allows_docker_workspace_even_when_cwd_is_root(monkeypatch):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/root", "docker")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "docker", "cwd": "/root", "host_cwd": None},
    )

    roots = backend_paths.allowed_workspace_roots_for_task("task-docker")

    assert "/workspace" in roots
    assert "/root" in roots
    assert backend_paths.resolve_path_for_policy("/workspace/app.py", "task-docker") == "/workspace/app.py"


def test_policy_uses_singularity_active_cwd_without_global_workspace_root(monkeypatch):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    active = FakeEnv("/analysis/project", "singularity")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "singularity", "cwd": "/root", "host_cwd": None},
    )

    roots = backend_paths.allowed_workspace_roots_for_task("task-singularity")

    assert "/analysis/project" in roots
    assert "/root" in roots
    assert "/workspace" not in roots
    assert backend_paths.resolve_path_for_policy("notes.txt", "task-singularity") == "/analysis/project/notes.txt"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_backend_path_policy.py -q
```

Expected: FAIL with `ImportError` for `agent_tools.file_toolkit.backend_paths`.

- [ ] **Step 3: Implement backend path policy module**

Create `agent_tools/file_toolkit/backend_paths.py`:

```python
"""Backend-aware path policy helpers for Hermes file tools."""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from agent_core.workspace import WORKDIR


@dataclass(frozen=True)
class BackendPathContext:
    task_id: str
    env_type: str
    cwd: str | None
    configured_cwd: str | None
    host_cwd: str | None


def _terminal_tool():
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    return terminal_tool


def _safe_env_config() -> dict:
    try:
        return _terminal_tool()._get_env_config()
    except Exception:
        return {}


def _active_env(task_id: str):
    try:
        return _terminal_tool().get_active_env(task_id)
    except Exception:
        return None


def _normal_env_type(value: object) -> str:
    return str(value or "local").lower()


def _env_type_from(active_env, config: dict) -> str:
    return _normal_env_type(
        getattr(active_env, "_hermes_env_type", None)
        or getattr(active_env, "env_type", None)
        or config.get("env_type")
    )


def _configured_cwd_from(active_env, config: dict) -> str | None:
    value = getattr(active_env, "_hermes_configured_cwd", None) or config.get("cwd")
    return str(value) if value else None


def _host_cwd_from(active_env, config: dict) -> str | None:
    value = getattr(active_env, "_hermes_host_cwd", None) or config.get("host_cwd")
    return str(value) if value else None


def get_backend_path_context(task_id: str = "default") -> BackendPathContext:
    effective_task_id = task_id or "default"
    config = _safe_env_config()
    active = _active_env(effective_task_id)
    cwd = getattr(active, "cwd", None)
    if cwd:
        cwd = str(cwd)
    return BackendPathContext(
        task_id=effective_task_id,
        env_type=_env_type_from(active, config),
        cwd=cwd,
        configured_cwd=_configured_cwd_from(active, config),
        host_cwd=_host_cwd_from(active, config),
    )


def _is_local(ctx: BackendPathContext) -> bool:
    return ctx.env_type == "local"


def _is_absolute_posix(path: str) -> bool:
    return path.startswith("/")


def _normalize_posix(path: str) -> str:
    normalized = posixpath.normpath(path)
    return "/" if normalized == "//" else normalized


def _append_root(roots: list[str], seen: set[str], root: str | None) -> None:
    if not root:
        return
    if not _is_absolute_posix(root):
        return
    normalized = _normalize_posix(root)
    if normalized not in seen:
        roots.append(normalized)
        seen.add(normalized)


def _append_host_root(roots: list[str], seen: set[str], root: Path) -> None:
    try:
        resolved = str(root.resolve())
    except Exception:
        resolved = str(root)
    if resolved not in seen:
        roots.append(resolved)
        seen.add(resolved)


def allowed_workspace_roots_for_task(task_id: str = "default") -> list[str]:
    ctx = get_backend_path_context(task_id)
    roots: list[str] = []
    seen: set[str] = set()

    _append_host_root(roots, seen, WORKDIR)

    if ctx.env_type == "docker":
        _append_root(roots, seen, "/workspace")
        _append_root(roots, seen, ctx.cwd)
        _append_root(roots, seen, ctx.configured_cwd)
    elif ctx.env_type in {"singularity", "ssh"}:
        _append_root(roots, seen, ctx.cwd)
        _append_root(roots, seen, ctx.configured_cwd)

    return roots


def safe_write_roots_for_env(env, fallback_cwd: str | None = None) -> list[str]:
    env_type = _normal_env_type(
        getattr(env, "_hermes_env_type", None)
        or getattr(env, "env_type", None)
        or type(env).__name__
    )
    cwd = str(getattr(env, "cwd", None) or fallback_cwd or "")
    configured_cwd = str(getattr(env, "_hermes_configured_cwd", "") or "")

    roots: list[str] = []
    seen: set[str] = set()
    if "docker" in env_type:
        _append_root(roots, seen, "/workspace")
        _append_root(roots, seen, cwd)
        _append_root(roots, seen, configured_cwd)
    elif "singularity" in env_type or "ssh" in env_type:
        _append_root(roots, seen, cwd)
        _append_root(roots, seen, configured_cwd)
    return roots


def resolve_path_for_policy(path: str | None, task_id: str = "default") -> str:
    ctx = get_backend_path_context(task_id)
    raw = str(path or ".")

    if _is_local(ctx):
        p = Path(raw).expanduser()
        if not p.is_absolute():
            base = Path(ctx.cwd or os.environ.get("TERMINAL_CWD") or os.getcwd())
            p = base / p
        return str(p.resolve())

    if raw == "~":
        if ctx.cwd and _is_absolute_posix(ctx.cwd):
            return _normalize_posix(ctx.cwd)
        return raw

    if raw.startswith("~/"):
        if ctx.cwd and _is_absolute_posix(ctx.cwd):
            return _normalize_posix(posixpath.join(ctx.cwd, raw[2:]))
        return raw

    if _is_absolute_posix(raw):
        return _normalize_posix(raw)

    base = ctx.cwd if ctx.cwd and _is_absolute_posix(ctx.cwd) else None
    if base is None and ctx.configured_cwd and _is_absolute_posix(ctx.configured_cwd):
        base = ctx.configured_cwd
    if base is None:
        base = os.environ.get("TERMINAL_CWD") or os.getcwd()
    return _normalize_posix(posixpath.join(base, raw))


def path_is_under_any_root(path: str, roots: Iterable[str]) -> bool:
    normalized = _normalize_posix(path) if path.startswith("/") else path
    for root in roots:
        root_normalized = _normalize_posix(str(root)) if str(root).startswith("/") else str(root)
        if normalized == root_normalized or normalized.startswith(root_normalized.rstrip("/") + "/"):
            return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_backend_path_policy.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/file_toolkit/backend_paths.py tests/test_backend_path_policy.py
git commit -m "feat: add Hermes backend path policy"
```

---

### Task 3: Use Backend Path Policy in Public File Tool Admission

**Files:**
- Modify: `agent_tools/public/files.py`
- Test: `tests/test_file_tools_runtime_task_id.py`

- [ ] **Step 1: Write failing public admission tests**

Append these tests to `tests/test_file_tools_runtime_task_id.py`:

```python
def test_write_file_allows_active_ssh_cwd_path_and_forwards_original_path(monkeypatch):
    import agent_tools.public.files as public_files
    from agent_core.session_context import hermes_task_id_from_thread_id
    from agent_tools.file_toolkit import file_tools
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class ActiveSSHEnv:
        cwd = "/home/remote/project"
        _hermes_env_type = "ssh"
        _hermes_configured_cwd = "~"

    calls = []
    runtime = SimpleNamespace(execution_info=SimpleNamespace(thread_id="ssh-write"))
    expected_task_id = hermes_task_id_from_thread_id("ssh-write")

    monkeypatch.setattr(file_tools, "get_active_env", lambda task_id: ActiveSSHEnv())
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: ActiveSSHEnv())
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "~", "host_cwd": None},
    )
    monkeypatch.setattr(
        public_files,
        "write_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"bytes_written": 5}),
    )

    raw = public_files._write_file_impl("notes.txt", "hello", runtime=runtime)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls == [{"path": "notes.txt", "content": "hello", "task_id": expected_task_id}]


def test_write_file_rejects_ssh_absolute_path_outside_active_cwd(monkeypatch):
    import agent_tools.public.files as public_files
    from agent_tools.file_toolkit import file_tools
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class ActiveSSHEnv:
        cwd = "/home/remote/project"
        _hermes_env_type = "ssh"
        _hermes_configured_cwd = "~"

    calls = []

    monkeypatch.setattr(file_tools, "get_active_env", lambda task_id: ActiveSSHEnv())
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: ActiveSSHEnv())
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "~", "host_cwd": None},
    )
    monkeypatch.setattr(
        public_files,
        "write_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"bytes_written": 5}),
    )

    raw = public_files._write_file_impl("/etc/passwd", "bad", runtime=None)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_path"
    assert calls == []


def test_read_file_allows_singularity_active_cwd_path(monkeypatch):
    import agent_tools.public.files as public_files
    from agent_tools.file_toolkit import file_tools
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class ActiveSingularityEnv:
        cwd = "/analysis/project"
        _hermes_env_type = "singularity"
        _hermes_configured_cwd = "/root"

    calls = []

    monkeypatch.setattr(file_tools, "get_active_env", lambda task_id: ActiveSingularityEnv())
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: ActiveSingularityEnv())
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "singularity", "cwd": "/root", "host_cwd": None},
    )
    monkeypatch.setattr(
        public_files,
        "read_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"content": "hello\n"}),
    )

    raw = public_files._read_file_impl("notes.txt", offset=1, limit=20, runtime=None)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls == [{"path": "notes.txt", "offset": 1, "limit": 20, "task_id": "default"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_file_tools_runtime_task_id.py::test_write_file_allows_active_ssh_cwd_path_and_forwards_original_path \
  tests/test_file_tools_runtime_task_id.py::test_write_file_rejects_ssh_absolute_path_outside_active_cwd \
  tests/test_file_tools_runtime_task_id.py::test_read_file_allows_singularity_active_cwd_path -q
```

Expected: at least the SSH/Singularity allow tests FAIL with `ok is False` before integration.

- [ ] **Step 3: Integrate policy into `agent_tools/public/files.py`**

Replace the import of `_resolve_path_for_task` with backend policy imports:

```python
from agent_tools.file_toolkit.backend_paths import (
    allowed_workspace_roots_for_task,
    path_is_under_any_root,
    resolve_path_for_policy,
)
```

Keep `patch_tool`, `read_file_tool`, `search_tool`, `write_file_tool` imports unchanged.

Replace `_allowed_workspace_roots_for_task()`, `_is_under_allowed_workspace_root()`, `_ensure_workspace_path_for_task()`, and `_ensure_read_allowed_for_task()` with:

```python
def _allowed_workspace_roots_for_task(task_id: str) -> list[Path]:
    return [Path(root) for root in allowed_workspace_roots_for_task(task_id)]


def _is_under_allowed_workspace_root(resolved: Path | str, task_id: str) -> bool:
    return path_is_under_any_root(
        str(resolved),
        [str(root) for root in _allowed_workspace_roots_for_task(task_id)],
    )


def _blocked_read_dirs_for_task(task_id: str) -> list[Path]:
    blocked_dirs: list[Path] = []
    seen: set[Path] = set()
    for root in _allowed_workspace_roots_for_task(task_id):
        for blocked_dir in (
            root / "skills" / ".hub",
            root / "skills" / ".hub" / "index-cache",
        ):
            if blocked_dir not in seen:
                blocked_dirs.append(blocked_dir)
                seen.add(blocked_dir)
    return blocked_dirs


def _ensure_workspace_path_for_task(path: str | None, task_id: str) -> str | None:
    try:
        resolved = resolve_path_for_policy(path or ".", task_id)
    except Exception as exc:
        return str(exc)
    if not _is_under_allowed_workspace_root(resolved, task_id):
        return f"Path escapes workspace: {path}"
    return None


def _ensure_read_allowed_for_task(path: str | None, task_id: str) -> str | None:
    try:
        resolved = resolve_path_for_policy(path or ".", task_id)
    except Exception as exc:
        return str(exc)
    if not _is_under_allowed_workspace_root(resolved, task_id):
        return f"Path escapes workspace: {path}"

    for blocked_dir in _blocked_read_dirs_for_task(task_id):
        if path_is_under_any_root(resolved, [str(blocked_dir)]):
            return (
                f"Access denied: {path} is an internal skill cache file "
                "and cannot be read directly to prevent prompt injection. "
                "Use the skills_list or skill_view tools instead."
            )
    return None
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_file_tools_runtime_task_id.py::test_write_file_allows_active_ssh_cwd_path_and_forwards_original_path \
  tests/test_file_tools_runtime_task_id.py::test_write_file_rejects_ssh_absolute_path_outside_active_cwd \
  tests/test_file_tools_runtime_task_id.py::test_read_file_allows_singularity_active_cwd_path \
  tests/test_file_tools_runtime_task_id.py::test_read_file_rejects_docker_skill_cache_path \
  tests/test_file_tools_runtime_task_id.py::test_search_files_rejects_internal_skill_cache_path -q
```

Expected: all selected tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/public/files.py tests/test_file_tools_runtime_task_id.py
git commit -m "fix: validate public file paths against backend roots"
```

---

### Task 4: Use Backend Path Policy in Low-Level File Tool Bookkeeping

**Files:**
- Modify: `agent_tools/file_toolkit/file_tools.py`
- Test: `tests/test_file_tools_hermes_env.py`

- [ ] **Step 1: Write failing tests for resolver behavior**

Append this test to `tests/test_file_tools_hermes_env.py`:

```python
def test_resolve_path_for_task_uses_backend_policy_for_ssh_active_cwd(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class ActiveSSHEnv:
        cwd = "/home/remote/project"
        _hermes_env_type = "ssh"
        _hermes_configured_cwd = "~"

    monkeypatch.setattr(file_tools, "get_active_env", lambda task_id: ActiveSSHEnv())
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: ActiveSSHEnv())
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "ssh", "cwd": "~", "host_cwd": None},
    )

    assert file_tools._resolve_path_for_task("notes.txt", "task-ssh") == Path("/home/remote/project/notes.txt")
```

- [ ] **Step 2: Run test to verify it fails if `file_tools` still bypasses policy**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_file_tools_hermes_env.py::test_resolve_path_for_task_uses_backend_policy_for_ssh_active_cwd -q
```

Expected: FAIL before the resolver delegates to `backend_paths.resolve_path_for_policy()`.

- [ ] **Step 3: Modify `agent_tools/file_toolkit/file_tools.py`**

Add the import:

```python
from agent_tools.file_toolkit.backend_paths import resolve_path_for_policy
```

Replace `_resolve_path_for_task()` with:

```python
def _resolve_path_for_task(filepath: str, task_id: str = "default") -> Path:
    """Resolve *filepath* against backend-aware live cwd when possible."""
    return Path(resolve_path_for_policy(filepath, task_id))
```

Keep `_get_live_tracking_cwd()` for compatibility with existing tests, but stop using it in `_resolve_path_for_task()`.

- [ ] **Step 4: Run resolver and existing bookkeeping tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_file_tools_hermes_env.py::test_resolve_path_for_task_uses_backend_policy_for_ssh_active_cwd \
  tests/test_file_tools_hermes_env.py::test_resolve_path_uses_active_hermes_env_cwd_without_cached_wrapper \
  tests/test_file_tools_hermes_env.py::test_write_file_tool_smokes_real_local_hermes_env -q
```

Expected: all selected tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/file_toolkit/file_tools.py tests/test_file_tools_hermes_env.py
git commit -m "fix: resolve file-tool paths with backend policy"
```

---

### Task 5: Centralize Safe-Root Extras in Shared File Safety

**Files:**
- Modify: `agent_tools/file_toolkit/file_safety.py`
- Modify: `agent_tools/file_toolkit/file_operations.py`
- Test: `tests/test_file_tools_hermes_env.py`

- [ ] **Step 1: Write failing safe-root tests**

Append these tests to `tests/test_file_tools_hermes_env.py`:

```python
def test_shell_file_operations_allows_docker_workspace_absolute_write(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))

    class DockerEnv:
        cwd = "/root"
        _hermes_env_type = "docker"
        _hermes_configured_cwd = "/root"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            if command.startswith("wc -c"):
                return {"output": "5\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    env = DockerEnv()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file("/workspace/notes.txt", "hello")

    assert result.error is None
    assert result.bytes_written == 5
    assert len(env.commands) == 2


def test_shell_file_operations_allows_singularity_effective_cwd_writes(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))

    class SingularityEnv:
        cwd = "/analysis/project"
        _hermes_env_type = "singularity"
        _hermes_configured_cwd = "/analysis/project"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            if command.startswith("wc -c"):
                return {"output": "5\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    env = SingularityEnv()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file("notes.txt", "hello")

    assert result.error is None
    assert result.bytes_written == 5
    assert [kwargs["cwd"] for _, kwargs in env.commands] == [
        "/analysis/project",
        "/analysis/project",
    ]


def test_shell_file_operations_keeps_workspace_root_denied_for_singularity(
    tmp_path, monkeypatch
):
    safe_root = tmp_path / "host-workdir"
    safe_root.mkdir()
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(safe_root))

    class SingularityEnv:
        cwd = "/analysis/project"
        _hermes_env_type = "singularity"
        _hermes_configured_cwd = "/analysis/project"

        def __init__(self):
            self.commands = []

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            return {"output": "", "returncode": 0}

    env = SingularityEnv()
    file_ops = file_tools.ShellFileOperations(env)

    result = file_ops.write_file("/workspace/notes.txt", "hello")

    assert "Write denied" in result.error
    assert env.commands == []
```

- [ ] **Step 2: Run tests to verify the Singularity workspace test fails**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_file_tools_hermes_env.py::test_shell_file_operations_allows_docker_workspace_absolute_write \
  tests/test_file_tools_hermes_env.py::test_shell_file_operations_allows_singularity_effective_cwd_writes \
  tests/test_file_tools_hermes_env.py::test_shell_file_operations_keeps_workspace_root_denied_for_singularity -q
```

Expected before implementation: the Singularity `/workspace` denial test FAILS because current code treats Singularity like Docker for `/workspace`.

- [ ] **Step 3: Extend shared `file_safety.is_write_denied()`**

Change `agent_tools/file_toolkit/file_safety.py`:

```python
from typing import Iterable, Optional
```

Add:

```python
def _is_under_root(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)
```

Replace `is_write_denied()` with:

```python
def is_write_denied(path: str, extra_allowed_roots: Optional[Iterable[str]] = None) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    if resolved in build_write_denied_paths(home):
        return True
    for prefix in build_write_denied_prefixes(home):
        if resolved.startswith(prefix):
            return True

    safe_root = get_safe_write_root()
    if not safe_root or _is_under_root(resolved, safe_root):
        return False

    for root in extra_allowed_roots or ():
        try:
            allowed_root = os.path.realpath(os.path.expanduser(str(root)))
        except Exception:
            continue
        if _is_under_root(resolved, allowed_root):
            return False

    return True
```

- [ ] **Step 4: Update `file_operations.py` safe-root logic**

In `agent_tools/file_toolkit/file_operations.py`, import the policy helper:

```python
from agent_tools.file_toolkit.backend_paths import safe_write_roots_for_env
```

Also import shared safety:

```python
from agent_tools.file_toolkit.file_safety import (
    build_write_denied_paths,
    build_write_denied_prefixes,
    get_safe_write_root as _shared_get_safe_write_root,
    is_write_denied as _shared_is_write_denied,
)
```

Replace `_is_write_denied()` with:

```python
def _is_write_denied(path: str, extra_allowed_roots: Optional[List[str]] = None) -> bool:
    """Return True if path is blocked by the denylist or safe-root policy."""
    return _shared_is_write_denied(path, extra_allowed_roots=extra_allowed_roots)
```

Replace `_extra_safe_write_roots()` with:

```python
def _extra_safe_write_roots(self) -> List[str]:
    """Return backend roots allowed in addition to the host safe root."""
    return safe_write_roots_for_env(self.env, fallback_cwd=self._effective_cwd())
```

- [ ] **Step 5: Run focused safe-root tests**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_file_tools_hermes_env.py::test_shell_file_operations_allows_docker_workspace_absolute_write \
  tests/test_file_tools_hermes_env.py::test_shell_file_operations_allows_singularity_effective_cwd_writes \
  tests/test_file_tools_hermes_env.py::test_shell_file_operations_keeps_workspace_root_denied_for_singularity \
  tests/test_file_tools_hermes_env.py::test_shell_file_operations_allows_effective_ssh_cwd_writes \
  tests/test_file_tools_hermes_env.py::test_shell_file_operations_keeps_workspace_root_denied_for_ssh -q
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_tools/file_toolkit/file_safety.py agent_tools/file_toolkit/file_operations.py tests/test_file_tools_hermes_env.py
git commit -m "fix: apply backend-aware safe write roots"
```

---

### Task 6: Regression Sweep and Documentation

**Files:**
- Modify: `agent_tools/file_toolkit/README.md`
- Test: existing suites

- [ ] **Step 1: Update README behavior notes**

In `agent_tools/file_toolkit/README.md`, add this paragraph under the Hermes env-backed file operation section:

```markdown
Backend-aware path policy:

- Local file tools are constrained to the host workspace and `AGENT_WRITE_SAFE_ROOT`.
- Docker file tools additionally allow backend paths under `/workspace` and the container's active/configured cwd.
- Singularity file tools additionally allow only the active/configured container cwd; `/workspace` is not automatically trusted unless it is the active/configured cwd.
- SSH file tools additionally allow only the active/configured remote cwd; `/workspace` remains denied for SSH.
- Internal skill cache paths such as `skills/.hub/index-cache` are denied for every allowed workspace root.
```

- [ ] **Step 2: Run focused suites**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_backend_path_policy.py \
  tests/test_file_tools_runtime_task_id.py \
  tests/test_file_tools_hermes_env.py \
  tests/test_hermes_active_env.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
PYTHONPATH=. /home/miku/miniforge3/envs/langchain/bin/python -m pytest -q
```

Expected: all tests PASS.

- [ ] **Step 4: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output, exit code 0.

- [ ] **Step 5: Commit documentation**

```bash
git add agent_tools/file_toolkit/README.md
git commit -m "docs: describe backend-aware file path policy"
```

---

## Final Verification Checklist

- [ ] Docker public file paths under `/workspace` pass validation.
- [ ] Docker safe-root exceptions allow `/workspace` even when `AGENT_WRITE_SAFE_ROOT` is a host path.
- [ ] Singularity public file paths under active/configured cwd pass validation.
- [ ] Singularity safe-root exceptions do not globally trust `/workspace`.
- [ ] SSH public file paths under active/configured cwd pass validation.
- [ ] SSH safe-root exceptions do not trust `/workspace`.
- [ ] Local backend remains constrained to host workspace and host `AGENT_WRITE_SAFE_ROOT`.
- [ ] Skill cache read/search denial still applies under host workspace and Docker `/workspace`.
- [ ] Full pytest suite passes with `/home/miku/miniforge3/envs/langchain/bin/python`.
- [ ] `git diff --check` is clean.

## Self-Review Notes

Spec coverage:
- Docker `/workspace`: Tasks 2, 3, and 5 cover public validation and safe-root write allowance.
- Singularity cwd: Tasks 2, 3, and 5 cover public validation and safe-root allowance for active/configured cwd.
- SSH cwd: Tasks 2, 3, 4, and 5 cover public validation, low-level resolver behavior, and safe-root allowance for active/configured remote cwd.

Placeholder scan:
- No forbidden placeholder markers or unspecified test steps remain.

Type consistency:
- `BackendPathContext`, `allowed_workspace_roots_for_task()`, `resolve_path_for_policy()`, `path_is_under_any_root()`, and `safe_write_roots_for_env()` are defined in Task 2 before later tasks reference them.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-20-hermes-backend-aware-path-safety.md`. Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** - execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?

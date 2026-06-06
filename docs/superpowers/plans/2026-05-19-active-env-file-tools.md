# Reference Implementation Active Env File Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract shared reference implementation active-environment acquisition into `get_or_create_active_env(task_id, workdir=None, timeout=None)` and make file tools wrap that terminal environment instead of `LocalTerminalEnvironment`.

**Architecture:** Move the environment creation/reuse path out of `terminal_tool()` into a public helper in `agent_tools/terminal_toolkit/terminal_tool.py`. `terminal_tool()` and `agent_tools/file_toolkit/file_tools.py` will both use that helper, so local, Docker, Singularity, and SSH commands share `_active_environments`, creation locks, idle activity tracking, and cleanup behavior. File operations remain shell-based through `ShellFileOperations`; only the backend object changes.

**Tech Stack:** Python 3.11, pytest, LangChain tool wrappers, vendored terminal toolkit, existing `ShellFileOperations`.

---

## File Structure

- Modify: `agent_tools/terminal_toolkit/terminal_tool.py`
  - Add `get_or_create_active_env(task_id, workdir=None, timeout=None)`.
  - Add small internal helpers for environment image/config construction if needed.
  - Refactor `terminal_tool()` to call the new helper instead of duplicating `_active_environments` / `_creation_locks` logic.
  - Add cache invalidation hook calls when environments are cleaned.

- Modify: `agent_tools/file_toolkit/file_tools.py`
  - Replace `LocalTerminalEnvironment` usage with `get_or_create_active_env()`.
  - Keep `_file_ops_cache` keyed byreference implementation `task_id`.
  - Avoid holding `_file_ops_lock` while creating Docker/Singularity/SSH environments.

- Modify: `agent_tools/file_toolkit/__init__.py` only if public exports need adjustment.
  - Expected: no change.

- Modify: `agent_tools/file_toolkit/terminal_environment.py`
  - Expected: no change in this plan. It remains as a legacy/minimal adapter for direct internal use unless later cleanup removes it.

- Create: `tests/test_active_env.py`
  - Unit-test helper creation/reuse behavior without starting real Docker/Singularity/SSH.
  - Unit-test `terminal_tool()` delegates env acquisition to the helper.
  - Unit-test `cleanup_vm()` and idle cleanup clear file-op cache.

- Create: `tests/test_file_tools_active_env.py`
  - Unit-test file toolkit `_get_file_ops()` wraps the terminal env returned by `get_or_create_active_env()`.
  - Unit-test cache refresh when the active env object changes.

- Existing regression tests:
  - `tests/test_terminal_tools.py`
  - `tests/test_file_tools_runtime_task_id.py`
  - `tests/test_terminal_lifecycle.py`
  - `tests/test_process_lifecycle.py`

## Non-Goals

- Do not redesign workspace mapping for Docker/Singularity/SSH in this plan.
- Do not convert `list_directory` / `file_info` to runtime-backed shell operations in this plan.
- Do not remove `LocalTerminalEnvironment`; after this change it should simply stop being the default backend for model-visible file tools.
- Do not add real Docker/Singularity/SSH integration tests that require host services; keep this plan unit-testable in the current CI environment.

## Behavior Contract

- `get_or_create_active_env(task_id, workdir=None, timeout=None)` returns the active environment object for the resolved runtime task id.
- The helper owns `_get_env_config()`, `_start_cleanup_thread()`, `_active_environments`, `_last_activity`, `_creation_locks`, and environment construction.
- The helper normalizes `task_id` through `_resolve_container_task_id()`.
- `timeout` controls the timeout used when creating a new environment; existing environments are reused unchanged.
- `workdir` is accepted for API symmetry and future workspace mapping, but this plan should preserve the current environment creation default: use `config["cwd"]` for creation and let callers pass execution CWD separately. If implementation chooses to use `workdir` for creation, tests must prove local behavior does not regress for missing/nonexistent command workdirs.
- File tools get the same active env that terminal commands use for the same `task_id`.
- Cleaning an env also clears file-op wrappers for that `task_id`, so wrappers cannot keep stale container/SSH refs.

---

### Task 1: Add Failing Active-Env Helper Tests

**Files:**
- Create: `tests/test_active_env.py`
- Test: `tests/test_active_env.py`

- [ ] **Step 1: Write helper creation/reuse tests**

Create `tests/test_active_env.py` with:

```python
import json


class FakeEnv:
    def __init__(self, cwd="/workspace"):
        self.cwd = cwd
        self.execute_calls = []
        self.cleaned = False

    def execute(self, command, **kwargs):
        self.execute_calls.append((command, kwargs))
        return {"output": "ok\n", "returncode": 0}

    def cleanup(self):
        self.cleaned = True


def _reset_terminal_env_state(monkeypatch, terminal_tool):
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_cleanup_thread", None)
    monkeypatch.setattr(terminal_tool, "_cleanup_running", False)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)


def _fake_config(env_type="local"):
    return {
        "env_type": env_type,
        "docker_image": "fake-docker-image",
        "singularity_image": "docker://fake-singularity-image",
        "cwd": "/workspace",
        "host_cwd": None,
        "docker_mount_cwd_to_workspace": False,
        "timeout": 180,
        "lifetime_seconds": 300,
        "ssh_host": "",
        "ssh_user": "",
        "ssh_port": 22,
        "ssh_key": "",
        "local_persistent": False,
        "container_cpu": 1,
        "container_memory": 5120,
        "container_disk": 51200,
        "container_persistent": True,
        "docker_volumes": [],
        "docker_forward_env": [],
        "docker_run_as_host_user": False,
    }


def test_get_or_create_active_env_creates_and_reuses(monkeypatch):
    from agent_tools.terminal_toolkit import terminal_tool

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    created = []

    def fake_create_environment(**kwargs):
        created.append(kwargs)
        return FakeEnv(cwd=kwargs["cwd"])

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _fake_config())
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    first = terminal_tool.get_or_create_active_env("task-1")
    second = terminal_tool.get_or_create_active_env("task-1")

    assert first is second
    assert len(created) == 1
    assert created[0]["env_type"] == "local"
    assert created[0]["cwd"] == "/workspace"
    assert created[0]["timeout"] == 180
    assert created[0]["task_id"] == "task-1"
    assert terminal_tool._active_environments["task-1"] is first
    assert "task-1" in terminal_tool._last_activity


def test_get_or_create_active_env_uses_timeout_for_new_environment(monkeypatch):
    from agent_tools.terminal_toolkit import terminal_tool

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    created = []

    def fake_create_environment(**kwargs):
        created.append(kwargs)
        return FakeEnv(cwd=kwargs["cwd"])

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _fake_config())
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    env = terminal_tool.get_or_create_active_env("task-timeout", timeout=42)

    assert isinstance(env, FakeEnv)
    assert created[0]["timeout"] == 42


def test_get_or_create_active_env_builds_docker_config(monkeypatch):
    from agent_tools.terminal_toolkit import terminal_tool

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    created = []
    config = _fake_config(env_type="docker")
    config["docker_mount_cwd_to_workspace"] = True
    config["host_cwd"] = "/home/miku/projects/langchain"
    config["docker_volumes"] = ["/tmp/cache:/cache"]
    config["docker_forward_env"] = ["CUSTOM_ENV"]
    config["docker_run_as_host_user"] = True

    def fake_create_environment(**kwargs):
        created.append(kwargs)
        return FakeEnv(cwd=kwargs["cwd"])

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    terminal_tool.get_or_create_active_env("docker-task")

    assert created[0]["env_type"] == "docker"
    assert created[0]["image"] == "fake-docker-image"
    assert created[0]["host_cwd"] == "/home/miku/projects/langchain"
    assert created[0]["container_config"]["docker_mount_cwd_to_workspace"] is True
    assert created[0]["container_config"]["docker_volumes"] == ["/tmp/cache:/cache"]
    assert created[0]["container_config"]["docker_forward_env"] == ["CUSTOM_ENV"]
    assert created[0]["container_config"]["docker_run_as_host_user"] is True
```

- [ ] **Step 2: Run helper tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_active_env.py -q
```

Expected: FAIL with `AttributeError: module 'agent_tools.terminal_toolkit.terminal_tool' has no attribute 'get_or_create_active_env'`.

- [ ] **Step 3: Commit failing helper tests**

Run:

```bash
git add tests/test_active_env.py
git commit -m "test: cover shared reference implementation active env helper"
```

---

### Task 2: Implement `get_or_create_active_env`

**Files:**
- Modify: `agent_tools/terminal_toolkit/terminal_tool.py`
- Test: `tests/test_active_env.py`

- [ ] **Step 1: Add helper implementation**

In `agent_tools/terminal_toolkit/terminal_tool.py`, insert this helper after `_cleanup_thread_worker()` and before `_start_cleanup_thread()`:

```python
def _image_for_env_type(config: dict, env_type: str) -> str:
    if env_type == "docker":
        return config["docker_image"]
    if env_type == "singularity":
        return config["singularity_image"]
    return ""


def _ssh_config_from_terminal_config(config: dict) -> dict | None:
    if config["env_type"] != "ssh":
        return None
    return {
        "host": config.get("ssh_host", ""),
        "user": config.get("ssh_user", ""),
        "port": config.get("ssh_port", 22),
        "key": config.get("ssh_key", ""),
    }


def _container_config_from_terminal_config(config: dict) -> dict | None:
    if config["env_type"] not in ("docker", "singularity"):
        return None
    return {
        "container_cpu": config.get("container_cpu", 1),
        "container_memory": config.get("container_memory", 5120),
        "container_disk": config.get("container_disk", 51200),
        "container_persistent": config.get("container_persistent", True),
        "docker_volumes": config.get("docker_volumes", []),
        "docker_mount_cwd_to_workspace": config.get("docker_mount_cwd_to_workspace", False),
        "docker_forward_env": config.get("docker_forward_env", []),
        "docker_env": config.get("docker_env", {}),
        "docker_run_as_host_user": config.get("docker_run_as_host_user", False),
    }


def get_or_create_active_env(
    task_id: Optional[str],
    workdir: Optional[str] = None,
    timeout: Optional[int] = None,
):
    """Return the active terminal environment for *task_id*, creating it if needed.

    This is the single owner of environment config resolution, environment
    creation locks, active-env reuse, and last-activity tracking.  ``workdir``
    is accepted for call-site symmetry; environment creation intentionally uses
    the configured backend cwd so existing terminal behavior does not change.
    Callers can pass per-command cwd to ``env.execute(...)`` after acquiring
    the env.
    """
    config = _get_env_config()
    env_type = config["env_type"]
    effective_task_id = _resolve_container_task_id(task_id)
    effective_timeout = timeout or config["timeout"]
    cwd = config["cwd"]

    _start_cleanup_thread()

    with _env_lock:
        env = _active_environments.get(effective_task_id)
        if env is not None:
            _last_activity[effective_task_id] = time.time()
            return env

    with _creation_locks_lock:
        if effective_task_id not in _creation_locks:
            _creation_locks[effective_task_id] = threading.Lock()
        task_lock = _creation_locks[effective_task_id]

    with task_lock:
        with _env_lock:
            env = _active_environments.get(effective_task_id)
            if env is not None:
                _last_activity[effective_task_id] = time.time()
                return env

        if env_type == "singularity":
            _check_disk_usage_warning()

        new_env = _create_environment(
            env_type=env_type,
            image=_image_for_env_type(config, env_type),
            cwd=cwd,
            timeout=effective_timeout,
            ssh_config=_ssh_config_from_terminal_config(config),
            container_config=_container_config_from_terminal_config(config),
            task_id=effective_task_id,
            host_cwd=config.get("host_cwd"),
        )

        with _env_lock:
            existing = _active_environments.get(effective_task_id)
            if existing is not None:
                _last_activity[effective_task_id] = time.time()
                try:
                    if hasattr(new_env, "cleanup"):
                        new_env.cleanup()
                    elif hasattr(new_env, "stop"):
                        new_env.stop()
                except Exception:
                    logger.debug("Failed to clean redundant environment for task %s", effective_task_id, exc_info=True)
                return existing

            _active_environments[effective_task_id] = new_env
            _last_activity[effective_task_id] = time.time()
            return new_env
```

- [ ] **Step 2: Run helper tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_active_env.py::test_get_or_create_active_env_creates_and_reuses tests/test_active_env.py::test_get_or_create_active_env_uses_timeout_for_new_environment tests/test_active_env.py::test_get_or_create_active_env_builds_docker_config -q
```

Expected: PASS.

- [ ] **Step 3: Run terminal regression tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_terminal_tools.py tests/test_terminal_lifecycle.py -q
```

Expected: PASS. `terminal_tool()` has not been refactored yet, so this verifies the helper is additive.

- [ ] **Step 4: Commit helper implementation**

Run:

```bash
git add agent_tools/terminal_toolkit/terminal_tool.py tests/test_active_env.py
git commit -m "feat: add shared reference implementation active env helper"
```

---

### Task 3: Refactor `terminal_tool()` To Use The Helper

**Files:**
- Modify: `agent_tools/terminal_toolkit/terminal_tool.py`
- Test: `tests/test_active_env.py`
- Test: `tests/test_terminal_tools.py`

- [ ] **Step 1: Add failing delegation test**

Append this test to `tests/test_active_env.py`:

```python
def test_terminal_tool_uses_get_or_create_active_env(monkeypatch):
    from agent_tools.terminal_toolkit import terminal_tool

    fake_env = FakeEnv(cwd="/workspace")
    helper_calls = []

    def fake_get_or_create_active_env(task_id, workdir=None, timeout=None):
        helper_calls.append({"task_id": task_id, "workdir": workdir, "timeout": timeout})
        return fake_env

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _fake_config())
    monkeypatch.setattr(terminal_tool, "get_or_create_active_env", fake_get_or_create_active_env)
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )

    raw = terminal_tool.terminal_tool(
        command="printf ok",
        background=False,
        timeout=7,
        task_id="task-terminal",
        force=False,
        workdir="/workspace/subdir",
    )
    payload = json.loads(raw)

    assert payload["exit_code"] == 0
    assert helper_calls == [
        {"task_id": "task-terminal", "workdir": "/workspace/subdir", "timeout": 7}
    ]
    assert fake_env.execute_calls == [
        ("printf ok", {"timeout": 7, "cwd": "/workspace/subdir"})
    ]
```

- [ ] **Step 2: Run delegation test to verify it fails**

Run:

```bash
PYTHONPATH=. pytest tests/test_active_env.py::test_terminal_tool_uses_get_or_create_active_env -q
```

Expected: FAIL because `terminal_tool()` still contains inline environment lookup/creation and never calls the helper.

- [ ] **Step 3: Replace inline environment acquisition in `terminal_tool()`**

In `agent_tools/terminal_toolkit/terminal_tool.py`, keep the initial command validation, config loading, timeout/background guidance, approval guard, and workdir validation. Replace the large block from `_start_cleanup_thread()` through `_create_environment(...)` with:

```python
        env = get_or_create_active_env(
            effective_task_id,
            workdir=workdir,
            timeout=effective_timeout,
        )
```

The resulting top half of `terminal_tool()` should follow this shape:

```python
        config = _get_env_config()
        env_type = config["env_type"]
        effective_task_id = _resolve_container_task_id(task_id)

        cwd = config["cwd"]
        default_timeout = config["timeout"]
        effective_timeout = timeout or default_timeout

        if not background and timeout and timeout > FOREGROUND_MAX_TIMEOUT:
            return json.dumps(
                {
                    "error": (
                        f"Foreground timeout {timeout}s exceeds the maximum of "
                        f"{FOREGROUND_MAX_TIMEOUT}s. Use background=true with "
                        "notify_on_complete=true for long-running commands."
                    )
                },
                ensure_ascii=False,
            )

        if not background:
            guidance = _foreground_background_guidance(command)
            if guidance:
                return json.dumps({"output": "", "exit_code": -1, "error": guidance, "status": "error"}, ensure_ascii=False)

        if not force:
            approval = _check_all_guards(command, env_type)
            if not approval["approved"]:
                desc = approval.get("description", "command flagged")
                fallback_msg = f"Command denied: {desc}. Pass force=True only when you explicitly trust the command."
                return json.dumps(
                    {"output": "", "exit_code": -1, "error": approval.get("message", fallback_msg), "status": "blocked"},
                    ensure_ascii=False,
                )
            if approval.get("user_approved"):
                desc = approval.get("description", "flagged as dangerous")
                approval_note = f"Command required approval ({desc}) and was approved by the user."

        if workdir:
            workdir_error = _validate_workdir(workdir)
            if workdir_error:
                logger.warning("Blocked dangerous workdir: %s (command: %s)", workdir[:200], safe_command_preview(command))
                return json.dumps({"output": "", "exit_code": -1, "error": workdir_error, "status": "blocked"}, ensure_ascii=False)

        env = get_or_create_active_env(
            effective_task_id,
            workdir=workdir,
            timeout=effective_timeout,
        )
```

Keep the existing background and foreground execution code after this point unchanged.

- [ ] **Step 4: Run delegation and terminal tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_active_env.py::test_terminal_tool_uses_get_or_create_active_env tests/test_terminal_tools.py -q
```

Expected: PASS.

- [ ] **Step 5: Run process lifecycle smoke tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_process_lifecycle.py tests/test_terminal_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit terminal refactor**

Run:

```bash
git add agent_tools/terminal_toolkit/terminal_tool.py tests/test_active_env.py
git commit -m "refactor: share terminal env acquisition in terminal tool"
```

---

### Task 4: Make File Tools Wrap reference implementation Active Env

**Files:**
- Modify: `agent_tools/file_toolkit/file_tools.py`
- Create: `tests/test_file_tools_active_env.py`
- Test: `tests/test_file_tools_active_env.py`
- Test: `tests/test_file_tools_runtime_task_id.py`

- [ ] **Step 1: Write failing file-ops backend tests**

Create `tests/test_file_tools_active_env.py` with:

```python
class FakeEnv:
    def __init__(self, cwd="/workspace"):
        self.cwd = cwd
        self.commands = []

    def execute(self, command, cwd="", timeout=None, stdin_data=None):
        self.commands.append(
            {
                "command": command,
                "cwd": cwd,
                "timeout": timeout,
                "stdin_data": stdin_data,
            }
        )
        return {"output": "", "returncode": 0}


def test_get_file_ops_wraps_runtime_active_env(monkeypatch):
    import agent_tools.file_toolkit.file_tools as file_tools

    fake_env = FakeEnv(cwd="/workspace")
    calls = []

    def fake_get_or_create_active_env(task_id, workdir=None, timeout=None):
        calls.append({"task_id": task_id, "workdir": workdir, "timeout": timeout})
        return fake_env

    file_tools.clear_file_ops_cache()
    monkeypatch.setattr(file_tools, "get_or_create_active_env", fake_get_or_create_active_env)

    ops = file_tools._get_file_ops("task-file")

    assert ops.env is fake_env
    assert calls == [{"task_id": "task-file", "workdir": None, "timeout": None}]


def test_get_file_ops_reuses_cached_wrapper_for_same_env(monkeypatch):
    import agent_tools.file_toolkit.file_tools as file_tools

    fake_env = FakeEnv(cwd="/workspace")
    call_count = 0

    def fake_get_or_create_active_env(task_id, workdir=None, timeout=None):
        nonlocal call_count
        call_count += 1
        return fake_env

    file_tools.clear_file_ops_cache()
    monkeypatch.setattr(file_tools, "get_or_create_active_env", fake_get_or_create_active_env)

    first = file_tools._get_file_ops("task-file")
    second = file_tools._get_file_ops("task-file")

    assert first is second
    assert first.env is fake_env
    assert call_count == 1


def test_get_file_ops_refreshes_wrapper_when_cached_env_changes(monkeypatch):
    import agent_tools.file_toolkit.file_tools as file_tools

    first_env = FakeEnv(cwd="/workspace")
    second_env = FakeEnv(cwd="/workspace")
    current = {"env": first_env}

    def fake_get_or_create_active_env(task_id, workdir=None, timeout=None):
        return current["env"]

    file_tools.clear_file_ops_cache()
    monkeypatch.setattr(file_tools, "get_or_create_active_env", fake_get_or_create_active_env)

    first = file_tools._get_file_ops("task-file")
    file_tools.clear_file_ops_cache("task-file")
    current["env"] = second_env
    second = file_tools._get_file_ops("task-file")

    assert first is not second
    assert first.env is first_env
    assert second.env is second_env
```

- [ ] **Step 2: Run file-ops backend tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_file_tools_active_env.py -q
```

Expected: FAIL because `agent_tools.file_toolkit.file_tools` has no `get_or_create_active_env` import and `_get_file_ops()` still creates `LocalTerminalEnvironment`.

- [ ] **Step 3: Replace `LocalTerminalEnvironment` usage**

In `agent_tools/file_toolkit/file_tools.py`, change imports near the top from:

```python
from agent_tools.file_toolkit.terminal_environment import LocalTerminalEnvironment
```

to:

```python
from agent_tools.terminal_toolkit.terminal_tool import get_or_create_active_env
```

Then replace `_get_file_ops()` with:

```python
def _get_file_ops(task_id: str = "default") -> ShellFileOperations:
    """Get or create ShellFileOperations backed by this task's terminal env."""
    effective_task_id = task_id or "default"

    with _file_ops_lock:
        cached = _file_ops_cache.get(effective_task_id)
        if cached is not None:
            return cached

    env = get_or_create_active_env(effective_task_id)

    with _file_ops_lock:
        cached = _file_ops_cache.get(effective_task_id)
        if cached is not None and getattr(cached, "env", None) is env:
            return cached

        file_ops = ShellFileOperations(env)
        _file_ops_cache[effective_task_id] = file_ops
        return file_ops
```

Update `__all__` at the bottom of the same file by removing `"LocalTerminalEnvironment"`:

```python
__all__ = [
    "read_file_tool",
    "write_file_tool",
    "patch_tool",
    "search_tool",
    "clear_file_ops_cache",
    "reset_file_dedup",
    "notify_other_tool_call",
]
```

- [ ] **Step 4: Run file-ops backend tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_file_tools_active_env.py -q
```

Expected: PASS.

- [ ] **Step 5: Run public file-tool runtime tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_file_tools_runtime_task_id.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit file-tool backend change**

Run:

```bash
git add agent_tools/file_toolkit/file_tools.py tests/test_file_tools_active_env.py
git commit -m "refactor: back file tools with reference implementation active env"
```

---

### Task 5: Clear File-Ops Cache When reference implementation Environments Are Cleaned

**Files:**
- Modify: `agent_tools/terminal_toolkit/terminal_tool.py`
- Modify: `tests/test_active_env.py`
- Test: `tests/test_active_env.py`

- [ ] **Step 1: Add failing cleanup cache tests**

Append these tests to `tests/test_active_env.py`:

```python
def test_cleanup_vm_clears_file_ops_cache(monkeypatch):
    from agent_tools.terminal_toolkit import terminal_tool
    import agent_tools.file_toolkit.file_tools as file_tools

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    fake_env = FakeEnv()
    cleared = []

    terminal_tool._active_environments["task-clean"] = fake_env
    terminal_tool._last_activity["task-clean"] = 123.0
    terminal_tool._creation_locks["task-clean"] = object()
    monkeypatch.setattr(file_tools, "clear_file_ops_cache", lambda task_id=None: cleared.append(task_id))

    terminal_tool.cleanup_vm("task-clean")

    assert fake_env.cleaned is True
    assert cleared == ["task-clean"]


def test_cleanup_inactive_envs_clears_file_ops_cache(monkeypatch):
    from agent_tools.terminal_toolkit import terminal_tool
    import agent_tools.file_toolkit.file_tools as file_tools

    _reset_terminal_env_state(monkeypatch, terminal_tool)
    fake_env = FakeEnv()
    cleared = []

    terminal_tool._active_environments["task-idle"] = fake_env
    terminal_tool._last_activity["task-idle"] = 1.0
    monkeypatch.setattr(terminal_tool.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(terminal_tool.process_registry, "has_active_processes", lambda task_id: False)
    monkeypatch.setattr(file_tools, "clear_file_ops_cache", lambda task_id=None: cleared.append(task_id))

    terminal_tool._cleanup_inactive_envs(lifetime_seconds=300)

    assert fake_env.cleaned is True
    assert cleared == ["task-idle"]
```

- [ ] **Step 2: Run cleanup tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_active_env.py::test_cleanup_vm_clears_file_ops_cache tests/test_active_env.py::test_cleanup_inactive_envs_clears_file_ops_cache -q
```

Expected: FAIL because cleanup currently does not clear `_file_ops_cache`.

- [ ] **Step 3: Add file-op cache invalidation helper**

In `agent_tools/terminal_toolkit/terminal_tool.py`, add this helper near `cleanup_vm()`:

```python
def _clear_file_ops_cache_for_task(task_id: str) -> None:
    try:
        from agent_tools.file_toolkit.file_tools import clear_file_ops_cache

        clear_file_ops_cache(task_id)
    except Exception:
        logger.debug("Failed to clear file ops cache for task %s", task_id, exc_info=True)
```

Update `_cleanup_inactive_envs()` cleanup loop to call the helper after env cleanup:

```python
    for task_id, env in envs_to_stop:
        try:
            if hasattr(env, "cleanup"):
                env.cleanup()
            elif hasattr(env, "stop"):
                env.stop()
            _clear_file_ops_cache_for_task(task_id)
            logger.info("Cleaned up inactive environment for task: %s", task_id)
        except Exception as e:
            logger.warning("Error cleaning up environment for task %s: %s", task_id, e)
```

Update `cleanup_vm()` to call the helper after env cleanup:

```python
def cleanup_vm(task_id: str):
    env = None
    with _env_lock:
        env = _active_environments.pop(task_id, None)
        _last_activity.pop(task_id, None)
    with _creation_locks_lock:
        _creation_locks.pop(task_id, None)
    if env is None:
        _clear_file_ops_cache_for_task(task_id)
        return
    if hasattr(env, "cleanup"):
        env.cleanup()
    elif hasattr(env, "stop"):
        env.stop()
    _clear_file_ops_cache_for_task(task_id)
```

- [ ] **Step 4: Run cleanup tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_active_env.py::test_cleanup_vm_clears_file_ops_cache tests/test_active_env.py::test_cleanup_inactive_envs_clears_file_ops_cache -q
```

Expected: PASS.

- [ ] **Step 5: Run lifecycle regressions**

Run:

```bash
PYTHONPATH=. pytest tests/test_terminal_lifecycle.py tests/test_process_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit cleanup integration**

Run:

```bash
git add agent_tools/terminal_toolkit/terminal_tool.py tests/test_active_env.py
git commit -m "fix: clear file ops cache when terminal envs clean up"
```

---

### Task 6: Add End-To-End Local File Tool Smoke Test

**Files:**
- Modify: `tests/test_file_tools_active_env.py`
- Test: `tests/test_file_tools_active_env.py`

- [ ] **Step 1: Add local runtime-backed file operation smoke test**

Append this test to `tests/test_file_tools_active_env.py`:

```python
import json


def test_file_tools_use_shared_local_runtime_env_for_real_file_ops(tmp_path, monkeypatch):
    import agent_tools.file_toolkit.file_tools as file_tools
    from agent_tools.terminal_toolkit import terminal_tool

    file_tools.clear_file_ops_cache()
    terminal_tool.cleanup_vm("task-real-file")

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setenv("AGENT_WRITE_SAFE_ROOT", str(tmp_path))

    write_raw = file_tools.write_file_tool(
        path="notes.txt",
        content="hello from runtime env\n",
        task_id="task-real-file",
    )
    write_payload = json.loads(write_raw)
    assert write_payload.get("error") is None
    assert write_payload["bytes_written"] == len("hello from runtime env\n")

    read_raw = file_tools.read_file_tool(
        path="notes.txt",
        offset=1,
        limit=20,
        task_id="task-real-file",
    )
    read_payload = json.loads(read_raw)
    assert read_payload.get("error") is None
    assert "hello from runtime env" in read_payload["content"]

    active = terminal_tool.get_active_env("task-real-file")
    assert active is not None
    assert file_tools._get_file_ops("task-real-file").env is active

    terminal_tool.cleanup_vm("task-real-file")
```

- [ ] **Step 2: Run local smoke test**

Run:

```bash
PYTHONPATH=. pytest tests/test_file_tools_active_env.py::test_file_tools_use_shared_local_runtime_env_for_real_file_ops -q
```

Expected: PASS.

- [ ] **Step 3: Run all file and terminal tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_file_tools_active_env.py tests/test_file_tools_runtime_task_id.py tests/test_terminal_tools.py tests/test_terminal_lifecycle.py tests/test_process_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit local smoke coverage**

Run:

```bash
git add tests/test_file_tools_active_env.py
git commit -m "test: smoke file tools through shared terminal env"
```

---

### Task 7: Documentation And Final Verification

**Files:**
- Modify: `agent_tools/file_toolkit/README.md`
- Modify: `agent_tools/terminal_toolkit/README.md`
- Test: full focused suite

- [ ] **Step 1: Update file toolkit README**

In `agent_tools/file_toolkit/README.md`, add this paragraph near the usage section:

```markdown
## Backend

Model-facing file tools use `ShellFileOperations` against the active reference implementation
terminal environment for the current runtime-derived `task_id`. This means
`read_file`, `write_file`, `patch`, and `search_files` run through the same
backend selected by `TERMINAL_ENV` (`local`, `docker`, `singularity`, or `ssh`)
and reuse the same environment lifecycle as the `terminal` tool. Direct internal
callers may still instantiate `ShellFileOperations` with any object that exposes
`execute(command, cwd=..., timeout=..., stdin_data=...)`.
```

- [ ] **Step 2: Update terminal toolkit README**

In `agent_tools/terminal_toolkit/README.md`, add this paragraph near the environment/lifecycle section:

```markdown
## Shared Active Environments

`get_or_create_active_env(task_id, workdir=None, timeout=None)` is the shared
entry point for acquiring a runtime backend environment. The terminal tool and
file toolkit both use it, so task-scoped commands and shell-backed file
operations share `_active_environments`, creation locks, last-activity updates,
idle cleanup, and explicit `cleanup_vm(task_id)` teardown.
```

- [ ] **Step 3: Run focused verification**

Run:

```bash
PYTHONPATH=. pytest tests/test_active_env.py tests/test_file_tools_active_env.py tests/test_file_tools_runtime_task_id.py tests/test_terminal_tools.py tests/test_terminal_lifecycle.py tests/test_process_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 4: Check import and lint sanity**

Run:

```bash
PYTHONPATH=. python -m compileall agent_tools/terminal_toolkit agent_tools/file_toolkit agent_tools/public tests -q
```

Expected: exit code 0.

- [ ] **Step 5: Review diff for accidental behavior drift**

Run:

```bash
git diff -- agent_tools/terminal_toolkit/terminal_tool.py agent_tools/file_toolkit/file_tools.py agent_tools/file_toolkit/README.md agent_tools/terminal_toolkit/README.md tests/test_active_env.py tests/test_file_tools_active_env.py
```

Expected:
- `terminal_tool()` no longer contains duplicated active-env creation logic.
- `get_or_create_active_env()` is the only new public env acquisition helper.
- `file_tools._get_file_ops()` imports and calls `get_or_create_active_env()`.
- Cleanup paths call `_clear_file_ops_cache_for_task(task_id)`.
- No real Docker/Singularity/SSH commands are invoked by tests.

- [ ] **Step 6: Commit docs and verification changes**

Run:

```bash
git add agent_tools/file_toolkit/README.md agent_tools/terminal_toolkit/README.md
git commit -m "docs: document shared terminal env file tools"
```

---

## Risk Notes

- The file toolkit currently uses host-path checks for dedup and staleness. This plan makes file operations run through terminal envs but does not fully solve backend-aware path stat for Docker/Singularity/SSH. That remains a follow-up.
- If `TERMINAL_ENV=docker` and the workspace is not mounted into the container, file tools will correctly run in Docker but may not see the host repository. Use `TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE=true` or configured Docker volumes until workspace mapping is addressed in a later plan.
- `LocalTerminalEnvironment` stays in the tree to avoid breaking direct internal imports. A later cleanup can remove it after checking external callers.
- `get_or_create_active_env()` intentionally creates envs before file reads/searches. Docker image pulls and SSH connection setup can therefore happen from file tools for the first time.

## Follow-Up Plan Candidates

- Convert `list_directory` and `file_info` to `ShellFileOperations` so every model-facing file operation is backend-aware.
- Add backend-aware stat/mtime abstraction for dedup and stale-write detection.
- Add Docker workspace path mapping tests with `TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE=true`.
- Decide whether public `terminal` should pass host `WORKDIR` to Docker/Singularity execution or map it to backend workspace paths.

## Self-Review

- Spec coverage: The plan extracts `get_or_create_active_env(task_id, workdir=None, timeout=None)`, moves `_get_env_config()` and active environment locking into it, refactors `terminal_tool()`, and makes file tools wrap the shared terminal env.
- Placeholder scan: No `TBD`, `TODO`, or unspecified test steps remain.
- Type consistency: The plan consistently uses `task_id`, `workdir`, `timeout`, `ShellFileOperations`, `clear_file_ops_cache`, and `get_or_create_active_env`.

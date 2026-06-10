# Local And Docker Terminal Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove SSH and Singularity execution backends so terminal configuration, runtime construction, file policy, tests, and current documentation support only `local` and `docker`.

**Architecture:** Make `agent_core.permissions.profiles.resolve_terminal_env()` the single whitelist boundary and fail immediately for every unsupported explicit value. Delete the two backend modules and simplify downstream code to a two-backend model while preserving local behavior, Docker lifecycle behavior, and security references to sensitive `.ssh` paths.

**Tech Stack:** Python 3.11, pytest, LangChain tool wrappers, terminal toolkit environments, Markdown documentation

---

## Execution Prerequisite

Use the project Conda interpreter for every Python and pytest command:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -c "import sys; print(sys.executable); print(sys.version)"
/home/miku/miniforge3/envs/langchain/bin/python -m pytest --version
```

Expected: both commands succeed and report
`/home/miku/miniforge3/envs/langchain/bin/python`.

## File Structure

- Modify `agent_core/permissions/profiles.py`: own the supported terminal backend whitelist and reject unsupported explicit values.
- Modify `agent_tools/terminal_toolkit/terminal_tool.py`: retain only local and Docker configuration, construction, lifecycle, and requirement checks.
- Delete `agent_tools/terminal_toolkit/environments/ssh.py`: remove the SSH backend implementation.
- Delete `agent_tools/terminal_toolkit/environments/singularity.py`: remove the Singularity/Apptainer backend implementation.
- Modify `agent_tools/terminal_toolkit/environments/base.py`: remove stale descriptions of deleted or unsupported backends.
- Modify `agent_tools/terminal_toolkit/process_registry.py`: describe local and Docker background execution only.
- Modify `agent_tools/file_toolkit/backend_paths.py`: reduce backend path inference and defaults to local and Docker.
- Modify `agent_tools/file_toolkit/file_operations.py`: reduce host-vs-container inference and backend documentation to local and Docker.
- Modify `agent_tools/file_toolkit/file_tools.py`: update mtime documentation to local and Docker.
- Modify tests under `tests/`: add whitelist coverage, preserve Docker equivalents of generic non-local tests, and delete backend-specific SSH/Singularity cases.
- Modify current runtime docs: `README.md`, `agent_tools/terminal_toolkit/README.md`, and `agent_tools/file_toolkit/README.md`.
- Leave all existing historical files under `docs/superpowers/specs/` and `docs/superpowers/plans/` unchanged.

### Task 1: Enforce The Terminal Backend Whitelist

**Files:**
- Modify: `agent_core/permissions/profiles.py:7-51`
- Modify: `tests/test_permissions_profiles.py:74-94`

- [ ] **Step 1: Replace the permissive explicit-value test with whitelist tests**

Replace `test_explicit_terminal_env_wins` and append the following tests:

```python
@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("local", "local"),
        ("docker", "docker"),
        (" LOCAL ", "local"),
        (" Docker ", "docker"),
    ],
)
def test_explicit_terminal_env_accepts_supported_values(
    monkeypatch, raw_value, expected
):
    from agent_core.permissions.profiles import resolve_terminal_env

    monkeypatch.setenv("TERMINAL_ENV", raw_value)

    assert resolve_terminal_env("prod") == expected


@pytest.mark.parametrize("raw_value", ["ssh", "singularity", "podman", "dockre"])
def test_explicit_terminal_env_rejects_unsupported_values(monkeypatch, raw_value):
    from agent_core.permissions.profiles import resolve_terminal_env

    monkeypatch.setenv("TERMINAL_ENV", raw_value)

    with pytest.raises(
        ValueError,
        match=rf"Unsupported TERMINAL_ENV {raw_value!r}\. Supported values: local, docker\.",
    ):
        resolve_terminal_env("dev")


def test_blank_terminal_env_uses_profile_default(monkeypatch):
    from agent_core.permissions.profiles import resolve_terminal_env

    monkeypatch.setenv("TERMINAL_ENV", "   ")

    assert resolve_terminal_env("dev") == "local"
    assert resolve_terminal_env("prod") == "docker"
```

- [ ] **Step 2: Run the focused tests and verify the unsupported-value cases fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_permissions_profiles.py -q
```

Expected: the new unsupported-value tests fail because
`resolve_terminal_env()` still returns arbitrary explicit values.

- [ ] **Step 3: Add the supported backend constant and validate explicit values**

Update `agent_core/permissions/profiles.py`:

```python
VALID_PROFILES: set[str] = {"dev", "test", "hosted", "prod"}
SUPPORTED_TERMINAL_ENVS: frozenset[str] = frozenset({"local", "docker"})
```

Replace `resolve_terminal_env()` with:

```python
def resolve_terminal_env(profile: RuntimeProfile | str | None = None) -> str:
    explicit = os.getenv("TERMINAL_ENV", "").strip().lower()
    if explicit:
        if explicit not in SUPPORTED_TERMINAL_ENVS:
            raise ValueError(
                f"Unsupported TERMINAL_ENV {explicit!r}. "
                "Supported values: local, docker."
            )
        return explicit
    return default_terminal_env(profile or resolve_runtime_profile())
```

- [ ] **Step 4: Run profile tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_permissions_profiles.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the configuration boundary**

```bash
git add agent_core/permissions/profiles.py tests/test_permissions_profiles.py
git commit -m "refactor: restrict terminal backends"
```

### Task 2: Remove SSH And Singularity From The Terminal Toolkit

**Files:**
- Modify: `agent_tools/terminal_toolkit/terminal_tool.py:1-34`
- Modify: `agent_tools/terminal_toolkit/terminal_tool.py:267-431`
- Modify: `agent_tools/terminal_toolkit/terminal_tool.py:477-565`
- Modify: `agent_tools/terminal_toolkit/terminal_tool.py:931-961`
- Delete: `agent_tools/terminal_toolkit/environments/ssh.py`
- Delete: `agent_tools/terminal_toolkit/environments/singularity.py`
- Modify: `tests/test_terminal_toolkit_active_env.py:22-49`
- Modify: `tests/test_terminal_toolkit_active_env.py:101-238`
- Modify: `tests/test_terminal_toolkit_active_env.py:395-450`

- [ ] **Step 1: Reduce the fake config to the two-backend contract**

Change `_fake_config()` in `tests/test_terminal_toolkit_active_env.py` to:

```python
def _fake_config(env_type="local"):
    return {
        "env_type": env_type,
        "docker_image": "fake-docker-image",
        "cwd": "/workspace",
        "host_cwd": None,
        "docker_mount_cwd_to_workspace": False,
        "timeout": 180,
        "lifetime_seconds": 300,
        "local_persistent": False,
        "container_cpu": 1,
        "container_memory": 5120,
        "container_disk": 51200,
        "container_persistent": True,
        "container_network": True,
        "docker_volumes": [],
        "docker_forward_env": [],
        "docker_run_as_host_user": False,
    }
```

- [ ] **Step 2: Add tests for removed config keys and the defensive factory error**

Append:

```python
def test_get_env_config_exposes_only_local_and_docker_backend_settings(monkeypatch):
    from agent_tools.terminal_toolkit import terminal_tool

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "dev")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    config = terminal_tool._get_env_config()

    assert "singularity_image" not in config
    assert "ssh_host" not in config
    assert "ssh_user" not in config
    assert "ssh_port" not in config
    assert "ssh_key" not in config


def test_create_environment_rejects_unsupported_direct_input():
    import pytest

    from agent_tools.terminal_toolkit import terminal_tool

    with pytest.raises(
        ValueError,
        match="Unknown environment type: podman. Use 'local' or 'docker'",
    ):
        terminal_tool._create_environment(
            env_type="podman",
            image="",
            cwd="/workspace",
            timeout=30,
        )
```

- [ ] **Step 3: Run the terminal active-environment tests and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_terminal_toolkit_active_env.py -q
```

Expected: the config-key test fails because legacy SSH/Singularity keys remain,
and the factory message still lists four backends.

- [ ] **Step 4: Remove legacy imports, constants, config keys, and cwd branches**

In `agent_tools/terminal_toolkit/terminal_tool.py`:

- delete imports from `.environments.singularity` and `.environments.ssh`;
- delete `DISK_USAGE_WARNING_THRESHOLD_GB`;
- delete `_check_disk_usage_warning()`;
- make the cwd default and Docker host-path filter:

```python
    default_cwd = os.getcwd() if env_type == "local" else "/root"

    cwd = os.getenv("TERMINAL_CWD", default_cwd)
    if cwd:
        cwd = os.path.expanduser(cwd)
    host_cwd = None
    host_prefixes = ("/Users/", "/home/", "C:\\", "C:/")
    if env_type == "docker" and mount_docker_cwd:
        docker_cwd_source = os.getenv("TERMINAL_CWD") or os.getcwd()
        candidate = os.path.abspath(os.path.expanduser(docker_cwd_source))
        if any(candidate.startswith(p) for p in host_prefixes) or (
            os.path.isabs(candidate)
            and os.path.isdir(candidate)
            and not candidate.startswith(("/workspace", "/root"))
        ):
            host_cwd = candidate
            cwd = "/workspace"
    elif env_type == "docker" and cwd:
        is_host_path = any(cwd.startswith(p) for p in host_prefixes)
        is_relative = not os.path.isabs(cwd)
        if (is_host_path or is_relative) and cwd != default_cwd:
            logger.info(
                "Ignoring TERMINAL_CWD=%r for docker backend "
                "(host/relative path won't work in sandbox). Using %r instead.",
                cwd,
                default_cwd,
            )
            cwd = default_cwd
```

Return only these backend-specific keys:

```python
        "env_type": env_type,
        "docker_image": os.getenv("TERMINAL_DOCKER_IMAGE", default_image),
        "cwd": cwd,
        "host_cwd": host_cwd,
```

Keep the existing timeout, lifetime, local persistence, container resource,
network, volume, forwarding, and host-user keys after this block. Delete
`singularity_image` and all `ssh_*` entries.

- [ ] **Step 5: Simplify environment construction and helper functions**

Change `_create_environment()` to remove `ssh_config`:

```python
def _create_environment(
    env_type: str,
    image: str,
    cwd: str,
    timeout: int,
    container_config: dict = None,
    task_id: str = "default",
    host_cwd: str = None,
):
```

Retain the existing local and Docker branches unchanged, then end with:

```python
    raise ValueError(
        f"Unknown environment type: {env_type}. Use 'local' or 'docker'."
    )
```

Replace `_image_for_env_type()` and `_container_config_from_terminal_config()`:

```python
def _image_for_env_type(config: dict, env_type: str) -> str:
    return config["docker_image"] if env_type == "docker" else ""


def _container_config_from_terminal_config(config: dict) -> dict | None:
    if config["env_type"] != "docker":
        return None
    return {
        "container_cpu": config.get("container_cpu", 1),
        "container_memory": config.get("container_memory", 5120),
        "container_disk": config.get("container_disk", 51200),
        "container_persistent": config.get("container_persistent", True),
        "container_network": config.get("container_network", True),
        "docker_volumes": config.get("docker_volumes", []),
        "docker_mount_cwd_to_workspace": config.get(
            "docker_mount_cwd_to_workspace", False
        ),
        "docker_forward_env": config.get("docker_forward_env", []),
        "docker_env": config.get("docker_env", {}),
        "docker_run_as_host_user": config.get(
            "docker_run_as_host_user", False
        ),
    }
```

Delete `_ssh_config_from_terminal_config()`. In
`get_or_create_active_env()`, delete the Singularity disk check and remove the
`ssh_config=` argument from `_create_environment(...)`.

- [ ] **Step 6: Simplify requirement checks**

Replace `check_terminal_requirements()` with:

```python
def check_terminal_requirements() -> bool:
    config = _get_env_config()
    env_type = config["env_type"]
    try:
        if env_type == "local":
            return True
        if env_type == "docker":
            docker = find_docker()
            if not docker:
                logger.error(
                    "Docker executable not found in PATH or common install locations"
                )
                return False
            result = subprocess.run(
                [docker, "version"], capture_output=True, timeout=5
            )
            return result.returncode == 0
        logger.error(
            "Unknown TERMINAL_ENV '%s'. Use one of: local, docker.", env_type
        )
        return False
    except Exception as e:
        logger.error(
            "Terminal requirements check failed: %s", e, exc_info=True
        )
        return False
```

- [ ] **Step 7: Delete the backend modules**

Delete:

```text
agent_tools/terminal_toolkit/environments/ssh.py
agent_tools/terminal_toolkit/environments/singularity.py
```

- [ ] **Step 8: Run terminal toolkit tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_terminal_toolkit_active_env.py \
  tests/test_terminal_tools.py \
  tests/test_permissions_docker_network.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit the terminal toolkit removal**

```bash
git add agent_tools/terminal_toolkit/terminal_tool.py \
  agent_tools/terminal_toolkit/environments/ssh.py \
  agent_tools/terminal_toolkit/environments/singularity.py \
  tests/test_terminal_toolkit_active_env.py
git commit -m "refactor: remove ssh and singularity environments"
```

### Task 3: Reduce Backend Path Policy To Local And Docker

**Files:**
- Modify: `agent_tools/file_toolkit/backend_paths.py:15-234`
- Modify: `tests/test_backend_path_policy.py:1-395`

- [ ] **Step 1: Delete SSH-only test fixtures and backend-specific tests**

Remove the `SSHEnvironment` and `ConfigOnlySSHEnvironment` fixtures and delete
these tests from `tests/test_backend_path_policy.py`:

```text
test_safe_write_roots_for_untagged_ssh_environment_uses_class_name_fallback
test_safe_write_roots_for_class_name_ssh_fallback_excludes_host_safe_root
test_safe_write_roots_for_class_name_ssh_fallback_excludes_config_host_safe_root
test_safe_write_roots_for_class_name_ssh_fallback_keeps_remote_home_cwd
test_policy_uses_active_ssh_cwd_without_host_expanding_tilde
test_policy_rejects_host_expanded_home_as_ssh_configured_root
test_policy_rejects_host_home_descendant_as_ssh_configured_root
test_policy_uses_singularity_active_cwd_without_stale_config_root
```

- [ ] **Step 2: Preserve the generic non-local tilde test using Docker**

Replace `test_policy_rejects_non_local_tilde_paths_without_backend_home` with:

```python
@pytest.mark.parametrize("path", ["~", "~/outside.txt", "~user/file"])
def test_policy_rejects_docker_tilde_paths_without_backend_home(
    monkeypatch, path
):
    from agent_tools.file_toolkit import backend_paths
    from agent_tools.terminal_toolkit import terminal_tool

    active = FakeEnv("/workspace/project", "docker")
    monkeypatch.setattr(terminal_tool, "get_active_env", lambda task_id: active)
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "docker",
            "cwd": "/workspace/project",
            "host_cwd": None,
        },
    )

    with pytest.raises(ValueError):
        backend_paths.resolve_path_for_policy(path, "task-docker-tilde")
```

- [ ] **Step 3: Add a focused class-name inference assertion**

Extend
`test_safe_write_roots_for_untagged_docker_environment_uses_class_name_fallback`:

```python
    assert backend_paths._env_type_from_class_name(
        DockerEnvironment("/workspace")
    ) == "docker"
    assert backend_paths._env_type_from_class_name(
        FakeEnv("/root", "local")
    ) is None
```

- [ ] **Step 4: Run backend path policy tests before implementation**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_backend_path_policy.py -q
```

Expected: tests pass after the test cleanup, establishing the local/Docker
behavior that must remain intact.

- [ ] **Step 5: Simplify backend inference and default cwd**

In `agent_tools/file_toolkit/backend_paths.py`, replace:

```python
def _env_type_from_class_name(env) -> str | None:
    class_name = env.__class__.__name__.lower()
    return "docker" if "docker" in class_name else None
```

Change the fallback in `get_backend_path_context()` from:

```python
            or _default_backend_cwd(env_type)
```

to:

```python
            or "/root"
```

Delete `_default_backend_cwd()`.

Keep the local branch, Docker `/workspace` allowance, configured-cwd filtering,
host-home filtering, host-workspace filtering, and tilde rejection unchanged.

- [ ] **Step 6: Run backend path and permission tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_backend_path_policy.py \
  tests/test_permissions_file_policy.py \
  tests/test_permissions_file_wrappers.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the backend path simplification**

```bash
git add agent_tools/file_toolkit/backend_paths.py \
  tests/test_backend_path_policy.py
git commit -m "refactor: simplify backend path policy"
```

### Task 4: Remove Legacy Backend Cases From File Tools

**Files:**
- Modify: `agent_tools/file_toolkit/file_operations.py:1-7`
- Modify: `agent_tools/file_toolkit/file_operations.py:180-186`
- Modify: `agent_tools/file_toolkit/file_operations.py:282-292`
- Modify: `agent_tools/file_toolkit/file_operations.py:420-424`
- Modify: `agent_tools/file_toolkit/file_tools.py:123-128`
- Modify: `tests/test_file_tools_runtime_task_id.py:420-770`
- Modify: `tests/test_file_tools_terminal_env.py:174-1000`
- Modify: `tests/test_file_tools_terminal_env.py:1270-1455`

- [ ] **Step 1: Delete file-tool tests that specify removed backend semantics**

Delete these tests from `tests/test_file_tools_runtime_task_id.py`:

```text
test_write_file_allows_active_ssh_cwd_path_and_forwards_original_path
test_write_file_rejects_ssh_absolute_path_outside_active_cwd
test_read_file_allows_singularity_active_cwd_path
test_file_info_uses_active_ssh_backend_not_host_path_info
test_list_directory_rejects_ssh_path_outside_active_cwd_without_backend_exec
```

Delete these tests from `tests/test_file_tools_terminal_env.py`:

```text
test_resolve_path_for_task_uses_backend_policy_for_ssh_active_cwd
test_shell_file_operations_allows_singularity_effective_cwd_writes
test_shell_file_operations_keeps_workspace_root_denied_for_singularity
test_shell_file_operations_allows_effective_ssh_cwd_writes
test_shell_file_operations_keeps_workspace_root_denied_for_ssh
test_shell_file_operations_denies_host_safe_root_for_untagged_ssh_without_backend_roots
test_shell_file_operations_denies_config_host_safe_root_for_untagged_ssh
```

- [ ] **Step 2: Convert generic backend tests from SSH to Docker**

In
`test_shell_file_operations_denies_host_safe_root_absolute_path_outside_backend_roots`,
change:

```python
@pytest.mark.parametrize("env_type", ["docker"])
```

In the mtime tests near the end of
`tests/test_file_tools_terminal_env.py`, replace fake
`_backend_env_type = "ssh"` declarations and SSH cwd/config values with:

```python
_backend_env_type = "docker"
cwd = "/workspace/project"
```

and:

```python
lambda: {
    "env_type": "docker",
    "cwd": "/workspace/project",
    "host_cwd": None,
}
```

Rename affected test-local task ids from `ssh-*` to `docker-*`. Keep assertions
about backend mtime, unknown mtime, deduplication, and stale-write behavior
unchanged.

- [ ] **Step 3: Run the file-tool suites before implementation cleanup**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_file_tools_runtime_task_id.py \
  tests/test_file_tools_terminal_env.py \
  tests/test_file_operations_backend_stat.py \
  tests/test_file_state_backend_mtime.py -q
```

Expected: all retained tests pass, proving Docker still covers generic
non-local shell and mtime behavior.

- [ ] **Step 4: Simplify file operation backend detection**

In `agent_tools/file_toolkit/file_operations.py`, replace
`_host_safe_root_applies()` with:

```python
    def _host_safe_root_applies(self) -> bool:
        """Return whether the host safe root should be used for this env."""
        env_type = self._env_type()
        if env_type is not None:
            return str(env_type) == "local"

        class_name = self.env.__class__.__name__.lower()
        return "docker" not in class_name
```

Update the module and class docstrings to say file operations work with the
supported local and Docker terminal backends. Update `get_mtime()` documentation
to:

```python
        """Return file mtime from the active filesystem, or None if unavailable.

        GNU stat (`stat -c %Y %y`) covers the Linux Docker backend. The BSD
        fallback keeps the helper portable when an environment provides BSD
        stat semantics. The caller decides how to degrade when no mtime can be
        sampled.
        """
```

In `agent_tools/file_toolkit/file_tools.py`, change the mtime description to:

```python
    """Sample mtime from the correct filesystem for this task.

    Local environments use host os.path.getmtime. Docker uses the active
    terminal toolkit backend shell. A None result is an explicit degradation:
    dedup and external-drift checks are skipped, but file_state coordination
    remains active.
    """
```

- [ ] **Step 5: Run file-tool and public wrapper tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_file_tools_runtime_task_id.py \
  tests/test_file_tools_terminal_env.py \
  tests/test_file_operations_backend_stat.py \
  tests/test_file_state_backend_mtime.py \
  tests/test_permissions_file_wrappers.py \
  tests/test_public_toolmessage_results.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit file-tool cleanup**

```bash
git add agent_tools/file_toolkit/file_operations.py \
  agent_tools/file_toolkit/file_tools.py \
  tests/test_file_tools_runtime_task_id.py \
  tests/test_file_tools_terminal_env.py
git commit -m "test: remove legacy file backend cases"
```

### Task 5: Clean Generic Runtime References And Current Documentation

**Files:**
- Modify: `agent_tools/terminal_toolkit/environments/base.py:81-85`
- Modify: `agent_tools/terminal_toolkit/environments/base.py:666-710`
- Modify: `agent_tools/terminal_toolkit/process_registry.py:11-15`
- Modify: `agent_tools/terminal_toolkit/process_registry.py:599-603`
- Modify: `tests/test_code_execution_tool.py:36-52`
- Modify: `tests/test_code_execution_tool.py:478-500`
- Modify: `README.md:39-48`
- Modify: `agent_tools/terminal_toolkit/README.md:1-55`
- Modify: `agent_tools/file_toolkit/README.md:27-48`

- [ ] **Step 1: Keep unsupported-backend coverage without naming removed backends**

Change the two code-execution unsupported-backend tests to use `podman`:

```python
def test_execute_code_with_backend_rejects_unknown_non_local_backend():
    from agent_tools.code_execution.runners import execute_code_with_backend

    result = execute_code_with_backend(
        code='print("hello")',
        runtime=None,
        enabled_tools=[],
        include_web=False,
        backend_env_type="podman",
    )

    assert result.artifact["ok"] is False
    assert result.artifact["error"]["code"] == "unsupported_backend"
    assert result.artifact["data"]["env_type"] == "podman"
```

```python
def test_execute_code_impl_rejects_unsupported_non_local_backend(monkeypatch):
    from agent_tools.public.code_execution import execute_code_impl

    monkeypatch.setattr(
        "agent_tools.file_toolkit.backend_paths.get_backend_path_context",
        lambda task_id: SimpleNamespace(
            env_type="podman", cwd="/unsupported/project"
        ),
    )

    result = execute_code_impl(
        code='print("backend")',
        runtime=_runtime("code-exec-podman"),
        enabled_tools=["read_file"],
        include_web=False,
        timeout_seconds=5,
    )

    assert result.status == "error"
    assert result.artifact["error"]["code"] == "unsupported_backend"
    assert result.artifact["data"]["env_type"] == "podman"
```

- [ ] **Step 2: Update shared terminal comments without changing behavior**

In `agent_tools/terminal_toolkit/environments/base.py`:

- describe `get_sandbox_dir()` as host-side Docker sandbox storage;
- describe cwd marker updates as used by Docker;
- describe `pre_execute_sync()` as an extension hook and state that local and
  Docker do not require file synchronization.

Use:

```python
def get_sandbox_dir() -> Path:
    """Return the host-side root for Docker sandbox storage.

    Configurable via TERMINAL_SANDBOX_DIR. Defaults to terminal toolkit
    home/sandboxes/.
    """
```

```python
        """Update cwd from the command marker and strip it from output.

        The Docker backend uses this to persist shell cwd between calls.
        """
```

```python
        """Hook called before each command execution.

        Local and Docker do not require file synchronization. The hook remains
        available for the base environment contract.
        """
```

In `agent_tools/terminal_toolkit/process_registry.py`, state that background
commands run on the host only for local and inside the container for Docker.
Replace the backend list in `spawn_via_env()` with:

```python
        For Docker, the command runs inside the container through the
        environment's execute() interface.
```

- [ ] **Step 3: Update the root README backend contract**

Replace the code-execution backend section in `README.md` with:

```markdown
Backend behavior:

- `local`: runs a child Python process and uses Unix domain socket RPC.
- `docker`: reuses the active terminal toolkit Docker environment and uses
  file-based RPC under `/workspace/.code_execution/<run_id>/`.

`TERMINAL_ENV` accepts only `local` or `docker`. Any other explicit value fails
during configuration resolution; unsupported values never fall back to another
backend.
```

Keep the existing profile defaults and `.ssh` sensitive-path denial text.

- [ ] **Step 4: Update terminal and file toolkit READMEs**

In `agent_tools/terminal_toolkit/README.md`:

```markdown
- environment backends for `local` and `docker`
```

Replace the env-var list entry with:

```markdown
- `TERMINAL_ENV`: `local` or `docker`; any other explicit value is rejected
```

Remove:

```text
TERMINAL_SSH_HOST
TERMINAL_SSH_USER
TERMINAL_SSH_PORT
TERMINAL_SSH_KEY
TERMINAL_SINGULARITY_IMAGE
```

In `agent_tools/file_toolkit/README.md`, keep only:

```markdown
Low-level safe write roots are backend-aware:

- Local low-level write safety enforces the shared denylist and, when set,
  `AGENT_WRITE_SAFE_ROOT`.
- Docker file tools also allow backend paths under `/workspace` and the
  container active or configured cwd.
```

Change mtime ownership to:

```markdown
- local backend: host `os.path.getmtime()`
- Docker: backend shell `stat` through `ShellFileOperations`
```

- [ ] **Step 5: Run code-execution and documentation-adjacent tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_code_execution_tool.py \
  tests/test_code_execution_docker.py \
  tests/test_terminal_toolkit_active_env.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit runtime text and current documentation**

```bash
git add README.md \
  agent_tools/terminal_toolkit/README.md \
  agent_tools/file_toolkit/README.md \
  agent_tools/terminal_toolkit/environments/base.py \
  agent_tools/terminal_toolkit/process_registry.py \
  tests/test_code_execution_tool.py
git commit -m "docs: document local and docker backends"
```

### Task 6: Add A Regression Guard And Run Full Verification

**Files:**
- Modify: `tests/test_agent_tools_structure.py`

- [ ] **Step 1: Add a structural regression test**

Append to `tests/test_agent_tools_structure.py`:

```python
def test_terminal_runtime_exposes_only_local_and_docker_backends():
    removed_modules = [
        Path("agent_tools/terminal_toolkit/environments/ssh.py"),
        Path("agent_tools/terminal_toolkit/environments/singularity.py"),
    ]
    assert [path for path in removed_modules if path.exists()] == []

    forbidden_runtime_tokens = {
        "agent_tools/terminal_toolkit/terminal_tool.py": [
            "TERMINAL_SSH_",
            "TERMINAL_SINGULARITY_IMAGE",
            "SSHEnvironment",
            "SingularityEnvironment",
        ],
        "agent_tools/file_toolkit/backend_paths.py": [
            '"ssh"',
            '"singularity"',
        ],
        "agent_tools/terminal_toolkit/README.md": [
            "`ssh`",
            "`singularity`",
            "TERMINAL_SSH_",
            "TERMINAL_SINGULARITY_IMAGE",
        ],
        "agent_tools/file_toolkit/README.md": [
            "SSH file tools",
            "Singularity file tools",
        ],
    }

    for filename, forbidden_tokens in forbidden_runtime_tokens.items():
        source = Path(filename).read_text(encoding="utf-8")
        assert [token for token in forbidden_tokens if token in source] == []
```

This intentionally does not scan `file_safety.py`, permission policy, cron
security rules, or root README sensitive-path examples. References to `.ssh`
and SSH backdoor detection remain valid security controls.

- [ ] **Step 2: Run the regression test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_agent_tools_structure.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run a scoped literal residual check**

Run:

```bash
rg -n -S \
  'TERMINAL_SSH_|TERMINAL_SINGULARITY_IMAGE|SSHEnvironment|SingularityEnvironment|env_type == "ssh"|env_type == "singularity"' \
  agent_core agent_tools tests README.md \
  --glob '*.py' --glob '*.md' \
  --glob '!test_agent_tools_structure.py'
```

Expected: no matches. The structural guard is excluded because it intentionally
contains the forbidden tokens as test data. Security-only `.ssh`,
`authorized_keys`, and `ssh_backdoor` references are outside this
backend-specific pattern and remain.

- [ ] **Step 4: Run the focused backend regression suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_permissions_profiles.py \
  tests/test_terminal_toolkit_active_env.py \
  tests/test_backend_path_policy.py \
  tests/test_file_tools_runtime_task_id.py \
  tests/test_file_tools_terminal_env.py \
  tests/test_code_execution_tool.py \
  tests/test_code_execution_docker.py \
  tests/test_agent_tools_structure.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run the complete test suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 6: Check formatting and worktree scope**

Run:

```bash
git diff --check
git status --short
```

Expected: `git diff --check` produces no output. `git status --short` lists
only the intended implementation changes plus any unrelated pre-existing user
files; do not stage or modify the pre-existing untracked
`docs/superpowers/plans/2026-06-09-code-execution-tool.md`.

- [ ] **Step 7: Commit the regression guard**

```bash
git add tests/test_agent_tools_structure.py
git commit -m "test: guard supported terminal backends"
```

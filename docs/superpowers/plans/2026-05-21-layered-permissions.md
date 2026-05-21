# Layered Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a unified, profile-aware permission system that automatically allows low-risk workspace work, reviews risky file/shell actions, denies sensitive operations, and enforces Docker network isolation for hosted/prod.

**Architecture:** Add `agent_core.permissions` as the policy source of truth, then wire it into the existing HITL middleware and public file/terminal wrappers. Keep low-level file and Hermes terminal toolkits as defensive backstops, with narrowly scoped extension points for approved workspace escapes and one-shot Docker network leases.

**Tech Stack:** Python, LangChain/LangGraph middleware, Pydantic tool schemas, Hermes terminal toolkit, pytest.

---

## File Structure

- Create `agent_core/permissions/__init__.py`: package exports for policy decisions and evaluation helpers.
- Create `agent_core/permissions/models.py`: `PolicyDecision`, `PolicyOutcome`, `RiskTag`, and helpers.
- Create `agent_core/permissions/profiles.py`: runtime profile resolution and default terminal backend selection.
- Create `agent_core/permissions/approvals.py`: in-memory single-use approval registry keyed by task id and tool call id.
- Create `agent_core/permissions/audit.py`: structured policy audit logging.
- Create `agent_core/permissions/command_policy.py`: shell command risk classification.
- Create `agent_core/permissions/file_policy.py`: file tool path classification and sensitive path checks.
- Create `agent_core/permissions/tool_policy.py`: top-level tool-call evaluation used by middleware and wrappers.
- Modify `agent_core/human_loop.py`: make HITL policy-driven for file/terminal/process tools while keeping mandatory review for memory/skills.
- Modify `agent_core/builders.py`: replace static review config for file/terminal/process tools with policy-aware middleware configuration.
- Modify `agent_tools/public/files.py`: enforce wrapper-level file policy and consume approvals for reviewed workspace escapes.
- Modify `agent_tools/file_toolkit/file_tools.py`: pass approved write roots to low-level operations for approved ordinary escapes.
- Modify `agent_tools/file_toolkit/file_operations.py`: support temporary approved write roots while preserving sensitive denylist.
- Modify `agent_tools/public/terminal.py`: enforce wrapper-level terminal/process policy, consume approvals, and forward one-shot network grants.
- Modify `agent_tools/hermes_terminal_toolkit/terminal.py`: pass `allow_network_once` into `terminal_tool`.
- Modify `agent_tools/hermes_terminal_toolkit/terminal_tool.py`: use profile-derived default backend and wrap approved commands in network leases.
- Modify `agent_tools/hermes_terminal_toolkit/environments/docker.py`: support default no-network and temporary network attach/detach.
- Modify `agent_tools/hermes_terminal_toolkit/process_registry.py`: release background network leases when sandbox-backed processes finish.
- Add tests under `tests/` for profiles, command policy, file policy, approval registry, middleware, wrappers, Docker network behavior, and integration defaults.

---

### Task 1: Permission Models, Profiles, Audit, and Approval Registry

**Files:**
- Create: `agent_core/permissions/__init__.py`
- Create: `agent_core/permissions/models.py`
- Create: `agent_core/permissions/profiles.py`
- Create: `agent_core/permissions/approvals.py`
- Create: `agent_core/permissions/audit.py`
- Test: `tests/test_permissions_profiles.py`
- Test: `tests/test_permissions_approvals.py`

- [ ] **Step 1: Write failing profile tests**

Add `tests/test_permissions_profiles.py`:

```python
import pytest


def test_explicit_profile_wins(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "hosted")
    monkeypatch.setenv("CI", "true")

    assert resolve_runtime_profile() == "hosted"


def test_invalid_explicit_profile_raises(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "staging")

    with pytest.raises(ValueError, match="Invalid AGENT_RUNTIME_PROFILE"):
        resolve_runtime_profile()


def test_profile_inference_order(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    monkeypatch.delenv("AGENT_RUNTIME_PROFILE", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AGENT_HOSTED", "true")
    monkeypatch.setenv("CI", "true")

    assert resolve_runtime_profile() == "prod"


def test_hosted_inference(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    monkeypatch.delenv("AGENT_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.setenv("AGENT_HOSTED", "true")

    assert resolve_runtime_profile() == "hosted"


def test_ci_inference(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    monkeypatch.delenv("AGENT_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("AGENT_HOSTED", raising=False)
    monkeypatch.setenv("CI", "true")

    assert resolve_runtime_profile() == "test"


def test_fallback_profile_is_dev(monkeypatch):
    from agent_core.permissions.profiles import resolve_runtime_profile

    for key in (
        "AGENT_RUNTIME_PROFILE",
        "ENVIRONMENT",
        "APP_ENV",
        "NODE_ENV",
        "AGENT_HOSTED",
        "LANGGRAPH_DEPLOYMENT_ID",
        "CI",
        "GITHUB_ACTIONS",
    ):
        monkeypatch.delenv(key, raising=False)

    assert resolve_runtime_profile() == "dev"


def test_default_terminal_env_by_profile(monkeypatch):
    from agent_core.permissions.profiles import default_terminal_env

    monkeypatch.delenv("TERMINAL_ENV", raising=False)

    assert default_terminal_env("dev") == "local"
    assert default_terminal_env("test") == "local"
    assert default_terminal_env("hosted") == "docker"
    assert default_terminal_env("prod") == "docker"


def test_explicit_terminal_env_wins(monkeypatch):
    from agent_core.permissions.profiles import resolve_terminal_env

    monkeypatch.setenv("TERMINAL_ENV", "ssh")

    assert resolve_terminal_env("prod") == "ssh"
```

- [ ] **Step 2: Write failing approval registry tests**

Add `tests/test_permissions_approvals.py`:

```python
def test_approval_is_single_use_and_argument_bound():
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        consume_approval,
        make_args_digest,
        record_approval,
    )

    record = ApprovalRecord(
        approval_id="approval-1",
        decision_id="decision-1",
        task_id="task-1",
        tool_call_id="call-1",
        tool_name="terminal",
        args_digest=make_args_digest({"command": "pip install rich"}),
        risk_tags=("package_install",),
        allow_network_once=True,
    )
    record_approval(record)

    consumed = consume_approval(
        task_id="task-1",
        tool_call_id="call-1",
        tool_name="terminal",
        args={"command": "pip install rich"},
        required_risk_tags=("package_install",),
    )

    assert consumed is not None
    assert consumed.allow_network_once is True
    assert consume_approval(
        task_id="task-1",
        tool_call_id="call-1",
        tool_name="terminal",
        args={"command": "pip install rich"},
        required_risk_tags=("package_install",),
    ) is None


def test_approval_rejects_changed_arguments():
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        consume_approval,
        make_args_digest,
        record_approval,
    )

    record_approval(
        ApprovalRecord(
            approval_id="approval-2",
            decision_id="decision-2",
            task_id="task-1",
            tool_call_id="call-2",
            tool_name="write_file",
            args_digest=make_args_digest({"path": "/tmp/a.txt", "content": "a"}),
            risk_tags=("writes_outside_workspace",),
            allow_network_once=False,
        )
    )

    assert consume_approval(
        task_id="task-1",
        tool_call_id="call-2",
        tool_name="write_file",
        args={"path": "/tmp/a.txt", "content": "b"},
        required_risk_tags=("writes_outside_workspace",),
    ) is None


def test_approval_rejects_missing_risk_tag():
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        consume_approval,
        make_args_digest,
        record_approval,
    )

    record_approval(
        ApprovalRecord(
            approval_id="approval-3",
            decision_id="decision-3",
            task_id="task-1",
            tool_call_id="call-3",
            tool_name="terminal",
            args_digest=make_args_digest({"command": "curl https://example.com"}),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )

    assert consume_approval(
        task_id="task-1",
        tool_call_id="call-3",
        tool_name="terminal",
        args={"command": "curl https://example.com"},
        required_risk_tags=("package_install",),
    ) is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_profiles.py tests/test_permissions_approvals.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core.permissions'`.

- [ ] **Step 4: Implement models, profiles, audit, and approvals**

Add `agent_core/permissions/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


PolicyOutcome = Literal["allow", "review", "deny"]
RuntimeProfile = Literal["dev", "test", "hosted", "prod"]


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason: str
    risk_tags: tuple[str, ...] = ()
    message: str = ""
    requires_network: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, reason: str = "allowed", **kwargs: Any) -> "PolicyDecision":
        return cls(outcome="allow", reason=reason, **kwargs)

    @classmethod
    def review(
        cls,
        reason: str,
        *,
        risk_tags: tuple[str, ...],
        requires_network: bool = False,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> "PolicyDecision":
        return cls(
            outcome="review",
            reason=reason,
            risk_tags=risk_tags,
            requires_network=requires_network,
            message=message,
            data=data or {},
        )

    @classmethod
    def deny(
        cls,
        reason: str,
        *,
        risk_tags: tuple[str, ...] = (),
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> "PolicyDecision":
        return cls(
            outcome="deny",
            reason=reason,
            risk_tags=risk_tags,
            message=message,
            data=data or {},
        )

    @property
    def human_message(self) -> str:
        return self.message or self.reason.replace("_", " ")
```

Add `agent_core/permissions/profiles.py`:

```python
from __future__ import annotations

import os

from agent_core.permissions.models import RuntimeProfile


VALID_PROFILES: set[str] = {"dev", "test", "hosted", "prod"}


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_runtime_profile() -> RuntimeProfile:
    explicit = os.getenv("AGENT_RUNTIME_PROFILE", "").strip().lower()
    if explicit:
        if explicit not in VALID_PROFILES:
            raise ValueError(
                "Invalid AGENT_RUNTIME_PROFILE "
                f"{explicit!r}. Expected one of: dev, test, hosted, prod."
            )
        return explicit  # type: ignore[return-value]

    production_values = {
        os.getenv("ENVIRONMENT", "").strip().lower(),
        os.getenv("APP_ENV", "").strip().lower(),
        os.getenv("NODE_ENV", "").strip().lower(),
    }
    if production_values & {"prod", "production"}:
        return "prod"

    if _truthy_env("AGENT_HOSTED") or os.getenv("LANGGRAPH_DEPLOYMENT_ID"):
        return "hosted"

    if _truthy_env("CI") or _truthy_env("GITHUB_ACTIONS"):
        return "test"

    return "dev"


def default_terminal_env(profile: RuntimeProfile | str) -> str:
    return "docker" if profile in {"hosted", "prod"} else "local"


def resolve_terminal_env(profile: RuntimeProfile | str | None = None) -> str:
    explicit = os.getenv("TERMINAL_ENV", "").strip().lower()
    if explicit:
        return explicit
    return default_terminal_env(profile or resolve_runtime_profile())


def profile_enforces_docker_network(profile: RuntimeProfile | str | None = None) -> bool:
    return (profile or resolve_runtime_profile()) in {"hosted", "prod"}
```

Add `agent_core/permissions/approvals.py`:

```python
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    decision_id: str
    task_id: str
    tool_call_id: str
    tool_name: str
    args_digest: str
    risk_tags: tuple[str, ...]
    allow_network_once: bool = False


_lock = threading.Lock()
_approvals: dict[tuple[str, str], ApprovalRecord] = {}


def make_args_digest(args: dict[str, Any]) -> str:
    payload = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_approval(record: ApprovalRecord) -> None:
    with _lock:
        _approvals[(record.task_id, record.tool_call_id)] = record


def consume_approval(
    *,
    task_id: str,
    tool_call_id: str | None,
    tool_name: str,
    args: dict[str, Any],
    required_risk_tags: tuple[str, ...],
) -> ApprovalRecord | None:
    if not tool_call_id:
        return None
    key = (task_id, tool_call_id)
    with _lock:
        record = _approvals.get(key)
        if record is None:
            return None
        if record.tool_name != tool_name:
            return None
        if record.args_digest != make_args_digest(args):
            return None
        if not set(required_risk_tags).issubset(set(record.risk_tags)):
            return None
        return _approvals.pop(key)


def clear_approvals() -> None:
    with _lock:
        _approvals.clear()
```

Add `agent_core/permissions/audit.py`:

```python
from __future__ import annotations

import logging
from typing import Any

from agent_core.permissions.models import PolicyDecision


logger = logging.getLogger(__name__)


def audit_policy_event(
    *,
    profile: str,
    tool_name: str,
    task_id: str,
    decision: PolicyDecision,
    preview: str = "",
    approved_by_human: bool = False,
    network_once: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    logger.info(
        "policy_event profile=%s tool=%s task=%s outcome=%s reason=%s risks=%s "
        "approved=%s network_once=%s preview=%r extra=%s",
        profile,
        tool_name,
        task_id,
        decision.outcome,
        decision.reason,
        ",".join(decision.risk_tags),
        approved_by_human,
        network_once,
        preview[:200],
        extra or {},
    )
```

Add `agent_core/permissions/__init__.py`:

```python
from agent_core.permissions.models import PolicyDecision, PolicyOutcome, RuntimeProfile
from agent_core.permissions.profiles import (
    default_terminal_env,
    profile_enforces_docker_network,
    resolve_runtime_profile,
    resolve_terminal_env,
)

__all__ = [
    "PolicyDecision",
    "PolicyOutcome",
    "RuntimeProfile",
    "default_terminal_env",
    "profile_enforces_docker_network",
    "resolve_runtime_profile",
    "resolve_terminal_env",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_profiles.py tests/test_permissions_approvals.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_core/permissions tests/test_permissions_profiles.py tests/test_permissions_approvals.py
git commit -m "feat: add permission policy primitives"
```

---

### Task 2: Shell Command Policy

**Files:**
- Create: `agent_core/permissions/command_policy.py`
- Test: `tests/test_permissions_command_policy.py`

- [ ] **Step 1: Write failing command policy tests**

Add `tests/test_permissions_command_policy.py`:

```python
import pytest


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "ls -la",
        "find . -maxdepth 2 -type f",
        "rg sandbox .",
        "grep -R policy agent_core",
        "cat README.md",
        "sed -n '1,20p' README.md",
        "git status --short",
        "git diff",
        "git log --oneline -n 3",
        "git show HEAD --stat",
        "pytest tests/test_terminal_tools.py -q",
        "python -m pytest tests/test_terminal_tools.py -q",
        "npm test",
        "python --version",
        "command -v docker",
    ],
)
def test_low_risk_commands_are_allowed(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "allow"


@pytest.mark.parametrize(
    ("command", "risk"),
    [
        ("echo hi > out.txt", "write_redirect"),
        ("printf hi | tee out.txt", "write_redirect"),
        ("cp a b", "filesystem_write"),
        ("mv a b", "filesystem_write"),
        ("mkdir -p build", "filesystem_write"),
        ("touch notes.txt", "filesystem_write"),
        ("rm -rf node_modules", "destructive_command"),
        ("chmod -R 777 .", "permission_change"),
        ("chown -R root .", "permission_change"),
        ("pip install rich", "package_install"),
        ("npm install", "package_install"),
        ("apt install jq", "package_install"),
        ("curl https://example.com", "network_access"),
        ("wget https://example.com/file", "network_access"),
        ("git pull", "network_access"),
        ("ssh host.example.com", "network_access"),
        ("sudo true", "privilege_escalation"),
        ("systemctl restart sshd", "service_control"),
        ("npm run dev", "long_running_process"),
    ],
)
def test_risky_commands_require_review(command, risk):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "review"
    assert risk in decision.risk_tags


def test_background_commands_require_review():
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command("python -m http.server", background=True)

    assert decision.outcome == "review"
    assert "long_running_process" in decision.risk_tags


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "mkfs.ext4 /dev/sda",
        ":(){ :|:& };:",
        "shutdown now",
    ],
)
def test_hardline_commands_are_denied(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "deny"
    assert "hardline_destructive_command" in decision.risk_tags


def test_complex_unknown_shell_requires_review():
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command("for f in *; do echo $f; done", background=False)

    assert decision.outcome == "review"
    assert "complex_shell" in decision.risk_tags
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_command_policy.py -q
```

Expected: FAIL with `ModuleNotFoundError` for `agent_core.permissions.command_policy`.

- [ ] **Step 3: Implement command policy**

Add `agent_core/permissions/command_policy.py`:

```python
from __future__ import annotations

import re
import shlex

from agent_core.permissions.models import PolicyDecision
from agent_tools.hermes_terminal_toolkit.approval import check_all_command_guards


_READ_ONLY_COMMANDS = {
    "pwd",
    "ls",
    "find",
    "rg",
    "grep",
    "cat",
    "head",
    "tail",
    "wc",
    "which",
    "command",
}
_READ_ONLY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "branch"}
_TEST_COMMANDS = {
    ("pytest",),
    ("python", "-m", "pytest"),
    ("npm", "test"),
    ("pnpm", "test"),
    ("yarn", "test"),
    ("cargo", "test"),
    ("go", "test"),
}
_INFO_FLAGS = {"--version", "-V", "version"}

_WRITE_COMMANDS = {"cp", "mv", "mkdir", "touch"}
_DELETE_COMMANDS = {"rm"}
_PERMISSION_COMMANDS = {"chmod", "chown"}
_PACKAGE_INSTALL_PATTERNS = (
    ("pip", "install"),
    ("python", "-m", "pip", "install"),
    ("npm", "install"),
    ("pnpm", "install"),
    ("yarn", "add"),
    ("apt", "install"),
    ("apt-get", "install"),
    ("brew", "install"),
)
_NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp"}
_NETWORK_GIT_SUBCOMMANDS = {"clone", "fetch", "pull", "push"}
_SERVICE_COMMANDS = {"systemctl", "service"}
_LONG_RUNNING_TOKENS = {"vite", "uvicorn", "watch"}


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _contains_complex_shell(command: str) -> bool:
    return bool(re.search(r"(\$\(|`|<<|;|\|\||&&|\n|\bfor\b|\bwhile\b)", command))


def _contains_write_redirect(command: str) -> bool:
    return bool(re.search(r"(^|[^<])>{1,2}($|[^>])", command)) or bool(re.search(r"\btee\b", command))


def _starts_with(tokens: list[str], prefix: tuple[str, ...]) -> bool:
    return tuple(tokens[: len(prefix)]) == prefix


def _is_test_command(tokens: list[str]) -> bool:
    return any(_starts_with(tokens, prefix) for prefix in _TEST_COMMANDS)


def _is_read_only(tokens: list[str]) -> bool:
    if not tokens:
        return False
    base = tokens[0]
    if base == "sed":
        return len(tokens) > 1 and tokens[1] == "-n"
    if base == "git":
        return len(tokens) > 1 and tokens[1] in _READ_ONLY_GIT_SUBCOMMANDS
    if base in _READ_ONLY_COMMANDS:
        return True
    if len(tokens) >= 2 and tokens[1] in _INFO_FLAGS:
        return True
    return False


def classify_command(command: str, *, background: bool = False) -> PolicyDecision:
    guard = check_all_command_guards(command, env_type="local")
    if guard.get("hardline"):
        return PolicyDecision.deny(
            "hardline_destructive_command",
            risk_tags=("hardline_destructive_command",),
            message=guard.get("message", "Command is unconditionally blocked."),
        )

    risk_tags: list[str] = []
    tokens = _tokens(command)
    base = tokens[0] if tokens else ""

    if background:
        risk_tags.append("long_running_process")
    if _contains_write_redirect(command):
        risk_tags.append("write_redirect")
    if base in _WRITE_COMMANDS:
        risk_tags.append("filesystem_write")
    if base in _DELETE_COMMANDS:
        risk_tags.append("destructive_command")
    if base in _PERMISSION_COMMANDS:
        risk_tags.append("permission_change")
    if any(_starts_with(tokens, prefix) for prefix in _PACKAGE_INSTALL_PATTERNS):
        risk_tags.append("package_install")
    if base in _NETWORK_COMMANDS or (base == "git" and len(tokens) > 1 and tokens[1] in _NETWORK_GIT_SUBCOMMANDS):
        risk_tags.append("network_access")
    if base == "sudo":
        risk_tags.append("privilege_escalation")
    if base in _SERVICE_COMMANDS:
        risk_tags.append("service_control")
    if base in _LONG_RUNNING_TOKENS or tuple(tokens[:3]) == ("npm", "run", "dev"):
        risk_tags.append("long_running_process")
    if not risk_tags and _contains_complex_shell(command):
        risk_tags.append("complex_shell")
    if not risk_tags and not (_is_read_only(tokens) or _is_test_command(tokens)):
        risk_tags.append("unknown_shell")

    if risk_tags:
        unique_risks = tuple(dict.fromkeys(risk_tags))
        return PolicyDecision.review(
            unique_risks[0],
            risk_tags=unique_risks,
            requires_network="network_access" in unique_risks,
        )

    return PolicyDecision.allow("low_risk_command")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_command_policy.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/permissions/command_policy.py tests/test_permissions_command_policy.py
git commit -m "feat: classify terminal command risk"
```

---

### Task 3: File Policy

**Files:**
- Create: `agent_core/permissions/file_policy.py`
- Test: `tests/test_permissions_file_policy.py`

- [ ] **Step 1: Write failing file policy tests**

Add `tests/test_permissions_file_policy.py`:

```python
from pathlib import Path


def test_workspace_write_is_allowed(monkeypatch, tmp_path):
    import agent_core.permissions.file_policy as policy

    monkeypatch.setattr(policy, "WORKDIR", tmp_path)
    decision = policy.classify_file_write("notes.txt", task_id="task-local")

    assert decision.outcome == "allow"


def test_ordinary_workspace_escape_requires_review(monkeypatch, tmp_path):
    import agent_core.permissions.file_policy as policy

    monkeypatch.setattr(policy, "WORKDIR", tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()

    decision = policy.classify_file_write(str(tmp_path / "report.txt"), task_id="task-local")

    assert decision.outcome == "review"
    assert "writes_outside_workspace" in decision.risk_tags


def test_sensitive_host_path_is_denied(monkeypatch, tmp_path):
    import agent_core.permissions.file_policy as policy

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(policy, "WORKDIR", workspace)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    decision = policy.classify_file_write(str(home / ".ssh" / "id_rsa"), task_id="task-local")

    assert decision.outcome == "deny"
    assert "sensitive_path" in decision.risk_tags


def test_sensitive_container_path_is_denied():
    from agent_core.permissions.file_policy import classify_file_write

    decision = classify_file_write("/root/.aws/credentials", task_id="task-docker")

    assert decision.outcome == "deny"
    assert "sensitive_path" in decision.risk_tags


def test_docker_workspace_path_is_allowed(monkeypatch):
    import agent_core.permissions.file_policy as policy

    monkeypatch.setattr(
        policy,
        "allowed_workspace_roots_for_task",
        lambda task_id: ["/workspace"],
    )
    monkeypatch.setattr(
        policy,
        "resolve_path_for_policy",
        lambda path, task_id: path,
    )

    decision = policy.classify_file_write("/workspace/app.py", task_id="task-docker")

    assert decision.outcome == "allow"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_file_policy.py -q
```

Expected: FAIL with `ModuleNotFoundError` for `agent_core.permissions.file_policy`.

- [ ] **Step 3: Implement file policy**

Add `agent_core/permissions/file_policy.py`:

```python
from __future__ import annotations

import os
import posixpath
from pathlib import Path

from agent_core.permissions.models import PolicyDecision
from agent_core.workspace import WORKDIR
from agent_tools.file_toolkit.backend_paths import (
    allowed_workspace_roots_for_task,
    path_is_under_any_root,
    resolve_path_for_policy,
)


_HOST_SENSITIVE_NAMES = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".azure",
)
_HOST_SENSITIVE_FILES = (
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
)
_CONTAINER_SENSITIVE_PREFIXES = (
    "/root/.ssh/",
    "/root/.aws/",
    "/root/.gnupg/",
    "/root/.kube/",
    "/root/.docker/",
    "/root/.azure/",
    "/etc/",
)
_SENSITIVE_EXACT = {
    "/etc",
    "/var/run/docker.sock",
    "/run/docker.sock",
}


def _norm(path: str) -> str:
    return posixpath.normpath(str(path))


def is_sensitive_path(path: str) -> bool:
    raw = str(path)
    expanded = os.path.expanduser(raw)
    try:
        host_path = Path(expanded).resolve()
        home = Path.home().resolve()
        for name in _HOST_SENSITIVE_NAMES:
            sensitive_dir = home / name
            if host_path == sensitive_dir or sensitive_dir in host_path.parents:
                return True
        for filename in _HOST_SENSITIVE_FILES:
            if host_path == home / filename:
                return True
    except Exception:
        pass

    normalized = _norm(raw)
    if normalized in _SENSITIVE_EXACT:
        return True
    if normalized.startswith("/etc/"):
        return True
    return any(normalized.startswith(prefix) for prefix in _CONTAINER_SENSITIVE_PREFIXES)


def classify_file_write(path: str, *, task_id: str) -> PolicyDecision:
    if is_sensitive_path(path):
        return PolicyDecision.deny(
            "sensitive_path",
            risk_tags=("sensitive_path",),
            message=f"Write denied for sensitive path: {path}",
            data={"path": path},
        )

    try:
        resolved = str(resolve_path_for_policy(path, task_id))
    except Exception as exc:
        return PolicyDecision.review(
            "path_resolution_failed",
            risk_tags=("path_resolution_failed",),
            message=f"Path requires review because it could not be resolved: {exc}",
            data={"path": path},
        )

    roots = allowed_workspace_roots_for_task(task_id)
    if path_is_under_any_root(resolved, roots):
        return PolicyDecision.allow("workspace_write", data={"path": path, "resolved_path": resolved})

    return PolicyDecision.review(
        "writes_outside_workspace",
        risk_tags=("writes_outside_workspace",),
        message=f"Write requires approval because it targets outside the workspace: {path}",
        data={"path": path, "resolved_path": resolved},
    )


def approved_write_root_for_path(path: str, *, task_id: str) -> str:
    resolved = str(resolve_path_for_policy(path, task_id))
    parent = posixpath.dirname(_norm(resolved))
    return parent or "/"
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_file_policy.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/permissions/file_policy.py tests/test_permissions_file_policy.py
git commit -m "feat: classify file write permissions"
```

---

### Task 4: Top-Level Tool Policy

**Files:**
- Create: `agent_core/permissions/tool_policy.py`
- Test: `tests/test_permissions_tool_policy.py`

- [ ] **Step 1: Write failing top-level policy tests**

Add `tests/test_permissions_tool_policy.py`:

```python
def test_read_tools_are_allowed():
    from agent_core.permissions.tool_policy import evaluate_tool_call

    for tool_name in ("read_file", "search_files", "list_directory", "file_info"):
        decision = evaluate_tool_call(
            tool_name=tool_name,
            args={"path": "."},
            task_id="task-1",
            tool_call_id="call-1",
        )
        assert decision.outcome == "allow"


def test_workspace_write_is_allowed(monkeypatch):
    import agent_core.permissions.tool_policy as tool_policy

    monkeypatch.setattr(
        tool_policy.file_policy,
        "classify_file_write",
        lambda path, task_id: tool_policy.PolicyDecision.allow("workspace_write"),
    )

    decision = tool_policy.evaluate_tool_call(
        tool_name="write_file",
        args={"path": "notes.txt", "content": "hello"},
        task_id="task-1",
        tool_call_id="call-1",
    )

    assert decision.outcome == "allow"


def test_patch_uses_all_patch_paths(monkeypatch):
    import agent_core.permissions.tool_policy as tool_policy

    seen = []

    def fake_classify(path, task_id):
        seen.append(path)
        return tool_policy.PolicyDecision.allow("workspace_write")

    monkeypatch.setattr(tool_policy.file_policy, "classify_file_write", fake_classify)

    decision = tool_policy.evaluate_tool_call(
        tool_name="patch",
        args={
            "mode": "patch",
            "patch": "*** Begin Patch\n*** Add File: a.txt\n+hello\n*** End Patch\n",
        },
        task_id="task-1",
        tool_call_id="call-1",
    )

    assert decision.outcome == "allow"
    assert seen == ["a.txt"]


def test_terminal_uses_command_policy(monkeypatch):
    import agent_core.permissions.tool_policy as tool_policy

    monkeypatch.setattr(
        tool_policy.command_policy,
        "classify_command",
        lambda command, background=False: tool_policy.PolicyDecision.review(
            "package_install",
            risk_tags=("package_install",),
        ),
    )

    decision = tool_policy.evaluate_tool_call(
        tool_name="terminal",
        args={"command": "pip install rich"},
        task_id="task-1",
        tool_call_id="call-1",
    )

    assert decision.outcome == "review"
    assert decision.risk_tags == ("package_install",)


def test_process_write_requires_review():
    from agent_core.permissions.tool_policy import evaluate_tool_call

    decision = evaluate_tool_call(
        tool_name="process",
        args={"action": "submit", "session_id": "proc_1", "data": "rm -rf tmp"},
        task_id="task-1",
        tool_call_id="call-1",
    )

    assert decision.outcome == "review"
    assert "process_stdin" in decision.risk_tags
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_tool_policy.py -q
```

Expected: FAIL with `ModuleNotFoundError` for `agent_core.permissions.tool_policy`.

- [ ] **Step 3: Implement top-level tool policy**

Add `agent_core/permissions/tool_policy.py`:

```python
from __future__ import annotations

from agent_core.permissions import command_policy, file_policy
from agent_core.permissions.models import PolicyDecision
from agent_tools.file_toolkit.patch_parser import parse_v4a_patch


_READ_TOOLS = {"read_file", "search_files", "list_directory", "file_info"}
_MANDATORY_REVIEW_TOOLS = {"memory_manage", "skill_manage"}
_PROCESS_STDIN_ACTIONS = {"write", "submit"}


def _patch_paths(args: dict) -> list[str]:
    mode = args.get("mode", "replace")
    if mode == "replace":
        path = args.get("path")
        return [path] if path else []

    patch_content = args.get("patch") or ""
    operations, parse_error = parse_v4a_patch(patch_content)
    if parse_error:
        return []

    paths: list[str] = []
    for operation in operations:
        if operation.file_path:
            paths.append(operation.file_path)
        if operation.new_path:
            paths.append(operation.new_path)
    return paths


def _combine_write_decisions(decisions: list[PolicyDecision]) -> PolicyDecision:
    for decision in decisions:
        if decision.outcome == "deny":
            return decision
    review_decisions = [decision for decision in decisions if decision.outcome == "review"]
    if review_decisions:
        risk_tags = tuple(dict.fromkeys(tag for decision in review_decisions for tag in decision.risk_tags))
        return PolicyDecision.review(
            review_decisions[0].reason,
            risk_tags=risk_tags,
            message=review_decisions[0].human_message,
            data={"decisions": [decision.data for decision in review_decisions]},
        )
    return PolicyDecision.allow("workspace_write")


def evaluate_tool_call(
    *,
    tool_name: str,
    args: dict,
    task_id: str,
    tool_call_id: str | None = None,
) -> PolicyDecision:
    if tool_name in _READ_TOOLS:
        return PolicyDecision.allow("read_tool")
    if tool_name in _MANDATORY_REVIEW_TOOLS:
        return PolicyDecision.review(
            "mandatory_review",
            risk_tags=("mandatory_review",),
            message=f"{tool_name} requires human review.",
        )
    if tool_name == "write_file":
        return file_policy.classify_file_write(str(args.get("path") or ""), task_id=task_id)
    if tool_name == "patch":
        paths = _patch_paths(args)
        if not paths:
            return PolicyDecision.review(
                "patch_paths_unresolved",
                risk_tags=("path_resolution_failed",),
                message="Patch paths could not be resolved and require review.",
            )
        return _combine_write_decisions(
            [file_policy.classify_file_write(path, task_id=task_id) for path in paths]
        )
    if tool_name == "terminal":
        return command_policy.classify_command(
            str(args.get("command") or ""),
            background=bool(args.get("background", False)),
        )
    if tool_name == "process":
        action = str(args.get("action") or "")
        if action in _PROCESS_STDIN_ACTIONS:
            return PolicyDecision.review(
                "process_stdin",
                risk_tags=("process_stdin",),
                message="Writing to a background process stdin requires review.",
            )
        return PolicyDecision.allow("process_control")
    return PolicyDecision.allow("unmanaged_tool")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_tool_policy.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/permissions/tool_policy.py tests/test_permissions_tool_policy.py
git commit -m "feat: evaluate tool calls with unified policy"
```

---

### Task 5: Profile-Aware Terminal Defaults

**Files:**
- Modify: `agent_tools/hermes_terminal_toolkit/terminal_tool.py`
- Modify: `tests/test_hermes_active_env.py`

- [ ] **Step 1: Add failing terminal env config tests**

Append to `tests/test_hermes_active_env.py`:

```python
def test_get_env_config_uses_profile_default_for_hosted(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "hosted")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["container_network"] is False


def test_get_env_config_respects_explicit_terminal_env(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"


def test_get_env_config_dev_keeps_network_default(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "dev")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"
    assert config["container_network"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_hermes_active_env.py::test_get_env_config_uses_profile_default_for_hosted tests/test_hermes_active_env.py::test_get_env_config_respects_explicit_terminal_env tests/test_hermes_active_env.py::test_get_env_config_dev_keeps_network_default -q
```

Expected: FAIL because `_get_env_config()` still defaults to `local` and has no `container_network`.

- [ ] **Step 3: Update terminal env config**

In `agent_tools/hermes_terminal_toolkit/terminal_tool.py`, import profile helpers:

```python
from agent_core.permissions.profiles import (
    profile_enforces_docker_network,
    resolve_runtime_profile,
    resolve_terminal_env,
)
```

Replace the first lines of `_get_env_config()` with:

```python
def _get_env_config() -> Dict[str, Any]:
    default_image = "nikolaik/python-nodejs:python3.11-nodejs20"
    profile = resolve_runtime_profile()
    env_type = resolve_terminal_env(profile)
    mount_docker_cwd = os.getenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "false").lower() in ("true", "1", "yes")
    explicit_network = os.getenv("TERMINAL_CONTAINER_NETWORK")
    if explicit_network is None:
        container_network = not profile_enforces_docker_network(profile)
    else:
        container_network = explicit_network.lower() in ("true", "1", "yes")
```

Add these keys to the returned dict:

```python
        "runtime_profile": profile,
        "container_network": container_network,
```

In `_container_config_from_terminal_config()`, add:

```python
        "container_network": config.get("container_network", True),
```

In `_create_environment()`, add:

```python
    network = cc.get("container_network", True)
```

Pass `network=network` into `DockerEnvironment(...)`.

- [ ] **Step 4: Run targeted tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_hermes_active_env.py::test_get_env_config_uses_profile_default_for_hosted tests/test_hermes_active_env.py::test_get_env_config_respects_explicit_terminal_env tests/test_hermes_active_env.py::test_get_env_config_dev_keeps_network_default tests/test_hermes_active_env.py::test_get_or_create_active_env_builds_docker_config -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/hermes_terminal_toolkit/terminal_tool.py tests/test_hermes_active_env.py
git commit -m "feat: derive terminal backend from runtime profile"
```

---

### Task 6: Policy-Driven Human-in-the-Loop Middleware

**Files:**
- Modify: `agent_core/human_loop.py`
- Modify: `agent_core/builders.py`
- Test: `tests/test_permissions_human_loop.py`

- [ ] **Step 1: Write failing middleware tests**

Add `tests/test_permissions_human_loop.py`:

```python
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage


def _runtime():
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-1"),
        config={"configurable": {"thread_id": "thread-1"}},
    )


def test_policy_allow_does_not_interrupt(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.models import PolicyDecision

    called = False

    def fail_interrupt(payload):
        nonlocal called
        called = True
        raise AssertionError("allow decisions should not interrupt")

    monkeypatch.setattr(human_loop, "interrupt", fail_interrupt)
    monkeypatch.setattr(
        human_loop.tool_policy,
        "evaluate_tool_call",
        lambda **kwargs: PolicyDecision.allow("read_tool"),
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": "terminal", "args": {"command": "pwd"}, "id": "call-1"}],
            )
        ]
    }

    assert middleware.after_model(state, _runtime()) is None
    assert called is False


def test_policy_deny_synthesizes_tool_message(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.models import PolicyDecision

    monkeypatch.setattr(
        human_loop.tool_policy,
        "evaluate_tool_call",
        lambda **kwargs: PolicyDecision.deny(
            "hardline_destructive_command",
            risk_tags=("hardline_destructive_command",),
            message="Blocked hardline command.",
        ),
    )

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": "terminal", "args": {"command": "rm -rf /"}, "id": "call-1"}],
            )
        ]
    }

    result = middleware.after_model(state, _runtime())

    assert result is not None
    messages = result["messages"]
    assert len(messages[0].tool_calls) == 0
    tool_message = messages[1]
    assert isinstance(tool_message, ToolMessage)
    payload = json.loads(tool_message.content)
    assert payload["error"]["code"] == "policy_denied"
    assert payload["message"] == "Blocked hardline command."


def test_policy_review_records_approval(monkeypatch):
    import agent_core.human_loop as human_loop
    from agent_core.permissions.approvals import clear_approvals, consume_approval
    from agent_core.permissions.models import PolicyDecision

    clear_approvals()
    monkeypatch.setattr(
        human_loop.tool_policy,
        "evaluate_tool_call",
        lambda **kwargs: PolicyDecision.review(
            "package_install",
            risk_tags=("package_install",),
            requires_network=True,
            message="Package install requires review.",
        ),
    )
    monkeypatch.setattr(human_loop, "interrupt", lambda payload: {"type": "approve"})

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={},
        policy_tools={"terminal"},
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "terminal", "args": {"command": "pip install rich"}, "id": "call-1"}
                ],
            )
        ]
    }

    result = middleware.after_model(state, _runtime())

    assert result is not None
    assert result["messages"][0].tool_calls[0]["id"] == "call-1"
    approval = consume_approval(
        task_id="lg_97d5b8029b6b63130cb1a6e4",
        tool_call_id="call-1",
        tool_name="terminal",
        args={"command": "pip install rich"},
        required_risk_tags=("package_install",),
    )
    assert approval is not None
    assert approval.allow_network_once is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_human_loop.py -q
```

Expected: FAIL because the middleware has no `policy_tools` behavior.

- [ ] **Step 3: Implement policy-aware middleware behavior**

In `agent_core/human_loop.py`, add imports:

```python
import uuid

from agent_core.permissions import tool_policy
from agent_core.permissions.approvals import ApprovalRecord, make_args_digest, record_approval
from agent_core.permissions.audit import audit_policy_event
from agent_core.session_context import hermes_task_id_from_runtime
from agent_tools.shared.tool_output import tool_error
```

Add an `__init__` method to `FlexibleHumanInTheLoopMiddleware`:

```python
    def __init__(self, *args: Any, policy_tools: set[str] | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.policy_tools = policy_tools or set()
```

Add helper methods:

```python
    def _policy_decision_for_tool_call(self, tool_call: ToolCall, runtime: Runtime[Any]):
        task_id = hermes_task_id_from_runtime(runtime)
        return tool_policy.evaluate_tool_call(
            tool_name=tool_call["name"],
            args=tool_call.get("args") or {},
            task_id=task_id,
            tool_call_id=tool_call.get("id"),
        )

    def _tool_message_for_denial(self, tool_call: ToolCall, message: str) -> ToolMessage:
        return ToolMessage(
            content=tool_error(
                tool_call["name"],
                message,
                code="policy_denied",
            ),
            name=tool_call["name"],
            tool_call_id=tool_call["id"],
            status="error",
        )
```

In `after_model`, replace the loop that appends every interrupt config with policy-aware logic:

```python
        denied_messages: list[ToolMessage] = []
        policy_decisions: dict[int, Any] = {}

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if tool_call["name"] in self.policy_tools:
                decision = self._policy_decision_for_tool_call(tool_call, runtime)
                policy_decisions[idx] = decision
                task_id = hermes_task_id_from_runtime(runtime)
                audit_policy_event(
                    profile="runtime",
                    tool_name=tool_call["name"],
                    task_id=task_id,
                    decision=decision,
                    preview=str((tool_call.get("args") or {}).get("command") or (tool_call.get("args") or {}).get("path") or ""),
                )
                if decision.outcome == "allow":
                    continue
                if decision.outcome == "deny":
                    denied_messages.append(self._tool_message_for_denial(tool_call, decision.human_message))
                    interrupt_configs[idx] = None
                    continue
                config = {
                    "allowed_decisions": ["approve", "edit", "reject", "respond"],
                    "description": decision.human_message,
                }
            else:
                config = self._resolve_interrupt_config(tool_call, self.interrupt_on)

            if config is not None:
                action_request, review_config = self._create_action_and_config(
                    tool_call, config, state, runtime
                )
                action_requests.append(action_request)
                review_configs.append(review_config)
                interrupt_indices.append(idx)
                interrupt_configs[idx] = config
```

Before returning `None` when no action requests exist, return denial messages if present:

```python
        if not action_requests:
            if denied_messages:
                last_ai_msg.tool_calls = [
                    tool_call
                    for idx, tool_call in enumerate(last_ai_msg.tool_calls)
                    if idx not in interrupt_configs
                ]
                return {"messages": [last_ai_msg, *denied_messages]}
            return None
```

After `_process_decision(...)`, when an approval occurred for a policy-reviewed tool, record approval:

```python
                if (
                    decision.get("type") == "approve"
                    and idx in policy_decisions
                    and revised_tool_call is not None
                ):
                    policy_decision = policy_decisions[idx]
                    task_id = hermes_task_id_from_runtime(runtime)
                    record_approval(
                        ApprovalRecord(
                            approval_id=f"approval_{uuid.uuid4().hex}",
                            decision_id=f"decision_{uuid.uuid4().hex}",
                            task_id=task_id,
                            tool_call_id=revised_tool_call["id"],
                            tool_name=revised_tool_call["name"],
                            args_digest=make_args_digest(revised_tool_call.get("args") or {}),
                            risk_tags=policy_decision.risk_tags,
                            allow_network_once=policy_decision.requires_network,
                        )
                    )
```

- [ ] **Step 4: Wire builders to use policy tools**

In `agent_core/builders.py`, remove `terminal`, `process`, `write_file`, and `patch` from `HUMAN_INTERRUPT_ON`. Keep `memory_manage` and `skill_manage`.

Define:

```python
POLICY_REVIEW_TOOLS = {"terminal", "process", "write_file", "patch"}
```

Pass it to middleware:

```python
            FlexibleHumanInTheLoopMiddleware(
                interrupt_on=HUMAN_INTERRUPT_ON,
                policy_tools=POLICY_REVIEW_TOOLS,
                description_prefix="Approval required before tool execution",
            ),
```

- [ ] **Step 5: Run middleware tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_human_loop.py -q
```

Expected: PASS.

- [ ] **Step 6: Run existing builder/HITL-adjacent tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_agent_tools_public_imports.py tests/test_terminal_tools.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_core/human_loop.py agent_core/builders.py tests/test_permissions_human_loop.py
git commit -m "feat: route tool review through policy engine"
```

---

### Task 7: File Wrapper Enforcement and Approved Workspace Escapes

**Files:**
- Modify: `agent_tools/public/files.py`
- Modify: `agent_tools/file_toolkit/file_tools.py`
- Modify: `agent_tools/file_toolkit/file_operations.py`
- Test: `tests/test_permissions_file_wrappers.py`

- [ ] **Step 1: Write failing file wrapper tests**

Add `tests/test_permissions_file_wrappers.py`:

```python
import json
from types import SimpleNamespace


def _runtime(tool_call_id="call-1"):
    return SimpleNamespace(
        tool_call_id=tool_call_id,
        execution_info=SimpleNamespace(thread_id="file-policy-thread"),
        config={"configurable": {"thread_id": "file-policy-thread"}},
    )


def test_workspace_write_does_not_need_approval(monkeypatch):
    import agent_tools.public.files as files

    calls = []
    monkeypatch.setattr(
        files,
        "write_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"path": "notes.txt", "bytes_written": 5}),
    )

    raw = files._write_file_impl("notes.txt", "hello", runtime=_runtime())
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["path"] == "notes.txt"


def test_workspace_escape_without_approval_is_denied(monkeypatch, tmp_path):
    import agent_tools.public.files as files

    calls = []
    monkeypatch.setattr(
        files,
        "write_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"path": str(tmp_path / "x.txt")}),
    )
    monkeypatch.setattr(
        files.file_policy,
        "classify_file_write",
        lambda path, task_id: files.PolicyDecision.review(
            "writes_outside_workspace",
            risk_tags=("writes_outside_workspace",),
            data={"resolved_path": str(tmp_path / "x.txt")},
        ),
    )

    raw = files._write_file_impl(str(tmp_path / "x.txt"), "hello", runtime=_runtime())
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert calls == []


def test_workspace_escape_with_approval_passes_approved_roots(monkeypatch, tmp_path):
    import agent_tools.public.files as files
    from agent_core.permissions.approvals import (
        ApprovalRecord,
        make_args_digest,
        record_approval,
    )
    from agent_core.session_context import hermes_task_id_from_thread_id

    target = tmp_path / "x.txt"
    task_id = hermes_task_id_from_thread_id("file-policy-thread")
    calls = []

    monkeypatch.setattr(
        files.file_policy,
        "classify_file_write",
        lambda path, task_id: files.PolicyDecision.review(
            "writes_outside_workspace",
            risk_tags=("writes_outside_workspace",),
            data={"resolved_path": str(target)},
        ),
    )
    monkeypatch.setattr(
        files.file_policy,
        "approved_write_root_for_path",
        lambda path, task_id: str(tmp_path),
    )
    monkeypatch.setattr(
        files,
        "write_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"path": str(target), "bytes_written": 5}),
    )
    record_approval(
        ApprovalRecord(
            approval_id="approval-file",
            decision_id="decision-file",
            task_id=task_id,
            tool_call_id="call-escape",
            tool_name="write_file",
            args_digest=make_args_digest({"path": str(target), "content": "hello"}),
            risk_tags=("writes_outside_workspace",),
        )
    )

    raw = files._write_file_impl(str(target), "hello", runtime=_runtime("call-escape"))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["approved_write_roots"] == [str(tmp_path)]


def test_sensitive_path_is_denied_without_consuming_approval(monkeypatch):
    import agent_tools.public.files as files

    calls = []
    monkeypatch.setattr(
        files,
        "write_file_tool",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"path": "/root/.ssh/id_rsa"}),
    )

    raw = files._write_file_impl("/root/.ssh/id_rsa", "secret", runtime=_runtime())
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "policy_denied"
    assert calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_file_wrappers.py -q
```

Expected: FAIL because wrappers do not consume approvals and low-level tools do not accept `approved_write_roots`.

- [ ] **Step 3: Add low-level approved write root support**

In `agent_tools/file_toolkit/file_operations.py`, import `contextmanager`:

```python
from contextlib import contextmanager
```

In `ShellFileOperations.__init__`, add:

```python
        self._approved_write_roots: list[str] = []
```

Add this method to `ShellFileOperations`:

```python
    @contextmanager
    def approved_write_roots(self, roots: list[str] | None):
        previous = list(self._approved_write_roots)
        try:
            self._approved_write_roots = previous + [str(root) for root in (roots or []) if root]
            yield
        finally:
            self._approved_write_roots = previous
```

Update `_extra_safe_write_roots()`:

```python
    def _extra_safe_write_roots(self) -> List[str]:
        """Return backend roots allowed in addition to the host safe root."""
        roots = safe_write_roots_for_env(self.env, fallback_cwd=self._effective_cwd())
        return [*roots, *self._approved_write_roots]
```

In `agent_tools/file_toolkit/file_tools.py`, change signatures:

```python
def write_file_tool(
    path: str,
    content: str,
    task_id: str = "default",
    approved_write_roots: list[str] | None = None,
) -> str:
```

Wrap the `file_ops.write_file(...)` calls:

```python
            file_ops = _get_file_ops(task_id)
            with file_ops.approved_write_roots(approved_write_roots):
                result = file_ops.write_file(path, content)
```

Change `patch_tool` signature:

```python
def patch_tool(
    mode: str = "replace",
    path: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False,
    patch: str = None,
    task_id: str = "default",
    approved_write_roots: list[str] | None = None,
) -> str:
```

Wrap the replace and patch apply block:

```python
            file_ops = _get_file_ops(task_id)
            with file_ops.approved_write_roots(approved_write_roots):
                if mode == "replace":
                    if not path:
                        return tool_error("path required")
                    if old_string is None or new_string is None:
                        return tool_error("old_string and new_string required")
                    result = file_ops.patch_replace(path, old_string, new_string, replace_all)
                elif mode == "patch":
                    if not patch:
                        return tool_error("patch content required")
                    result = file_ops.patch_v4a(patch)
                else:
                    return tool_error(f"Unknown mode: {mode}")
```

- [ ] **Step 4: Enforce policy in public file wrappers**

In `agent_tools/public/files.py`, add imports:

```python
from agent_core.permissions import file_policy
from agent_core.permissions.approvals import consume_approval
from agent_core.permissions.models import PolicyDecision
```

Add helper:

```python
def _tool_call_id_from_runtime(runtime: ToolRuntime | None) -> str | None:
    return str(getattr(runtime, "tool_call_id", "") or "") or None


def _approval_roots_for_file_decision(
    *,
    tool_name: str,
    path: str,
    args: dict,
    task_id: str,
    runtime: ToolRuntime | None,
    decision: PolicyDecision,
) -> list[str] | None | str:
    if decision.outcome == "allow":
        return []
    if decision.outcome == "deny":
        return decision.human_message
    approval = consume_approval(
        task_id=task_id,
        tool_call_id=_tool_call_id_from_runtime(runtime),
        tool_name=tool_name,
        args=args,
        required_risk_tags=decision.risk_tags,
    )
    if approval is None:
        return "Approval required before writing outside the workspace."
    return [file_policy.approved_write_root_for_path(path, task_id=task_id)]
```

In `_write_file_impl`, replace the current workspace-only check with:

```python
    decision = file_policy.classify_file_write(path, task_id=task_id)
    approved_roots = _approval_roots_for_file_decision(
        tool_name="write_file",
        path=path,
        args={"path": path, "content": content},
        task_id=task_id,
        runtime=runtime,
        decision=decision,
    )
    if isinstance(approved_roots, str):
        code = "policy_denied" if decision.outcome == "deny" else "approval_required"
        return tool_error("write_file", approved_roots, code=code)
    raw = write_file_tool(
        path=path,
        content=content,
        task_id=task_id,
        approved_write_roots=approved_roots,
    )
```

In `_patch_impl`, classify all paths. Use `_ensure_patch_paths_for_task` only for read-like validation when policy allows. The implementation should:

```python
    paths_to_classify = [path] if mode == "replace" and path else []
    if mode == "patch" and patch:
        operations, parse_error = parse_v4a_patch(patch)
        if parse_error:
            return tool_error("patch", f"Failed to parse patch: {parse_error}", code="invalid_input")
        for operation in operations:
            if operation.file_path:
                paths_to_classify.append(operation.file_path)
            if operation.new_path:
                paths_to_classify.append(operation.new_path)
    decisions = [file_policy.classify_file_write(p, task_id=task_id) for p in paths_to_classify]
    denied = next((decision for decision in decisions if decision.outcome == "deny"), None)
    if denied:
        return tool_error("patch", denied.human_message, code="policy_denied")
    review = next((decision for decision in decisions if decision.outcome == "review"), None)
    approved_roots: list[str] = []
    if review:
        args = {
            "mode": mode,
            "path": path,
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
            "patch": patch,
        }
        approval = consume_approval(
            task_id=task_id,
            tool_call_id=_tool_call_id_from_runtime(runtime),
            tool_name="patch",
            args=args,
            required_risk_tags=review.risk_tags,
        )
        if approval is None:
            return tool_error("patch", "Approval required before patching outside the workspace.", code="approval_required")
        approved_roots = [
            file_policy.approved_write_root_for_path(p, task_id=task_id)
            for p in paths_to_classify
            if file_policy.classify_file_write(p, task_id=task_id).outcome == "review"
        ]
```

Pass `approved_write_roots=approved_roots` into `patch_tool(...)`.

- [ ] **Step 5: Run wrapper tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_file_wrappers.py -q
```

Expected: PASS.

- [ ] **Step 6: Run existing file tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_file_tools_runtime_task_id.py tests/test_file_tools_hermes_env.py tests/test_backend_path_policy.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_tools/public/files.py agent_tools/file_toolkit/file_tools.py agent_tools/file_toolkit/file_operations.py tests/test_permissions_file_wrappers.py
git commit -m "feat: enforce policy for file writes"
```

---

### Task 8: Terminal Wrapper Enforcement

**Files:**
- Modify: `agent_tools/public/terminal.py`
- Modify: `agent_tools/hermes_terminal_toolkit/terminal.py`
- Test: `tests/test_permissions_terminal_wrappers.py`

- [ ] **Step 1: Write failing terminal wrapper tests**

Add `tests/test_permissions_terminal_wrappers.py`:

```python
import json
from types import SimpleNamespace


def _runtime(tool_call_id="call-1"):
    return SimpleNamespace(
        tool_call_id=tool_call_id,
        execution_info=SimpleNamespace(thread_id="terminal-policy-thread"),
        config={"configurable": {"thread_id": "terminal-policy-thread"}},
    )


def test_low_risk_command_runs_without_approval(monkeypatch):
    import agent_tools.public.terminal as terminal_tools

    calls = []
    monkeypatch.setattr(
        terminal_tools,
        "run_terminal",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"output": "ok\n", "exit_code": 0, "error": None}),
    )

    raw = terminal_tools._terminal_impl(command="pwd", runtime=_runtime())
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["allow_network_once"] is False


def test_reviewed_command_without_approval_is_denied(monkeypatch):
    import agent_tools.public.terminal as terminal_tools

    calls = []
    monkeypatch.setattr(
        terminal_tools,
        "run_terminal",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"output": "ok\n", "exit_code": 0}),
    )

    raw = terminal_tools._terminal_impl(command="pip install rich", runtime=_runtime())
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert calls == []


def test_reviewed_network_command_with_approval_passes_network_once(monkeypatch):
    import agent_tools.public.terminal as terminal_tools
    from agent_core.permissions.approvals import ApprovalRecord, make_args_digest, record_approval
    from agent_core.session_context import hermes_task_id_from_thread_id

    task_id = hermes_task_id_from_thread_id("terminal-policy-thread")
    calls = []
    record_approval(
        ApprovalRecord(
            approval_id="approval-terminal",
            decision_id="decision-terminal",
            task_id=task_id,
            tool_call_id="call-terminal",
            tool_name="terminal",
            args_digest=make_args_digest(
                {
                    "command": "curl https://example.com",
                    "background": False,
                    "timeout": None,
                    "workdir": None,
                    "pty": False,
                    "notify_on_complete": False,
                    "watch_patterns": None,
                }
            ),
            risk_tags=("network_access",),
            allow_network_once=True,
        )
    )
    monkeypatch.setattr(
        terminal_tools,
        "run_terminal",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"output": "ok\n", "exit_code": 0, "error": None}),
    )

    raw = terminal_tools._terminal_impl(command="curl https://example.com", runtime=_runtime("call-terminal"))
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["allow_network_once"] is True


def test_hardline_command_is_denied(monkeypatch):
    import agent_tools.public.terminal as terminal_tools

    calls = []
    monkeypatch.setattr(
        terminal_tools,
        "run_terminal",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"output": "should not run", "exit_code": 0}),
    )

    raw = terminal_tools._terminal_impl(command="rm -rf /", runtime=_runtime())
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "policy_denied"
    assert calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_terminal_wrappers.py -q
```

Expected: FAIL because `_terminal_impl()` does not enforce policy or pass `allow_network_once`.

- [ ] **Step 3: Add `allow_network_once` to Hermes wrapper**

In `agent_tools/hermes_terminal_toolkit/terminal.py`, update `run_terminal` signature:

```python
    allow_network_once: bool = False,
```

Pass it to `terminal_tool(...)`:

```python
        allow_network_once=allow_network_once,
```

- [ ] **Step 4: Enforce terminal policy in public wrapper**

In `agent_tools/public/terminal.py`, add imports:

```python
from agent_core.permissions import tool_policy
from agent_core.permissions.approvals import consume_approval
```

Add helper:

```python
def _tool_call_id_from_runtime(runtime: ToolRuntime | None) -> str | None:
    return str(getattr(runtime, "tool_call_id", "") or "") or None


def _terminal_policy_args(
    *,
    command: str,
    background: bool,
    timeout: int | None,
    workdir: str | None,
    pty: bool,
    notify_on_complete: bool,
    watch_patterns: list[str] | None,
) -> dict:
    return {
        "command": command,
        "background": background,
        "timeout": timeout,
        "workdir": workdir,
        "pty": pty,
        "notify_on_complete": notify_on_complete,
        "watch_patterns": watch_patterns,
    }
```

At the start of `_terminal_impl`, after `task_id`:

```python
    policy_args = _terminal_policy_args(
        command=command,
        background=background,
        timeout=timeout,
        workdir=workdir,
        pty=pty,
        notify_on_complete=notify_on_complete,
        watch_patterns=watch_patterns,
    )
    decision = tool_policy.evaluate_tool_call(
        tool_name="terminal",
        args=policy_args,
        task_id=task_id,
        tool_call_id=_tool_call_id_from_runtime(runtime),
    )
    allow_network_once = False
    if decision.outcome == "deny":
        return tool_error("terminal", decision.human_message, code="policy_denied", data=decision.data)
    if decision.outcome == "review":
        approval = consume_approval(
            task_id=task_id,
            tool_call_id=_tool_call_id_from_runtime(runtime),
            tool_name="terminal",
            args=policy_args,
            required_risk_tags=decision.risk_tags,
        )
        if approval is None:
            return tool_error("terminal", decision.human_message, code="approval_required", data=decision.data)
        allow_network_once = approval.allow_network_once
```

Pass `allow_network_once=allow_network_once` in both `run_terminal(...)` calls.

- [ ] **Step 5: Run terminal wrapper tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_terminal_wrappers.py -q
```

Expected: PASS.

- [ ] **Step 6: Run existing terminal tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_terminal_tools.py tests/test_terminal_process_policy.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_tools/public/terminal.py agent_tools/hermes_terminal_toolkit/terminal.py tests/test_permissions_terminal_wrappers.py
git commit -m "feat: enforce policy for terminal commands"
```

---

### Task 9: Process Policy Enforcement

**Files:**
- Modify: `agent_tools/public/terminal.py`
- Test: `tests/test_permissions_process_wrappers.py`

- [ ] **Step 1: Write failing process wrapper tests**

Add `tests/test_permissions_process_wrappers.py`:

```python
import json
from types import SimpleNamespace


def _runtime(tool_call_id="call-process"):
    return SimpleNamespace(
        tool_call_id=tool_call_id,
        execution_info=SimpleNamespace(thread_id="process-policy-thread"),
        config={"configurable": {"thread_id": "process-policy-thread"}},
    )


def test_process_poll_does_not_need_approval(monkeypatch):
    import agent_tools.public.terminal as terminal_tools

    monkeypatch.setattr(terminal_tools, "_session_belongs_to_task", lambda session_id, task_id: True)
    calls = []
    monkeypatch.setattr(
        terminal_tools,
        "run_process",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"status": "running"}),
    )

    raw = terminal_tools._process_impl(action="poll", session_id="proc_1", runtime=_runtime())
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["action"] == "poll"


def test_process_submit_without_approval_is_denied(monkeypatch):
    import agent_tools.public.terminal as terminal_tools

    monkeypatch.setattr(terminal_tools, "_session_belongs_to_task", lambda session_id, task_id: True)
    calls = []
    monkeypatch.setattr(
        terminal_tools,
        "run_process",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"status": "ok"}),
    )

    raw = terminal_tools._process_impl(
        action="submit",
        session_id="proc_1",
        data="rm -rf tmp",
        runtime=_runtime(),
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "approval_required"
    assert calls == []


def test_process_submit_with_approval_runs(monkeypatch):
    import agent_tools.public.terminal as terminal_tools
    from agent_core.permissions.approvals import ApprovalRecord, make_args_digest, record_approval
    from agent_core.session_context import hermes_task_id_from_thread_id

    task_id = hermes_task_id_from_thread_id("process-policy-thread")
    monkeypatch.setattr(terminal_tools, "_session_belongs_to_task", lambda session_id, task_id: True)
    calls = []
    monkeypatch.setattr(
        terminal_tools,
        "run_process",
        lambda **kwargs: calls.append(kwargs) or json.dumps({"status": "ok"}),
    )
    record_approval(
        ApprovalRecord(
            approval_id="approval-process",
            decision_id="decision-process",
            task_id=task_id,
            tool_call_id="call-process-approved",
            tool_name="process",
            args_digest=make_args_digest(
                {
                    "action": "submit",
                    "session_id": "proc_1",
                    "data": "exit",
                    "timeout": None,
                    "offset": 0,
                    "limit": 200,
                }
            ),
            risk_tags=("process_stdin",),
        )
    )

    raw = terminal_tools._process_impl(
        action="submit",
        session_id="proc_1",
        data="exit",
        runtime=_runtime("call-process-approved"),
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert calls[0]["action"] == "submit"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_process_wrappers.py -q
```

Expected: FAIL because process actions do not enforce policy approval.

- [ ] **Step 3: Enforce process policy in public wrapper**

In `agent_tools/public/terminal.py`, add helper:

```python
def _process_policy_args(
    *,
    action: str,
    session_id: str,
    data: str,
    timeout: int | None,
    offset: int,
    limit: int,
) -> dict:
    return {
        "action": action,
        "session_id": session_id,
        "data": data,
        "timeout": timeout,
        "offset": offset,
        "limit": limit,
    }
```

After session ownership validation in `_process_impl`, add:

```python
    policy_args = _process_policy_args(
        action=action,
        session_id=session_id,
        data=data,
        timeout=timeout,
        offset=offset,
        limit=limit,
    )
    decision = tool_policy.evaluate_tool_call(
        tool_name="process",
        args=policy_args,
        task_id=task_id,
        tool_call_id=_tool_call_id_from_runtime(runtime),
    )
    if decision.outcome == "deny":
        return tool_error("process", decision.human_message, code="policy_denied", data=decision.data)
    if decision.outcome == "review":
        approval = consume_approval(
            task_id=task_id,
            tool_call_id=_tool_call_id_from_runtime(runtime),
            tool_name="process",
            args=policy_args,
            required_risk_tags=decision.risk_tags,
        )
        if approval is None:
            return tool_error("process", decision.human_message, code="approval_required", data=decision.data)
```

- [ ] **Step 4: Run process tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_process_wrappers.py tests/test_terminal_tools.py::test_process_schema_does_not_expose_task_id -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/public/terminal.py tests/test_permissions_process_wrappers.py
git commit -m "feat: enforce policy for process stdin"
```

---

### Task 10: Docker Temporary Network Leases

**Files:**
- Modify: `agent_tools/hermes_terminal_toolkit/environments/docker.py`
- Modify: `agent_tools/hermes_terminal_toolkit/terminal_tool.py`
- Modify: `agent_tools/hermes_terminal_toolkit/process_registry.py`
- Test: `tests/test_permissions_docker_network.py`

- [ ] **Step 1: Write failing Docker network tests**

Add `tests/test_permissions_docker_network.py`:

```python
import contextlib
import json


class FakeDockerEnv:
    def __init__(self):
        self.calls = []
        self.network_events = []
        self.cwd = "/workspace"

    @contextlib.contextmanager
    def temporary_network(self):
        self.network_events.append("connect")
        try:
            yield
        finally:
            self.network_events.append("disconnect")

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"output": "ok\n", "returncode": 0}


def test_terminal_tool_wraps_foreground_command_in_network_context(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    env = FakeDockerEnv()
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "docker",
            "cwd": "/workspace",
            "timeout": 180,
            "runtime_profile": "hosted",
        },
    )
    monkeypatch.setattr(terminal_tool, "get_or_create_active_env", lambda *args, **kwargs: env)
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda command, env_type: {"approved": True})

    raw = terminal_tool.terminal_tool(
        command="curl https://example.com",
        task_id="task-1",
        allow_network_once=True,
    )
    payload = json.loads(raw)

    assert payload["exit_code"] == 0
    assert env.network_events == ["connect", "disconnect"]


def test_terminal_tool_cleans_vm_when_network_disconnect_fails(monkeypatch):
    from agent_tools.hermes_terminal_toolkit import terminal_tool

    class BrokenEnv(FakeDockerEnv):
        @contextlib.contextmanager
        def temporary_network(self):
            self.network_events.append("connect")
            yield
            raise RuntimeError("disconnect failed")

    env = BrokenEnv()
    cleaned = []
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "docker",
            "cwd": "/workspace",
            "timeout": 180,
            "runtime_profile": "hosted",
        },
    )
    monkeypatch.setattr(terminal_tool, "get_or_create_active_env", lambda *args, **kwargs: env)
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda command, env_type: {"approved": True})
    monkeypatch.setattr(terminal_tool, "cleanup_vm", lambda task_id: cleaned.append(task_id))

    raw = terminal_tool.terminal_tool(
        command="curl https://example.com",
        task_id="task-1",
        allow_network_once=True,
    )
    payload = json.loads(raw)

    assert payload["exit_code"] == 0
    assert "network_warning" in payload
    assert cleaned == ["task-1"]


def test_process_registry_releases_background_network_lease(monkeypatch):
    from agent_tools.hermes_terminal_toolkit.process_registry import ProcessRegistry

    released = []
    registry = ProcessRegistry()
    session = registry.spawn_via_env(
        env=FakeDockerEnv(),
        command="python server.py",
        cwd="/workspace",
        task_id="task-bg",
        session_key="",
        network_release=lambda: released.append("released"),
    )

    registry._move_to_finished(session)

    assert released == ["released"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_docker_network.py -q
```

Expected: FAIL because `allow_network_once`, `temporary_network`, and `network_release` do not exist.

- [ ] **Step 3: Implement Docker temporary network context**

In `agent_tools/hermes_terminal_toolkit/environments/docker.py`, add imports:

```python
from contextlib import contextmanager
```

In `DockerEnvironment.__init__`, store network state:

```python
        self._network_enabled = bool(network)
        self._egress_network = os.getenv("HERMES_DOCKER_NETWORK", "bridge")
```

Add methods:

```python
    def _docker_network_connect(self):
        if not self._container_id or self._network_enabled:
            return
        subprocess.run(
            [self._docker_exe, "network", "connect", self._egress_network, self._container_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        self._network_enabled = True

    def _docker_network_disconnect(self):
        if not self._container_id or not self._network_enabled:
            return
        subprocess.run(
            [self._docker_exe, "network", "disconnect", self._egress_network, self._container_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        self._network_enabled = False

    @contextmanager
    def temporary_network(self):
        was_enabled = self._network_enabled
        if not was_enabled:
            self._docker_network_connect()
        try:
            yield
        finally:
            if not was_enabled:
                self._docker_network_disconnect()
```

- [ ] **Step 4: Add `allow_network_once` to terminal_tool**

In `agent_tools/hermes_terminal_toolkit/terminal_tool.py`, update `terminal_tool` signature:

```python
    allow_network_once: bool = False,
```

After `env = get_or_create_active_env(...)`, add:

```python
        network_cm = None
        network_warning = None
        if allow_network_once:
            network_cm = getattr(env, "temporary_network", None)
            if network_cm is None:
                return json.dumps(
                    {
                        "output": "",
                        "exit_code": -1,
                        "error": "One-shot network access is only supported by backends with temporary_network().",
                        "status": "blocked",
                    },
                    ensure_ascii=False,
                )
```

Wrap foreground `env.execute(...)` with helper logic:

```python
        def _execute_with_optional_network():
            nonlocal network_warning
            if network_cm is None:
                return env.execute(command, **execute_kwargs)
            try:
                with network_cm():
                    return env.execute(command, **execute_kwargs)
            except Exception as exc:
                if "disconnect" in str(exc).lower():
                    network_warning = f"Network cleanup failed after command execution: {exc}"
                    try:
                        cleanup_vm(effective_task_id)
                    except Exception:
                        logger.debug("Failed to cleanup env after network warning", exc_info=True)
                    return {"output": "", "returncode": 0}
                raise
```

Use `result = _execute_with_optional_network()` instead of direct `env.execute(...)` for foreground commands. When building `result_dict`, include:

```python
        if network_warning:
            result_dict["network_warning"] = network_warning
```

For background commands, if `allow_network_once` and `network_cm` is available:

```python
                    lease = network_cm()
                    lease.__enter__()
                    release_network = lease.__exit__
```

Pass `network_release=lambda: release_network(None, None, None)` to `process_registry.spawn_via_env(...)`. If spawning fails, call the release lambda before returning the error.

- [ ] **Step 5: Implement process registry lease release**

In `agent_tools/hermes_terminal_toolkit/process_registry.py`, add field to `ProcessSession`:

```python
    network_release: Any = field(default=None, repr=False)
```

Update `spawn_via_env` signature:

```python
        network_release: Any = None,
```

Pass into `ProcessSession(...)`:

```python
            network_release=network_release,
```

In `_move_to_finished`, before `_write_checkpoint()`:

```python
        release = getattr(session, "network_release", None)
        if release is not None:
            try:
                release()
            except Exception:
                logger.warning("Failed to release network lease for %s", session.id, exc_info=True)
            finally:
                session.network_release = None
```

- [ ] **Step 6: Run Docker network tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_permissions_docker_network.py -q
```

Expected: PASS.

- [ ] **Step 7: Run terminal lifecycle/process tests**

Run:

```bash
PYTHONPATH=. pytest tests/test_hermes_active_env.py tests/test_terminal_tools.py tests/test_terminal_lifecycle.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add agent_tools/hermes_terminal_toolkit/environments/docker.py agent_tools/hermes_terminal_toolkit/terminal_tool.py agent_tools/hermes_terminal_toolkit/process_registry.py tests/test_permissions_docker_network.py
git commit -m "feat: add one-shot docker network leases"
```

---

### Task 11: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Test: full relevant suite

- [ ] **Step 1: Update README runtime policy section**

In `README.md`, add this section after the "Security and approval" bullets:

```markdown
Layered permission model:

- `AGENT_RUNTIME_PROFILE` controls default runtime posture: `dev`, `test`, `hosted`, or `prod`.
- If `TERMINAL_ENV` is unset, `dev/test` default to `local` and `hosted/prod` default to `docker`.
- Read-only file tools run without human review when existing workspace admission allows them.
- Workspace-local `write_file` and `patch` calls run without review.
- Ordinary writes outside the workspace require one-shot approval; sensitive paths such as `~/.ssh`, `~/.aws`, `/etc`, and Docker socket paths are denied.
- Low-risk shell commands such as read-only file inspection, read-only Git commands, and tests run without review.
- Package installs, network commands, destructive commands, permission changes, background servers, and complex shell require one-shot approval.
- Hardline destructive commands are denied.
- In `hosted/prod`, Docker sandboxes default to no network. Approved network commands receive temporary network access for the command lifetime.
```

- [ ] **Step 2: Run policy test group**

Run:

```bash
PYTHONPATH=. pytest \
  tests/test_permissions_profiles.py \
  tests/test_permissions_approvals.py \
  tests/test_permissions_command_policy.py \
  tests/test_permissions_file_policy.py \
  tests/test_permissions_tool_policy.py \
  tests/test_permissions_human_loop.py \
  tests/test_permissions_file_wrappers.py \
  tests/test_permissions_terminal_wrappers.py \
  tests/test_permissions_process_wrappers.py \
  tests/test_permissions_docker_network.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run existing impacted tests**

Run:

```bash
PYTHONPATH=. pytest \
  tests/test_agent_tools_public_imports.py \
  tests/test_backend_path_policy.py \
  tests/test_file_operations_backend_stat.py \
  tests/test_file_state_backend_mtime.py \
  tests/test_file_tools_hermes_env.py \
  tests/test_file_tools_runtime_task_id.py \
  tests/test_hermes_active_env.py \
  tests/test_terminal_lifecycle.py \
  tests/test_terminal_notifications.py \
  tests/test_terminal_process_policy.py \
  tests/test_terminal_tools.py \
  -q
```

Expected: PASS.

- [ ] **Step 4: Run full suite**

Run:

```bash
PYTHONPATH=. pytest tests -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: describe layered permission runtime"
```

---

## Self-Review Checklist

- Spec coverage:
  - Runtime profile resolution is covered by Task 1 and Task 5.
  - Unified policy engine is covered by Tasks 1 through 4.
  - HITL policy routing and approval registry are covered by Task 6.
  - File permissions and ordinary workspace escape approval are covered by Task 7.
  - Shell command policy and wrapper enforcement are covered by Tasks 2 and 8.
  - Process stdin review is covered by Task 9.
  - Docker default no-network and temporary network access are covered by Task 10.
  - Documentation and full verification are covered by Task 11.
- Placeholder scan: no task contains open-ended placeholders; each code-changing step includes concrete file paths and snippets.
- Type consistency:
  - `PolicyDecision` is the shared decision type across command, file, tool, middleware, and wrappers.
  - Approval registry keys use `task_id` and `tool_call_id`; wrappers read `runtime.tool_call_id`.
  - One-shot network grants use `allow_network_once` from middleware approval through public terminal wrapper into Hermes terminal execution.

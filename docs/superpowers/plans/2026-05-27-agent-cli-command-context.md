# Agent CLI CommandContext Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a thin `CommandContext` facade so Agent CLI command handlers depend on a stable context API instead of the full `AgentCLI` runtime object.

**Architecture:** `AgentCLI` remains the runtime state owner. `CommandContext` wraps an `AgentCLI` instance and exposes only properties and stable operations needed by handlers. The command registry and handlers switch from `handler(cli, arg, command)` to `handler(ctx, arg, command)` without changing slash command behavior.

**Tech Stack:** Python 3.11+, dataclasses, existing Agent CLI command registry, pytest.

---

## File Structure

- Create `agent_cli/command_context.py`: thin facade over `AgentCLI`.
- Modify `agent_cli/command_handlers/__init__.py`: handler type and `build_command_handlers(ctx)`.
- Modify `agent_cli/command_handlers/session.py`: use `ctx`.
- Modify `agent_cli/command_handlers/background.py`: use `ctx`.
- Modify `agent_cli/command_handlers/skills.py`: use `ctx`.
- Modify `agent_cli/command_handlers/debug.py`: use `ctx`.
- Modify `agent_cli/command_handlers/clipboard.py`: use `ctx`.
- Modify `agent_cli/repl.py`: create `self.command_context`, dispatch with context, keep runtime lifecycle unchanged.
- Create `tests/test_agent_cli_command_context.py`: context facade tests.
- Modify `tests/test_agent_cli_repl.py`: dispatch receives context and dynamic skill fallback remains unchanged.

Use `/home/miku/miniforge3/envs/langchain/bin/python -m pytest ...` for all test commands.

---

### Task 1: Add CommandContext Facade

**Files:**
- Create: `agent_cli/command_context.py`
- Test: `tests/test_agent_cli_command_context.py`

- [ ] **Step 1: Write failing context proxy tests**

Create `tests/test_agent_cli_command_context.py`:

```python
from types import SimpleNamespace

import pytest

from agent_cli.command_context import CommandContext
from agent_cli.repl import AgentCLI


class FakeStore:
    def __init__(self):
        self.created = []
        self.sessions = {}
        self._counter = 0

    def new_session_id(self):
        self._counter += 1
        return f"s{self._counter}"

    def create_session(self, *, workdir, model, title="New session", session_id=None):
        session_id = session_id or self.new_session_id()
        record = SimpleNamespace(
            session_id=session_id,
            title=title,
            workdir=workdir,
            model=model,
            created_at="created",
            updated_at="updated",
            last_message_preview=None,
        )
        self.created.append(record)
        self.sessions[session_id] = record
        return record

    def touch_session(self, session_id, **kwargs):
        pass

    def title_from_message(self, message, max_length=60):
        return message[:max_length] or "New session"


def make_cli(**overrides):
    kwargs = {
        "session_store": FakeStore(),
        "checkpointer": "cp",
        "agent_factory": lambda checkpointer: "agent",
        "runner": lambda agent, input_data, config: {
            "messages": [{"role": "assistant", "content": "ok"}]
        },
        "workdir": "/repo",
        "model_name": "model",
    }
    kwargs.update(overrides)
    return AgentCLI(**kwargs)


def test_context_proxies_core_runtime_properties():
    cli = make_cli(profile="dev", cli_home="/tmp/agent-cli", display_theme="slate")
    ctx = CommandContext(cli)

    assert ctx.session_store is cli.session_store
    assert ctx.checkpointer == "cp"
    assert ctx.workdir == "/repo"
    assert ctx.model_name == "model"
    assert ctx.profile == "dev"
    assert ctx.cli_home == "/tmp/agent-cli"
    assert ctx.display_theme == "slate"
    assert ctx.display_markdown == "render"
    assert ctx.session_id is None
    assert ctx.session is None
    assert ctx.assistant_replies == []


def test_context_session_operations_delegate_to_cli():
    cli = make_cli()
    ctx = CommandContext(cli)

    assert ctx.ensure_session("hello") == "s1"
    assert ctx.session_id == "s1"

    ctx.set_session("manual")
    assert cli.session_id == "manual"
    assert ctx.session_id == "manual"


def test_context_submit_message_delegates_to_cli():
    cli = make_cli()
    ctx = CommandContext(cli)

    assert ctx.submit_message("hello") == "ok"
    assert ctx.last_user_message == "hello"
    assert ctx.assistant_replies == ["ok"]


def test_context_background_registry_access():
    registry = object()
    ctx = CommandContext(make_cli(background_registry=registry))

    assert ctx.require_background_registry() is registry

    with pytest.raises(RuntimeError, match="background tasks are not configured"):
        CommandContext(make_cli(background_registry=None)).require_background_registry()


def test_context_reload_delegates_to_cli(monkeypatch):
    cli = make_cli()
    monkeypatch.setattr(cli, "reload_runtime_settings", lambda: "reloaded")

    assert CommandContext(cli).reload_runtime_settings() == "reloaded"


def test_context_skill_helpers_delegate():
    loaded = object()
    discovery = object()
    command = object()
    cli = make_cli(
        skill_commands_provider=lambda: {"x": command},
        skill_discovery_provider=lambda: discovery,
        skill_loader=lambda item: loaded,
    )
    ctx = CommandContext(cli)

    assert ctx.skill_commands() == {"x": command}
    assert ctx.skill_discovery() is discovery
    assert ctx.load_skill(command) is loaded
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_command_context.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_cli.command_context'`.

- [ ] **Step 3: Implement CommandContext**

Create `agent_cli/command_context.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_cli.repl import AgentCLI


@dataclass
class CommandContext:
    cli: AgentCLI

    @property
    def session_store(self):
        return self.cli.session_store

    @property
    def checkpointer(self):
        return self.cli.checkpointer

    @property
    def workdir(self):
        return self.cli.workdir

    @property
    def model_name(self):
        return self.cli.model_name

    @property
    def profile(self):
        return self.cli.profile

    @property
    def cli_home(self):
        return self.cli.cli_home

    @property
    def display_theme(self):
        return self.cli.display_theme

    @property
    def display_markdown(self):
        return self.cli.display_markdown

    @property
    def session_id(self):
        return self.cli.session_id

    @property
    def session(self):
        return self.cli.session

    @property
    def assistant_replies(self):
        return self.cli.assistant_replies

    @property
    def last_user_message(self):
        return self.cli.last_user_message

    @property
    def last_call_elapsed_seconds(self):
        return self.cli.last_call_elapsed_seconds

    @property
    def last_usage_metadata(self):
        return self.cli.last_usage_metadata

    def ensure_session(self, first_message: str | None = None) -> str:
        return self.cli.ensure_session(first_message)

    def set_session(self, session_id: str) -> None:
        self.cli._set_session(session_id)

    def submit_message(self, text: str) -> str:
        return self.cli.submit_message(text)

    def effective_cli_home(self):
        return self.cli._effective_cli_home()

    def require_background_registry(self):
        return self.cli._require_background_registry()

    def reload_runtime_settings(self) -> str:
        return self.cli.reload_runtime_settings()

    def skill_commands(self) -> dict[str, Any]:
        return self.cli.skill_commands_provider()

    def skill_discovery(self) -> Any:
        if self.cli.skill_discovery_provider is not None:
            return self.cli.skill_discovery_provider()
        from agent_cli.commands import COMMAND_LOOKUP
        from agent_cli.skill_commands import load_skill_discovery

        return load_skill_discovery(built_in_names=set(COMMAND_LOOKUP))

    def load_skill(self, command: Any) -> Any:
        if self.cli.skill_loader is not None:
            return self.cli.skill_loader(command)
        from agent_cli.skill_commands import load_skill_for_command

        return load_skill_for_command(command)
```

- [ ] **Step 4: Run context tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_command_context.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit context facade**

```bash
git add agent_cli/command_context.py tests/test_agent_cli_command_context.py
git commit -m "feat: add agent cli command context"
```

---

### Task 2: Route Command Dispatch Through CommandContext

**Files:**
- Modify: `agent_cli/command_handlers/__init__.py`
- Modify: `agent_cli/repl.py`
- Modify: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing dispatch test**

Update `tests/test_agent_cli_repl.py` test `test_handle_command_dispatches_through_handler_registry`:

```python
def test_handle_command_dispatches_through_handler_registry():
    from agent_cli.command_context import CommandContext

    calls = []
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    def fake_handler(ctx_arg, arg, command):
        calls.append((ctx_arg, arg, command.name))
        return "handled"

    cli.command_handlers = {"help": fake_handler}

    assert cli.handle_command("/help extra") == "handled"
    assert isinstance(calls[0][0], CommandContext)
    assert calls[0][0].cli is cli
    assert calls[0][1:] == ("extra", "help")
```

Update `tests/test_agent_cli_repl.py` test `test_all_builtin_commands_have_registered_handlers`:

```python
def test_all_builtin_commands_have_registered_handlers():
    from agent_cli.command_handlers import build_command_handlers
    from agent_cli.commands import COMMAND_REGISTRY

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )
    handlers = build_command_handlers(cli.command_context)

    missing = [
        command.name
        for command in COMMAND_REGISTRY
        if command.effective_handler_key not in handlers
    ]
    assert missing == []
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_handle_command_dispatches_through_handler_registry tests/test_agent_cli_repl.py::test_all_builtin_commands_have_registered_handlers -q
```

Expected: FAIL because `AgentCLI` does not create `command_context` and handlers still receive `AgentCLI`.

- [ ] **Step 3: Update handler registry type**

Modify `agent_cli/command_handlers/__init__.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_cli.commands import CommandDef
from agent_cli.command_handlers.background import background_handlers
from agent_cli.command_handlers.clipboard import clipboard_handlers
from agent_cli.command_handlers.debug import debug_handlers
from agent_cli.command_handlers.session import session_handlers
from agent_cli.command_handlers.skills import skills_handlers

if TYPE_CHECKING:
    from agent_cli.command_context import CommandContext

CommandHandler = Callable[["CommandContext", str, CommandDef], str | None]


def build_command_handlers(ctx: "CommandContext") -> dict[str, CommandHandler]:
    handlers: dict[str, CommandHandler] = {}
    for group in (
        debug_handlers(),
        session_handlers(),
        background_handlers(),
        skills_handlers(),
        clipboard_handlers(),
    ):
        handlers.update(group)
    return handlers
```

- [ ] **Step 4: Update AgentCLI to own CommandContext**

In `agent_cli/repl.py`, import:

```python
from agent_cli.command_context import CommandContext
```

Replace:

```python
self.command_handlers = build_command_handlers(self)
```

with:

```python
self.command_context = CommandContext(self)
self.command_handlers = build_command_handlers(self.command_context)
```

Replace dispatch:

```python
return handler(self, arg, command)
```

with:

```python
return handler(self.command_context, arg, command)
```

In dynamic skill fallback, replace direct loader access with context:

```python
loaded = self.command_context.load_skill(dynamic[skill_name])
message = build_skill_invocation_message(loaded, arg)
return self.command_context.submit_message(message)
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_command_context.py tests/test_agent_cli_repl.py::test_handle_command_dispatches_through_handler_registry tests/test_agent_cli_repl.py::test_all_builtin_commands_have_registered_handlers tests/test_agent_cli_repl.py::test_handle_command_routes_dynamic_skill_to_agent -q
```

Expected: PASS.

- [ ] **Step 6: Commit dispatch migration**

```bash
git add agent_cli/command_handlers/__init__.py agent_cli/repl.py tests/test_agent_cli_repl.py
git commit -m "refactor: dispatch agent cli commands with context"
```

---

### Task 3: Migrate Session, Background, Skills, Debug, and Clipboard Handlers

**Files:**
- Modify: `agent_cli/command_handlers/session.py`
- Modify: `agent_cli/command_handlers/background.py`
- Modify: `agent_cli/command_handlers/skills.py`
- Modify: `agent_cli/command_handlers/debug.py`
- Modify: `agent_cli/command_handlers/clipboard.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Add failing boundary test**

Add to `tests/test_agent_cli_repl.py`:

```python
def test_command_handlers_do_not_depend_on_agent_cli_private_helpers():
    from pathlib import Path

    handler_dir = Path("agent_cli/command_handlers")
    forbidden = (
        "._set_session",
        "._require_background_registry",
        "._effective_cli_home",
        "from agent_cli.repl import AgentCLI",
    )

    offenders = {}
    for path in handler_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        found = [item for item in forbidden if item in text]
        if found:
            offenders[str(path)] = found

    assert offenders == {}
```

- [ ] **Step 2: Run boundary test and verify it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_command_handlers_do_not_depend_on_agent_cli_private_helpers -q
```

Expected: FAIL listing `session.py` and `background.py`.

- [ ] **Step 3: Migrate session handlers to ctx**

In `agent_cli/command_handlers/session.py`, rename handler parameter `cli` to `ctx` and replace:

```python
cli.session_store -> ctx.session_store
cli.workdir -> ctx.workdir
cli.model_name -> ctx.model_name
cli.default_title -> ctx.cli.default_title
cli._set_session(...) -> ctx.set_session(...)
cli.session -> ctx.session
cli.ensure_session(...) -> ctx.ensure_session(...)
cli.submit_message(...) -> ctx.submit_message(...)
cli.checkpointer -> ctx.checkpointer
cli.last_call_elapsed_seconds -> ctx.last_call_elapsed_seconds
cli.assistant_replies -> ctx.assistant_replies
cli.last_usage_metadata -> ctx.last_usage_metadata
```

For `default_title`, either add a `default_title` property to `CommandContext` in `agent_cli/command_context.py` or use `ctx.cli.default_title`. Prefer adding:

```python
@property
def default_title(self):
    return self.cli.default_title
```

Then use `ctx.default_title`.

- [ ] **Step 4: Migrate background handlers to ctx**

In `agent_cli/command_handlers/background.py`, rename handler parameter `cli` to `ctx` and replace every:

```python
cli._require_background_registry()
```

with:

```python
ctx.require_background_registry()
```

Change `render_task_detail(cli, task_id)` to `render_task_detail(ctx, task_id)`.

- [ ] **Step 5: Migrate skills, debug, and clipboard handlers**

In `agent_cli/command_handlers/skills.py`:

```python
discovery = ctx.skill_discovery()
```

In `agent_cli/command_handlers/debug.py`:

```python
return ctx.reload_runtime_settings()
results = run_health_checks(workdir=ctx.workdir)
```

In `agent_cli/command_handlers/clipboard.py`:

```python
if index > len(ctx.assistant_replies):
    return "No assistant reply available to copy."
text = ctx.assistant_replies[-index]
```

- [ ] **Step 6: Run focused handler tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_command_handlers_do_not_depend_on_agent_cli_private_helpers tests/test_agent_cli_repl.py tests/test_agent_cli_command_context.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit handler migration**

```bash
git add agent_cli/command_context.py agent_cli/command_handlers/session.py agent_cli/command_handlers/background.py agent_cli/command_handlers/skills.py agent_cli/command_handlers/debug.py agent_cli/command_handlers/clipboard.py tests/test_agent_cli_repl.py
git commit -m "refactor: migrate agent cli handlers to command context"
```

---

### Task 4: Final Verification and Documentation Check

**Files:**
- No planned production changes unless verification exposes a gap.

- [ ] **Step 1: Verify no handler private helper usage remains**

Run:

```bash
rg -n "_set_session|_require_background_registry|_effective_cli_home|from agent_cli.repl import AgentCLI" agent_cli/command_handlers
```

Expected: no output.

- [ ] **Step 2: Run focused verification**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_agent_cli_command_context.py \
  tests/test_agent_cli_repl.py \
  tests/test_agent_cli_commands.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full Agent CLI suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_*.py -q
```

Expected: PASS.

- [ ] **Step 4: Smoke command registry behavior**

Run:

```bash
AGENT_CLI_HOME=/tmp/agent-cli-command-context-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli --help
AGENT_CLI_HOME=/tmp/agent-cli-command-context-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli config show
AGENT_CLI_HOME=/tmp/agent-cli-command-context-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli sessions
```

Expected:

- `--help` exits `0` and lists slash-command documentation source.
- `config show` exits `0`.
- `sessions` exits `0`.

- [ ] **Step 5: Inspect git status**

Run:

```bash
git status --short
```

Expected: Only intentional CommandContext migration files are modified. Preserve unrelated untracked files such as `exports/`.

- [ ] **Step 6: Commit final fix if needed**

If verification required a small correction, stage the exact changed files and commit:

```bash
git add agent_cli/command_context.py agent_cli/command_handlers agent_cli/repl.py tests/test_agent_cli_command_context.py tests/test_agent_cli_repl.py
git commit -m "fix: finalize agent cli command context migration"
```

If no correction was needed, do not create an empty commit.

---

## Review Checklist

- `CommandContext` exists and wraps `AgentCLI`.
- `AgentCLI` owns `self.command_context`.
- `build_command_handlers()` accepts context.
- Fake dispatch tests prove handlers receive `CommandContext`.
- Handler modules no longer call `cli._set_session()`, `cli._require_background_registry()`, or `cli._effective_cli_home()`.
- Dynamic skill fallback behavior is unchanged.
- Existing slash command output is unchanged.
- Full Agent CLI tests pass.

# Agent CLI Command Config Background Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor Agent CLI command dispatch into a handler registry, add schema-backed config commands and reload, and improve background task inspection UX.

**Architecture:** Keep `agent_cli/commands.py` as the single static command metadata source. Move command implementations into focused `agent_cli/command_handlers/` modules while keeping `AgentCLI` as the state owner. Add a lightweight dataclass config schema and small formatting helpers instead of introducing a new framework or daemon.

**Tech Stack:** Python 3.11+, argparse, dataclasses, PyYAML when installed, pytest, existing Agent CLI SQLite stores.

---

## File Structure

- Create `agent_cli/command_handlers/__init__.py`: builds the handler registry.
- Create `agent_cli/command_handlers/session.py`: session/history/export/usage/retry/clear handlers.
- Create `agent_cli/command_handlers/background.py`: background, tasks, queue, steer, stop, approve, tail handlers.
- Create `agent_cli/command_handlers/skills.py`: skills and skill detail handlers.
- Create `agent_cli/command_handlers/debug.py`: help, doctor, reload, exit handlers.
- Create `agent_cli/command_handlers/clipboard.py`: copy handler.
- Create `agent_cli/config_schema.py`: dataclass config model, validation, path get/set helpers.
- Create `agent_cli/output_format.py`: live assistant output formatting for `display.markdown`.
- Create `agent_cli/config.example.yaml`: supported config example.
- Modify `agent_cli/commands.py`: add `handler_key` and new commands.
- Modify `agent_cli/repl.py`: replace long if-chain with registry dispatch, add runtime reload support, use output formatter.
- Modify `agent_cli/config.py`: parse config through schema and expose load/save helpers.
- Modify `agent_cli/main.py`: add `agent_cli config show|get|set`.
- Modify `agent_cli/doctor.py`: use schema path errors and check example config.
- Modify `agent_cli/background.py`: add steer retrieval helpers.
- Modify tests under `tests/test_agent_cli_*.py`: focused coverage per task.
- Modify `README.md`: document config commands, `/reload`, `/tail`, and updated background task usage.

Use `/home/miku/miniforge3/envs/langchain/bin/python -m pytest ...` for all test commands.

---

### Task 1: Add CommandDef Handler Keys and Registry Dispatch Tests

**Files:**
- Modify: `agent_cli/commands.py`
- Modify: `agent_cli/repl.py`
- Create: `agent_cli/command_handlers/__init__.py`
- Test: `tests/test_agent_cli_commands.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing command metadata tests**

Add to `tests/test_agent_cli_commands.py`:

```python
def test_every_builtin_command_has_handler_key():
    from agent_cli.commands import COMMAND_REGISTRY

    for command in COMMAND_REGISTRY:
        assert command.effective_handler_key
        assert command.effective_handler_key == (command.handler_key or command.name)


def test_aliases_resolve_to_canonical_handler_key():
    from agent_cli.commands import resolve_command

    command = resolve_command("/h")
    assert command is not None
    assert command.name == "help"
    assert command.effective_handler_key == "help"
```

- [ ] **Step 2: Write failing dispatcher tests**

Add to `tests/test_agent_cli_repl.py`:

```python
def test_handle_command_dispatches_through_handler_registry():
    calls = []
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    def fake_handler(cli_arg, arg, command):
        calls.append((cli_arg, arg, command.name))
        return "handled"

    cli.command_handlers = {"help": fake_handler}

    assert cli.handle_command("/help extra") == "handled"
    assert calls == [(cli, "extra", "help")]


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
    handlers = build_command_handlers(cli)

    missing = [
        command.name
        for command in COMMAND_REGISTRY
        if command.effective_handler_key not in handlers
    ]
    assert missing == []
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py::test_every_builtin_command_has_handler_key tests/test_agent_cli_commands.py::test_aliases_resolve_to_canonical_handler_key tests/test_agent_cli_repl.py::test_handle_command_dispatches_through_handler_registry tests/test_agent_cli_repl.py::test_all_builtin_commands_have_registered_handlers -q
```

Expected: FAIL because `handler_key`, `effective_handler_key`, and `command_handlers` do not exist yet.

- [ ] **Step 4: Add handler key metadata**

Update `agent_cli/commands.py`:

```python
@dataclass(frozen=True)
class CommandDef:
    name: str
    description: str
    category: str
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    completion: CompletionKind = "none"
    handler_key: str | None = None

    @property
    def usage(self) -> str:
        suffix = f" {self.args_hint}" if self.args_hint else ""
        return f"/{self.name}{suffix}"

    @property
    def effective_handler_key(self) -> str:
        return self.handler_key or self.name
```

- [ ] **Step 5: Create temporary handler registry shims**

Create `agent_cli/command_handlers/__init__.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_cli.commands import CommandDef

if TYPE_CHECKING:
    from agent_cli.repl import AgentCLI

CommandHandler = Callable[["AgentCLI", str, CommandDef], str | None]


def build_command_handlers(cli: "AgentCLI") -> dict[str, CommandHandler]:
    return {
        "help": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "doctor": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "new": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "sessions": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "resume": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "status": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "title": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "history": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "export": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "clear": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "background": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "tasks": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "queue": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "steer": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "stop": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "approve": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "skills": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "skill": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "copy": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "retry": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "usage": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
        "exit": lambda cli, arg, command: cli._legacy_handle_command(command, arg),
    }
```

- [ ] **Step 6: Split dispatch from legacy implementation**

In `agent_cli/repl.py`, add `self.command_handlers` in `__init__` after state fields:

```python
from agent_cli.command_handlers import build_command_handlers

self.command_handlers = build_command_handlers(self)
```

Rename the current long `handle_command()` body to `_legacy_handle_command(self, command, arg)`, removing the initial sanitize/split/resolve/dynamic-skill block. Keep all `if command.name == ...` branches inside `_legacy_handle_command`.

Replace `handle_command()` with:

```python
def handle_command(self, raw: str) -> str | None:
    raw = sanitize_terminal_input(raw)
    parts = raw.strip().split(maxsplit=1)
    token = parts[0] if parts else ""
    command = resolve_command(token)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command is None:
        dynamic = self.skill_commands_provider()
        skill_name = token.lstrip("/") if token else ""
        if skill_name in dynamic:
            from agent_cli.skill_commands import (
                build_skill_invocation_message,
                load_skill_for_command,
            )

            loader = self.skill_loader or load_skill_for_command
            loaded = loader(dynamic[skill_name])
            message = build_skill_invocation_message(loaded, arg)
            return self.submit_message(message)
        return f"Unknown command: {token if token else raw}"

    handler = self.command_handlers.get(command.effective_handler_key)
    if handler is None:
        return f"Unhandled command: /{command.name}"
    return handler(self, arg, command)
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit registry foundation**

```bash
git add agent_cli/commands.py agent_cli/repl.py agent_cli/command_handlers/__init__.py tests/test_agent_cli_commands.py tests/test_agent_cli_repl.py
git commit -m "refactor: add agent cli command handler registry"
```

---

### Task 2: Move Built-In Handlers Into Domain Modules

**Files:**
- Modify: `agent_cli/command_handlers/__init__.py`
- Create: `agent_cli/command_handlers/session.py`
- Create: `agent_cli/command_handlers/background.py`
- Create: `agent_cli/command_handlers/skills.py`
- Create: `agent_cli/command_handlers/debug.py`
- Create: `agent_cli/command_handlers/clipboard.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Add a test that legacy dispatch is gone**

Add to `tests/test_agent_cli_repl.py`:

```python
def test_agent_cli_no_long_legacy_command_dispatch():
    assert not hasattr(AgentCLI, "_legacy_handle_command")
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_agent_cli_no_long_legacy_command_dispatch -q
```

Expected: FAIL while `_legacy_handle_command` still exists.

- [ ] **Step 3: Create session handlers**

Create `agent_cli/command_handlers/session.py` with functions copied from the existing branches for `new`, `sessions`, `resume`, `status`, `title`, `history`, `export`, `clear`, `retry`, and `usage`. Use this exact registry shape:

```python
from __future__ import annotations

import os

from agent_cli.rendering import format_sessions
from agent_cli.session import (
    Session,
    export_to_markdown,
    render_history,
    render_session_status,
    update_session_title,
)
from agent_cli.text_input import render_usage_summary


def session_handlers():
    return {
        "new": handle_new,
        "sessions": handle_sessions,
        "resume": handle_resume,
        "status": handle_status,
        "title": handle_title,
        "history": handle_history,
        "export": handle_export,
        "clear": handle_clear,
        "retry": handle_retry,
        "usage": handle_usage,
    }
```

Each handler should call the same `cli` methods and state updates currently used in `repl.py`. For repeated session construction, call a new helper on `AgentCLI` named `_set_session(session_id: str) -> None`.

- [ ] **Step 4: Add AgentCLI session helper**

In `agent_cli/repl.py`, add:

```python
def _set_session(self, session_id: str) -> None:
    self.session_id = session_id
    self.session = Session(
        session_store=self.session_store,
        session_id=self.session_id,
        model_name=self.model_name,
        session_store_for_checkpoints=self.checkpointer,
        workdir=self.workdir,
        profile=self.profile,
        display_theme=self.display_theme,
        cli_home=self.cli_home,
        db_path=str(self.session_store.db_path)
        if hasattr(self.session_store, "db_path")
        else None,
    )
```

Update `ensure_session()` and existing session creation sites to use `_set_session()`.

- [ ] **Step 5: Create background, skills, debug, and clipboard modules**

Move existing private method behavior into:

```python
# agent_cli/command_handlers/background.py
def background_handlers():
    return {
        "background": handle_background,
        "tasks": handle_tasks,
        "queue": handle_queue,
        "steer": handle_steer,
        "stop": handle_stop,
        "approve": handle_approve,
    }
```

```python
# agent_cli/command_handlers/skills.py
def skills_handlers():
    return {"skills": handle_skills, "skill": handle_skill}
```

```python
# agent_cli/command_handlers/debug.py
def debug_handlers():
    return {"help": handle_help, "doctor": handle_doctor, "exit": handle_exit}
```

```python
# agent_cli/command_handlers/clipboard.py
def clipboard_handlers():
    return {"copy": handle_copy}
```

Use existing implementation details from `AgentCLI._handle_*`, `_render_skills`, and `_render_skill`. Keep helper methods only when they are still used by multiple handlers.

- [ ] **Step 6: Build registry from modules**

Update `agent_cli/command_handlers/__init__.py`:

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
    from agent_cli.repl import AgentCLI

CommandHandler = Callable[["AgentCLI", str, CommandDef], str | None]


def build_command_handlers(cli: "AgentCLI") -> dict[str, CommandHandler]:
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

- [ ] **Step 7: Remove legacy command body**

Delete `_legacy_handle_command()` from `agent_cli/repl.py`. Keep reusable non-dispatch helpers such as `_require_background_registry`, `_effective_cli_home`, `_capture_usage_metadata`, `_handle_interrupts`, `_drain_background_notifications`, and `_stop_active_background_tasks_on_exit`.

- [ ] **Step 8: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py tests/test_agent_cli_commands.py -q
```

Expected: PASS.

- [ ] **Step 9: Run full Agent CLI tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_*.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit handler module split**

```bash
git add agent_cli/repl.py agent_cli/command_handlers tests/test_agent_cli_repl.py
git commit -m "refactor: split agent cli command handlers"
```

---

### Task 3: Add Config Schema and File Helpers

**Files:**
- Create: `agent_cli/config_schema.py`
- Modify: `agent_cli/config.py`
- Create: `agent_cli/config.example.yaml`
- Test: `tests/test_agent_cli_config.py`
- Modify: `tests/test_agent_cli_main.py`
- Modify: `tests/test_agent_cli_doctor.py`

- [ ] **Step 1: Create failing config schema tests**

Create `tests/test_agent_cli_config.py`:

```python
import pytest

from agent_cli.config_schema import (
    ConfigValidationError,
    config_to_dict,
    get_config_path_value,
    parse_config,
    set_config_path_value,
)


def test_parse_config_defaults():
    config = parse_config({})

    assert config.display.markdown == "render"
    assert config.display.theme == "default"
    assert config.model.name is None
    assert config.session.default_title == "New session"


def test_parse_config_reports_path_for_invalid_markdown():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"display": {"markdown": "weird"}})

    assert exc.value.path == "display.markdown"
    assert "raw" in exc.value.message
    assert "render" in exc.value.message
    assert "strip" in exc.value.message


def test_parse_config_rejects_unknown_path():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"unknown": True})

    assert exc.value.path == "unknown"


def test_get_and_set_config_path_value():
    data = {}
    updated = set_config_path_value(data, "display.markdown", "strip")
    config = parse_config(updated)

    assert config.display.markdown == "strip"
    assert get_config_path_value(config, "display.markdown") == "strip"
    assert config_to_dict(config)["display"]["markdown"] == "strip"
```

- [ ] **Step 2: Run schema tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_config.py -q
```

Expected: FAIL because `agent_cli.config_schema` does not exist.

- [ ] **Step 3: Implement config schema**

Create `agent_cli/config_schema.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_cli.theme import SUPPORTED_THEMES

DISPLAY_MARKDOWN_VALUES = {"render", "strip", "raw"}
DISPLAY_THEME_VALUES = set(SUPPORTED_THEMES)


@dataclass(frozen=True)
class ConfigValidationError(ValueError):
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class DisplayConfig:
    markdown: str = "render"
    theme: str = "default"


@dataclass(frozen=True)
class ModelConfig:
    name: str | None = None


@dataclass(frozen=True)
class SessionConfig:
    default_title: str = "New session"


@dataclass(frozen=True)
class AgentCLIConfig:
    display: DisplayConfig = DisplayConfig()
    model: ModelConfig = ModelConfig()
    session: SessionConfig = SessionConfig()


ALLOWED_TOP_LEVEL = {"display", "model", "session"}
ALLOWED_CHILDREN = {
    "display": {"markdown", "theme"},
    "model": {"name"},
    "session": {"default_title"},
}


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigValidationError(path, "must be a mapping")
    return value


def _reject_unknown(data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key not in ALLOWED_TOP_LEVEL:
            raise ConfigValidationError(str(key), "unknown config key")
        section = _mapping(value, str(key))
        for child in section:
            if child not in ALLOWED_CHILDREN[key]:
                raise ConfigValidationError(f"{key}.{child}", "unknown config key")


def parse_config(data: dict[str, Any]) -> AgentCLIConfig:
    _reject_unknown(data)
    display = _mapping(data.get("display"), "display")
    model = _mapping(data.get("model"), "model")
    session = _mapping(data.get("session"), "session")

    markdown = str(display.get("markdown") or "render")
    if markdown not in DISPLAY_MARKDOWN_VALUES:
        allowed = ", ".join(sorted(DISPLAY_MARKDOWN_VALUES))
        raise ConfigValidationError("display.markdown", f"must be one of: {allowed}")

    raw_theme = display.get("theme", "default")
    theme = "default" if raw_theme is None else str(raw_theme)
    if theme not in DISPLAY_THEME_VALUES:
        allowed = ", ".join(sorted(DISPLAY_THEME_VALUES))
        raise ConfigValidationError("display.theme", f"must be one of: {allowed}")

    model_name = model.get("name")
    if model_name is not None:
        model_name = str(model_name)

    default_title = str(session.get("default_title") or "New session").strip()
    if not default_title:
        raise ConfigValidationError("session.default_title", "must not be empty")

    return AgentCLIConfig(
        display=DisplayConfig(markdown=markdown, theme=theme),
        model=ModelConfig(name=model_name),
        session=SessionConfig(default_title=default_title),
    )


def config_to_dict(config: AgentCLIConfig) -> dict[str, Any]:
    return {
        "display": {
            "markdown": config.display.markdown,
            "theme": config.display.theme,
        },
        "model": {"name": config.model.name},
        "session": {"default_title": config.session.default_title},
    }


def get_config_path_value(config: AgentCLIConfig, path: str) -> Any:
    if path == "display.markdown":
        return config.display.markdown
    if path == "display.theme":
        return config.display.theme
    if path == "model.name":
        return config.model.name
    if path == "session.default_title":
        return config.session.default_title
    raise ConfigValidationError(path, "unknown config key")


def set_config_path_value(data: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    if path not in {
        "display.markdown",
        "display.theme",
        "model.name",
        "session.default_title",
    }:
        raise ConfigValidationError(path, "unknown config key")
    section, key = path.split(".", 1)
    updated = {name: dict(raw) if isinstance(raw, dict) else raw for name, raw in data.items()}
    section_data = updated.get(section)
    if section_data is None:
        section_data = {}
    if not isinstance(section_data, dict):
        raise ConfigValidationError(section, "must be a mapping")
    section_data[key] = value
    updated[section] = section_data
    parse_config(updated)
    return updated
```

- [ ] **Step 4: Update config.py to use schema**

In `agent_cli/config.py`, import:

```python
from agent_cli.config_schema import (
    DISPLAY_MARKDOWN_VALUES,
    DISPLAY_THEME_VALUES,
    ConfigValidationError,
    config_to_dict,
    get_config_path_value,
    parse_config,
    set_config_path_value,
)
```

Remove the existing local `DISPLAY_MARKDOWN_VALUES` and `DISPLAY_THEME_VALUES` assignments from `agent_cli/config.py` after importing them from `config_schema`. Update `settings_from_config()` to call `parse_config(config)` and map fields from the schema object. Catch `ConfigValidationError` and raise `ConfigError(str(exc))`.

Add helpers:

```python
def load_normalized_config(cli_home: Path):
    config_path = cli_home / "config.yaml"
    raw = load_config_file(config_path)
    return parse_config(raw), raw, config_path


def save_config_file(path: Path, data: dict[str, Any]) -> None:
    if yaml is None:
        raise ConfigError("PyYAML is required to write config.yaml.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
```

- [ ] **Step 5: Add config example**

Create `agent_cli/config.example.yaml`:

```yaml
display:
  markdown: render
  theme: default
model:
  name: null
session:
  default_title: New session
```

- [ ] **Step 6: Run focused config tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_config.py tests/test_agent_cli_main.py::test_main_invalid_display_markdown_returns_code_2 tests/test_agent_cli_doctor.py::test_doctor_fails_invalid_display_theme -q
```

Expected: PASS.

- [ ] **Step 7: Commit schema**

```bash
git add agent_cli/config.py agent_cli/config_schema.py agent_cli/config.example.yaml tests/test_agent_cli_config.py tests/test_agent_cli_main.py tests/test_agent_cli_doctor.py
git commit -m "feat: add agent cli config schema"
```

---

### Task 4: Add Config CLI Commands and REPL Reload

**Files:**
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/commands.py`
- Modify: `agent_cli/repl.py`
- Modify: `agent_cli/command_handlers/debug.py`
- Modify: `agent_cli/config.py`
- Test: `tests/test_agent_cli_main.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing top-level config command tests**

Add to `tests/test_agent_cli_main.py`:

```python
def test_main_config_set_and_get_round_trip(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module

    set_code = main_module.main(["config", "set", "display.markdown", "strip"])
    get_code = main_module.main(["config", "get", "display.markdown"])

    captured = capsys.readouterr()
    assert set_code == 0
    assert get_code == 0
    assert "strip" in captured.out


def test_main_config_set_rejects_unknown_path(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module

    code = main_module.main(["config", "set", "unknown.path", "value"])

    captured = capsys.readouterr()
    assert code == 2
    assert "unknown.path" in captured.err
```

- [ ] **Step 2: Write failing reload test**

Add to `tests/test_agent_cli_repl.py`:

```python
def test_reload_updates_runtime_settings_and_clears_agent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "display:\n  markdown: strip\n  theme: slate\n"
        "model:\n  name: new-model\n"
        "session:\n  default_title: Reloaded\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_CLI_HOME", str(home))

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir=str(tmp_path),
        model_name="old-model",
        cli_home=str(home),
        display_theme="default",
    )
    cli._agent = "old-agent"

    output = cli.handle_command("/reload")

    assert "Reloaded" in output
    assert cli.model_name == "new-model"
    assert cli.default_title == "Reloaded"
    assert cli.display_theme == "slate"
    assert cli.display_markdown == "strip"
    assert cli._agent is None
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_main_config_set_and_get_round_trip tests/test_agent_cli_main.py::test_main_config_set_rejects_unknown_path tests/test_agent_cli_repl.py::test_reload_updates_runtime_settings_and_clears_agent -q
```

Expected: FAIL because `config` subcommands and `/reload` do not exist.

- [ ] **Step 4: Add argparse config subcommands**

In `agent_cli/main.py`, add:

```python
config_parser = subparsers.add_parser(
    "config",
    help="Show or edit CLI config.",
    parents=[public_options],
)
config_subparsers = config_parser.add_subparsers(dest="config_command")
config_subparsers.add_parser("show", help="Show normalized CLI config.")
config_get = config_subparsers.add_parser("get", help="Get a config value.")
config_get.add_argument("path")
config_set = config_subparsers.add_parser("set", help="Set a config value.")
config_set.add_argument("path")
config_set.add_argument("value")
```

Before checkpointer creation, handle `command == "config"` by loading `cli_home`, reading/writing config, printing output, and returning `0` or `2`.

- [ ] **Step 5: Add config render/set helpers**

In `agent_cli/config.py`, add:

```python
def parse_config_value(raw: str) -> Any:
    if raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    return raw


def render_config_show(config) -> str:
    data = config_to_dict(config)
    if yaml is None:
        lines = []
        for section, values in data.items():
            lines.append(f"{section}:")
            for key, value in values.items():
                lines.append(f"  {key}: {value}")
        return "\n".join(lines)
    return yaml.safe_dump(data, sort_keys=True).rstrip()
```

- [ ] **Step 6: Add reload command metadata and handler**

In `agent_cli/commands.py`, add:

```python
CommandDef("reload", "Reload dotenv and CLI config.", "Info")
```

In `agent_cli/command_handlers/debug.py`, add `reload` to `debug_handlers()` and implement:

```python
def handle_reload(cli, arg, command):
    if arg:
        return "Usage: /reload"
    return cli.reload_runtime_settings()
```

In `agent_cli/repl.py`, add constructor args `display_markdown: str = "render"` and `dotenv_module: Any | None = None`. Store `self.display_markdown` and `self.dotenv_module`. Then implement:

```python
def reload_runtime_settings(self) -> str:
    from pathlib import Path

    import agent_cli.config as config_module

    cli_home = Path(self._effective_cli_home())
    old = (
        self.model_name,
        self.default_title,
        self.display_theme,
        self.display_markdown,
    )
    try:
        config_module.load_dotenv_files(
            cli_home=cli_home,
            project_root=Path(self.workdir),
            dotenv_module=self.dotenv_module,
        )
        settings = config_module.settings_from_config(
            cli_home=cli_home,
            profile=self.profile,
            cli_model=None,
        )
    except Exception as exc:
        (
            self.model_name,
            self.default_title,
            self.display_theme,
            self.display_markdown,
        ) = old
        return f"Reload failed: {exc}"

    model_changed = settings.model_name != self.model_name
    self.model_name = settings.model_name
    self.default_title = settings.default_title
    self.display_theme = settings.display_theme
    self.display_markdown = settings.display_markdown
    if model_changed:
        self._agent = None
    return "Reloaded config and dotenv."
```

- [ ] **Step 7: Pass display markdown from main**

In `agent_cli/main.py`, pass `display_markdown=settings.display_markdown` and `dotenv_module=dotenv` to `AgentCLI`.

- [ ] **Step 8: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_main_config_set_and_get_round_trip tests/test_agent_cli_main.py::test_main_config_set_rejects_unknown_path tests/test_agent_cli_repl.py::test_reload_updates_runtime_settings_and_clears_agent tests/test_agent_cli_commands.py::test_every_builtin_command_has_handler_key -q
```

Expected: PASS.

- [ ] **Step 9: Commit config commands**

```bash
git add agent_cli/main.py agent_cli/commands.py agent_cli/repl.py agent_cli/command_handlers/debug.py agent_cli/config.py tests/test_agent_cli_main.py tests/test_agent_cli_repl.py
git commit -m "feat: add agent cli config commands and reload"
```

---

### Task 5: Apply display.markdown to Live Assistant Output and Doctor

**Files:**
- Create: `agent_cli/output_format.py`
- Modify: `agent_cli/repl.py`
- Modify: `agent_cli/doctor.py`
- Modify: `README.md`
- Test: `tests/test_agent_cli_repl.py`
- Test: `tests/test_agent_cli_doctor.py`

- [ ] **Step 1: Write failing output formatting test**

Add to `tests/test_agent_cli_repl.py`:

```python
def test_display_markdown_strip_formats_live_assistant_output():
    def fake_runner(agent, input_data, config):
        return {"messages": [{"role": "assistant", "content": "**hello** `world`"}]}

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name="model",
        display_markdown="strip",
    )

    assert cli.submit_message("hi") == "hello world"
```

- [ ] **Step 2: Write failing doctor example/schema test**

Add to `tests/test_agent_cli_doctor.py`:

```python
def test_doctor_reports_config_schema_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("display:\n  markdown: weird\n", encoding="utf-8")

    from agent_cli.doctor import run_health_checks

    results = run_health_checks(workdir=str(tmp_path), cli_home=home)
    config = next(result for result in results if result.name == "Config")

    assert config.status == "FAIL"
    assert "display.markdown" in config.message
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_display_markdown_strip_formats_live_assistant_output tests/test_agent_cli_doctor.py::test_doctor_reports_config_schema_path -q
```

Expected: FAIL because live output formatting and path-specific doctor output are incomplete.

- [ ] **Step 4: Implement output formatting**

Create `agent_cli/output_format.py`:

```python
from __future__ import annotations

import re


def strip_markdown(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    return text


def format_assistant_output(text: str, mode: str) -> str:
    if mode == "strip":
        return strip_markdown(text)
    if mode in {"render", "raw"}:
        return text
    return text
```

In `agent_cli/repl.py`, import `format_assistant_output` and change:

```python
output = latest_ai_text(result)
if output:
    self.assistant_replies.append(output)
return output
```

to:

```python
output = latest_ai_text(result)
if output:
    self.assistant_replies.append(output)
    return format_assistant_output(output, self.display_markdown)
return output
```

- [ ] **Step 5: Update doctor config check**

In `agent_cli/doctor.py`, ensure config validation catches `ConfigError` or `ConfigValidationError` and formats the message with the path included. If current code already catches `ConfigError`, make sure `ConfigError(str(exc))` from `settings_from_config()` preserves path text such as `display.markdown:`.

Add a doctor check for `agent_cli/config.example.yaml` existence, returning OK when present and WARN if missing.

- [ ] **Step 6: Update README**

In `README.md`, add concise documentation for:

```text
agent_cli config show
agent_cli config get display.theme
agent_cli config set display.markdown strip
/reload
display.markdown: render | strip | raw
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_display_markdown_strip_formats_live_assistant_output tests/test_agent_cli_doctor.py::test_doctor_reports_config_schema_path tests/test_agent_cli_doctor.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit output formatting and doctor docs**

```bash
git add agent_cli/output_format.py agent_cli/repl.py agent_cli/doctor.py README.md tests/test_agent_cli_repl.py tests/test_agent_cli_doctor.py
git commit -m "feat: apply agent cli display markdown config"
```

---

### Task 6: Improve Background Task Detail and Tail UX

**Files:**
- Modify: `agent_cli/commands.py`
- Modify: `agent_cli/background.py`
- Modify: `agent_cli/command_handlers/background.py`
- Modify: `agent_cli/repl.py`
- Modify: `README.md`
- Test: `tests/test_agent_cli_background.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing store test for steer retrieval**

Add to `tests/test_agent_cli_background.py`:

```python
def test_background_store_lists_recent_steers(tmp_path):
    from agent_cli.background import BackgroundTaskStore

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_1",
        session_id="s1",
        title="Task",
        prompt_preview="Prompt",
        owner_id="owner",
    )
    first = store.add_steer("bg_1", "first")
    second = store.add_steer("bg_1", "second")

    steers = store.get_steers("bg_1", limit=1)

    assert [item.id for item in steers] == [second.id]
    assert steers[0].message == "second"
```

- [ ] **Step 2: Write failing REPL tests for tasks detail and tail**

Add to `tests/test_agent_cli_repl.py`:

```python
def test_tasks_with_task_id_renders_detail_and_resume_hint():
    registry = FakeBackgroundRegistry()
    record = registry.start("do work")
    record.status = "completed"
    record.last_result_preview = "done"
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    output = cli.handle_command(f"/tasks {record.task_id}")

    assert record.task_id in output
    assert "completed" in output
    assert f"/resume {record.session_id}" in output


def test_tail_renders_result_error_and_steers():
    registry = FakeBackgroundRegistry()
    record = registry.start("do work")
    record.last_result_preview = "latest result"
    record.last_error = "latest error"
    registry.steer(record.task_id, "extra instruction")
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    output = cli.handle_command(f"/tail {record.task_id}")

    assert "latest result" in output
    assert "latest error" in output
    assert "extra instruction" in output
```

Update `FakeBackgroundRegistry` in `tests/test_agent_cli_repl.py` with:

```python
def get_task(self, task_id):
    return self.records.get(task_id)

def get_steers(self, task_id, limit=20):
    return [
        SimpleNamespace(
            id=index + 1,
            task_id=task_id,
            message=message,
            status="pending",
            created_at="now",
            consumed_at=None,
        )
        for index, (stored_task_id, message) in enumerate(self.steers)
        if stored_task_id == task_id
    ][-limit:]
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_background.py::test_background_store_lists_recent_steers tests/test_agent_cli_repl.py::test_tasks_with_task_id_renders_detail_and_resume_hint tests/test_agent_cli_repl.py::test_tail_renders_result_error_and_steers -q
```

Expected: FAIL because `get_steers`, `/tasks <id>`, and `/tail` are missing.

- [ ] **Step 4: Add tail command metadata**

In `agent_cli/commands.py`, add:

```python
CommandDef(
    "tail",
    "Show recent background task result, error, and steer messages.",
    "Background",
    args_hint="<task_id>",
)
```

- [ ] **Step 5: Add background store and registry helpers**

In `agent_cli/background.py`, add to `BackgroundTaskStore`:

```python
def get_steers(self, task_id: str, *, limit: int = 20) -> list[BackgroundSteerRecord]:
    with self.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, task_id, message, status, created_at, consumed_at
            FROM cli_background_steers
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
    return [self._steer_from_row(row) for row in reversed(rows)]
```

The existing `_steer_from_row()` helper already maps `id, task_id, message, status, created_at, consumed_at` into `BackgroundSteerRecord`; use that helper rather than duplicating row mapping.

Add to `BackgroundTaskRegistry`:

```python
def get_task(self, task_id: str) -> BackgroundTaskRecord | None:
    return self.store.get_task(task_id)


def get_steers(self, task_id: str, *, limit: int = 20) -> list[BackgroundSteerRecord]:
    return self.store.get_steers(task_id, limit=limit)
```

- [ ] **Step 6: Implement task detail and tail handlers**

In `agent_cli/command_handlers/background.py`, change `handle_tasks` to parse args:

```python
def handle_tasks(cli, arg, command):
    arg = arg.strip()
    if arg == "all":
        return render_task_list(cli._require_background_registry().list_tasks(active_only=False, limit=100))
    if arg:
        return render_task_detail(cli, arg)
    return render_task_list(cli._require_background_registry().list_tasks(active_only=False, limit=20))
```

Add:

```python
def handle_tail(cli, arg, command):
    task_id = arg.strip()
    if not task_id:
        return "Usage: /tail <task_id>"
    registry = cli._require_background_registry()
    record = registry.get_task(task_id)
    if record is None:
        return f"Unknown background task: {task_id}"
    steers = registry.get_steers(task_id, limit=20)
    lines = [f"Tail for {record.task_id}:"]
    lines.append(f"  Result: {record.last_result_preview or '-'}")
    lines.append(f"  Error: {record.last_error or '-'}")
    lines.append("  Steers:")
    if not steers:
        lines.append("    -")
    for steer in steers:
        lines.append(f"    {steer.id} {steer.status} {steer.message}")
    return "\n".join(lines)
```

Register `"tail": handle_tail`.

- [ ] **Step 7: Improve notifications and exit summary**

In `agent_cli/repl.py`, update `_drain_background_notifications()` message formatting:

```python
if item.status == "completed":
    action = f"resume with /resume {item.session_id}"
elif item.status == "failed":
    action = f"inspect with /tasks {item.task_id}"
else:
    action = ""
suffix = f" · {action}" if action else ""
print(f"[background {item.kind}] {item.task_id} · {item.status} · session {item.session_id}{suffix}")
```

Update `_stop_active_background_tasks_on_exit()` to print each stop request and a final `Inspect with /tasks all` line when it touched any task.

- [ ] **Step 8: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_background.py::test_background_store_lists_recent_steers tests/test_agent_cli_repl.py::test_tasks_with_task_id_renders_detail_and_resume_hint tests/test_agent_cli_repl.py::test_tail_renders_result_error_and_steers -q
```

Expected: PASS.

- [ ] **Step 9: Update README**

Document:

```text
/tasks
/tasks all
/tasks <task_id>
/tail <task_id>
Background completion messages include /resume <session_id>.
```

- [ ] **Step 10: Run full Agent CLI tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_*.py -q
```

Expected: PASS.

- [ ] **Step 11: Commit background UX**

```bash
git add agent_cli/commands.py agent_cli/background.py agent_cli/command_handlers/background.py agent_cli/repl.py README.md tests/test_agent_cli_background.py tests/test_agent_cli_repl.py
git commit -m "feat: improve agent cli background task ux"
```

---

### Task 7: Final Verification

**Files:**
- No planned code changes unless verification exposes an issue.

- [ ] **Step 1: Run full Agent CLI test subset**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_*.py -q
```

Expected: PASS.

- [ ] **Step 2: Run smoke commands with isolated CLI home**

Run:

```bash
AGENT_CLI_HOME=/tmp/agent-cli-command-config-bg-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli --help
AGENT_CLI_HOME=/tmp/agent-cli-command-config-bg-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli config show
AGENT_CLI_HOME=/tmp/agent-cli-command-config-bg-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli config set display.markdown strip
AGENT_CLI_HOME=/tmp/agent-cli-command-config-bg-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli config get display.markdown
AGENT_CLI_HOME=/tmp/agent-cli-command-config-bg-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli sessions
AGENT_CLI_HOME=/tmp/agent-cli-command-config-bg-smoke /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli doctor --workdir /home/miku/projects/langchain
```

Expected:

- `--help` exits `0` and lists `config`.
- `config show` exits `0`.
- `config get display.markdown` prints `strip`.
- `sessions` exits `0`.
- `doctor` exits `0` or `1` depending on WARN/FAIL environment checks, and never prints a traceback.

- [ ] **Step 3: Inspect git status**

Run:

```bash
git status --short
```

Expected: Only intentional project changes are staged or committed. Preserve unrelated user changes already present in the main worktree.

- [ ] **Step 4: Commit final docs or fixes if needed**

If verification required a small fix, commit it:

If verification changed README only, run:

```bash
git add README.md
git commit -m "docs: finalize agent cli command config background ux"
```

If verification changed tests or implementation, stage the exact files reported by `git status --short` that belong to this feature and commit:

```bash
git commit -m "fix: finalize agent cli command config background ux"
```

If no changes were needed, do not create an empty commit.

---

## Review Checklist

- `AgentCLI.handle_command()` is a short dispatcher.
- Command metadata in `commands.py` drives help, completion, and dispatch.
- Dynamic skill fallback still happens only when built-ins do not resolve.
- `agent_cli config show|get|set` works without creating a checkpointer.
- Invalid config reports stable schema paths.
- `/reload` preserves old runtime values on failure.
- `display.markdown=strip` changes live assistant output only.
- `/tasks`, `/tasks all`, `/tasks <task_id>`, and `/tail <task_id>` are covered by tests.
- No daemon or long-lived external background worker was introduced.

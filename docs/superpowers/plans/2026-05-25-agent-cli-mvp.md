# Agent CLI MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a minimal `python -m agent_cli` CLI that runs the existing LangGraph/LangChain agent with SQLite checkpoint persistence, lightweight session metadata, slash commands, and basic approve/reject interrupt handling.

**Architecture:** Replace the copied Hermes CLI runtime path with small project-native modules. LangGraph checkpointing stores conversation state keyed by `configurable.thread_id`; a CLI-owned SQLite table stores session list metadata only. Agent execution goes through `agent_core.agent_runner.invoke_agent_with_terminal_notifications`.

**Tech Stack:** Python stdlib `argparse`, `sqlite3`, `dataclasses`, `uuid`; existing `agent_core` and `agent_tools`; LangGraph SQLite checkpointer when installed.

---

## File Structure

Create or replace these runtime files:

- `agent_cli/__init__.py`: package marker and version string.
- `agent_cli/__main__.py`: `python -m agent_cli` entrypoint.
- `agent_cli/main.py`: argparse command dispatch.
- `agent_cli/repl.py`: plain terminal chat loop and `ask` flow.
- `agent_cli/commands.py`: minimal slash command registry.
- `agent_cli/session_store.py`: `cli_sessions` metadata table.
- `agent_cli/checkpoints.py`: SQLite checkpointer construction.
- `agent_cli/interrupts.py`: interrupt extraction and approve/reject resume values.
- `agent_cli/rendering.py`: small formatting helpers.
- `agent_cli/paths.py`: CLI home and database path resolution.

Move current Hermes-derived files into:

- `agent_cli/_hermes_reference/`

Modify:

- `agent_core/builders.py`: accept and forward optional `checkpointer`.

Create tests:

- `tests/test_agent_cli_commands.py`
- `tests/test_agent_cli_paths.py`
- `tests/test_agent_cli_session_store.py`
- `tests/test_agent_cli_interrupts.py`
- `tests/test_agent_cli_main.py`
- `tests/test_agent_cli_builders.py`

---

### Task 1: Isolate Hermes Reference Files

**Files:**
- Move: `agent_cli/banner.py` -> `agent_cli/_hermes_reference/banner.py`
- Move: `agent_cli/cli.py` -> `agent_cli/_hermes_reference/cli.py`
- Move: `agent_cli/config.py` -> `agent_cli/_hermes_reference/config.py`
- Move: `agent_cli/main.py` -> `agent_cli/_hermes_reference/main.py`
- Move: `agent_cli/session.py` -> `agent_cli/_hermes_reference/session.py`
- Move: `agent_cli/session_context.py` -> `agent_cli/_hermes_reference/session_context.py`
- Move: `agent_cli/skill_commands.py` -> `agent_cli/_hermes_reference/skill_commands.py`
- Move: `agent_cli/skin_engine.py` -> `agent_cli/_hermes_reference/skin_engine.py`
- Create: `agent_cli/_hermes_reference/__init__.py`

- [ ] **Step 1: Move the copied Hermes files out of the runtime path**

Run:

```bash
mkdir -p agent_cli/_hermes_reference
git mv agent_cli/banner.py agent_cli/_hermes_reference/banner.py
git mv agent_cli/cli.py agent_cli/_hermes_reference/cli.py
git mv agent_cli/config.py agent_cli/_hermes_reference/config.py
git mv agent_cli/main.py agent_cli/_hermes_reference/main.py
git mv agent_cli/session.py agent_cli/_hermes_reference/session.py
git mv agent_cli/session_context.py agent_cli/_hermes_reference/session_context.py
git mv agent_cli/skill_commands.py agent_cli/_hermes_reference/skill_commands.py
git mv agent_cli/skin_engine.py agent_cli/_hermes_reference/skin_engine.py
```

Expected: commands succeed and `agent_cli/` no longer contains the Hermes runtime files at top level.

- [ ] **Step 2: Add a package marker for the reference folder**

Create `agent_cli/_hermes_reference/__init__.py`:

```python
"""Copied Hermes CLI source kept as reference material only.

Modules in this package are not imported by the Agent CLI MVP runtime.
"""
```

- [ ] **Step 3: Verify runtime path is clean**

Run:

```bash
find agent_cli -maxdepth 1 -type f -printf '%f\n' | sort
```

Expected output includes no Hermes files except package files created in later tasks.

- [ ] **Step 4: Commit**

```bash
git add agent_cli
git commit -m "refactor: isolate hermes cli reference files"
```

---

### Task 2: Add Package Entrypoints and Path Helpers

**Files:**
- Create: `agent_cli/__init__.py`
- Create: `agent_cli/__main__.py`
- Create: `agent_cli/paths.py`
- Test: `tests/test_agent_cli_paths.py`

- [ ] **Step 1: Write failing path tests**

Create `tests/test_agent_cli_paths.py`:

```python
from pathlib import Path


def test_get_cli_home_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path / "custom-home"))

    from agent_cli.paths import get_cli_home

    assert get_cli_home() == (tmp_path / "custom-home").resolve()


def test_get_cli_home_defaults_to_user_home(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CLI_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from agent_cli.paths import get_cli_home

    assert get_cli_home() == tmp_path / ".langchain-agent"


def test_get_db_path_points_under_cli_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.paths import get_db_path

    assert get_db_path() == tmp_path.resolve() / "cli.sqlite"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_agent_cli_paths.py -q
```

Expected: FAIL because `agent_cli.paths` does not exist.

- [ ] **Step 3: Implement package files and paths**

Create `agent_cli/__init__.py`:

```python
"""Minimal CLI for the project LangGraph agent."""

__version__ = "0.1.0"
```

Create `agent_cli/__main__.py`:

```python
from agent_cli.main import main


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `agent_cli/paths.py`:

```python
from __future__ import annotations

import os
from pathlib import Path


DEFAULT_HOME_NAME = ".langchain-agent"
DB_FILENAME = "cli.sqlite"


def get_cli_home() -> Path:
    configured = os.getenv("AGENT_CLI_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / DEFAULT_HOME_NAME


def ensure_cli_home() -> Path:
    home = get_cli_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_db_path() -> Path:
    return get_cli_home() / DB_FILENAME


def ensure_db_parent() -> Path:
    home = ensure_cli_home()
    return home / DB_FILENAME
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_agent_cli_paths.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/__init__.py agent_cli/__main__.py agent_cli/paths.py tests/test_agent_cli_paths.py
git commit -m "feat: add agent cli package paths"
```

---

### Task 3: Implement Minimal Command Registry

**Files:**
- Create: `agent_cli/commands.py`
- Test: `tests/test_agent_cli_commands.py`

- [ ] **Step 1: Write failing command tests**

Create `tests/test_agent_cli_commands.py`:

```python
from agent_cli.commands import COMMANDS_BY_CATEGORY, resolve_command, render_help


def test_resolve_command_handles_slash_and_alias():
    assert resolve_command("/help").name == "help"
    assert resolve_command("quit").name == "exit"
    assert resolve_command("/q").name == "exit"


def test_resolve_command_returns_none_for_unknown():
    assert resolve_command("/missing") is None


def test_render_help_lists_core_commands():
    help_text = render_help()
    assert "/help" in help_text
    assert "/sessions" in help_text
    assert "/resume <session_id>" in help_text


def test_commands_group_by_category():
    assert "Session" in COMMANDS_BY_CATEGORY
    assert any(cmd.name == "new" for cmd in COMMANDS_BY_CATEGORY["Session"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_agent_cli_commands.py -q
```

Expected: FAIL because `agent_cli.commands` does not exist.

- [ ] **Step 3: Implement command registry**

Create `agent_cli/commands.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CommandDef:
    name: str
    description: str
    category: str
    aliases: tuple[str, ...] = ()
    args_hint: str = ""

    @property
    def usage(self) -> str:
        suffix = f" {self.args_hint}" if self.args_hint else ""
        return f"/{self.name}{suffix}"


COMMAND_REGISTRY: tuple[CommandDef, ...] = (
    CommandDef("help", "Show available commands.", "Info", aliases=("h",)),
    CommandDef("new", "Start a new session.", "Session"),
    CommandDef("sessions", "List recent sessions.", "Session", aliases=("ls",)),
    CommandDef("resume", "Resume a session.", "Session", args_hint="<session_id>"),
    CommandDef("clear", "Clear the terminal screen.", "Session"),
    CommandDef("skills", "List available local skills.", "Skills"),
    CommandDef("skill", "Show a skill summary.", "Skills", args_hint="<name>"),
    CommandDef("exit", "Exit the CLI.", "Exit", aliases=("quit", "q")),
)


def _names_for(command: CommandDef) -> Iterable[str]:
    yield command.name
    for alias in command.aliases:
        yield alias


COMMAND_LOOKUP: dict[str, CommandDef] = {
    name: command
    for command in COMMAND_REGISTRY
    for name in _names_for(command)
}

COMMANDS_BY_CATEGORY: dict[str, list[CommandDef]] = {}
for _command in COMMAND_REGISTRY:
    COMMANDS_BY_CATEGORY.setdefault(_command.category, []).append(_command)


def normalize_command_name(raw: str) -> str:
    return raw.strip().split(maxsplit=1)[0].lstrip("/").lower()


def resolve_command(raw: str) -> CommandDef | None:
    if not raw.strip():
        return None
    return COMMAND_LOOKUP.get(normalize_command_name(raw))


def render_help() -> str:
    lines = ["Available commands:"]
    for category, commands in COMMANDS_BY_CATEGORY.items():
        lines.append("")
        lines.append(f"{category}:")
        for command in commands:
            lines.append(f"  {command.usage:<24} {command.description}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_agent_cli_commands.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/commands.py tests/test_agent_cli_commands.py
git commit -m "feat: add minimal cli command registry"
```

---

### Task 4: Implement Session Metadata Store

**Files:**
- Create: `agent_cli/session_store.py`
- Test: `tests/test_agent_cli_session_store.py`

- [ ] **Step 1: Write failing session store tests**

Create `tests/test_agent_cli_session_store.py`:

```python
from agent_cli.session_store import SessionStore


def test_create_session_persists_metadata(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")

    session = store.create_session(workdir="/repo", model="test-model", title="Hello")

    loaded = store.get_session(session.session_id)
    assert loaded is not None
    assert loaded.session_id == session.session_id
    assert loaded.title == "Hello"
    assert loaded.workdir == "/repo"
    assert loaded.model == "test-model"
    assert loaded.status == "active"


def test_list_sessions_orders_by_updated_at(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    first = store.create_session(workdir="/repo", model=None, title="First")
    second = store.create_session(workdir="/repo", model=None, title="Second")
    store.touch_session(first.session_id, last_message_preview="later")

    sessions = store.list_sessions(limit=10)

    assert [item.session_id for item in sessions[:2]] == [first.session_id, second.session_id]


def test_resume_unknown_session_returns_none(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")

    assert store.get_session("missing") is None


def test_title_from_message_truncates_and_normalizes_whitespace():
    assert SessionStore.title_from_message("  hello\\nworld  ", max_length=20) == "hello world"
    assert SessionStore.title_from_message("x" * 80, max_length=10) == "xxxxxxx..."
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_agent_cli_session_store.py -q
```

Expected: FAIL because `agent_cli.session_store` does not exist.

- [ ] **Step 3: Implement session store**

Create `agent_cli/session_store.py`:

```python
from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS cli_sessions (
  session_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  workdir TEXT NOT NULL,
  model TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  last_message_preview TEXT
);
"""


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    title: str
    created_at: str
    updated_at: str
    workdir: str
    model: str | None
    status: str
    last_message_preview: str | None


class SessionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def new_session_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"{stamp}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def title_from_message(message: str, max_length: int = 60) -> str:
        normalized = re.sub(r"\s+", " ", message).strip()
        if not normalized:
            return "New session"
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3].rstrip() + "..."

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(SCHEMA)

    @staticmethod
    def _record_from_row(row: sqlite3.Row | tuple) -> SessionRecord:
        return SessionRecord(
            session_id=row[0],
            title=row[1],
            created_at=row[2],
            updated_at=row[3],
            workdir=row[4],
            model=row[5],
            status=row[6],
            last_message_preview=row[7],
        )

    def create_session(
        self,
        *,
        workdir: str,
        model: str | None,
        title: str = "New session",
        session_id: str | None = None,
    ) -> SessionRecord:
        now = self.now()
        sid = session_id or self.new_session_id()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cli_sessions (
                    session_id, title, created_at, updated_at,
                    workdir, model, status, last_message_preview
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', NULL)
                """,
                (sid, title, now, now, workdir, model),
            )
        record = self.get_session(sid)
        if record is None:
            raise RuntimeError(f"failed to create session {sid}")
        return record

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, title, created_at, updated_at,
                       workdir, model, status, last_message_preview
                FROM cli_sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._record_from_row(row) if row else None

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, title, created_at, updated_at,
                       workdir, model, status, last_message_preview
                FROM cli_sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def touch_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        last_message_preview: str | None = None,
    ) -> None:
        existing = self.get_session(session_id)
        if existing is None:
            raise ValueError(f"unknown session: {session_id}")
        new_title = title if title is not None else existing.title
        preview = (
            last_message_preview
            if last_message_preview is not None
            else existing.last_message_preview
        )
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE cli_sessions
                SET title = ?, updated_at = ?, last_message_preview = ?
                WHERE session_id = ?
                """,
                (new_title, self.now(), preview, session_id),
            )
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_agent_cli_session_store.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/session_store.py tests/test_agent_cli_session_store.py
git commit -m "feat: add cli session metadata store"
```

---

### Task 5: Add SQLite Checkpoint Adapter

**Files:**
- Create: `agent_cli/checkpoints.py`
- Test: `tests/test_agent_cli_checkpoints.py`

- [ ] **Step 1: Write failing checkpoint tests**

Create `tests/test_agent_cli_checkpoints.py`:

```python
import sys
import types

import pytest


def test_create_sqlite_checkpointer_uses_langgraph_factory(monkeypatch, tmp_path):
    calls = []

    class FakeSaver:
        @classmethod
        def from_conn_string(cls, conn_string):
            calls.append(conn_string)
            return "checkpointer"

    sqlite_mod = types.ModuleType("langgraph.checkpoint.sqlite")
    sqlite_mod.SqliteSaver = FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph", types.ModuleType("langgraph"))
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint", types.ModuleType("langgraph.checkpoint"))
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", sqlite_mod)

    from agent_cli.checkpoints import create_sqlite_checkpointer

    result = create_sqlite_checkpointer(tmp_path / "cli.sqlite")

    assert result == "checkpointer"
    assert calls == [str(tmp_path / "cli.sqlite")]


def test_create_sqlite_checkpointer_reports_missing_dependency(monkeypatch, tmp_path):
    for name in [
        "langgraph.checkpoint.sqlite",
        "langgraph.checkpoint",
        "langgraph",
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    from agent_cli.checkpoints import CheckpointDependencyError, create_sqlite_checkpointer

    with pytest.raises(CheckpointDependencyError) as exc:
        create_sqlite_checkpointer(tmp_path / "cli.sqlite")

    assert "langgraph-checkpoint-sqlite" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_agent_cli_checkpoints.py -q
```

Expected: FAIL because `agent_cli.checkpoints` does not exist.

- [ ] **Step 3: Implement checkpoint adapter**

Create `agent_cli/checkpoints.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any


class CheckpointDependencyError(RuntimeError):
    """Raised when the LangGraph SQLite checkpointer is unavailable."""


def create_sqlite_checkpointer(db_path: str | Path) -> Any:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ModuleNotFoundError as exc:
        raise CheckpointDependencyError(
            "SQLite checkpointing requires the LangGraph SQLite package. "
            "Install langgraph-checkpoint-sqlite in the project environment."
        ) from exc

    factory = getattr(SqliteSaver, "from_conn_string", None)
    if callable(factory):
        return factory(str(path))
    return SqliteSaver(str(path))
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_agent_cli_checkpoints.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/checkpoints.py tests/test_agent_cli_checkpoints.py
git commit -m "feat: add langgraph sqlite checkpoint adapter"
```

---

### Task 6: Allow Agent Builder Checkpointer Injection

**Files:**
- Modify: `agent_core/builders.py`
- Test: `tests/test_agent_cli_builders.py`

- [ ] **Step 1: Write failing builder test**

Create `tests/test_agent_cli_builders.py`:

```python
import importlib
import sys
import types


def test_build_agent_forwards_checkpointer(monkeypatch):
    captured = {}

    fake_langchain = types.ModuleType("langchain")
    fake_agents = types.ModuleType("langchain.agents")
    fake_middleware = types.ModuleType("langchain.agents.middleware")

    class FakeMiddleware:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return "agent"

    fake_agents.create_agent = fake_create_agent
    fake_middleware.SummarizationMiddleware = FakeMiddleware
    fake_middleware.TodoListMiddleware = FakeMiddleware
    fake_middleware.ModelRetryMiddleware = FakeMiddleware
    fake_middleware.ToolRetryMiddleware = FakeMiddleware
    fake_middleware.ModelCallLimitMiddleware = FakeMiddleware

    monkeypatch.setitem(sys.modules, "langchain", fake_langchain)
    monkeypatch.setitem(sys.modules, "langchain.agents", fake_agents)
    monkeypatch.setitem(sys.modules, "langchain.agents.middleware", fake_middleware)

    fake_delegation = types.ModuleType("agent_core.delegation")
    fake_delegation.BASE_TOOLS = []
    fake_delegation.task = object()
    monkeypatch.setitem(sys.modules, "agent_core.delegation", fake_delegation)

    fake_human_loop = types.ModuleType("agent_core.human_loop")
    fake_human_loop.FlexibleHumanInTheLoopMiddleware = FakeMiddleware
    monkeypatch.setitem(sys.modules, "agent_core.human_loop", fake_human_loop)

    fake_memory = types.ModuleType("agent_core.memory")
    fake_memory.memory_store = types.SimpleNamespace(
        load_from_disk=lambda: None,
        format_for_system_prompt=lambda name: "",
    )
    monkeypatch.setitem(sys.modules, "agent_core.memory", fake_memory)

    fake_model_config = types.ModuleType("agent_core.model_config")
    fake_model_config.MAIN_MODEL = "main-model"
    fake_model_config.SMALL_MODEL = "small-model"
    monkeypatch.setitem(sys.modules, "agent_core.model_config", fake_model_config)

    fake_policy = types.ModuleType("agent_core.policy_tool_middleware")
    fake_policy.PolicyToolMiddleware = FakeMiddleware
    monkeypatch.setitem(sys.modules, "agent_core.policy_tool_middleware", fake_policy)

    fake_process = types.ModuleType("agent_core.process_lifecycle")
    fake_process.install_process_signal_handlers = lambda: None
    monkeypatch.setitem(sys.modules, "agent_core.process_lifecycle", fake_process)

    fake_system_prompt = types.ModuleType("agent_core.system_prompt")
    fake_system_prompt.SystemPromptBuilder = lambda: types.SimpleNamespace(
        build_parent=lambda context: "system"
    )
    fake_system_prompt.build_prompt_context = lambda **kwargs: kwargs
    fake_system_prompt.load_project_instruction_blocks = lambda workdir: []
    fake_system_prompt.model_display_name = lambda model: str(model)
    monkeypatch.setitem(sys.modules, "agent_core.system_prompt", fake_system_prompt)

    fake_terminal = types.ModuleType("agent_core.terminal_lifecycle")
    fake_terminal.recover_terminal_processes = lambda: None
    monkeypatch.setitem(sys.modules, "agent_core.terminal_lifecycle", fake_terminal)

    fake_limits = types.ModuleType("agent_core.tool_limits")
    fake_limits.build_tool_call_limit_middleware = lambda include_task: []
    monkeypatch.setitem(sys.modules, "agent_core.tool_limits", fake_limits)

    fake_workspace = types.ModuleType("agent_core.workspace")
    fake_workspace.WORKDIR = "/repo"
    monkeypatch.setitem(sys.modules, "agent_core.workspace", fake_workspace)

    fake_memory_tool = types.ModuleType("agent_tools.public.memory")
    fake_memory_tool.memory_manage = object()
    monkeypatch.setitem(sys.modules, "agent_tools.public.memory", fake_memory_tool)

    sys.modules.pop("agent_core.builders", None)
    builders = importlib.import_module("agent_core.builders")

    assert builders.build_agent(checkpointer="checkpoint") == "agent"
    assert captured["checkpointer"] == "checkpoint"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_agent_cli_builders.py::test_build_agent_forwards_checkpointer -q
```

Expected: FAIL because `build_agent()` does not accept `checkpointer`.

- [ ] **Step 3: Modify builder signature and create_agent call**

In `agent_core/builders.py`, change:

```python
def build_agent(*, include_cron_tools: bool = False):
```

to:

```python
def build_agent(*, include_cron_tools: bool = False, checkpointer=None):
```

Change the `create_agent` call to include:

```python
        checkpointer=checkpointer,
```

The tail of the function should look like:

```python
    return create_agent(
        model=MAIN_MODEL,
        system_prompt=SystemPromptBuilder().build_parent(prompt_context),
        middleware=[
            SummarizationMiddleware(
                model=SMALL_MODEL,
                trigger=[("messages", 50), ("tokens", 30000)],
                keep=("messages", 20),
                trim_tokens_to_summarize=12000,
            ),
            TodoListMiddleware(
                system_prompt=TODO_SYSTEM_PROMPT,
                tool_description=TODO_TOOL_DESCRIPTION,
            ),
            *build_tool_call_limit_middleware(include_task=True),
            FlexibleHumanInTheLoopMiddleware(
                interrupt_on=HUMAN_INTERRUPT_ON,
                policy_tools=POLICY_REVIEW_TOOLS,
                description_prefix="Approval required before tool execution",
            ),
            PolicyToolMiddleware(policy_tools=POLICY_REVIEW_TOOLS),
            ToolRetryMiddleware(max_retries=3),
            ModelRetryMiddleware(max_retries=2),
        ],
        tools=tools,
        checkpointer=checkpointer,
    )
```

- [ ] **Step 4: Run builder test**

Run:

```bash
pytest tests/test_agent_cli_builders.py::test_build_agent_forwards_checkpointer -q
```

Expected: PASS.

- [ ] **Step 5: Run existing cron builder tests**

Run:

```bash
pytest tests/test_cronjob_tool.py::test_build_agent_excludes_cronjob_by_default tests/test_cronjob_tool.py::test_build_agent_can_include_cronjob -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_core/builders.py tests/test_agent_cli_builders.py
git commit -m "feat: allow agent builder checkpoint injection"
```

---

### Task 7: Implement Interrupt Mapping

**Files:**
- Create: `agent_cli/interrupts.py`
- Test: `tests/test_agent_cli_interrupts.py`

- [ ] **Step 1: Write failing interrupt tests**

Create `tests/test_agent_cli_interrupts.py`:

```python
from agent_cli.interrupts import (
    build_resume_value,
    extract_interrupt_requests,
    has_interrupt,
)


def test_has_interrupt_detects_key():
    assert has_interrupt({"__interrupt__": [{"value": {"action_requests": []}}]})
    assert not has_interrupt({"messages": []})


def test_extract_interrupt_requests_reads_action_requests():
    result = {
        "__interrupt__": [
            {
                "value": {
                    "action_requests": [
                        {"name": "terminal", "args": {"command": "rm file"}}
                    ]
                }
            }
        ]
    }

    requests = extract_interrupt_requests(result)

    assert requests == [{"name": "terminal", "args": {"command": "rm file"}}]


def test_extract_interrupt_requests_handles_unknown_shape():
    assert extract_interrupt_requests({"__interrupt__": ["raw"]}) == [{"raw": "raw"}]


def test_build_resume_value_approves_all_requests():
    resume = build_resume_value(approved=True, request_count=2)

    assert resume == {
        "decisions": [
            {"type": "approve"},
            {"type": "approve"},
        ]
    }


def test_build_resume_value_rejects_all_requests():
    resume = build_resume_value(approved=False, request_count=1)

    assert resume == {
        "decisions": [
            {"type": "reject", "message": "Rejected by user."},
        ]
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_agent_cli_interrupts.py -q
```

Expected: FAIL because `agent_cli.interrupts` does not exist.

- [ ] **Step 3: Implement interrupts module**

Create `agent_cli/interrupts.py`:

```python
from __future__ import annotations

from typing import Any


INTERRUPT_KEY = "__interrupt__"


def has_interrupt(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get(INTERRUPT_KEY))


def _unwrap_interrupt_value(item: Any) -> Any:
    if isinstance(item, dict) and "value" in item:
        return item["value"]
    return item


def extract_interrupt_requests(result: Any) -> list[dict[str, Any]]:
    if not has_interrupt(result):
        return []
    raw_items = result.get(INTERRUPT_KEY) or []
    if not isinstance(raw_items, list):
        raw_items = [raw_items]

    requests: list[dict[str, Any]] = []
    for item in raw_items:
        value = _unwrap_interrupt_value(item)
        if isinstance(value, dict):
            action_requests = value.get("action_requests")
            if isinstance(action_requests, list):
                for request in action_requests:
                    if isinstance(request, dict):
                        requests.append(request)
                    else:
                        requests.append({"raw": request})
                continue
            requests.append(value)
        else:
            requests.append({"raw": value})
    return requests or [{"raw": raw_items}]


def build_resume_value(*, approved: bool, request_count: int) -> dict[str, list[dict[str, str]]]:
    count = max(1, request_count)
    if approved:
        return {"decisions": [{"type": "approve"} for _ in range(count)]}
    return {
        "decisions": [
            {"type": "reject", "message": "Rejected by user."}
            for _ in range(count)
        ]
    }
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_agent_cli_interrupts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/interrupts.py tests/test_agent_cli_interrupts.py
git commit -m "feat: add cli interrupt approve reject mapping"
```

---

### Task 8: Implement Rendering Helpers

**Files:**
- Create: `agent_cli/rendering.py`
- Test: extend `tests/test_agent_cli_interrupts.py`

- [ ] **Step 1: Add failing rendering tests**

Append to `tests/test_agent_cli_interrupts.py`:

```python
from agent_cli.rendering import format_interrupt_summary, latest_ai_text


def test_format_interrupt_summary_includes_tool_preview():
    summary = format_interrupt_summary([
        {"name": "terminal", "args": {"command": "pytest tests"}}
    ])

    assert "Approval required" in summary
    assert "terminal" in summary
    assert "pytest tests" in summary


def test_latest_ai_text_reads_last_ai_message():
    result = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    }

    assert latest_ai_text(result) == "hello"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_agent_cli_interrupts.py -q
```

Expected: FAIL because `agent_cli.rendering` does not exist.

- [ ] **Step 3: Implement rendering module**

Create `agent_cli/rendering.py`:

```python
from __future__ import annotations

from typing import Any


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or message.get("type") or "")
    return str(getattr(message, "type", "") or getattr(message, "role", ""))


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def latest_ai_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result) if result is not None else ""
    messages = result.get("messages") or []
    for message in reversed(messages):
        role = _message_role(message)
        if role in {"assistant", "ai"} or message.__class__.__name__ == "AIMessage":
            return _message_content(message)
    final = result.get("final_response")
    return str(final) if final else ""


def format_sessions(rows: list[Any]) -> str:
    if not rows:
        return "No sessions found."
    lines = ["Recent sessions:"]
    for row in rows:
        preview = f" - {row.last_message_preview}" if row.last_message_preview else ""
        lines.append(f"{row.session_id}  {row.updated_at}  {row.title}{preview}")
    return "\n".join(lines)


def _request_preview(request: dict[str, Any]) -> str:
    name = str(request.get("name") or request.get("tool") or "request")
    args = request.get("args")
    if isinstance(args, dict):
        for key in ("command", "path", "query"):
            if key in args and args[key]:
                return f"{name}: {args[key]}"
    return f"{name}: {request}"


def format_interrupt_summary(requests: list[dict[str, Any]]) -> str:
    lines = ["Approval required:"]
    for idx, request in enumerate(requests, start=1):
        lines.append(f"  {idx}. {_request_preview(request)}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_agent_cli_interrupts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/rendering.py tests/test_agent_cli_interrupts.py
git commit -m "feat: add cli rendering helpers"
```

---

### Task 9: Implement REPL Core

**Files:**
- Create: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing REPL tests**

Create `tests/test_agent_cli_repl.py`:

```python
from types import SimpleNamespace

from agent_cli.repl import AgentCLI


class FakeStore:
    def __init__(self):
        self.created = []
        self.touched = []
        self.sessions = {}

    def create_session(self, *, workdir, model, title="New session", session_id=None):
        sid = session_id or f"s{len(self.created) + 1}"
        record = SimpleNamespace(
            session_id=sid,
            title=title,
            workdir=workdir,
            model=model,
            updated_at="now",
            last_message_preview=None,
        )
        self.created.append(record)
        self.sessions[sid] = record
        return record

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def list_sessions(self, limit=20):
        return list(self.sessions.values())

    def touch_session(self, session_id, **kwargs):
        self.touched.append((session_id, kwargs))


def test_submit_message_invokes_runner_with_thread_id(monkeypatch):
    calls = []

    def fake_runner(agent, input_data, config):
        calls.append((agent, input_data, config))
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name="model",
    )

    output = cli.submit_message("hello")

    assert output == "ok"
    assert calls[0][0] == "agent"
    assert calls[0][1] == {"messages": [{"role": "user", "content": "hello"}]}
    assert calls[0][2] == {"configurable": {"thread_id": "s1"}}


def test_handle_command_new_switches_session():
    store = FakeStore()
    cli = AgentCLI(
        session_store=store,
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    result = cli.handle_command("/new")

    assert "Started session" in result
    assert cli.session_id == "s1"


def test_handle_command_resume_rejects_unknown_session():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    assert cli.handle_command("/resume missing") == "Unknown session: missing"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_agent_cli_repl.py -q
```

Expected: FAIL because `agent_cli.repl` does not exist.

- [ ] **Step 3: Implement REPL core**

Create `agent_cli/repl.py`:

```python
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from agent_cli.commands import render_help, resolve_command
from agent_cli.interrupts import build_resume_value, extract_interrupt_requests, has_interrupt
from agent_cli.rendering import format_interrupt_summary, format_sessions, latest_ai_text
from agent_cli.session_store import SessionStore


AgentFactory = Callable[[Any], Any]
Runner = Callable[[Any, dict[str, Any], dict[str, Any]], Any]


class AgentCLI:
    def __init__(
        self,
        *,
        session_store: SessionStore,
        checkpointer: Any,
        agent_factory: AgentFactory,
        runner: Runner,
        workdir: str,
        model_name: str | None,
        session_id: str | None = None,
    ):
        self.session_store = session_store
        self.checkpointer = checkpointer
        self.agent_factory = agent_factory
        self.runner = runner
        self.workdir = workdir
        self.model_name = model_name
        self.session_id = session_id
        self._agent: Any | None = None

    @property
    def agent(self) -> Any:
        if self._agent is None:
            self._agent = self.agent_factory(self.checkpointer)
        return self._agent

    def ensure_session(self, first_message: str | None = None) -> str:
        if self.session_id:
            return self.session_id
        title = (
            self.session_store.title_from_message(first_message)
            if first_message
            else "New session"
        )
        record = self.session_store.create_session(
            workdir=self.workdir,
            model=self.model_name,
            title=title,
        )
        self.session_id = record.session_id
        return self.session_id

    def submit_message(self, text: str) -> str:
        session_id = self.ensure_session(text)
        self.session_store.touch_session(
            session_id,
            last_message_preview=self.session_store.title_from_message(text, max_length=80),
        )
        result = self.runner(
            self.agent,
            {"messages": [{"role": "user", "content": text}]},
            {"configurable": {"thread_id": session_id}},
        )
        result = self._handle_interrupts(result)
        return latest_ai_text(result)

    def _handle_interrupts(self, result: Any) -> Any:
        while has_interrupt(result):
            requests = extract_interrupt_requests(result)
            print(format_interrupt_summary(requests))
            answer = input("Approve? [y/N]: ").strip().lower()
            approved = answer in {"y", "yes"}
            resume_value = build_resume_value(
                approved=approved,
                request_count=len(requests),
            )
            try:
                from langgraph.types import Command
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Cannot resume interrupt because langgraph is not installed."
                ) from exc
            result = self.runner(
                self.agent,
                Command(resume=resume_value),
                {"configurable": {"thread_id": self.session_id}},
            )
        return result

    def handle_command(self, raw: str) -> str | None:
        parts = raw.strip().split(maxsplit=1)
        command = resolve_command(parts[0] if parts else "")
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command is None:
            return f"Unknown command: {parts[0] if parts else raw}"
        if command.name == "help":
            return render_help()
        if command.name == "new":
            record = self.session_store.create_session(
                workdir=self.workdir,
                model=self.model_name,
                title="New session",
            )
            self.session_id = record.session_id
            return f"Started session: {record.session_id}"
        if command.name == "sessions":
            return format_sessions(self.session_store.list_sessions())
        if command.name == "resume":
            if not arg:
                return "Usage: /resume <session_id>"
            record = self.session_store.get_session(arg)
            if record is None:
                return f"Unknown session: {arg}"
            self.session_id = record.session_id
            return f"Resumed session: {record.session_id}"
        if command.name == "clear":
            os.system("cls" if os.name == "nt" else "clear")
            return None
        if command.name == "skills":
            return self._render_skills()
        if command.name == "skill":
            return self._render_skill(arg)
        if command.name == "exit":
            raise EOFError
        return f"Unhandled command: /{command.name}"

    def _render_skills(self) -> str:
        from agent_tools.public.skills import _all_skills

        skills = _all_skills()
        if not skills:
            return "No skills found."
        return "\n".join(
            f"{item['name']} - {item.get('description') or ''}".rstrip()
            for item in skills
        )

    def _render_skill(self, name: str) -> str:
        if not name:
            return "Usage: /skill <name>"
        from agent_tools.public.skills import _find_skill

        meta = _find_skill(name)
        if not meta:
            return f"Skill not found: {name}"
        desc = meta.get("description") or ""
        return f"{meta['name']}\n{desc}".strip()

    def run_repl(self) -> int:
        self.ensure_session()
        print(f"Session: {self.session_id}")
        print("Type /help for commands. Ctrl-D exits.")
        while True:
            try:
                text = input("> ").strip()
            except EOFError:
                print()
                return 0
            except KeyboardInterrupt:
                print()
                continue
            if not text:
                continue
            try:
                if text.startswith("/"):
                    output = self.handle_command(text)
                else:
                    output = self.submit_message(text)
                if output:
                    print(output)
            except EOFError:
                return 0


def default_agent_factory(checkpointer: Any) -> Any:
    from agent_core.builders import build_agent

    return build_agent(checkpointer=checkpointer)


def default_runner(agent: Any, input_data: dict[str, Any], config: dict[str, Any]) -> Any:
    from agent_core.agent_runner import invoke_agent_with_terminal_notifications

    return invoke_agent_with_terminal_notifications(agent, input_data, config)
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/repl.py tests/test_agent_cli_repl.py
git commit -m "feat: add minimal agent cli repl"
```

---

### Task 10: Implement Argparse Main Entrypoint

**Files:**
- Create: `agent_cli/main.py`
- Test: `tests/test_agent_cli_main.py`

- [ ] **Step 1: Write failing main tests**

Create `tests/test_agent_cli_main.py`:

```python
from types import SimpleNamespace


def test_main_sessions_prints_sessions(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.session_store import SessionStore

    store = SessionStore(tmp_path / "cli.sqlite")
    store.create_session(workdir="/repo", model="m", title="Existing")

    from agent_cli.main import main

    code = main(["sessions"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Existing" in out


def test_main_ask_invokes_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    class FakeCLI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def submit_message(self, text):
            return f"answer: {text}"

    monkeypatch.setattr("agent_cli.main.AgentCLI", FakeCLI)
    monkeypatch.setattr("agent_cli.main.create_sqlite_checkpointer", lambda path: "cp")

    from agent_cli.main import main

    code = main(["ask", "hello"])

    assert code == 0
    assert "answer: hello" in capsys.readouterr().out


def test_main_chat_resume_rejects_unknown_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.main import main

    code = main(["chat", "--resume", "missing"])

    assert code == 2
    assert "Unknown session: missing" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_agent_cli_main.py -q
```

Expected: FAIL because `agent_cli.main` does not exist.

- [ ] **Step 3: Implement main module**

Create `agent_cli/main.py`:

```python
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import dotenv
except ModuleNotFoundError:
    dotenv = None

from agent_cli.checkpoints import CheckpointDependencyError, create_sqlite_checkpointer
from agent_cli.paths import ensure_db_parent
from agent_cli.rendering import format_sessions
from agent_cli.repl import AgentCLI, default_agent_factory, default_runner
from agent_cli.session_store import SessionStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_cli")
    parser.add_argument("--workdir", default=None, help="Workspace directory for the agent.")
    parser.add_argument("--model", default=None, help="Model display metadata for session list.")

    subparsers = parser.add_subparsers(dest="command")

    chat = subparsers.add_parser("chat", help="Start interactive chat.")
    chat.add_argument("--resume", default=None, help="Resume an existing session id.")

    ask = subparsers.add_parser("ask", help="Ask one question and exit.")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--resume", default=None, help="Resume an existing session id.")

    subparsers.add_parser("sessions", help="List recent sessions.")
    return parser


def load_dotenv() -> None:
    if dotenv is None:
        return
    dotenv.load_dotenv(Path.cwd() / ".env")


def make_cli(*, args: argparse.Namespace, store: SessionStore, checkpointer) -> AgentCLI:
    workdir = str(Path(args.workdir).expanduser().resolve()) if args.workdir else os.getcwd()
    return AgentCLI(
        session_store=store,
        checkpointer=checkpointer,
        agent_factory=default_agent_factory,
        runner=default_runner,
        workdir=workdir,
        model_name=args.model,
        session_id=getattr(args, "resume", None),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "chat"
    load_dotenv()

    db_path = ensure_db_parent()
    store = SessionStore(db_path)

    if command == "sessions":
        print(format_sessions(store.list_sessions()))
        return 0

    resume_id = getattr(args, "resume", None)
    if resume_id and store.get_session(resume_id) is None:
        print(f"Unknown session: {resume_id}", file=sys.stderr)
        return 2

    try:
        checkpointer = create_sqlite_checkpointer(db_path)
    except CheckpointDependencyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cli = make_cli(args=args, store=store, checkpointer=checkpointer)

    if command == "ask":
        text = " ".join(args.question)
        output = cli.submit_message(text)
        if output:
            print(output)
        return 0

    return cli.run_repl()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run main tests**

Run:

```bash
pytest tests/test_agent_cli_main.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/main.py tests/test_agent_cli_main.py
git commit -m "feat: add agent cli argparse entrypoint"
```

---

### Task 11: Add End-to-End Smoke Tests

**Files:**
- Modify: `tests/test_agent_cli_main.py`

- [ ] **Step 1: Add smoke tests**

Append to `tests/test_agent_cli_main.py`:

```python
import subprocess
import sys


def test_python_module_help_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "agent_cli", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout


def test_python_module_sessions_smoke(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "agent_cli", "sessions"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "AGENT_CLI_HOME": str(tmp_path)},
    )

    assert result.returncode == 0
    assert "No sessions found." in result.stdout
```

- [ ] **Step 2: Fix missing import**

At the top of `tests/test_agent_cli_main.py`, add:

```python
import os
```

- [ ] **Step 3: Run smoke tests**

Run:

```bash
pytest tests/test_agent_cli_main.py::test_python_module_help_smoke tests/test_agent_cli_main.py::test_python_module_sessions_smoke -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_agent_cli_main.py
git commit -m "test: add agent cli module smoke tests"
```

---

### Task 12: Run Focused Verification and Update README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add README usage section**

Append this section to `README.md`:

```markdown

## Agent CLI

This repository includes a minimal local CLI for the LangGraph agent:

```bash
python -m agent_cli
python -m agent_cli ask "Summarize this repository"
python -m agent_cli sessions
python -m agent_cli chat --resume <session_id>
```

The CLI stores LangGraph checkpoints and lightweight session metadata in
`~/.langchain-agent/cli.sqlite` by default. Set `AGENT_CLI_HOME` to place this
state elsewhere.

Inside chat, use `/help` to list slash commands. The MVP supports basic
approve/reject prompts for human-in-the-loop interrupts.
```

- [ ] **Step 2: Run all new CLI tests**

Run:

```bash
pytest \
  tests/test_agent_cli_paths.py \
  tests/test_agent_cli_commands.py \
  tests/test_agent_cli_session_store.py \
  tests/test_agent_cli_checkpoints.py \
  tests/test_agent_cli_interrupts.py \
  tests/test_agent_cli_repl.py \
  tests/test_agent_cli_main.py \
  tests/test_agent_cli_builders.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run related existing tests**

Run:

```bash
pytest \
  tests/test_agent_runner.py \
  tests/test_session_context.py \
  tests/test_cronjob_tool.py::test_build_agent_excludes_cronjob_by_default \
  tests/test_cronjob_tool.py::test_build_agent_can_include_cronjob \
  -q
```

Expected: PASS.

- [ ] **Step 4: Run import and help smoke commands**

Run:

```bash
python -m agent_cli --help
AGENT_CLI_HOME="$(mktemp -d)" python -m agent_cli sessions
```

Expected: first command prints argparse help and exits 0; second command prints `No sessions found.` and exits 0.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: document minimal agent cli"
```

---

## Self-Review

Spec coverage:

- Minimal CLI entrypoints are covered in Tasks 2, 10, and 11.
- SQLite checkpoint plus metadata persistence is covered in Tasks 4 and 5.
- `build_agent(checkpointer=...)` integration is covered in Task 6.
- REPL, ask, and slash commands are covered in Tasks 3, 9, and 10.
- Basic approve/reject interrupt handling is covered in Tasks 7 and 9.
- Hermes reference isolation is covered in Task 1.
- Tests and usage documentation are covered in Tasks 11 and 12.

Placeholder scan:

- The plan contains no unresolved fill-in markers or unspecified implementation
  steps. The only `TODO` text is in existing code identifiers such as
  `TODO_SYSTEM_PROMPT`.
- Code-changing steps include concrete code blocks or exact edits.

Type consistency:

- `SessionStore`, `SessionRecord`, `AgentCLI`, `create_sqlite_checkpointer`,
  `build_resume_value`, and `latest_ai_text` are defined before they are used
  by later tasks.

# Agent CLI Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first usability layer on top of the minimal Agent CLI: PromptSession input, slash completion, status/title/history/export commands, doctor checks, and CLI logging.

**Architecture:** Keep the current small `agent_cli` package and add focused helper modules. LangGraph SQLite checkpoints remain the conversation state source; the CLI metadata table remains a lightweight index for session listing, title, workdir, model, and timestamps.

**Tech Stack:** Python 3.11, LangGraph checkpointing, SQLite, `prompt_toolkit`, pytest, existing `agent_cli` modules.

---

## File Structure

- Create `agent_cli/input.py`
  - Owns `prompt_toolkit.PromptSession` creation, persistent prompt history, slash command completion, session-id completion, skill-name completion, and export path completion.
- Create `agent_cli/history.py`
  - Owns checkpoint state lookup and extraction of user/assistant message turns.
- Create `agent_cli/doctor.py`
  - Owns non-interactive local health checks and text rendering.
- Create `agent_cli/logging.py`
  - Owns CLI file logging setup and helper logging calls.
- Modify `agent_cli/commands.py`
  - Add Phase 1 commands and completion metadata to `CommandDef`.
- Modify `agent_cli/session_store.py`
  - Add title update support if current `touch_session(title=...)` is not enough for direct command clarity.
- Modify `agent_cli/rendering.py`
  - Add status, history, and Markdown export rendering helpers.
- Modify `agent_cli/repl.py`
  - Use prompt adapter instead of `input()`, add `/status`, `/title`, `/history`, `/export`, and structured logging calls.
- Modify `agent_cli/main.py`
  - Add `doctor` subcommand and initialize logging during startup.
- Modify `README.md`
  - Document Phase 1 commands, `doctor`, history/export, and logs.
- Tests:
  - Add `tests/test_agent_cli_input.py`.
  - Add `tests/test_agent_cli_history.py`.
  - Add `tests/test_agent_cli_doctor.py`.
  - Add `tests/test_agent_cli_logging.py`.
  - Update `tests/test_agent_cli_commands.py`.
  - Update `tests/test_agent_cli_repl.py`.
  - Update `tests/test_agent_cli_main.py`.

Use the project interpreter for verification:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py tests/test_agent_cli_input.py tests/test_agent_cli_history.py tests/test_agent_cli_doctor.py tests/test_agent_cli_logging.py tests/test_agent_cli_repl.py tests/test_agent_cli_main.py -q
```

---

### Task 1: Extend Command Registry For Phase 1

**Files:**
- Modify: `agent_cli/commands.py`
- Test: `tests/test_agent_cli_commands.py`

- [ ] **Step 1: Write failing command registry tests**

Append these tests to `tests/test_agent_cli_commands.py`:

```python
from agent_cli.commands import commands_for_completion


def test_phase_1_commands_are_registered():
    help_text = render_help()

    assert "/status" in help_text
    assert "/title <name>" in help_text
    assert "/history" in help_text
    assert "/export <path.md>" in help_text


def test_commands_expose_completion_metadata():
    completion_names = [item.name for item in commands_for_completion()]

    assert "status" in completion_names
    assert "title" in completion_names
    assert "history" in completion_names
    assert "export" in completion_names
    assert resolve_command("/status").completion == "none"
    assert resolve_command("/resume").completion == "session"
    assert resolve_command("/skill").completion == "skill"
    assert resolve_command("/export").completion == "path"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py -q
```

Expected: FAIL because `commands_for_completion` and new commands do not exist.

- [ ] **Step 3: Implement command metadata**

Update `agent_cli/commands.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


CompletionKind = Literal["none", "session", "skill", "path"]


@dataclass(frozen=True)
class CommandDef:
    name: str
    description: str
    category: str
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    completion: CompletionKind = "none"

    @property
    def usage(self) -> str:
        suffix = f" {self.args_hint}" if self.args_hint else ""
        return f"/{self.name}{suffix}"
```

Replace `COMMAND_REGISTRY` with:

```python
COMMAND_REGISTRY: tuple[CommandDef, ...] = (
    CommandDef("help", "Show available commands.", "Info", aliases=("h",)),
    CommandDef("new", "Start a new session.", "Session"),
    CommandDef("sessions", "List recent sessions.", "Session", aliases=("ls",)),
    CommandDef(
        "resume",
        "Resume a session.",
        "Session",
        args_hint="<session_id>",
        completion="session",
    ),
    CommandDef("status", "Show current CLI session status.", "Session"),
    CommandDef(
        "title",
        "Set the current session title.",
        "Session",
        args_hint="<name>",
    ),
    CommandDef("history", "Show user and assistant messages.", "Session"),
    CommandDef(
        "export",
        "Export user and assistant messages to Markdown.",
        "Session",
        args_hint="<path.md>",
        completion="path",
    ),
    CommandDef("clear", "Clear the terminal screen.", "Session"),
    CommandDef("skills", "List available local skills.", "Skills"),
    CommandDef(
        "skill",
        "Show a skill summary.",
        "Skills",
        args_hint="<name>",
        completion="skill",
    ),
    CommandDef("exit", "Exit the CLI.", "Exit", aliases=("quit", "q")),
)
```

Add this helper near `render_help()`:

```python
def commands_for_completion() -> tuple[CommandDef, ...]:
    return COMMAND_REGISTRY
```

- [ ] **Step 4: Run command tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/commands.py tests/test_agent_cli_commands.py
git commit -m "feat: extend agent cli command registry"
```

---

### Task 2: Add PromptSession Input And Completion

**Files:**
- Create: `agent_cli/input.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_input.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing input tests**

Create `tests/test_agent_cli_input.py`:

```python
from pathlib import Path

from prompt_toolkit.document import Document

from agent_cli.input import SlashCommandCompleter, build_prompt_session
from agent_cli.session_store import SessionStore


def _completion_texts(completer, text):
    document = Document(text=text, cursor_position=len(text))
    return [item.text for item in completer.get_completions(document, None)]


def test_slash_command_completion_uses_registry(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    completer = SlashCommandCompleter(session_store=store, workdir=str(tmp_path))

    completions = _completion_texts(completer, "/sta")

    assert "status" in completions


def test_resume_completion_uses_session_store(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    record = store.create_session(workdir="/repo", model=None, title="Known")
    completer = SlashCommandCompleter(session_store=store, workdir=str(tmp_path))

    completions = _completion_texts(completer, "/resume " + record.session_id[:6])

    assert record.session_id in completions


def test_export_completion_lists_paths(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    (tmp_path / "exports").mkdir()
    completer = SlashCommandCompleter(session_store=store, workdir=str(tmp_path))

    completions = _completion_texts(completer, "/export exp")

    assert "exports/" in completions


def test_build_prompt_session_uses_file_history(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    session = build_prompt_session(
        history_path=tmp_path / "history.txt",
        session_store=store,
        workdir=str(tmp_path),
    )

    assert session is not None
    assert Path(tmp_path / "history.txt").parent.exists()
```

- [ ] **Step 2: Write failing REPL prompt adapter test**

Append this test to `tests/test_agent_cli_repl.py`:

```python
def test_run_repl_uses_prompt_adapter(monkeypatch, capsys):
    entries = iter(["/help", EOFError])
    prompts = []

    class FakePrompt:
        def prompt(self, prompt_text):
            prompts.append(prompt_text)
            entry = next(entries)
            if entry is EOFError:
                raise EOFError
            return entry

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
    )

    code = cli.run_repl()

    captured = capsys.readouterr()
    assert code == 0
    assert prompts == ["> ", "> "]
    assert "Available commands:" in captured.out
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_input.py tests/test_agent_cli_repl.py::test_run_repl_uses_prompt_adapter -q
```

Expected: FAIL because `agent_cli.input` and `prompt_session` injection do not exist.

- [ ] **Step 4: Implement `agent_cli/input.py`**

Create `agent_cli/input.py`:

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory

from agent_cli.commands import commands_for_completion, resolve_command
from agent_cli.session_store import SessionStore


class SlashCommandCompleter(Completer):
    def __init__(self, *, session_store: SessionStore, workdir: str):
        self.session_store = session_store
        self.workdir = Path(workdir)

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        parts = text.split(maxsplit=1)
        if len(parts) == 1 and not text.endswith(" "):
            word = parts[0][1:].lower()
            for command in commands_for_completion():
                names = (command.name, *command.aliases)
                for name in names:
                    if name.startswith(word):
                        yield Completion(
                            name,
                            start_position=-len(word),
                            display=f"/{name}",
                            display_meta=command.description,
                        )
            return

        command = resolve_command(parts[0])
        if command is None:
            return
        arg = parts[1] if len(parts) > 1 else ""
        if command.completion == "session":
            yield from self._session_completions(arg)
        elif command.completion == "skill":
            yield from self._skill_completions(arg)
        elif command.completion == "path":
            yield from self._path_completions(arg)

    def _session_completions(self, prefix: str) -> Iterable[Completion]:
        for record in self.session_store.list_sessions(limit=50):
            if record.session_id.startswith(prefix):
                yield Completion(
                    record.session_id,
                    start_position=-len(prefix),
                    display=record.session_id,
                    display_meta=record.title,
                )

    def _skill_completions(self, prefix: str) -> Iterable[Completion]:
        try:
            from agent_tools.public.skills import _all_skills
        except Exception:
            return
        prefix_lower = prefix.lower()
        for item in _all_skills():
            name = str(item.get("name") or "")
            if name.lower().startswith(prefix_lower):
                yield Completion(
                    name,
                    start_position=-len(prefix),
                    display=name,
                    display_meta=str(item.get("description") or ""),
                )

    def _path_completions(self, prefix: str) -> Iterable[Completion]:
        raw = prefix or "."
        expanded = Path(os.path.expanduser(raw))
        if not expanded.is_absolute():
            expanded = self.workdir / expanded
        directory = expanded if raw.endswith("/") else expanded.parent
        basename = "" if raw.endswith("/") else expanded.name
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries[:100]:
            if basename and not entry.name.startswith(basename):
                continue
            suffix = "/" if entry.is_dir() else ""
            text = entry.name + suffix
            yield Completion(text, start_position=-len(basename), display=text)


def build_prompt_session(
    *,
    history_path: str | Path,
    session_store: SessionStore,
    workdir: str,
) -> PromptSession:
    path = Path(history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=FileHistory(str(path)),
        completer=SlashCommandCompleter(session_store=session_store, workdir=workdir),
    )
```

- [ ] **Step 5: Modify `AgentCLI` to accept prompt sessions**

Update `agent_cli/repl.py`:

```python
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
        prompt_session: Any | None = None,
    ):
        self.session_store = session_store
        self.checkpointer = checkpointer
        self.agent_factory = agent_factory
        self.runner = runner
        self.workdir = workdir
        self.model_name = model_name
        self.session_id = session_id
        self.prompt_session = prompt_session
        self._agent: Any | None = None
```

Replace the prompt line inside `run_repl()`:

```python
text = self._prompt("> ").strip()
```

Add this method to `AgentCLI`:

```python
def _prompt(self, prompt_text: str) -> str:
    if self.prompt_session is None:
        from agent_cli.input import build_prompt_session
        from agent_cli.paths import ensure_cli_home

        self.prompt_session = build_prompt_session(
            history_path=ensure_cli_home() / "history.txt",
            session_store=self.session_store,
            workdir=self.workdir,
        )
    return self.prompt_session.prompt(prompt_text)
```

Keep the existing `EOFError` and `KeyboardInterrupt` handling in `run_repl()`.

- [ ] **Step 6: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_input.py tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_cli/input.py agent_cli/repl.py tests/test_agent_cli_input.py tests/test_agent_cli_repl.py
git commit -m "feat: add prompt session cli input"
```

---

### Task 3: Add Session Status And Title Commands

**Files:**
- Modify: `agent_cli/rendering.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing status/title tests**

Append these tests to `tests/test_agent_cli_repl.py`:

```python
def test_handle_command_status_shows_current_session_metadata():
    store = FakeStore()
    record = store.create_session(
        workdir="/repo",
        model="model-a",
        title="Known title",
        session_id="known",
    )
    record.created_at = "created"
    record.updated_at = "updated"
    record.last_message_preview = "preview"
    cli = AgentCLI(
        session_store=store,
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name="model-a",
        session_id="known",
    )

    result = cli.handle_command("/status")

    assert "Session: known" in result
    assert "Title: Known title" in result
    assert "Workdir: /repo" in result
    assert "Model: model-a" in result
    assert "Checkpointer: active" in result


def test_handle_command_title_updates_current_session():
    store = FakeStore()
    store.create_session(workdir="/repo", model=None, title="Old", session_id="s1")
    cli = AgentCLI(
        session_store=store,
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        session_id="s1",
    )

    result = cli.handle_command("/title New Name")

    assert result == "Title updated: New Name"
    assert store.touched == [("s1", {"title": "New Name"})]


def test_handle_command_title_requires_argument():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    assert cli.handle_command("/title") == "Usage: /title <name>"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_handle_command_status_shows_current_session_metadata tests/test_agent_cli_repl.py::test_handle_command_title_updates_current_session tests/test_agent_cli_repl.py::test_handle_command_title_requires_argument -q
```

Expected: FAIL because `/status` and `/title` are unhandled.

- [ ] **Step 3: Add status renderer**

Append to `agent_cli/rendering.py`:

```python
def format_status(
    *,
    session,
    workdir: str,
    model_name: str | None,
    cli_home,
    db_path,
    checkpointer_active: bool,
) -> str:
    if session is None:
        return "No active session."
    lines = [
        f"Session: {session.session_id}",
        f"Title: {session.title}",
        f"Created: {getattr(session, 'created_at', '?')}",
        f"Updated: {getattr(session, 'updated_at', '?')}",
        f"Last message: {session.last_message_preview or '-'}",
        f"Workdir: {workdir}",
        f"Model: {model_name or session.model or 'unknown'}",
        f"CLI home: {cli_home}",
        f"SQLite DB: {db_path}",
        f"Checkpointer: {'active' if checkpointer_active else 'inactive'}",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Implement `/status` and `/title`**

Update imports in `agent_cli/repl.py`:

```python
from agent_cli.paths import get_cli_home, get_db_path
from agent_cli.rendering import (
    format_interrupt_summary,
    format_sessions,
    format_status,
    latest_ai_text,
)
```

Add to `handle_command()` after `/resume`:

```python
if command.name == "status":
    session_id = self.ensure_session()
    return format_status(
        session=self.session_store.get_session(session_id),
        workdir=self.workdir,
        model_name=self.model_name,
        cli_home=get_cli_home(),
        db_path=get_db_path(),
        checkpointer_active=self.checkpointer is not None,
    )
if command.name == "title":
    if not arg:
        return "Usage: /title <name>"
    session_id = self.ensure_session()
    self.session_store.touch_session(session_id, title=arg)
    return f"Title updated: {arg}"
```

- [ ] **Step 5: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/rendering.py agent_cli/repl.py tests/test_agent_cli_repl.py
git commit -m "feat: add cli status and title commands"
```

---

### Task 4: Add Checkpoint-Backed History Extraction

**Files:**
- Create: `agent_cli/history.py`
- Modify: `agent_cli/rendering.py`
- Test: `tests/test_agent_cli_history.py`

- [ ] **Step 1: Write failing history tests**

Create `tests/test_agent_cli_history.py`:

```python
from dataclasses import dataclass

from agent_cli.history import ConversationTurn, extract_user_assistant_turns, load_thread_messages
from agent_cli.rendering import format_history, render_history_markdown


@dataclass
class Message:
    type: str
    content: object


class FakeCheckpointer:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def get_tuple(self, config):
        self.calls.append(config)
        return self.state


def test_extract_user_assistant_turns_filters_internal_messages():
    messages = [
        {"role": "system", "content": "hidden"},
        {"role": "user", "content": "hello"},
        {"role": "tool", "content": "tool result"},
        {"role": "assistant", "content": "hi"},
        Message(type="human", content=[{"text": "second"}]),
        Message(type="ai", content="answer"),
    ]

    turns = extract_user_assistant_turns(messages)

    assert turns == [
        ConversationTurn(role="User", content="hello"),
        ConversationTurn(role="Assistant", content="hi"),
        ConversationTurn(role="User", content="second"),
        ConversationTurn(role="Assistant", content="answer"),
    ]


def test_load_thread_messages_supports_checkpoint_tuple_channel_values():
    state = type("State", (), {"checkpoint": {"channel_values": {"messages": [{"role": "user", "content": "hello"}]}}})()
    checkpointer = FakeCheckpointer(state)

    messages = load_thread_messages(checkpointer, "thread-1")

    assert messages == [{"role": "user", "content": "hello"}]
    assert checkpointer.calls == [{"configurable": {"thread_id": "thread-1"}}]


def test_format_history_renders_turns():
    text = format_history(
        [
            ConversationTurn(role="User", content="hello"),
            ConversationTurn(role="Assistant", content="hi"),
        ]
    )

    assert "User:\nhello" in text
    assert "Assistant:\nhi" in text


def test_render_history_markdown_includes_metadata():
    markdown = render_history_markdown(
        session_id="s1",
        workdir="/repo",
        model_name="model",
        exported_at="2026-05-26T00:00:00+00:00",
        turns=[ConversationTurn(role="User", content="hello")],
    )

    assert markdown.startswith("# Session s1")
    assert "- Workdir: /repo" in markdown
    assert "## User\n\nhello" in markdown
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_history.py -q
```

Expected: FAIL because `agent_cli.history`, `format_history`, and `render_history_markdown` do not exist.

- [ ] **Step 3: Implement `agent_cli/history.py`**

Create `agent_cli/history.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_cli.rendering import _content_text


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    content: str


def load_thread_messages(checkpointer: Any, thread_id: str) -> list[Any]:
    config = {"configurable": {"thread_id": thread_id}}
    if hasattr(checkpointer, "get_tuple"):
        checkpoint_tuple = checkpointer.get_tuple(config)
        return _messages_from_checkpoint_tuple(checkpoint_tuple)
    if hasattr(checkpointer, "get"):
        checkpoint = checkpointer.get(config)
        return _messages_from_checkpoint(checkpoint)
    return []


def _messages_from_checkpoint_tuple(checkpoint_tuple: Any) -> list[Any]:
    checkpoint = getattr(checkpoint_tuple, "checkpoint", None)
    if checkpoint is None and isinstance(checkpoint_tuple, dict):
        checkpoint = checkpoint_tuple.get("checkpoint")
    return _messages_from_checkpoint(checkpoint)


def _messages_from_checkpoint(checkpoint: Any) -> list[Any]:
    if not isinstance(checkpoint, dict):
        return []
    if isinstance(checkpoint.get("messages"), list):
        return checkpoint["messages"]
    channel_values = checkpoint.get("channel_values")
    if isinstance(channel_values, dict) and isinstance(channel_values.get("messages"), list):
        return channel_values["messages"]
    return []


def extract_user_assistant_turns(messages: list[Any]) -> list[ConversationTurn]:
    turns: list[ConversationTurn] = []
    for message in messages:
        role = _message_role(message)
        if role in {"user", "human"}:
            label = "User"
        elif role in {"assistant", "ai"} or message.__class__.__name__ == "AIMessage":
            label = "Assistant"
        else:
            continue
        content = _message_content(message).strip()
        if content:
            turns.append(ConversationTurn(role=label, content=content))
    return turns


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or message.get("type") or "").lower()
    return str(getattr(message, "type", "") or getattr(message, "role", "")).lower()


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        return _content_text(message.get("content"))
    return _content_text(getattr(message, "content", ""))
```

- [ ] **Step 4: Add history renderers**

Append to `agent_cli/rendering.py`:

```python
def format_history(turns: list[Any]) -> str:
    if not turns:
        return "No user/assistant messages found for this session."
    blocks = []
    for turn in turns:
        blocks.append(f"{turn.role}:\n{turn.content}")
    return "\n\n".join(blocks)


def render_history_markdown(
    *,
    session_id: str,
    workdir: str,
    model_name: str | None,
    exported_at: str,
    turns: list[Any],
) -> str:
    lines = [
        f"# Session {session_id}",
        "",
        f"- Workdir: {workdir}",
        f"- Model: {model_name or 'unknown'}",
        f"- Exported at: {exported_at}",
        "",
    ]
    for turn in turns:
        lines.extend([f"## {turn.role}", "", turn.content, ""])
    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 5: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_history.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/history.py agent_cli/rendering.py tests/test_agent_cli_history.py
git commit -m "feat: read cli history from checkpoints"
```

---

### Task 5: Wire `/history` And `/export`

**Files:**
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing command tests**

Append these tests to `tests/test_agent_cli_repl.py`:

```python
class FakeHistoryCheckpointer:
    def get_tuple(self, config):
        return {
            "checkpoint": {
                "channel_values": {
                    "messages": [
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "hi"},
                    ]
                }
            }
        }


def test_handle_command_history_uses_checkpointer_messages():
    store = FakeStore()
    store.create_session(workdir="/repo", model=None, title="Known", session_id="s1")
    cli = AgentCLI(
        session_store=store,
        checkpointer=FakeHistoryCheckpointer(),
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        session_id="s1",
    )

    result = cli.handle_command("/history")

    assert "User:\nhello" in result
    assert "Assistant:\nhi" in result


def test_handle_command_export_writes_markdown(tmp_path):
    store = FakeStore()
    store.create_session(workdir=str(tmp_path), model="model", title="Known", session_id="s1")
    cli = AgentCLI(
        session_store=store,
        checkpointer=FakeHistoryCheckpointer(),
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir=str(tmp_path),
        model_name="model",
        session_id="s1",
    )

    result = cli.handle_command("/export transcript.md")

    exported = tmp_path / "transcript.md"
    assert result == f"Exported history: {exported}"
    assert "# Session s1" in exported.read_text()
    assert "## User\n\nhello" in exported.read_text()


def test_handle_command_export_requires_path():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=FakeHistoryCheckpointer(),
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    assert cli.handle_command("/export") == "Usage: /export <path.md>"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_handle_command_history_uses_checkpointer_messages tests/test_agent_cli_repl.py::test_handle_command_export_writes_markdown tests/test_agent_cli_repl.py::test_handle_command_export_requires_path -q
```

Expected: FAIL because `/history` and `/export` are unhandled.

- [ ] **Step 3: Implement history/export command handlers**

Update imports in `agent_cli/repl.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

from agent_cli.history import extract_user_assistant_turns, load_thread_messages
from agent_cli.rendering import (
    format_history,
    format_interrupt_summary,
    format_sessions,
    format_status,
    latest_ai_text,
    render_history_markdown,
)
```

Add helper methods to `AgentCLI`:

```python
def _history_turns(self):
    session_id = self.ensure_session()
    messages = load_thread_messages(self.checkpointer, session_id)
    return extract_user_assistant_turns(messages)

def _export_path(self, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(self.workdir) / path
    return path.resolve()
```

Add to `handle_command()` after `/title`:

```python
if command.name == "history":
    return format_history(self._history_turns())
if command.name == "export":
    if not arg:
        return "Usage: /export <path.md>"
    session_id = self.ensure_session()
    turns = self._history_turns()
    if not turns:
        return "No user/assistant messages found for this session."
    export_path = self._export_path(arg)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text(
        render_history_markdown(
            session_id=session_id,
            workdir=self.workdir,
            model_name=self.model_name,
            exported_at=datetime.now(timezone.utc).isoformat(),
            turns=turns,
        ),
        encoding="utf-8",
    )
    return f"Exported history: {export_path}"
```

- [ ] **Step 4: Run REPL tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/repl.py tests/test_agent_cli_repl.py
git commit -m "feat: add cli history and export commands"
```

---

### Task 6: Add Doctor Subcommand

**Files:**
- Create: `agent_cli/doctor.py`
- Modify: `agent_cli/main.py`
- Test: `tests/test_agent_cli_doctor.py`
- Test: `tests/test_agent_cli_main.py`

- [ ] **Step 1: Write failing doctor tests**

Create `tests/test_agent_cli_doctor.py`:

```python
import sqlite3

from agent_cli.doctor import DoctorCheck, doctor_exit_code, render_doctor_checks, run_doctor


def test_render_doctor_checks_uses_status_lines():
    output = render_doctor_checks(
        [
            DoctorCheck(status="OK", name="python", detail="3.11"),
            DoctorCheck(status="WARN", name="OPENAI_API_KEY", detail="not set"),
        ]
    )

    assert "OK    python  3.11" in output
    assert "WARN  OPENAI_API_KEY  not set" in output


def test_doctor_exit_code_is_one_for_failures():
    assert doctor_exit_code([DoctorCheck(status="FAIL", name="db", detail="bad")]) == 1
    assert doctor_exit_code([DoctorCheck(status="WARN", name="key", detail="missing")]) == 0


def test_run_doctor_warns_when_openai_key_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    checks = run_doctor(
        cli_home=tmp_path,
        db_path=tmp_path / "cli.sqlite",
        workdir=str(tmp_path),
        dotenv_path=tmp_path / ".env",
    )

    assert any(check.status == "WARN" and check.name == "OPENAI_API_KEY" for check in checks)
    assert doctor_exit_code(checks) == 0


def test_run_doctor_fails_for_missing_workdir(tmp_path):
    checks = run_doctor(
        cli_home=tmp_path,
        db_path=tmp_path / "cli.sqlite",
        workdir=str(tmp_path / "missing"),
        dotenv_path=tmp_path / ".env",
    )

    assert any(check.status == "FAIL" and check.name == "workdir" for check in checks)
    assert doctor_exit_code(checks) == 1
```

Append this test to `tests/test_agent_cli_main.py`:

```python
def test_main_doctor_prints_checks(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module

    code = main_module.main(["doctor", "--workdir", str(tmp_path)])

    captured = capsys.readouterr()
    assert code in {0, 1}
    assert "python" in captured.out
    assert "prompt_toolkit" in captured.out
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_doctor.py tests/test_agent_cli_main.py::test_main_doctor_prints_checks -q
```

Expected: FAIL because `agent_cli.doctor` and `doctor` subcommand do not exist.

- [ ] **Step 3: Implement `agent_cli/doctor.py`**

Create `agent_cli/doctor.py`:

```python
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


DoctorStatus = Literal["OK", "WARN", "FAIL"]


@dataclass(frozen=True)
class DoctorCheck:
    status: DoctorStatus
    name: str
    detail: str


def run_doctor(*, cli_home: Path, db_path: Path, workdir: str, dotenv_path: Path) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    checks.append(DoctorCheck("OK", "python", sys.version.split()[0]))
    checks.append(_import_check("prompt_toolkit", "prompt_toolkit"))
    checks.append(_import_check("langgraph-checkpoint-sqlite", "langgraph.checkpoint.sqlite"))
    checks.append(_cli_home_check(cli_home))
    checks.append(_sqlite_check(db_path))
    checks.append(_workdir_check(workdir))
    checks.append(_dotenv_check(dotenv_path))
    checks.append(_openai_key_check())
    return checks


def _import_check(name: str, module: str) -> DoctorCheck:
    if importlib.util.find_spec(module) is None:
        return DoctorCheck("FAIL", name, f"module {module!r} is not importable")
    return DoctorCheck("OK", name, "importable")


def _cli_home_check(cli_home: Path) -> DoctorCheck:
    try:
        cli_home.mkdir(parents=True, exist_ok=True)
        probe = cli_home / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return DoctorCheck("FAIL", "CLI home", str(exc))
    return DoctorCheck("OK", "CLI home", str(cli_home))


def _sqlite_check(db_path: Path) -> DoctorCheck:
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("select 1")
    except sqlite3.Error as exc:
        return DoctorCheck("FAIL", "SQLite DB", str(exc))
    except OSError as exc:
        return DoctorCheck("FAIL", "SQLite DB", str(exc))
    return DoctorCheck("OK", "SQLite DB", str(db_path))


def _workdir_check(workdir: str) -> DoctorCheck:
    path = Path(workdir)
    if not path.exists():
        return DoctorCheck("FAIL", "workdir", f"{workdir} does not exist")
    if not path.is_dir():
        return DoctorCheck("FAIL", "workdir", f"{workdir} is not a directory")
    return DoctorCheck("OK", "workdir", str(path))


def _dotenv_check(dotenv_path: Path) -> DoctorCheck:
    if dotenv_path.exists():
        return DoctorCheck("OK", ".env", str(dotenv_path))
    return DoctorCheck("WARN", ".env", f"{dotenv_path} not found")


def _openai_key_check() -> DoctorCheck:
    if os.getenv("OPENAI_API_KEY"):
        return DoctorCheck("OK", "OPENAI_API_KEY", "set")
    return DoctorCheck("WARN", "OPENAI_API_KEY", "not set")


def render_doctor_checks(checks: list[DoctorCheck]) -> str:
    return "\n".join(f"{check.status:<5} {check.name}  {check.detail}" for check in checks)


def doctor_exit_code(checks: list[DoctorCheck]) -> int:
    return 1 if any(check.status == "FAIL" for check in checks) else 0
```

- [ ] **Step 4: Wire `doctor` into `main.py`**

Update `build_parser()` in `agent_cli/main.py`:

```python
doctor = subparsers.add_parser("doctor", help="Check local CLI dependencies and storage.")
doctor.add_argument("--workdir", default=None, help="Workspace directory to check.")
```

Add imports:

```python
from agent_cli.doctor import doctor_exit_code, render_doctor_checks, run_doctor
from agent_cli.paths import ensure_db_parent, get_cli_home, get_db_path
```

Add before the `sessions` branch in `main()`:

```python
if command == "doctor":
    workdir = str(Path(args.workdir).expanduser().resolve()) if args.workdir else os.getcwd()
    checks = run_doctor(
        cli_home=get_cli_home(),
        db_path=get_db_path(),
        workdir=workdir,
        dotenv_path=Path.cwd() / ".env",
    )
    print(render_doctor_checks(checks))
    return doctor_exit_code(checks)
```

- [ ] **Step 5: Run doctor tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_doctor.py tests/test_agent_cli_main.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/doctor.py agent_cli/main.py tests/test_agent_cli_doctor.py tests/test_agent_cli_main.py
git commit -m "feat: add agent cli doctor command"
```

---

### Task 7: Add CLI File Logging

**Files:**
- Create: `agent_cli/logging.py`
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_logging.py`

- [ ] **Step 1: Write failing logging tests**

Create `tests/test_agent_cli_logging.py`:

```python
import logging

from agent_cli.logging import setup_cli_logging


def test_setup_cli_logging_creates_log_files(tmp_path):
    logger = setup_cli_logging(tmp_path)

    logger.info("hello")
    logging.getLogger("agent_cli.errors").error("boom")

    agent_log = tmp_path / "logs" / "agent.log"
    error_log = tmp_path / "logs" / "errors.log"

    assert agent_log.exists()
    assert error_log.exists()
    assert "hello" in agent_log.read_text()
    assert "boom" in error_log.read_text()
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_logging.py -q
```

Expected: FAIL because `agent_cli.logging` does not exist.

- [ ] **Step 3: Implement logging setup**

Create `agent_cli/logging.py`:

```python
from __future__ import annotations

import logging
from pathlib import Path


def setup_cli_logging(cli_home: str | Path) -> logging.Logger:
    home = Path(cli_home)
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    agent_handler = logging.FileHandler(log_dir / "agent.log", encoding="utf-8")
    agent_handler.setLevel(logging.INFO)
    agent_handler.setFormatter(formatter)

    error_handler = logging.FileHandler(log_dir / "errors.log", encoding="utf-8")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    logger = logging.getLogger("agent_cli")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _replace_handlers(logger, [agent_handler])

    error_logger = logging.getLogger("agent_cli.errors")
    error_logger.setLevel(logging.ERROR)
    error_logger.propagate = False
    _replace_handlers(error_logger, [error_handler])

    return logger


def _replace_handlers(logger: logging.Logger, handlers: list[logging.Handler]) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in handlers:
        logger.addHandler(handler)
```

- [ ] **Step 4: Wire logging into main and REPL**

Update imports in `agent_cli/main.py`:

```python
import logging

from agent_cli.logging import setup_cli_logging
from agent_cli.paths import ensure_db_parent, get_cli_home, get_db_path
```

After `load_dotenv()` in `main()`:

```python
logger = setup_cli_logging(get_cli_home())
logger.info("agent_cli startup command=%s", command)
```

Before every normal return from `main()` is not necessary; add one shutdown line in the `finally` block around checkpointer usage:

```python
finally:
    logging.getLogger("agent_cli").info("agent_cli shutdown command=%s", command)
    checkpointer_handle.close()
```

Update `agent_cli/repl.py` imports:

```python
import logging
```

Add module loggers near type aliases:

```python
logger = logging.getLogger("agent_cli")
error_logger = logging.getLogger("agent_cli.errors")
```

In `submit_message()` before runner call:

```python
logger.info(
    "submit_message session_id=%s message_length=%s",
    session_id,
    len(text),
)
```

In `handle_command()` after resolving the command:

```python
logger.info("handle_command command=%s session_id=%s", command.name, self.session_id)
```

In the `except Exception as exc` block in `run_repl()`:

```python
error_logger.exception("repl error session_id=%s: %s", self.session_id, exc)
```

- [ ] **Step 5: Run logging tests and CLI main tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_logging.py tests/test_agent_cli_main.py tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/logging.py agent_cli/main.py agent_cli/repl.py tests/test_agent_cli_logging.py
git commit -m "feat: add agent cli file logging"
```

---

### Task 8: Update README And Full Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README CLI section**

Replace the existing `## Agent CLI` section in `README.md` with:

```markdown
## Agent CLI

This repository includes a local CLI for the LangGraph agent.

Chat and ask modes require the LangGraph SQLite checkpointer package
(`langgraph-checkpoint-sqlite`) because conversation state is stored in
SQLite. The interactive REPL uses `prompt_toolkit` for history and slash command
completion.

```bash
python -m agent_cli
python -m agent_cli chat
python -m agent_cli ask "Summarize this repository"
python -m agent_cli sessions
python -m agent_cli chat --resume <session_id>
python -m agent_cli doctor
```

The CLI stores LangGraph checkpoints and lightweight session metadata in
`~/.langchain-agent/cli.sqlite` by default. Set `AGENT_CLI_HOME` to place this
state elsewhere.

Inside chat, use `/help` to list slash commands. Common commands:

- `/status` shows the active session id, title, workdir, model, CLI home, and
  SQLite DB path.
- `/title <name>` updates the session title shown by `sessions`.
- `/history` prints user and assistant messages from the current LangGraph
  checkpoint thread.
- `/export <path.md>` writes the same filtered history to a Markdown file.
- `/sessions` lists recent sessions.
- `/resume <session_id>` resumes a previous session.
- `/skills` and `/skill <name>` inspect local procedural skills.

The MVP supports basic approve/reject prompts for human-in-the-loop interrupts.

`python -m agent_cli doctor` checks local dependencies, CLI storage, workdir, and
basic model environment variables without calling the model or accessing the
network.

CLI logs are written under `~/.langchain-agent/logs/` by default:

- `agent.log` records startup, shutdown, commands, session ids, and message
  lengths.
- `errors.log` records CLI exceptions.
```

- [ ] **Step 2: Run full CLI test suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_paths.py tests/test_agent_cli_commands.py tests/test_agent_cli_session_store.py tests/test_agent_cli_checkpoints.py tests/test_agent_cli_interrupts.py tests/test_agent_cli_input.py tests/test_agent_cli_history.py tests/test_agent_cli_doctor.py tests/test_agent_cli_logging.py tests/test_agent_cli_repl.py tests/test_agent_cli_main.py tests/test_agent_cli_builders.py -q
```

Expected: PASS.

- [ ] **Step 3: Run adjacent lifecycle regression tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_runner.py tests/test_session_context.py tests/test_cronjob_tool.py::test_build_agent_excludes_cronjob_by_default tests/test_cronjob_tool.py::test_build_agent_can_include_cronjob -q
```

Expected: PASS.

- [ ] **Step 4: Run CLI smoke commands**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m agent_cli --help
```

Expected: exit code `0`, output contains `doctor`, `chat`, `ask`, and `sessions`.

Run:

```bash
AGENT_CLI_HOME="$(mktemp -d)" /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli sessions
```

Expected: exit code `0`, output contains `No sessions found.`

Run:

```bash
AGENT_CLI_HOME="$(mktemp -d)" /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli doctor --workdir /home/miku/projects/langchain
```

Expected: exit code `0` or `1`. Output contains `python`, `prompt_toolkit`, `langgraph-checkpoint-sqlite`, `SQLite DB`, and `workdir`. Exit code may be `1` only if a local storage/import check fails.

- [ ] **Step 5: Commit docs and verification updates**

```bash
git add README.md
git commit -m "docs: document agent cli phase 1"
```

---

## Self-Review

- Spec coverage:
  - PromptSession input: Task 2.
  - Slash completion: Tasks 1 and 2.
  - `/status` and `/title`: Task 3.
  - `/history` and checkpoint-backed extraction: Tasks 4 and 5.
  - `/export` Markdown: Tasks 4 and 5.
  - `doctor`: Task 6.
  - logging: Task 7.
  - README and verification: Task 8.
- Placeholder scan:
  - The plan contains no unresolved markers or undefined future steps.
  - Each task includes concrete tests, implementation snippets, commands, and expected outcomes.
- Type consistency:
  - `CommandDef.completion` uses `CompletionKind`.
  - History data uses `ConversationTurn(role: str, content: str)`.
  - `AgentCLI(prompt_session=...)` is the test seam for non-interactive REPL tests.
  - `format_history()` and `render_history_markdown()` consume `ConversationTurn`-like objects.

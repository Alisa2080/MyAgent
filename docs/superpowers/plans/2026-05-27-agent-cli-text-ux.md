# Agent CLI Text UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add pure-text input safety, dragged-file references, and lightweight `/copy`, `/retry`, and `/usage` commands to the Agent CLI.

**Architecture:** Keep parsing and sanitizing in a new pure helper module, `agent_cli/text_input.py`, so behavior can be tested without prompt-toolkit. Wire bracketed paste handling through `agent_cli/input.py`, and keep command/session state changes in `agent_cli/repl.py` with small process-local fields.

**Tech Stack:** Python 3.11+, prompt-toolkit, LangGraph checkpoint helpers, pytest. Use `/home/miku/miniforge3/envs/langchain/bin/python` for tests in this repository.

---

## File Structure

- Create `agent_cli/text_input.py`: pure dataclasses and helpers for terminal input sanitization, paste collapse/expansion, OSC52 formatting, token estimation, usage rendering, and dragged-file detection.
- Modify `agent_cli/input.py`: import text helpers, add optional `cli_home` support to `build_prompt_session`, and attach a prompt-toolkit bracketed-paste key binding that collapses large text.
- Modify `agent_cli/repl.py`: sanitize raw REPL input before command detection, preprocess normal user messages, track retry/copy/latency/usage state, and handle `/copy`, `/retry`, `/usage`.
- Modify `agent_cli/commands.py`: register `/copy`, `/retry`, and `/usage`.
- Modify `README.md`: document paste collapse, file references, and the new commands.
- Modify tests:
  - `tests/test_agent_cli_input.py`
  - `tests/test_agent_cli_repl.py`
  - `tests/test_agent_cli_commands.py`

---

### Task 1: Pure Terminal Input Helpers

**Files:**
- Create: `agent_cli/text_input.py`
- Test: `tests/test_agent_cli_input.py`

- [ ] **Step 1: Write failing tests for sanitization, paste files, path detection, OSC52, and token estimation**

Append these tests to `tests/test_agent_cli_input.py`:

```python
from pathlib import Path

from agent_cli.text_input import (
    collapse_large_paste,
    detect_file_drop,
    expand_paste_references,
    format_file_reference_message,
    format_osc52,
    prepare_user_message,
    sanitize_terminal_input,
    should_collapse_paste,
    estimate_tokens,
)


def test_sanitize_terminal_input_strips_paste_and_cpr_sequences():
    raw = "\x1b[200~hello\r\nworld\x1b[201~\x1b[12;34R^[[56;78R"

    assert sanitize_terminal_input(raw) == "hello\nworld"


def test_sanitize_terminal_input_strips_degraded_wrappers_at_boundaries():
    raw = "[200~first\nsecond[201~\n00~third01~"

    assert sanitize_terminal_input(raw) == "first\nsecond\nthird"


def test_should_collapse_paste_uses_five_line_threshold_and_skips_commands():
    assert should_collapse_paste("1\n2\n3\n4\n5") is True
    assert should_collapse_paste("/note\n1\n2\n3\n4") is False
    assert should_collapse_paste("1\n2\n3\n4") is False


def test_collapse_large_paste_writes_file_and_expand_reference(tmp_path):
    text = "a\r\nb\r\nc\r\nd\r\ne"

    result = collapse_large_paste(text, cli_home=tmp_path, counter=3)

    assert result.collapsed is True
    assert result.path is not None
    assert result.path.parent == tmp_path / "pastes"
    assert result.path.read_text(encoding="utf-8") == "a\nb\nc\nd\ne"
    assert result.placeholder.startswith("[Pasted text #3: 5 lines -> ")
    assert expand_paste_references(f"please read {result.placeholder}") == (
        "please read a\nb\nc\nd\ne"
    )


def test_collapse_large_paste_returns_sanitized_text_for_small_paste(tmp_path):
    result = collapse_large_paste("a\r\nb", cli_home=tmp_path, counter=1)

    assert result.collapsed is False
    assert result.placeholder == "a\nb"
    assert result.path is None


def test_detect_file_drop_supports_workdir_relative_path_and_trailing_text(tmp_path):
    target = tmp_path / "notes file.md"
    target.write_text("notes", encoding="utf-8")

    drop = detect_file_drop("notes\\ file.md summarize this", workdir=str(tmp_path))

    assert drop is not None
    assert drop.path == target.resolve()
    assert drop.remainder == "summarize this"
    assert drop.is_image is False
    assert format_file_reference_message(drop) == (
        f"[User referenced file: {target.resolve()}]\nsummarize this"
    )


def test_detect_file_drop_supports_quoted_file_url(tmp_path):
    target = tmp_path / "screen shot.png"
    target.write_text("image bytes", encoding="utf-8")

    drop = detect_file_drop(f'"{target.as_uri()}" describe', workdir=str(tmp_path))

    assert drop is not None
    assert drop.path == target.resolve()
    assert drop.remainder == "describe"
    assert drop.is_image is True


def test_detect_file_drop_returns_none_for_missing_file(tmp_path):
    assert detect_file_drop("missing.txt summarize", workdir=str(tmp_path)) is None


def test_prepare_user_message_sanitizes_expands_paste_and_rewrites_file(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("notes", encoding="utf-8")
    paste = collapse_large_paste("a\nb\nc\nd\ne", cli_home=tmp_path, counter=1)

    prepared = prepare_user_message(
        f"\x1b[200~{target.name} read this and {paste.placeholder}\x1b[201~",
        workdir=str(tmp_path),
    )

    assert prepared == (
        f"[User referenced file: {target.resolve()}]\n"
        "read this and a\nb\nc\nd\ne"
    )


def test_format_osc52_encodes_text_for_clipboard():
    assert format_osc52("hello") == "\x1b]52;c;aGVsbG8=\x07"


def test_estimate_tokens_is_deterministic_and_nonzero():
    assert estimate_tokens("one two three four five") == 2
    assert estimate_tokens("") == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_input.py -q
```

Expected: FAIL because `agent_cli.text_input` does not exist.

- [ ] **Step 3: Implement `agent_cli/text_input.py`**

Create `agent_cli/text_input.py`:

```python
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse


IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg", ".ico"}
)

PASTE_REFERENCE_RE = re.compile(r"\[Pasted text #\d+: \d+ lines -> (.+?)\]")
_DSR_CPR_ESC_RE = re.compile(r"\x1b\[\d+;\d+R")
_DSR_CPR_VISIBLE_RE = re.compile(r"\^\[\[\d+;\d+R")


@dataclass(frozen=True)
class PasteCollapse:
    collapsed: bool
    placeholder: str
    path: Path | None = None


@dataclass(frozen=True)
class FileDrop:
    path: Path
    remainder: str
    is_image: bool


def sanitize_terminal_input(text: str) -> str:
    if not text:
        return text
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = (
        value.replace("\x1b[200~", "")
        .replace("\x1b[201~", "")
        .replace("^[[200~", "")
        .replace("^[[201~", "")
    )
    value = re.sub(r"(^|[\s\n>:\]\)])\[200~", r"\1", value)
    value = re.sub(r"\[201~(?=$|[\s\n<\[\(\):;.,!?])", "", value)
    value = re.sub(r"(^|[\s\n>:\]\)])00~", r"\1", value)
    value = re.sub(r"01~(?=$|[\s\n<\[\(\):;.,!?])", "", value)
    value = _DSR_CPR_ESC_RE.sub("", value)
    value = _DSR_CPR_VISIBLE_RE.sub("", value)
    return value


def should_collapse_paste(text: str, *, min_lines: int = 5) -> bool:
    sanitized = sanitize_terminal_input(text)
    if sanitized.lstrip().startswith("/"):
        return False
    return len(sanitized.splitlines()) >= min_lines


def collapse_large_paste(text: str, *, cli_home: str | Path, counter: int) -> PasteCollapse:
    sanitized = sanitize_terminal_input(text)
    if not should_collapse_paste(sanitized):
        return PasteCollapse(collapsed=False, placeholder=sanitized)
    paste_dir = Path(cli_home) / "pastes"
    paste_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    path = paste_dir / f"paste_{counter}_{stamp}.txt"
    path.write_text(sanitized, encoding="utf-8")
    line_count = len(sanitized.splitlines())
    placeholder = f"[Pasted text #{counter}: {line_count} lines -> {path}]"
    return PasteCollapse(collapsed=True, placeholder=placeholder, path=path)


def expand_paste_references(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        path = Path(match.group(1))
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return match.group(0)

    return PASTE_REFERENCE_RE.sub(replace, text)


def split_path_input(raw: str) -> tuple[str, str]:
    value = str(raw or "").strip()
    if not value:
        return "", ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        pos = 1
        while pos < len(value):
            ch = value[pos]
            if ch == "\\" and pos + 1 < len(value):
                pos += 2
                continue
            if ch == quote:
                return value[1:pos], value[pos + 1 :].strip()
            pos += 1
        return value[1:], ""
    pos = 0
    while pos < len(value):
        ch = value[pos]
        if ch == "\\" and pos + 1 < len(value) and value[pos + 1] == " ":
            pos += 2
        elif ch == " ":
            break
        else:
            pos += 1
    return value[:pos].replace("\\ ", " "), value[pos:].strip()


def resolve_file_path(raw_path: str, *, workdir: str | Path) -> Path | None:
    token = str(raw_path or "").strip()
    if not token:
        return None
    if (token.startswith('"') and token.endswith('"')) or (
        token.startswith("'") and token.endswith("'")
    ):
        token = token[1:-1].strip()
    token = token.replace("\\ ", " ")
    expanded = token
    if token.startswith("file://"):
        parsed = urlparse(token)
        expanded = unquote(parsed.path or "")
        if parsed.netloc and os.name == "nt":
            expanded = f"//{parsed.netloc}{expanded}"
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    path = Path(expanded)
    if not path.is_absolute():
        path = Path(workdir) / path
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if not resolved.exists() or not resolved.is_file():
        return None
    return resolved


def _starts_like_path(value: str) -> bool:
    return (
        value.startswith("/")
        or value.startswith("~")
        or value.startswith("./")
        or value.startswith("../")
        or value.startswith("file://")
        or value.startswith('"')
        or value.startswith("'")
        or bool(re.match(r"^[^\s/]+(?:\\ |\.[A-Za-z0-9])[^\s]*", value))
    )


def detect_file_drop(text: str, *, workdir: str | Path) -> FileDrop | None:
    value = sanitize_terminal_input(str(text or "")).strip()
    if not value or not _starts_like_path(value):
        return None
    direct = resolve_file_path(value, workdir=workdir)
    if direct is not None:
        return FileDrop(direct, "", direct.suffix.lower() in IMAGE_EXTENSIONS)
    token, remainder = split_path_input(value)
    path = resolve_file_path(token, workdir=workdir)
    if path is None and " " in value and value[0] not in {"'", '"'}:
        for pos in [idx for idx, ch in enumerate(value) if ch == " "][::-1]:
            candidate = value[:pos].rstrip()
            resolved = resolve_file_path(candidate, workdir=workdir)
            if resolved is not None:
                path = resolved
                remainder = value[pos + 1 :].strip()
                break
    if path is None:
        return None
    return FileDrop(path, remainder, path.suffix.lower() in IMAGE_EXTENSIONS)


def format_file_reference_message(drop: FileDrop) -> str:
    reference = f"[User referenced file: {drop.path}]"
    if drop.remainder:
        return f"{reference}\n{drop.remainder}"
    return reference


def prepare_user_message(text: str, *, workdir: str | Path) -> str:
    value = expand_paste_references(sanitize_terminal_input(text)).strip()
    drop = detect_file_drop(value, workdir=workdir)
    if drop is not None:
        return format_file_reference_message(drop)
    return value


def format_osc52(text: str) -> str:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\x1b]52;c;{payload}\x07"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)
```

- [ ] **Step 4: Run input helper tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_input.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add agent_cli/text_input.py tests/test_agent_cli_input.py
git commit -m "feat: add agent cli text input helpers"
```

---

### Task 2: Prompt Paste Collapse Hook

**Files:**
- Modify: `agent_cli/input.py`
- Test: `tests/test_agent_cli_input.py`

- [ ] **Step 1: Write failing tests for prompt session paste binding**

Append this test to `tests/test_agent_cli_input.py`:

```python
def test_build_prompt_session_accepts_cli_home_for_paste_collapse(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")

    session = build_prompt_session(
        history_path=tmp_path / "history.txt",
        session_store=store,
        workdir=str(tmp_path),
        cli_home=tmp_path / "cli-home",
    )

    assert session is not None
    assert Path(tmp_path / "cli-home").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_input.py::test_build_prompt_session_accepts_cli_home_for_paste_collapse -q
```

Expected: FAIL because `build_prompt_session()` does not accept `cli_home`.

- [ ] **Step 3: Modify `agent_cli/input.py` to install bracketed paste collapse**

Add imports near the top:

```python
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from agent_cli.text_input import collapse_large_paste, sanitize_terminal_input
```

Update `build_prompt_session` signature:

```python
def build_prompt_session(
    *,
    history_path: str | Path,
    session_store: SessionStore,
    workdir: str,
    skill_commands_provider=None,
    cli_home: str | Path | None = None,
) -> PromptSession:
```

Replace the `return PromptSession(...)` block with:

```python
    key_bindings = KeyBindings()
    paste_counter = [0]
    paste_home = Path(cli_home) if cli_home is not None else path.parent
    paste_home.mkdir(parents=True, exist_ok=True)

    @key_bindings.add(Keys.BracketedPaste, eager=True)
    def _handle_bracketed_paste(event):
        pasted_text = sanitize_terminal_input(event.data or "")
        if not pasted_text:
            return
        buffer = event.current_buffer
        try:
            if buffer.text.strip().startswith("/"):
                buffer.insert_text(pasted_text)
                return
            paste_counter[0] += 1
            collapsed = collapse_large_paste(
                pasted_text,
                cli_home=paste_home,
                counter=paste_counter[0],
            )
        except OSError:
            buffer.insert_text(pasted_text)
            return
        if not collapsed.collapsed:
            buffer.insert_text(collapsed.placeholder)
            return
        prefix = ""
        if buffer.cursor_position > 0 and buffer.text[buffer.cursor_position - 1] != "\n":
            prefix = "\n"
        buffer.insert_text(prefix + collapsed.placeholder)

    return PromptSession(
        history=FileHistory(str(path)),
        completer=SlashCommandCompleter(
            session_store=session_store,
            workdir=workdir,
            skill_commands_provider=skill_commands_provider,
        ),
        key_bindings=key_bindings,
    )
```

- [ ] **Step 4: Pass `cli_home` from `AgentCLI._prompt`**

Modify `agent_cli/repl.py` in `_prompt`:

```python
            self.prompt_session = build_prompt_session(
                history_path=ensure_cli_home() / "history.txt",
                session_store=self.session_store,
                workdir=self.workdir,
                skill_commands_provider=self.skill_commands_provider,
                cli_home=self.cli_home or ensure_cli_home(),
            )
```

- [ ] **Step 5: Run input and REPL tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_input.py tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add agent_cli/input.py agent_cli/repl.py tests/test_agent_cli_input.py
git commit -m "feat: collapse large cli pastes"
```

---

### Task 3: Preprocess Submitted User Messages

**Files:**
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing REPL tests for sanitization and file references**

Append these tests to `tests/test_agent_cli_repl.py`:

```python
def test_submit_message_sanitizes_control_sequences_before_runner(tmp_path):
    calls = []

    def fake_runner(agent, input_data, config):
        calls.append(input_data)
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir=str(tmp_path),
        model_name="model",
    )

    assert cli.submit_message("\x1b[200~hello\r\nworld\x1b[201~") == "ok"
    assert calls[0]["messages"][0]["content"] == "hello\nworld"


def test_submit_message_rewrites_dragged_file_path(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("notes", encoding="utf-8")
    calls = []

    def fake_runner(agent, input_data, config):
        calls.append(input_data)
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir=str(tmp_path),
        model_name="model",
    )

    cli.submit_message("notes.md summarize")

    assert calls[0]["messages"][0]["content"] == (
        f"[User referenced file: {target.resolve()}]\nsummarize"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py::test_submit_message_sanitizes_control_sequences_before_runner tests/test_agent_cli_repl.py::test_submit_message_rewrites_dragged_file_path -q
```

Expected: FAIL because `submit_message()` forwards raw text.

- [ ] **Step 3: Modify `AgentCLI.submit_message` to preprocess messages**

Add imports at the top of `agent_cli/repl.py`:

```python
import time

from agent_cli.text_input import prepare_user_message
```

In `AgentCLI.__init__`, add process-local fields:

```python
        self.last_user_message: str | None = None
        self.assistant_replies: list[str] = []
        self.last_call_elapsed_seconds: float | None = None
        self.last_usage_metadata: dict[str, Any] | None = None
```

At the start of `submit_message`, normalize text:

```python
    def submit_message(self, text: str) -> str:
        prepared_text = prepare_user_message(text, workdir=self.workdir)
        self.last_user_message = prepared_text
        started_at = time.perf_counter()
```

Replace all uses of `text` for title generation, runner input, and preview with
`prepared_text`. Wrap the runner call timing:

```python
        result = self.runner(
            self.agent,
            {"messages": [{"role": "user", "content": prepared_text}]},
            {"configurable": {"thread_id": session_id}},
        )
        self.last_call_elapsed_seconds = time.perf_counter() - started_at
```

After interrupt handling and before return, capture assistant output:

```python
        output = latest_ai_text(result)
        if output:
            self.assistant_replies.append(output)
        return output
```

Ensure the session title and touch preview use `prepared_text`.

- [ ] **Step 4: Preserve latency on runner exceptions**

Still in `submit_message`, wrap the runner call so failures record elapsed time:

```python
        try:
            result = self.runner(
                self.agent,
                {"messages": [{"role": "user", "content": prepared_text}]},
                {"configurable": {"thread_id": session_id}},
            )
        finally:
            self.last_call_elapsed_seconds = time.perf_counter() - started_at
```

Then call `_handle_interrupts(result, session_id=session_id)` after the `try/finally`.

- [ ] **Step 5: Run focused REPL tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add agent_cli/repl.py tests/test_agent_cli_repl.py
git commit -m "feat: preprocess agent cli user messages"
```

---

### Task 4: `/copy` and `/retry` Commands

**Files:**
- Modify: `agent_cli/commands.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_commands.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing command registry test**

Append to `tests/test_agent_cli_commands.py`:

```python
def test_text_ux_commands_are_registered():
    help_text = render_help()

    assert resolve_command("/copy").name == "copy"
    assert resolve_command("/retry").name == "retry"
    assert "/copy [N]" in help_text
    assert "/retry" in help_text
```

- [ ] **Step 2: Write failing REPL tests for `/copy` and `/retry`**

Append to `tests/test_agent_cli_repl.py`:

```python
def test_handle_copy_copies_recent_assistant_reply(capsys):
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )
    cli.assistant_replies.extend(["first", "second"])

    output = cli.handle_command("/copy")

    captured = capsys.readouterr()
    assert "\x1b]52;c;" in captured.out
    assert "Copied assistant reply 1" in output


def test_handle_copy_supports_nth_recent_assistant_reply(capsys):
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )
    cli.assistant_replies.extend(["first", "second"])

    output = cli.handle_command("/copy 2")

    captured = capsys.readouterr()
    assert "Zmlyc3Q=" in captured.out
    assert "Copied assistant reply 2" in output


def test_handle_copy_validates_args_and_empty_state():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    assert cli.handle_command("/copy nope") == "Usage: /copy [N]"
    assert cli.handle_command("/copy") == "No assistant reply available to copy."


def test_handle_retry_resubmits_last_user_message():
    calls = []

    def fake_runner(agent, input_data, config):
        calls.append(input_data["messages"][0]["content"])
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name=None,
    )

    cli.submit_message("hello")
    assert cli.handle_command("/retry") == "ok"
    assert calls == ["hello", "hello"]


def test_handle_retry_reports_missing_target():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    assert cli.handle_command("/retry") == "No user message available to retry."
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py::test_text_ux_commands_are_registered tests/test_agent_cli_repl.py::test_handle_copy_copies_recent_assistant_reply tests/test_agent_cli_repl.py::test_handle_retry_resubmits_last_user_message -q
```

Expected: FAIL because commands are not registered or handled.

- [ ] **Step 4: Register commands in `agent_cli/commands.py`**

Add these command definitions in the `"Session"` group after `/export`:

```python
    CommandDef(
        "copy",
        "Copy an assistant reply using OSC52.",
        "Session",
        args_hint="[N]",
    ),
    CommandDef("retry", "Retry the last user message.", "Session"),
```

- [ ] **Step 5: Implement `/copy` and `/retry` handlers**

Add import in `agent_cli/repl.py`:

```python
from agent_cli.text_input import format_osc52, prepare_user_message
```

Add branches in `handle_command` before `/clear`:

```python
        if command.name == "copy":
            return self._handle_copy(arg)
        if command.name == "retry":
            return self._handle_retry()
```

Add methods to `AgentCLI`:

```python
    def _handle_copy(self, arg: str) -> str:
        index = 1
        if arg:
            try:
                index = int(arg)
            except ValueError:
                return "Usage: /copy [N]"
        if index < 1:
            return "Usage: /copy [N]"
        if index > len(self.assistant_replies):
            return "No assistant reply available to copy."
        text = self.assistant_replies[-index]
        print(format_osc52(text), end="")
        return f"Copied assistant reply {index}."

    def _handle_retry(self) -> str:
        if not self.last_user_message:
            return "No user message available to retry."
        return self.submit_message(self.last_user_message)
```

- [ ] **Step 6: Run command and REPL tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add agent_cli/commands.py agent_cli/repl.py tests/test_agent_cli_commands.py tests/test_agent_cli_repl.py
git commit -m "feat: add cli copy and retry commands"
```

---

### Task 5: Lightweight `/usage`

**Files:**
- Modify: `agent_cli/text_input.py`
- Modify: `agent_cli/commands.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_commands.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing tests for `/usage` registration and output**

Append to `tests/test_agent_cli_commands.py`:

```python
def test_usage_command_is_registered():
    help_text = render_help()

    assert resolve_command("/usage").name == "usage"
    assert "/usage" in help_text
```

Append to `tests/test_agent_cli_repl.py`:

```python
def test_handle_usage_renders_local_session_stats(tmp_path):
    class FakeCheckpointer:
        pass

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=FakeCheckpointer(),
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir=str(tmp_path),
        model_name="model-x",
    )
    cli.ensure_session()
    cli.last_call_elapsed_seconds = 1.234
    cli.assistant_replies.append("assistant text")

    output = cli.handle_command("/usage")

    assert "Usage:" in output
    assert "Session ID: s1" in output
    assert "Model: model-x" in output
    assert "Estimated tokens:" in output
    assert "Last call: 1.234s" in output
    assert "Assistant replies tracked: 1" in output


def test_handle_usage_works_without_checkpointer(tmp_path):
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir=str(tmp_path),
        model_name=None,
    )

    output = cli.handle_command("/usage")

    assert "Checkpointer: unavailable" in output
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py::test_usage_command_is_registered tests/test_agent_cli_repl.py::test_handle_usage_renders_local_session_stats -q
```

Expected: FAIL because `/usage` is not implemented.

- [ ] **Step 3: Add a usage renderer to `agent_cli/text_input.py`**

Append:

```python
def render_usage_summary(
    *,
    session_id: str,
    model_name: str | None,
    message_count: int,
    turn_count: int,
    transcript_text: str,
    checkpointer_available: bool,
    last_call_elapsed_seconds: float | None,
    assistant_reply_count: int,
    usage_metadata: dict[str, object] | None = None,
) -> str:
    lines = [
        "Usage:",
        f"  Session ID: {session_id}",
        f"  Model: {model_name or 'default'}",
        f"  Messages: {message_count}",
        f"  Turns: {turn_count}",
        f"  Estimated tokens: {estimate_tokens(transcript_text)}",
        f"  Checkpointer: {'available' if checkpointer_available else 'unavailable'}",
        f"  Assistant replies tracked: {assistant_reply_count}",
    ]
    if last_call_elapsed_seconds is None:
        lines.append("  Last call: n/a")
    else:
        lines.append(f"  Last call: {last_call_elapsed_seconds:.3f}s")
    if usage_metadata:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage_metadata:
                label = key.replace("_", " ").title()
                lines.append(f"  {label}: {usage_metadata[key]}")
    return "\n".join(lines)
```

- [ ] **Step 4: Register and handle `/usage`**

In `agent_cli/commands.py`, add after `/retry`:

```python
    CommandDef("usage", "Show lightweight local usage and context stats.", "Session"),
```

In `agent_cli/repl.py`, import `render_usage_summary`:

```python
from agent_cli.text_input import format_osc52, prepare_user_message, render_usage_summary
```

Add branch in `handle_command`:

```python
        if command.name == "usage":
            if self.session is None:
                self.ensure_session()
            return self._handle_usage()
```

Add method:

```python
    def _handle_usage(self) -> str:
        if self.session is None:
            self.ensure_session()
        transcript = self.session.transcript() if self.session is not None else []
        transcript_text = "\n".join(message.content for message in transcript)
        return render_usage_summary(
            session_id=self.session_id or "",
            model_name=self.model_name,
            message_count=len(transcript),
            turn_count=sum(1 for message in transcript if message.role == "user"),
            transcript_text=transcript_text,
            checkpointer_available=self.checkpointer is not None,
            last_call_elapsed_seconds=self.last_call_elapsed_seconds,
            assistant_reply_count=len(self.assistant_replies),
            usage_metadata=self.last_usage_metadata,
        )
```

- [ ] **Step 5: Capture usage metadata from runner results**

Add helper method in `AgentCLI`:

```python
    def _capture_usage_metadata(self, result: Any) -> None:
        metadata = None
        if isinstance(result, dict):
            metadata = result.get("usage_metadata") or result.get("usage")
            messages = result.get("messages")
            if metadata is None and isinstance(messages, list) and messages:
                last = messages[-1]
                metadata = getattr(last, "usage_metadata", None)
                if metadata is None and isinstance(last, dict):
                    metadata = last.get("usage_metadata")
        if isinstance(metadata, dict):
            self.last_usage_metadata = metadata
```

Call it in `submit_message` after `_handle_interrupts`:

```python
        self._capture_usage_metadata(result)
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py tests/test_agent_cli_repl.py tests/test_agent_cli_input.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

```bash
git add agent_cli/text_input.py agent_cli/commands.py agent_cli/repl.py tests/test_agent_cli_commands.py tests/test_agent_cli_repl.py
git commit -m "feat: add lightweight cli usage command"
```

---

### Task 6: README and Smoke Coverage

**Files:**
- Modify: `README.md`
- Modify: `tests/test_agent_cli_commands.py`

- [ ] **Step 1: Write failing README coverage assertions**

Extend `test_readme_agent_cli_docs_cover_current_capabilities` in
`tests/test_agent_cli_commands.py` with:

```python
    assert "/copy" in text
    assert "/retry" in text
    assert "/usage" in text
    assert "pastes" in text.lower()
    assert "drag" in text.lower() or "drop" in text.lower()
    assert "OSC52" in text
```

- [ ] **Step 2: Run README coverage test to verify it fails**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py::test_readme_agent_cli_docs_cover_current_capabilities -q
```

Expected: FAIL because README does not document the new text UX.

- [ ] **Step 3: Update README Agent CLI command docs**

In the Agent CLI command list, add:

```markdown
- `/copy [N]` - Copy the latest or Nth latest assistant response using OSC52.
- `/retry` - Resubmit the last normal user message.
- `/usage` - Show local session usage, estimated tokens, checkpoint status, and last-call latency.
```

In the Agent CLI state or troubleshooting section, add:

```markdown
Large bracketed pastes are collapsed into the CLI home `pastes/` directory while editing and
expanded before submission. Dragged or pasted file paths at the start of a
message are converted into text references such as
`[User referenced file: /absolute/path]`. Image paths are referenced as text in
this phase; the CLI does not send multimodal image payloads yet.
```

In troubleshooting, add:

```markdown
- Clipboard copy issues: `/copy` uses OSC52, so terminal and multiplexer
  clipboard integration must allow OSC52 sequences.
```

- [ ] **Step 4: Run README coverage and command tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 6**

```bash
git add README.md tests/test_agent_cli_commands.py
git commit -m "docs: document agent cli text ux commands"
```

---

### Task 7: Full Verification

**Files:**
- No planned source edits unless verification exposes failures.

- [ ] **Step 1: Run all Agent CLI tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py tests/test_agent_cli_doctor.py tests/test_agent_cli_history.py tests/test_agent_cli_session.py tests/test_agent_cli_repl.py tests/test_agent_cli_commands.py tests/test_agent_cli_input.py tests/test_agent_cli_background.py tests/test_agent_cli_logging.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run CLI smoke commands**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m agent_cli --help
/home/miku/miniforge3/envs/langchain/bin/python -m agent_cli sessions
/home/miku/miniforge3/envs/langchain/bin/python -m agent_cli doctor
```

Expected:

- `--help` exits 0 and lists `chat`, `ask`, `sessions`, `doctor`;
- `sessions` exits 0;
- `doctor` exits 0 or 1 with stable `OK`/`WARN`/`FAIL` output and no traceback.

- [ ] **Step 3: Inspect git status**

Run:

```bash
git status --short
```

Expected: only intentional files are changed. Preserve unrelated pre-existing
dirty files in the main worktree.

- [ ] **Step 4: Finish verification**

If every verification command passed without additional edits, stop here and do
not create an empty commit. If a verification command failed, use
`superpowers:systematic-debugging`, make a focused fix in the affected files,
rerun the failed command and the full Agent CLI test command from Step 1, then
commit only the files changed for that focused fix with:

```bash
git commit -m "fix: stabilize agent cli text ux"
```

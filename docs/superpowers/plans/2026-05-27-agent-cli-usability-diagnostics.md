# Agent CLI Usability Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agent_cli` invocation consistent, doctor output script-friendly, documentation current, and session/history/export output accurate.

**Architecture:** Keep the existing lightweight CLI modules. `agent_cli.main` owns parser consistency, `agent_cli.doctor` owns local checks, new `agent_cli.history` owns checkpoint-backed transcript extraction/rendering, and `agent_cli.session` becomes a thin facade over that history layer.

**Tech Stack:** Python 3.11, argparse, sqlite3, LangGraph SQLite checkpointer, prompt_toolkit, pytest.

---

## File Structure

- Modify `agent_cli/main.py`
  - Add a shared argparse parent parser for `--workdir`, `--model`, and `--profile`.
  - Use the parent for top-level parsing and subcommands.
  - Use doctor exit code helper once doctor is enhanced.

- Modify `agent_cli/doctor.py`
  - Replace bool-style checks with `HealthCheck(status="OK"|"WARN"|"FAIL")`.
  - Add storage, dependency, API key, log, config, dotenv, and background metadata checks.
  - Render stable status columns.

- Create `agent_cli/history.py`
  - Normalize messages from dicts and LangChain-like objects.
  - Filter user/assistant transcript messages.
  - Compute counts.
  - Render terminal and Markdown transcript output.

- Modify `agent_cli/session.py`
  - Add runtime metadata to `Session`.
  - Use `agent_cli.history` for `history`, `status`, and `export_markdown`.

- Modify `agent_cli/repl.py`
  - Pass runtime metadata into `Session`.
  - Return usage text for invalid `/history` limits.

- Modify `README.md`
  - Replace stale MVP CLI docs with current behavior and troubleshooting.

- Modify tests:
  - `tests/test_agent_cli_main.py`
  - `tests/test_agent_cli_doctor.py`
  - `tests/test_agent_cli_session.py`
  - `tests/test_agent_cli_repl.py`
  - Add `tests/test_agent_cli_history.py`
  - Add or extend README smoke coverage in `tests/test_agent_cli_commands.py` or a new docs test.

---

### Task 1: Parser Public Options Work Before and After Subcommands

**Files:**
- Modify: `agent_cli/main.py`
- Test: `tests/test_agent_cli_main.py`

- [ ] **Step 1: Add failing parser tests**

Append these tests to `tests/test_agent_cli_main.py`:

```python
def test_parser_accepts_public_options_after_doctor_subcommand():
    from agent_cli.main import build_parser

    args = build_parser().parse_args(
        ["doctor", "--workdir", "/tmp/repo", "--profile", "dev", "--model", "m"]
    )

    assert args.command == "doctor"
    assert args.workdir == "/tmp/repo"
    assert args.profile == "dev"
    assert args.model == "m"


def test_parser_accepts_public_options_after_sessions_subcommand():
    from agent_cli.main import build_parser

    args = build_parser().parse_args(["sessions", "--profile", "dev"])

    assert args.command == "sessions"
    assert args.profile == "dev"


def test_parser_accepts_model_after_ask_subcommand_and_preserves_question():
    from agent_cli.main import build_parser

    args = build_parser().parse_args(["ask", "--model", "m", "hello", "world"])

    assert args.command == "ask"
    assert args.model == "m"
    assert args.question == ["hello", "world"]


def test_main_invalid_profile_after_subcommand_returns_code_2(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_CLI_HOME", raising=False)

    import agent_cli.main as main_module

    code = main_module.main(["sessions", "--profile", "../bad"])

    captured = capsys.readouterr()
    assert code == 2
    assert "Invalid profile" in captured.err
```

- [ ] **Step 2: Run parser tests and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_parser_accepts_public_options_after_doctor_subcommand tests/test_agent_cli_main.py::test_parser_accepts_public_options_after_sessions_subcommand tests/test_agent_cli_main.py::test_parser_accepts_model_after_ask_subcommand_and_preserves_question tests/test_agent_cli_main.py::test_main_invalid_profile_after_subcommand_returns_code_2 -q
```

Expected: at least the subcommand option tests fail because subparsers do not accept the public options yet.

- [ ] **Step 3: Implement shared public option parser**

In `agent_cli/main.py`, replace `build_parser()` with this structure:

```python
def build_public_options_parser(*, add_help: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)
    parser.add_argument("--workdir", default=None, help="Workspace directory for the agent.")
    parser.add_argument("--model", default=None, help="Model display metadata for session list.")
    parser.add_argument("--profile", "-p", default=None, help="Use a named CLI profile.")
    return parser


def build_parser() -> argparse.ArgumentParser:
    public_options = build_public_options_parser()
    parser = argparse.ArgumentParser(prog="agent_cli", parents=[public_options])

    subparsers = parser.add_subparsers(dest="command")

    chat = subparsers.add_parser(
        "chat",
        help="Start interactive chat.",
        parents=[public_options],
    )
    chat.add_argument("--resume", default=None, help="Resume an existing session id.")

    ask = subparsers.add_parser(
        "ask",
        help="Ask one question and exit.",
        parents=[public_options],
    )
    ask.add_argument("--resume", default=None, help="Resume an existing session id.")
    ask.add_argument("question", nargs="+")

    subparsers.add_parser(
        "sessions",
        help="List recent sessions.",
        parents=[public_options],
    )
    subparsers.add_parser(
        "doctor",
        help="Run CLI health checks.",
        parents=[public_options],
    )
    return parser
```

Keep `apply_profile_override(argv)` unchanged so profile resolution still happens before CLI home path resolution.

- [ ] **Step 4: Run parser tests and verify pass**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_parser_accepts_public_options_after_doctor_subcommand tests/test_agent_cli_main.py::test_parser_accepts_public_options_after_sessions_subcommand tests/test_agent_cli_main.py::test_parser_accepts_model_after_ask_subcommand_and_preserves_question tests/test_agent_cli_main.py::test_main_invalid_profile_after_subcommand_returns_code_2 -q
```

Expected: PASS.

- [ ] **Step 5: Run main CLI tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit parser changes**

```bash
git add agent_cli/main.py tests/test_agent_cli_main.py
git commit -m "feat: accept agent cli options after subcommands"
```

---

### Task 2: Expand Doctor Checks and Stable Output

**Files:**
- Modify: `agent_cli/doctor.py`
- Modify: `agent_cli/main.py`
- Test: `tests/test_agent_cli_doctor.py`
- Test: `tests/test_agent_cli_main.py`

- [ ] **Step 1: Replace doctor tests with WARN/FAIL expectations**

Update `tests/test_agent_cli_doctor.py` imports to include the new helpers:

```python
from agent_cli.doctor import (
    HealthCheck,
    check_dependencies,
    check_openai_api_key,
    check_python_version,
    check_workdir,
    doctor_exit_code,
    render_doctor_output,
    run_health_checks,
)
```

Replace `test_render_doctor_output_shows_status` with:

```python
def test_render_doctor_output_uses_stable_status_columns():
    results = [
        HealthCheck("Python Version", "OK", "Python 3.11"),
        HealthCheck("OPENAI_API_KEY", "WARN", "not set"),
        HealthCheck("SQLite DB", "FAIL", "cannot open"),
    ]

    output = render_doctor_output(results)

    assert "OK    Python Version" in output
    assert "WARN  OPENAI_API_KEY" in output
    assert "FAIL  SQLite DB" in output
    assert "✓" not in output
    assert "✗" not in output
```

Append:

```python
def test_doctor_exit_code_is_zero_for_ok_and_warn():
    checks = [
        HealthCheck("python", "OK", "ok"),
        HealthCheck("key", "WARN", "missing"),
    ]

    assert doctor_exit_code(checks) == 0


def test_doctor_exit_code_is_one_for_failures():
    checks = [
        HealthCheck("python", "OK", "ok"),
        HealthCheck("db", "FAIL", "bad"),
    ]

    assert doctor_exit_code(checks) == 1


def test_check_openai_api_key_warns_when_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = check_openai_api_key()

    assert result.status == "WARN"
    assert result.name == "OPENAI_API_KEY"


def test_check_openai_api_key_ok_when_present(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    result = check_openai_api_key()

    assert result.status == "OK"
    assert "set" in result.message
```

- [ ] **Step 2: Add storage and background doctor tests**

Append to `tests/test_agent_cli_doctor.py`:

```python
def test_run_health_checks_reports_storage_and_openai_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)
    by_name = {item.name: item for item in results}

    assert by_name["CLI Home"].status == "OK"
    assert by_name["SQLite DB"].status == "OK"
    assert by_name["Logs"].status == "OK"
    assert by_name["OPENAI_API_KEY"].status == "WARN"


def test_doctor_reports_stale_background_task_as_warn(tmp_path):
    from agent_cli.background import BackgroundTaskStore

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_stale",
        session_id="session-stale",
        title="Stale",
        prompt_preview="work",
        owner_id=None,
    )

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)
    background = next(item for item in results if item.name == "Background Tasks")

    assert background.status == "WARN"
    assert "stale" in background.message.lower() or "owner" in background.message.lower()
```

- [ ] **Step 3: Update main doctor exit-code test**

In `tests/test_agent_cli_main.py`, update `test_main_doctor_returns_1_when_health_check_fails` so it patches `doctor_exit_code` only through doctor module behavior. After implementation, the existing test can remain, but add this test:

```python
def test_main_doctor_returns_0_for_warn_only(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.doctor as doctor_module

    original_run = doctor_module.run_health_checks
    doctor_module.run_health_checks = lambda workdir, tmp_path=None, cli_home=None: [
        doctor_module.HealthCheck("OPENAI_API_KEY", "WARN", "not set")
    ]

    try:
        import agent_cli.main as main_module

        code = main_module.main(["doctor"])

        assert code == 0
    finally:
        doctor_module.run_health_checks = original_run
```

- [ ] **Step 4: Run doctor tests and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_doctor.py tests/test_agent_cli_main.py::test_main_doctor_returns_0_for_warn_only -q
```

Expected: FAIL because doctor does not yet support `WARN`, `doctor_exit_code`, storage checks, or API key checks.

- [ ] **Step 5: Implement doctor status helpers**

Replace the top of `agent_cli/doctor.py` with these definitions and keep existing imports that are still needed:

```python
from __future__ import annotations

import importlib
import os
import platform
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_cli.config import ConfigError, settings_from_config
from agent_cli.paths import get_cli_home, get_db_path
from agent_cli.session_store import SessionStore


DoctorStatus = Literal["OK", "WARN", "FAIL"]


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: DoctorStatus
    message: str


def ok(name: str, message: str) -> HealthCheck:
    return HealthCheck(name, "OK", message)


def warn(name: str, message: str) -> HealthCheck:
    return HealthCheck(name, "WARN", message)


def fail(name: str, message: str) -> HealthCheck:
    return HealthCheck(name, "FAIL", message)
```

- [ ] **Step 6: Implement dependency and environment checks**

In `agent_cli/doctor.py`, replace the old check functions with:

```python
def check_python_version() -> HealthCheck:
    version = sys.version_info
    message = f"Python {version.major}.{version.minor}.{version.micro}"
    if version >= (3, 11):
        return ok("Python Version", message)
    return fail("Python Version", f"{message}; Python >= 3.11 is required")


def check_platform() -> HealthCheck:
    return ok("Platform", platform.platform())


def check_dependencies() -> list[HealthCheck]:
    checks: list[HealthCheck] = []
    imports = [
        ("prompt_toolkit", "prompt_toolkit"),
        ("langchain", "langchain"),
        ("langgraph", "langgraph"),
        ("langgraph-checkpoint-sqlite", "langgraph.checkpoint.sqlite"),
    ]
    for display, module_name in imports:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            checks.append(fail(display, f"missing import {module_name}: {exc}"))
        else:
            checks.append(ok(display, "available"))
    return checks


def check_openai_api_key() -> HealthCheck:
    if os.getenv("OPENAI_API_KEY"):
        return ok("OPENAI_API_KEY", "set")
    return warn("OPENAI_API_KEY", "not set")


def check_workdir(workdir: str) -> HealthCheck:
    path = Path(workdir)
    if not path.exists():
        return fail("Workdir", f"does not exist: {workdir}")
    if not path.is_dir():
        return fail("Workdir", f"not a directory: {workdir}")
    return ok("Workdir", str(path))
```

- [ ] **Step 7: Implement storage, config, dotenv, and background checks**

Add to `agent_cli/doctor.py`:

```python
def _write_probe(directory: Path, filename: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / filename
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)


def check_cli_home(cli_home: Path) -> HealthCheck:
    try:
        _write_probe(cli_home, ".doctor-write-test")
    except Exception as exc:
        return fail("CLI Home", f"not writable at {cli_home}: {exc}")
    return ok("CLI Home", str(cli_home))


def check_sqlite_db(db_path: Path) -> HealthCheck:
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS doctor_probe (id INTEGER)")
            conn.execute("DROP TABLE doctor_probe")
    except Exception as exc:
        return fail("SQLite DB", f"cannot open {db_path}: {exc}")
    return ok("SQLite DB", str(db_path))


def check_logs(cli_home: Path) -> HealthCheck:
    logs_dir = cli_home / "logs"
    try:
        _write_probe(logs_dir, ".doctor-write-test")
    except Exception as exc:
        return fail("Logs", f"not writable at {logs_dir}: {exc}")
    return ok("Logs", str(logs_dir))


def check_config(cli_home: Path) -> HealthCheck:
    config_path = cli_home / "config.yaml"
    if not config_path.exists():
        return ok("Config", f"optional file missing: {config_path}")
    try:
        settings_from_config(cli_home=cli_home, profile=None, cli_model=None)
        return ok("Config", f"config readable: {config_path}")
    except ConfigError as exc:
        return fail("Config", str(exc))
    except Exception as exc:
        return fail("Config", f"cannot read {config_path}: {exc}")


def check_dotenv(cli_home: Path, cwd: Path) -> HealthCheck:
    env_paths = [cli_home / ".env", cwd / ".env"]
    existing = [path for path in env_paths if path.exists()]
    if not existing:
        return ok("Dotenv", f"no .env files found; checked {env_paths[0]} and {env_paths[1]}")
    try:
        for path in existing:
            path.read_text(encoding="utf-8")
    except Exception as exc:
        return fail("Dotenv", f"dotenv file cannot be read: {exc}")
    return ok("Dotenv", "visible .env files: " + ", ".join(str(path) for path in existing))


def check_background_tasks(db_path: Path) -> HealthCheck:
    try:
        from agent_cli.background import ACTIVE_TASK_STATUSES, BackgroundTaskStore

        store = BackgroundTaskStore(db_path)
        rows = store.list_tasks(statuses=ACTIVE_TASK_STATUSES, limit=200)
        stale = [row for row in rows if getattr(row, "cancel_requested", False)]
        ownerless = 0
        with store.connect() as conn:
            ownerless = conn.execute(
                "SELECT COUNT(*) FROM cli_background_tasks "
                "WHERE status IN ('queued', 'running', 'waiting_approval', 'completing', 'stopping') "
                "AND owner_id IS NULL"
            ).fetchone()[0]
    except Exception as exc:
        return fail("Background Tasks", f"cannot inspect background task metadata: {exc}")
    if ownerless:
        return warn("Background Tasks", f"{ownerless} active task(s) have no owner metadata")
    if stale:
        return warn("Background Tasks", f"{len(stale)} active task(s) look stale")
    return ok("Background Tasks", "metadata readable")
```

- [ ] **Step 8: Implement run/render/exit functions**

Replace `run_health_checks` and `render_doctor_output` in `agent_cli/doctor.py` with:

```python
def run_health_checks(
    workdir: str, tmp_path: Path | None = None, cli_home: Path | None = None
) -> list[HealthCheck]:
    if cli_home is None:
        cli_home = get_cli_home()
    db_path = get_db_path()
    cwd = Path.cwd()

    results: list[HealthCheck] = [
        check_python_version(),
        check_platform(),
        *check_dependencies(),
        check_workdir(workdir),
        check_cli_home(cli_home),
        check_sqlite_db(db_path),
        check_logs(cli_home),
        check_config(cli_home),
        check_dotenv(cli_home, cwd),
        check_openai_api_key(),
        check_background_tasks(db_path),
    ]
    return results


def doctor_exit_code(results: list[HealthCheck]) -> int:
    return 1 if any(result.status == "FAIL" for result in results) else 0


def render_doctor_output(results: list[HealthCheck]) -> str:
    lines = ["=== CLI Health Check ===", ""]
    name_width = max([len(result.name) for result in results] + [4])
    for result in results:
        lines.append(f"{result.status:<5} {result.name:<{name_width}}  {result.message}")
    return "\n".join(lines)
```

Note: if `get_db_path()` can be affected by explicit `cli_home`, use `cli_home / "cli.sqlite"` instead. Keep the behavior aligned with `agent_cli.paths.get_db_path()`.

- [ ] **Step 9: Update `main.py` to use doctor exit helper**

In `agent_cli/main.py`, replace the doctor branch with:

```python
    if command == "doctor":
        from agent_cli.doctor import doctor_exit_code, render_doctor_output, run_health_checks

        workdir = str(Path(args.workdir).expanduser().resolve()) if args.workdir else os.getcwd()
        results = run_health_checks(workdir=workdir, cli_home=cli_home)
        print(render_doctor_output(results))
        return doctor_exit_code(results)
```

- [ ] **Step 10: Run doctor tests and fix compatibility issues**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_doctor.py tests/test_agent_cli_main.py -q
```

Expected: PASS. If existing tests still expect tuple return values from `check_python_version`, update them to assert on `HealthCheck.status` and `HealthCheck.message`.

- [ ] **Step 11: Commit doctor changes**

```bash
git add agent_cli/doctor.py agent_cli/main.py tests/test_agent_cli_doctor.py tests/test_agent_cli_main.py
git commit -m "feat: expand agent cli doctor checks"
```

---

### Task 3: Add Shared History Layer and Improve Session Status/Export

**Files:**
- Create: `agent_cli/history.py`
- Modify: `agent_cli/session.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_history.py`
- Test: `tests/test_agent_cli_session.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Add history tests**

Create `tests/test_agent_cli_history.py`:

```python
from types import SimpleNamespace

from agent_cli.history import (
    TranscriptMessage,
    content_to_text,
    filter_transcript_messages,
    render_history_markdown,
    render_history_text,
    transcript_stats,
)


def test_content_to_text_handles_strings_dicts_and_text_blocks():
    assert content_to_text("hello") == "hello"
    assert content_to_text({"text": "hello"}) == "hello"
    assert content_to_text([{"type": "text", "text": "hello"}, "world"]) == "hello\nworld"


def test_filter_transcript_messages_supports_dicts_and_objects():
    messages = [
        {"role": "system", "content": "ignore"},
        {"role": "user", "content": "hi"},
        SimpleNamespace(type="ai", content=[{"type": "text", "text": "hello"}]),
        {"role": "tool", "content": "ignore"},
    ]

    transcript = filter_transcript_messages(messages)

    assert transcript == [
        TranscriptMessage(role="user", content="hi"),
        TranscriptMessage(role="assistant", content="hello"),
    ]


def test_transcript_stats_counts_messages_and_user_turns():
    transcript = [
        TranscriptMessage(role="user", content="one"),
        TranscriptMessage(role="assistant", content="two"),
        TranscriptMessage(role="user", content="three"),
    ]

    stats = transcript_stats(transcript)

    assert stats.message_count == 3
    assert stats.turn_count == 2


def test_render_history_text_uses_filtered_roles():
    transcript = [
        TranscriptMessage(role="user", content="hi"),
        TranscriptMessage(role="assistant", content="hello"),
    ]

    output = render_history_text(transcript)

    assert "User:" in output
    assert "Assistant:" in output
    assert "hello" in output


def test_render_history_markdown_includes_metadata():
    transcript = [TranscriptMessage(role="user", content="hi")]

    output = render_history_markdown(
        transcript,
        session_id="s1",
        title="Title",
        workdir="/repo",
        model="model",
        exported_at="2026-05-27T00:00:00+00:00",
    )

    assert "# Session s1" in output
    assert "- Title: Title" in output
    assert "- Workdir: /repo" in output
    assert "- Model: model" in output
    assert "- Messages: 1" in output
    assert "- Turns: 1" in output
    assert "## User" in output
```

- [ ] **Step 2: Run history tests and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_history.py -q
```

Expected: FAIL because `agent_cli.history` does not exist.

- [ ] **Step 3: Implement `agent_cli/history.py`**

Create `agent_cli/history.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TranscriptMessage:
    role: str
    content: str


@dataclass(frozen=True)
class TranscriptStats:
    message_count: int
    turn_count: int


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or message.get("type") or "")
    return str(getattr(message, "role", "") or getattr(message, "type", ""))


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", "")


def normalize_role(role: str) -> str | None:
    value = role.lower()
    if value in {"user", "human"}:
        return "user"
    if value in {"assistant", "ai"}:
        return "assistant"
    return None


def content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [content_to_text(item) for item in content]
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("content"), str):
            return content["content"]
        return ""
    return str(content)


def filter_transcript_messages(messages: list[Any]) -> list[TranscriptMessage]:
    transcript: list[TranscriptMessage] = []
    for message in messages:
        role = normalize_role(_message_role(message))
        if role is None:
            continue
        text = content_to_text(_message_content(message))
        if text:
            transcript.append(TranscriptMessage(role=role, content=text))
    return transcript


def transcript_stats(transcript: list[TranscriptMessage]) -> TranscriptStats:
    return TranscriptStats(
        message_count=len(transcript),
        turn_count=sum(1 for message in transcript if message.role == "user"),
    )


def render_history_text(transcript: list[TranscriptMessage]) -> str:
    if not transcript:
        return "No messages in session history.\n"
    lines = ["Session History:", ""]
    for message in transcript:
        label = "User" if message.role == "user" else "Assistant"
        lines.append(f"{label}:")
        lines.append(message.content)
        lines.append("")
    return "\n".join(lines)


def render_history_markdown(
    transcript: list[TranscriptMessage],
    *,
    session_id: str,
    title: str,
    workdir: str,
    model: str | None,
    exported_at: str,
) -> str:
    stats = transcript_stats(transcript)
    lines = [
        f"# Session {session_id}",
        "",
        f"- Title: {title}",
        f"- Workdir: {workdir}",
        f"- Model: {model or 'default'}",
        f"- Exported at: {exported_at}",
        f"- Messages: {stats.message_count}",
        f"- Turns: {stats.turn_count}",
        "",
    ]
    for message in transcript:
        heading = "User" if message.role == "user" else "Assistant"
        lines.extend([f"## {heading}", "", message.content, ""])
    return "\n".join(lines)


def load_thread_transcript(checkpointer: Any, thread_id: str) -> list[TranscriptMessage]:
    from agent_cli.checkpoints import extract_messages_from_checkpoints

    return filter_transcript_messages(
        extract_messages_from_checkpoints(checkpointer, thread_id)
    )
```

- [ ] **Step 4: Run history tests and verify pass**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_history.py -q
```

Expected: PASS.

- [ ] **Step 5: Add session status/export tests**

Append to `tests/test_agent_cli_session.py`:

```python
def test_session_status_includes_runtime_metadata_and_counts(monkeypatch, tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    session = Session(
        session_store=store,
        session_id="status-test",
        model_name="model",
        session_store_for_checkpoints="cp",
        workdir=str(tmp_path),
        profile="dev",
        display_theme="slate",
        cli_home=str(tmp_path),
        db_path=str(tmp_path / "cli.sqlite"),
    )

    monkeypatch.setattr(
        "agent_cli.history.load_thread_transcript",
        lambda checkpointer, thread_id: [
            __import__("agent_cli.history").history.TranscriptMessage("user", "hi"),
            __import__("agent_cli.history").history.TranscriptMessage("assistant", "hello"),
        ],
    )

    status = session.status()

    assert status.message_count == 2
    assert status.turn_count == 1
    assert status.profile == "dev"
    assert status.display_theme == "slate"
    assert status.cli_home == str(tmp_path)
    assert status.db_path == str(tmp_path / "cli.sqlite")


def test_session_export_relative_path_resolves_against_workdir(monkeypatch, tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    workdir = tmp_path / "repo"
    workdir.mkdir()
    session = Session(
        session_store=store,
        session_id="export-relative",
        model_name="model",
        session_store_for_checkpoints="cp",
        workdir=str(workdir),
    )

    monkeypatch.setattr(
        "agent_cli.history.load_thread_transcript",
        lambda checkpointer, thread_id: [
            __import__("agent_cli.history").history.TranscriptMessage("user", "hi"),
        ],
    )

    result = session.export_markdown("exports/session.md")
    export_path = workdir / "exports" / "session.md"

    assert export_path.exists()
    assert "Exported 1 messages" in result
    assert "- Workdir: " + str(workdir) in export_path.read_text(encoding="utf-8")
```

- [ ] **Step 6: Update `SessionStatus` and `Session`**

In `agent_cli/session.py`, update imports and dataclass:

```python
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_cli.history import (
    load_thread_transcript,
    render_history_markdown,
    render_history_text,
    transcript_stats,
)
```

Replace `SessionStatus` with:

```python
@dataclass
class SessionStatus:
    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    workdir: str
    message_count: int
    turn_count: int
    model_name: str | None
    profile: str | None = None
    display_theme: str = "default"
    cli_home: str | None = None
    db_path: str | None = None
    checkpointer_available: bool = False
```

Extend `Session.__init__` signature and assignments:

```python
        profile: str | None = None,
        display_theme: str = "default",
        cli_home: str | None = None,
        db_path: str | None = None,
```

```python
        self.profile = profile
        self.display_theme = display_theme
        self.cli_home = cli_home
        self.db_path = db_path
```

- [ ] **Step 7: Replace session history/status/export methods**

In `agent_cli/session.py`, replace `history`, `export_markdown`, and `status` with:

```python
    def transcript(self):
        return load_thread_transcript(self.session_store_for_checkpoints, self.session_id)

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        transcript = self.transcript()
        if limit:
            transcript = transcript[-limit:]
        return [
            {"role": message.role, "content": message.content}
            for message in transcript
        ]

    def export_markdown(self, path: str | Path) -> str:
        transcript = self.transcript()
        if not transcript:
            return "No messages to export."

        record = self.session_store.get_session(self.session_id)
        title = record.title if record else "New session"
        output_path = Path(path)
        if not output_path.is_absolute():
            output_path = Path(self.workdir) / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        exported_at = datetime.now(timezone.utc).isoformat()
        output_path.write_text(
            render_history_markdown(
                transcript,
                session_id=self.session_id,
                title=title,
                workdir=self.workdir,
                model=self.model_name,
                exported_at=exported_at,
            ),
            encoding="utf-8",
        )
        return f"Exported {len(transcript)} messages to {output_path}"

    def status(self) -> SessionStatus:
        record = self.session_store.get_session(self.session_id)
        transcript = self.transcript()
        stats = transcript_stats(transcript)
        return SessionStatus(
            session_id=self.session_id,
            title=record.title if record else "New session",
            created_at=_parse_datetime(record.created_at if record else None),
            updated_at=_parse_datetime(record.updated_at if record else None),
            workdir=record.workdir if record else self.workdir,
            message_count=stats.message_count,
            turn_count=stats.turn_count,
            model_name=self.model_name,
            profile=self.profile,
            display_theme=self.display_theme,
            cli_home=self.cli_home,
            db_path=self.db_path,
            checkpointer_available=self.session_store_for_checkpoints is not None,
        )
```

- [ ] **Step 8: Replace render helpers**

In `agent_cli/session.py`, replace `render_session_status` and `render_history` with:

```python
def render_session_status(session: Session) -> str:
    status = session.status()
    return "\n".join(
        [
            "Session Status:",
            f"  Session ID: {status.session_id}",
            f"  Title: {status.title}",
            f"  Created: {status.created_at.strftime('%Y-%m-%d %H:%M')}",
            f"  Updated: {status.updated_at.strftime('%Y-%m-%d %H:%M')}",
            f"  Workdir: {status.workdir}",
            f"  Model: {status.model_name or 'default'}",
            f"  Profile: {status.profile or 'default'}",
            f"  Theme: {status.display_theme}",
            f"  CLI Home: {status.cli_home or 'default'}",
            f"  DB Path: {status.db_path or 'default'}",
            f"  Messages: {status.message_count}",
            f"  Turns: {status.turn_count}",
            f"  Checkpointer: {'available' if status.checkpointer_available else 'unavailable'}",
        ]
    ) + "\n"


def render_history(session: Session, limit: int | None = None) -> str:
    transcript = session.transcript()
    if limit:
        transcript = transcript[-limit:]
    return render_history_text(transcript)
```

Remove `_is_user_or_assistant` if it becomes unused.

- [ ] **Step 9: Pass runtime metadata from `AgentCLI`**

In `agent_cli/repl.py`, every `Session(...)` construction should include:

```python
                profile=self.profile,
                display_theme=self.display_theme,
                cli_home=self.cli_home,
                db_path=str(self.session_store.db_path) if hasattr(self.session_store, "db_path") else None,
```

Apply this in:

- constructor resume path
- `ensure_session`
- `/new`
- `/resume`

- [ ] **Step 10: Return usage for invalid `/history` limits**

In `agent_cli/repl.py`, replace:

```python
                except ValueError:
                    pass
```

with:

```python
                except ValueError:
                    return "Usage: /history [N]"
```

- [ ] **Step 11: Add repl test for invalid history limit**

Append to `tests/test_agent_cli_repl.py`:

```python
def test_handle_command_history_rejects_invalid_limit():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    result = cli.handle_command("/history nope")

    assert result == "Usage: /history [N]"
```

- [ ] **Step 12: Run session/history/repl tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_history.py tests/test_agent_cli_session.py tests/test_agent_cli_repl.py -q
```

Expected: PASS. If fake stores lack `db_path`, the `hasattr` guard in Step 9 should keep tests compatible.

- [ ] **Step 13: Commit history/session changes**

```bash
git add agent_cli/history.py agent_cli/session.py agent_cli/repl.py tests/test_agent_cli_history.py tests/test_agent_cli_session.py tests/test_agent_cli_repl.py
git commit -m "feat: improve agent cli session history export"
```

---

### Task 4: Update README and Add Documentation Smoke Test

**Files:**
- Modify: `README.md`
- Test: `tests/test_agent_cli_commands.py`

- [ ] **Step 1: Add README smoke test**

Append to `tests/test_agent_cli_commands.py`:

```python
def test_readme_agent_cli_docs_cover_current_capabilities():
    from pathlib import Path

    text = Path("README.md").read_text(encoding="utf-8")

    assert "--profile" in text
    assert "config.yaml" in text
    assert "/background" in text
    assert "/tasks" in text
    assert "/queue" in text
    assert "/steer" in text
    assert "/approve" in text
    assert "/stop" in text
    assert "logs" in text.lower()
    assert "OPENAI_API_KEY" in text
    assert "edit JSON" in text or "edit" in text
```

- [ ] **Step 2: Run README smoke test and verify failure**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py::test_readme_agent_cli_docs_cover_current_capabilities -q
```

Expected: FAIL because README does not yet document these capabilities.

- [ ] **Step 3: Replace Agent CLI README section**

In `README.md`, replace the section from `## Agent CLI` through the existing CLI command bullets with:

```markdown
## Agent CLI

This repository includes a local terminal CLI for the LangGraph agent. It
supports interactive chat, one-shot questions, session resume, local health
checks, profile-specific state, skill commands, human approval decisions, and
in-process background tasks.

Chat and ask modes require the LangGraph SQLite checkpointer package
(`langgraph-checkpoint-sqlite`) because conversation state is stored in SQLite.
`sessions`, `doctor`, and `--help` do not need to start an agent model call.

```bash
python -m agent_cli
python -m agent_cli chat --resume <session_id>
python -m agent_cli ask "Summarize this repository"
python -m agent_cli sessions
python -m agent_cli doctor --workdir /home/miku/projects/langchain
```

Public options can be passed before or after a subcommand:

```bash
python -m agent_cli --profile dev doctor --workdir /repo
python -m agent_cli doctor --profile dev --workdir /repo
python -m agent_cli ask --model gpt-4.1 "hello"
```

Options:

- `--workdir <path>`: workspace directory for the CLI session.
- `--model <name>`: model display metadata for session lists and status.
- `--profile, -p <name>`: use `~/.langchain-agent/profiles/<name>` as CLI home
  unless `AGENT_CLI_HOME` is explicitly set.

State locations:

- Default CLI home: `~/.langchain-agent`
- Override: `AGENT_CLI_HOME=/path/to/home`
- SQLite database: `<cli_home>/cli.sqlite`
- Prompt history: `<cli_home>/history.txt`
- Logs: `<cli_home>/logs/agent.log` and `<cli_home>/logs/errors.log`

Optional `<cli_home>/config.yaml`:

```yaml
display:
  markdown: render
  theme: default
model:
  name: gpt-4.1
session:
  default_title: New session
```

Inside chat, use `/help` to list slash commands.

Session commands:

- `/status` - Show session metadata, workdir, model, profile, storage paths, and message counts.
- `/title <name>` - Set session title.
- `/history [N]` - Show filtered user/assistant history.
- `/export <path.md>` - Export filtered history to Markdown.
- `/new` - Start a new session.
- `/resume <session_id>` - Resume a previous session.
- `/sessions` - List recent sessions.

Background commands:

- `/background <prompt>` - Start an in-process background task in a new session.
- `/tasks` - List background tasks.
- `/queue` - Show active background work.
- `/steer <task_id> <message>` - Queue a steering message for a task.
- `/approve <task_id>` - Continue a task waiting for human approval.
- `/stop <task_id>` - Request cooperative stop.

Skill and utility commands:

- `/skills` - List available local skills and dynamic skill slash commands.
- `/skill <name>` - Show skill details.
- `/doctor` - Run local health checks.
- `/clear` - Clear the terminal.
- `/exit` - Exit the CLI.

Human approval prompts support approving, rejecting with a message, editing tool
arguments as JSON, responding to the agent, and approving or rejecting all
remaining requests.

`python -m agent_cli doctor` prints stable `OK`, `WARN`, and `FAIL` lines. WARN
does not make the command fail; any FAIL exits with code `1`.

Troubleshooting:

- Missing `langgraph-checkpoint-sqlite`: install it in the project environment.
- Missing `OPENAI_API_KEY`: `doctor` reports WARN; chat may still fail if the configured model requires OpenAI.
- Malformed `config.yaml`: `doctor` reports FAIL and chat startup returns code `2`.
- SQLite or CLI home not writable: choose another `AGENT_CLI_HOME` or fix permissions.
- Unknown session id: run `python -m agent_cli sessions` and retry with a listed id.
- Stale background task warnings: restart the CLI and inspect `/tasks`; stale rows are metadata only unless a live worker exists.
```

- [ ] **Step 4: Run README smoke test**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py::test_readme_agent_cli_docs_cover_current_capabilities -q
```

Expected: PASS.

- [ ] **Step 5: Commit README update**

```bash
git add README.md tests/test_agent_cli_commands.py
git commit -m "docs: update agent cli usage documentation"
```

---

### Task 5: Full Verification

**Files:**
- No edits unless verification finds a bug.

- [ ] **Step 1: Run focused Agent CLI tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py tests/test_agent_cli_doctor.py tests/test_agent_cli_history.py tests/test_agent_cli_session.py tests/test_agent_cli_repl.py tests/test_agent_cli_commands.py tests/test_agent_cli_input.py tests/test_agent_cli_background.py -q
```

Expected: PASS.

- [ ] **Step 2: Run smoke commands**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m agent_cli --help
AGENT_CLI_HOME="$(mktemp -d)" /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli sessions
AGENT_CLI_HOME="$(mktemp -d)" /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli doctor --workdir /home/miku/projects/langchain
```

Expected:

- help exits `0` and lists `chat`, `ask`, `sessions`, and `doctor`
- sessions exits `0`
- doctor exits `0` if only WARN checks exist, or `1` if an actual FAIL is present; output uses `OK/WARN/FAIL`

- [ ] **Step 3: Inspect git status**

Run:

```bash
git status --short
```

Expected: only unrelated pre-existing user changes may remain. Implementation files from this plan should be committed.


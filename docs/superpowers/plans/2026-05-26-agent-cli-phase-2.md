# Agent CLI Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add richer approval decisions, lightweight profile/config loading, and skill slash commands to the project-native Agent CLI.

**Architecture:** Keep Phase 1's small CLI modules and add three focused helpers: `agent_cli.approval`, `agent_cli.config`, and `agent_cli.skill_commands`. `main.py` remains the startup boundary, `repl.py` remains the REPL/agent orchestration boundary, and LangGraph SQLite checkpointing remains the conversation state store.

**Tech Stack:** Python, argparse, prompt_toolkit, python-dotenv when installed, PyYAML when installed, LangGraph `Command(resume=...)`, pytest.

---

## File Structure

- Create `agent_cli/approval.py`: approval prompt loop, args summaries, JSON edit parsing, decision construction.
- Create `agent_cli/config.py`: profile validation, profile home resolution, dotenv loading, YAML config loading, runtime settings object.
- Create `agent_cli/skill_commands.py`: dynamic skill command registry, conflict detection, skill invocation message builder.
- Modify `agent_cli/interrupts.py`: preserve paired `action_requests` and `review_configs`, keep compatibility helpers.
- Modify `agent_cli/repl.py`: use approval helper in `_handle_interrupts`, route dynamic skill slash commands, use configured default title.
- Modify `agent_cli/main.py`: add `--profile`, apply profile early, load dotenv/config before constructing stores, pass runtime settings to `AgentCLI`, return doctor failure code when needed.
- Modify `agent_cli/paths.py`: support explicit and profile-derived CLI home without caching stale paths.
- Modify `agent_cli/input.py`: include dynamic skill commands in slash completion.
- Modify `agent_cli/commands.py`: keep built-in commands authoritative and add a concise help note for dynamic skill commands.
- Modify `agent_cli/doctor.py`: report profile/config/dotenv visibility.
- Add or modify tests under `tests/test_agent_cli_*.py`.

Implementation should use `/home/miku/miniforge3/envs/langchain/bin/python` for pytest commands.

---

## Task 1: Approval Data Model and Interrupt Parsing

**Files:**
- Create: `agent_cli/approval.py`
- Modify: `agent_cli/interrupts.py`
- Test: `tests/test_agent_cli_interrupts.py`

- [ ] **Step 1: Write failing tests for paired action requests and review configs**

Add these tests to `tests/test_agent_cli_interrupts.py`:

```python
from agent_cli.interrupts import extract_interrupt_review_requests


def test_extract_interrupt_review_requests_preserves_review_config_pairing():
    result = {
        "__interrupt__": [
            {
                "value": {
                    "action_requests": [
                        {"name": "terminal", "args": {"command": "rm build"}},
                        {"name": "write_file", "args": {"path": "x.txt", "content": "hi"}},
                    ],
                    "review_configs": [
                        {"description": "Shell command requires review."},
                        {"description": "File write requires review."},
                    ],
                }
            }
        ]
    }

    requests = extract_interrupt_review_requests(result)

    assert [item.action_request["name"] for item in requests] == ["terminal", "write_file"]
    assert requests[0].review_config["description"] == "Shell command requires review."
    assert requests[1].review_config["description"] == "File write requires review."


def test_extract_interrupt_review_requests_handles_missing_review_configs():
    result = {
        "__interrupt__": [
            {
                "value": {
                    "action_requests": [
                        {"name": "terminal", "args": {"command": "pwd"}},
                    ],
                }
            }
        ]
    }

    requests = extract_interrupt_review_requests(result)

    assert len(requests) == 1
    assert requests[0].action_request["name"] == "terminal"
    assert requests[0].review_config == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_interrupts.py::test_extract_interrupt_review_requests_preserves_review_config_pairing tests/test_agent_cli_interrupts.py::test_extract_interrupt_review_requests_handles_missing_review_configs -v
```

Expected: FAIL because `extract_interrupt_review_requests` does not exist.

- [ ] **Step 3: Implement review request extraction**

Create `agent_cli/approval.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApprovalRequest:
    action_request: dict[str, Any]
    review_config: dict[str, Any]

    @property
    def tool_name(self) -> str:
        name = self.action_request.get("name")
        return str(name) if name else "unknown"

    @property
    def args(self) -> Any:
        return self.action_request.get("args") or {}

    @property
    def description(self) -> str:
        value = self.review_config.get("description") or self.review_config.get("risk")
        return str(value) if value else ""
```

Update `agent_cli/interrupts.py`:

```python
from agent_cli.approval import ApprovalRequest
```

Add:

```python
def extract_interrupt_review_requests(result: Any) -> list[ApprovalRequest]:
    if not has_interrupt(result):
        return []
    raw_items = result.get(INTERRUPT_KEY) or []
    if not isinstance(raw_items, (list, tuple)):
        raw_items = [raw_items]

    paired: list[ApprovalRequest] = []
    for item in raw_items:
        value = _unwrap_interrupt_value(item)
        if not isinstance(value, dict):
            paired.append(ApprovalRequest({"raw": value}, {}))
            continue

        action_requests = value.get("action_requests")
        review_configs = value.get("review_configs") or []
        if not isinstance(action_requests, list):
            paired.append(ApprovalRequest(value, {}))
            continue

        if not isinstance(review_configs, list):
            review_configs = []

        for index, request in enumerate(action_requests):
            action = request if isinstance(request, dict) else {"raw": request}
            config = review_configs[index] if index < len(review_configs) else {}
            if not isinstance(config, dict):
                config = {"raw": config}
            paired.append(ApprovalRequest(action, config))
    return paired
```

Keep `extract_interrupt_requests()` working by deriving from the new function:

```python
def extract_interrupt_requests(result: Any) -> list[dict[str, Any]]:
    requests = extract_interrupt_review_requests(result)
    return [item.action_request for item in requests] or [{"raw": result.get(INTERRUPT_KEY)}]
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_interrupts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/approval.py agent_cli/interrupts.py tests/test_agent_cli_interrupts.py
git commit -m "feat: preserve cli approval review configs"
```

---

## Task 2: Approval Decision Prompt and Resume Values

**Files:**
- Modify: `agent_cli/approval.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_interrupts.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing tests for decision construction**

Add to `tests/test_agent_cli_interrupts.py`:

```python
from agent_cli.approval import ApprovalRequest, collect_approval_decisions, summarize_args


def test_summarize_args_prefers_command_and_path():
    assert summarize_args({"command": "pytest tests", "cwd": "/repo"}) == 'command="pytest tests"'
    assert summarize_args({"path": "README.md", "content": "x" * 200}) == 'path="README.md"'


def test_collect_approval_decisions_supports_approve_reject_respond_edit():
    requests = [
        ApprovalRequest({"name": "terminal", "args": {"command": "pwd"}}, {"description": "review"}),
        ApprovalRequest({"name": "memory_manage", "args": {"action": "set"}}, {}),
        ApprovalRequest({"name": "skill_manage", "args": {"name": "old"}}, {}),
        ApprovalRequest({"name": "write_file", "args": {"path": "a.txt"}}, {}),
    ]
    answers = iter([
        "y",
        "n", "not useful",
        "r", "please explain first",
        "e", '{"name": "new"}',
    ])

    resume = collect_approval_decisions(requests, input_func=lambda prompt: next(answers), print_func=lambda text="": None)

    assert resume == {
        "decisions": [
            {"type": "approve"},
            {"type": "reject", "message": "not useful"},
            {"type": "respond", "message": "please explain first"},
            {"type": "edit", "args": {"name": "new"}},
        ]
    }


def test_collect_approval_decisions_supports_approve_all_and_reject_all():
    requests = [
        ApprovalRequest({"name": "terminal", "args": {"command": "pwd"}}, {}),
        ApprovalRequest({"name": "write_file", "args": {"path": "a.txt"}}, {}),
    ]

    approved = collect_approval_decisions(
        requests,
        input_func=lambda prompt: "a",
        print_func=lambda text="": None,
    )
    assert approved == {"decisions": [{"type": "approve"}, {"type": "approve"}]}

    answers = iter(["q", "stop now"])
    rejected = collect_approval_decisions(
        requests,
        input_func=lambda prompt: next(answers),
        print_func=lambda text="": None,
    )
    assert rejected == {
        "decisions": [
            {"type": "reject", "message": "stop now"},
            {"type": "reject", "message": "stop now"},
        ]
    }


def test_collect_approval_decisions_retries_invalid_edit_json():
    requests = [ApprovalRequest({"name": "write_file", "args": {"path": "a.txt"}}, {})]
    answers = iter(["e", "{bad", "e", '{"path": "b.txt"}'])
    printed = []

    resume = collect_approval_decisions(
        requests,
        input_func=lambda prompt: next(answers),
        print_func=printed.append,
    )

    assert resume == {"decisions": [{"type": "edit", "args": {"path": "b.txt"}}]}
    assert any("Invalid JSON" in line for line in printed)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_interrupts.py::test_collect_approval_decisions_supports_approve_reject_respond_edit -v
```

Expected: FAIL because `collect_approval_decisions` does not exist.

- [ ] **Step 3: Implement approval decision collection**

Add to `agent_cli/approval.py`:

```python
import json


def summarize_args(args: Any, *, max_length: int = 160) -> str:
    if isinstance(args, dict):
        for key in ("command", "path", "file_path", "name", "action"):
            if key in args and args[key] not in (None, ""):
                text = f'{key}="{args[key]}"'
                return text if len(text) <= max_length else text[: max_length - 3] + "..."
    try:
        text = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(args)
    return text if len(text) <= max_length else text[: max_length - 3] + "..."


def _message_or_default(value: str, default: str) -> str:
    value = value.strip()
    return value or default


def _parse_json_args(raw: str) -> Any:
    parsed = json.loads(raw)
    if not isinstance(parsed, (dict, list)):
        raise ValueError("Edited args must be a JSON object or array.")
    return parsed


def _render_request(index: int, total: int, request: ApprovalRequest) -> str:
    lines = [
        f"[{index}/{total}] {request.tool_name}",
        f"Args: {summarize_args(request.args)}",
    ]
    if request.description:
        lines.append(f"Risk: {request.description}")
    lines.append("")
    return "\n".join(lines)


def collect_approval_decisions(
    requests: list[ApprovalRequest],
    *,
    input_func=input,
    print_func=print,
) -> dict[str, list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    total = len(requests)
    index = 0
    while index < total:
        request = requests[index]
        print_func(_render_request(index + 1, total, request))
        try:
            answer = input_func("Decision [y/n/e/r/a/q]: ").strip().lower()
        except EOFError:
            message = "Rejected because approval input ended."
            decisions.extend(
                {"type": "reject", "message": message}
                for _ in range(total - index)
            )
            break

        if answer in {"y", "yes"}:
            decisions.append({"type": "approve"})
            index += 1
            continue
        if answer in {"a", "all"}:
            decisions.extend({"type": "approve"} for _ in range(total - index))
            break
        if answer in {"n", "no"}:
            message = _message_or_default(
                input_func("Reject message: "),
                "Rejected by user.",
            )
            decisions.append({"type": "reject", "message": message})
            index += 1
            continue
        if answer in {"q", "quit"}:
            message = _message_or_default(
                input_func("Reject message for all remaining: "),
                "Rejected by user.",
            )
            decisions.extend(
                {"type": "reject", "message": message}
                for _ in range(total - index)
            )
            break
        if answer in {"r", "respond"}:
            message = _message_or_default(
                input_func("Response message: "),
                "Please revise the request.",
            )
            decisions.append({"type": "respond", "message": message})
            index += 1
            continue
        if answer in {"e", "edit"}:
            print_func("Current args JSON:")
            print_func(json.dumps(request.args, ensure_ascii=False, indent=2, sort_keys=True))
            raw = input_func("Edited args JSON: ")
            try:
                edited_args = _parse_json_args(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                print_func(f"Invalid JSON: {exc}")
                continue
            decisions.append({"type": "edit", "args": edited_args})
            index += 1
            continue

        print_func("Choose y, n, e, r, a, or q.")

    return {"decisions": decisions}
```

- [ ] **Step 4: Wire REPL interrupt handling to approval helper**

In `agent_cli/repl.py`, replace the body inside `while has_interrupt(result):` with:

```python
            requests = extract_interrupt_review_requests(result)
            resume_value = collect_approval_decisions(requests)
```

Update imports:

```python
from agent_cli.approval import collect_approval_decisions
from agent_cli.interrupts import extract_interrupt_review_requests, has_interrupt
```

Remove `format_interrupt_summary`, `extract_interrupt_requests`, and `build_resume_value` imports if they are no longer used in `repl.py`.

- [ ] **Step 5: Add REPL resume test**

Add to `tests/test_agent_cli_repl.py`:

```python
def test_handle_interrupts_uses_collected_decisions(monkeypatch, tmp_path):
    from agent_cli.repl import AgentCLI
    from agent_cli.session_store import SessionStore

    calls = []

    class FakeAgent:
        pass

    def runner(agent, input_data, config):
        calls.append(input_data)
        if len(calls) == 1:
            return {
                "__interrupt__": [
                    {
                        "value": {
                            "action_requests": [{"name": "terminal", "args": {"command": "pwd"}}],
                            "review_configs": [{"description": "review"}],
                        }
                    }
                ]
            }
        return {"messages": [{"role": "assistant", "content": "done"}]}

    monkeypatch.setattr(
        "agent_cli.repl.collect_approval_decisions",
        lambda requests: {"decisions": [{"type": "reject", "message": "no"}]},
    )

    cli = AgentCLI(
        session_store=SessionStore(tmp_path / "cli.sqlite"),
        checkpointer=object(),
        agent_factory=lambda checkpointer: FakeAgent(),
        runner=runner,
        workdir=str(tmp_path),
        model_name=None,
    )

    assert cli.submit_message("hi") == "done"
    assert calls[1].resume == {"decisions": [{"type": "reject", "message": "no"}]}
```

- [ ] **Step 6: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_interrupts.py tests/test_agent_cli_repl.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_cli/approval.py agent_cli/interrupts.py agent_cli/repl.py tests/test_agent_cli_interrupts.py tests/test_agent_cli_repl.py
git commit -m "feat: add interactive cli approval decisions"
```

---

## Task 3: Profile-Aware Paths

**Files:**
- Modify: `agent_cli/paths.py`
- Create: `agent_cli/config.py`
- Modify: `agent_cli/main.py`
- Test: `tests/test_agent_cli_paths.py`
- Test: `tests/test_agent_cli_main.py`

- [ ] **Step 1: Write failing tests for profile validation and home resolution**

Add to `tests/test_agent_cli_paths.py`:

```python
from pathlib import Path

import pytest

from agent_cli.config import apply_profile_override, validate_profile_name


def test_validate_profile_name_accepts_safe_names():
    assert validate_profile_name("dev") == "dev"
    assert validate_profile_name("work.profile-1") == "work.profile-1"


@pytest.mark.parametrize("name", ["", "../x", "a/b", "a\\b", ".."])
def test_validate_profile_name_rejects_unsafe_names(name):
    with pytest.raises(ValueError):
        validate_profile_name(name)


def test_apply_profile_override_sets_home_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CLI_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    applied = apply_profile_override(["--profile", "dev", "sessions"])

    assert applied.profile == "dev"
    assert applied.used_existing_home is False
    assert Path(applied.cli_home) == tmp_path / ".langchain-agent" / "profiles" / "dev"
    assert Path(applied.cli_home) == Path(applied.env_value)


def test_apply_profile_override_respects_existing_agent_cli_home(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("AGENT_CLI_HOME", str(explicit))

    applied = apply_profile_override(["--profile", "dev", "sessions"])

    assert applied.profile == "dev"
    assert applied.used_existing_home is True
    assert Path(applied.cli_home) == explicit.resolve()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_paths.py::test_apply_profile_override_sets_home_when_env_unset -v
```

Expected: FAIL because `agent_cli.config` does not exist.

- [ ] **Step 3: Implement profile config helpers**

Create `agent_cli/config.py`:

```python
from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path


PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class ProfileApplication:
    profile: str | None
    cli_home: str | None
    env_value: str | None
    used_existing_home: bool


def validate_profile_name(name: str) -> str:
    if not name or not PROFILE_RE.fullmatch(name) or name == "..":
        raise ValueError("Profile name must match [A-Za-z0-9_.-]+ and cannot be '..'.")
    return name


def _profile_from_argv(argv: list[str] | None) -> str | None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parser.add_argument("-p", "--profile-short", dest="profile")
    args, _ = parser.parse_known_args(argv)
    return args.profile


def apply_profile_override(argv: list[str] | None = None) -> ProfileApplication:
    profile = _profile_from_argv(argv)
    existing = os.getenv("AGENT_CLI_HOME")
    if not profile:
        return ProfileApplication(None, str(Path(existing).expanduser().resolve()) if existing else None, existing, bool(existing))

    profile = validate_profile_name(profile)
    if existing:
        resolved = str(Path(existing).expanduser().resolve())
        return ProfileApplication(profile, resolved, existing, True)

    home = Path.home() / ".langchain-agent" / "profiles" / profile
    resolved = str(home.resolve())
    os.environ["AGENT_CLI_HOME"] = resolved
    return ProfileApplication(profile, resolved, resolved, False)
```

- [ ] **Step 4: Add `--profile` to parser and apply it early**

In `agent_cli/main.py`, import:

```python
from agent_cli.config import apply_profile_override
```

Add global parser argument in `build_parser()`:

```python
    parser.add_argument("--profile", "-p", default=None, help="Use a named CLI profile.")
```

At the top of `main()` before `setup_cli_logging()`:

```python
    try:
        profile_application = apply_profile_override(argv)
    except ValueError as exc:
        print(f"Invalid profile: {exc}", file=sys.stderr)
        return 2
```

The local `profile_application` may remain unused until Task 5.

- [ ] **Step 5: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_paths.py tests/test_agent_cli_main.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/config.py agent_cli/main.py tests/test_agent_cli_paths.py tests/test_agent_cli_main.py
git commit -m "feat: add cli profile home resolution"
```

---

## Task 4: Dotenv and Config YAML Loading

**Files:**
- Modify: `agent_cli/config.py`
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/repl.py`
- Modify: `agent_cli/doctor.py`
- Test: `tests/test_agent_cli_main.py`
- Test: `tests/test_agent_cli_doctor.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing tests for runtime config**

Add to `tests/test_agent_cli_main.py`:

```python
def test_main_model_config_used_when_cli_model_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  name: config-model\n", encoding="utf-8")
    created = []

    class FakeCLI:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def run_repl(self):
            return 0

    import agent_cli.main as main_module

    handle = type("Handle", (), {"checkpointer": "cp", "close": lambda self: None})()
    monkeypatch.setattr(main_module, "AgentCLI", FakeCLI)
    monkeypatch.setattr(main_module, "create_sqlite_checkpointer", lambda path: handle)

    assert main_module.main(["chat"]) == 0
    assert created[0]["model_name"] == "config-model"


def test_main_cli_model_overrides_config_model(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  name: config-model\n", encoding="utf-8")
    created = []

    class FakeCLI:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def run_repl(self):
            return 0

    import agent_cli.main as main_module

    handle = type("Handle", (), {"checkpointer": "cp", "close": lambda self: None})()
    monkeypatch.setattr(main_module, "AgentCLI", FakeCLI)
    monkeypatch.setattr(main_module, "create_sqlite_checkpointer", lambda path: handle)

    assert main_module.main(["--model", "cli-model", "chat"]) == 0
    assert created[0]["model_name"] == "cli-model"


def test_main_malformed_config_returns_code_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model: [", encoding="utf-8")

    import agent_cli.main as main_module

    code = main_module.main(["sessions"])

    captured = capsys.readouterr()
    assert code == 2
    assert "config.yaml" in captured.err
```

Add to `tests/test_agent_cli_repl.py`:

```python
def test_new_command_uses_configured_default_title(tmp_path):
    from agent_cli.repl import AgentCLI
    from agent_cli.session_store import SessionStore

    cli = AgentCLI(
        session_store=SessionStore(tmp_path / "cli.sqlite"),
        checkpointer=object(),
        agent_factory=lambda checkpointer: object(),
        runner=lambda agent, input_data, config: {},
        workdir=str(tmp_path),
        model_name=None,
        default_title="Configured title",
    )

    output = cli.handle_command("/new")

    assert "Started session:" in output
    assert cli.session_store.get_session(cli.session_id).title == "Configured title"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_main_model_config_used_when_cli_model_missing tests/test_agent_cli_repl.py::test_new_command_uses_configured_default_title -v
```

Expected: FAIL because config loading and `default_title` are not implemented.

- [ ] **Step 3: Implement runtime settings and config loading**

Add to `agent_cli/config.py`:

```python
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


@dataclass(frozen=True)
class RuntimeSettings:
    profile: str | None = None
    cli_home: Path | None = None
    model_name: str | None = None
    default_title: str = "New session"
    display_markdown: str = "render"
    config_path: Path | None = None
    dotenv_paths: tuple[Path, ...] = ()


class ConfigError(ValueError):
    pass


def load_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        raise ConfigError("PyYAML is required to read config.yaml.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ConfigError(f"Failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping.")
    return data


def settings_from_config(
    *,
    cli_home: Path,
    profile: str | None,
    cli_model: str | None,
) -> RuntimeSettings:
    config_path = cli_home / "config.yaml"
    config = load_config_file(config_path)
    model_section = config.get("model") if isinstance(config.get("model"), dict) else {}
    session_section = config.get("session") if isinstance(config.get("session"), dict) else {}
    display_section = config.get("display") if isinstance(config.get("display"), dict) else {}

    default_title = str(session_section.get("default_title") or "New session")
    display_markdown = str(display_section.get("markdown") or "render")
    model_name = cli_model or model_section.get("name")
    if model_name is not None:
        model_name = str(model_name)

    return RuntimeSettings(
        profile=profile,
        cli_home=cli_home,
        model_name=model_name,
        default_title=default_title,
        display_markdown=display_markdown,
        config_path=config_path,
    )
```

- [ ] **Step 4: Replace dotenv loading with ordered helper**

In `agent_cli/config.py`, add:

```python
def load_dotenv_files(*, cli_home: Path, project_root: Path, dotenv_module: Any) -> tuple[Path, ...]:
    paths = (cli_home / ".env", project_root / ".env")
    if dotenv_module is None:
        return paths
    for path in paths:
        if path.exists():
            dotenv_module.load_dotenv(path, override=False)
    return paths
```

In `agent_cli/main.py`, replace `load_dotenv()` with a function that calls `load_dotenv_files`.

- [ ] **Step 5: Pass settings into CLI**

In `agent_cli/main.py`, after `args = parser.parse_args(argv)` and `db_path = ensure_db_parent()`:

```python
    cli_home = db_path.parent
    try:
        settings = settings_from_config(
            cli_home=cli_home,
            profile=profile_application.profile,
            cli_model=args.model,
        )
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
```

Call dotenv loading before config or immediately after `cli_home` is known:

```python
    load_dotenv_files(cli_home=cli_home, project_root=Path.cwd(), dotenv_module=dotenv)
```

In `make_cli()`, pass:

```python
        model_name=settings.model_name,
        default_title=settings.default_title,
```

Update `make_cli` signature to accept `settings`.

In `AgentCLI.__init__`, add parameter:

```python
        default_title: str = "New session",
```

Store:

```python
        self.default_title = default_title
```

Use it in `ensure_session()` when `first_message` is absent and in `/new`.

- [ ] **Step 6: Update doctor visibility checks**

Extend `agent_cli/doctor.py` health checks to include:

```python
HealthCheck("config", "OK" if config path missing or readable else "FAIL", ...)
HealthCheck("dotenv", "OK" or "WARN", "Checked <cli_home>/.env and <cwd>/.env")
```

Do not print secret values.

- [ ] **Step 7: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py tests/test_agent_cli_repl.py tests/test_agent_cli_doctor.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add agent_cli/config.py agent_cli/main.py agent_cli/repl.py agent_cli/doctor.py tests/test_agent_cli_main.py tests/test_agent_cli_repl.py tests/test_agent_cli_doctor.py
git commit -m "feat: load cli profile config and dotenv"
```

---

## Task 5: Skill Command Registry and Invocation Builder

**Files:**
- Create: `agent_cli/skill_commands.py`
- Modify: `agent_cli/commands.py`
- Test: `tests/test_agent_cli_commands.py`

- [ ] **Step 1: Write failing tests for dynamic skill registration**

Add to `tests/test_agent_cli_commands.py`:

```python
from agent_cli.skill_commands import SkillCommand, build_skill_command_map, build_skill_invocation_message


def test_build_skill_command_map_registers_skill_name_and_dir_alias():
    skills = [
        {
            "name": "Python Debug",
            "description": "Debug Python failures.",
            "path": "/repo/skills/python-debug/SKILL.md",
            "dir": "/repo/skills/python-debug",
        }
    ]

    commands = build_skill_command_map(skills, built_in_names={"help"})

    assert commands["python-debug"].skill["name"] == "Python Debug"


def test_build_skill_command_map_builtin_conflict_is_not_registered():
    skills = [
        {
            "name": "help",
            "description": "conflict",
            "path": "/repo/skills/help/SKILL.md",
            "dir": "/repo/skills/help",
        }
    ]

    commands = build_skill_command_map(skills, built_in_names={"help"})

    assert commands == {}


def test_build_skill_invocation_message_includes_content_and_supporting_files():
    command = SkillCommand(
        command="python-debug",
        skill={
            "name": "python-debug",
            "title": "Python Debug",
            "description": "Debug Python.",
            "dir": "/repo/skills/python-debug",
            "content": "# Python Debug\nUse pytest.",
            "supporting_files": ["references/example.md"],
        },
    )

    message = build_skill_invocation_message(command, "fix traceback")

    assert '<skill name="python-debug" dir="/repo/skills/python-debug">' in message
    assert "# Python Debug" in message
    assert "- references/example.md" in message
    assert "User request:\nfix traceback" in message
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py::test_build_skill_command_map_registers_skill_name_and_dir_alias -v
```

Expected: FAIL because `agent_cli.skill_commands` does not exist.

- [ ] **Step 3: Implement skill command helpers**

Create `agent_cli/skill_commands.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


COMMAND_RE = re.compile(r"[^a-z0-9_.-]+")


@dataclass(frozen=True)
class SkillCommand:
    command: str
    skill: dict[str, Any]
    conflict: bool = False


def normalize_skill_command(value: str) -> str:
    normalized = COMMAND_RE.sub("-", value.strip().lower()).strip("-")
    return normalized


def build_skill_command_map(
    skills: list[dict[str, Any]],
    *,
    built_in_names: set[str],
) -> dict[str, SkillCommand]:
    commands: dict[str, SkillCommand] = {}
    for skill in skills:
        names = [normalize_skill_command(str(skill.get("name") or ""))]
        if skill.get("dir"):
            names.append(normalize_skill_command(Path(str(skill["dir"])).name))
        for name in dict.fromkeys(item for item in names if item):
            if name in built_in_names or name in commands:
                continue
            commands[name] = SkillCommand(command=name, skill=skill)
    return commands


def build_skill_invocation_message(command: SkillCommand, prompt: str) -> str:
    skill = command.skill
    supporting_files = skill.get("supporting_files") or []
    file_lines = "\n".join(f"- {path}" for path in supporting_files) or "- none"
    return (
        "Use the following skill for this task.\n\n"
        f"<skill name=\"{skill.get('name') or command.command}\" dir=\"{skill.get('dir') or ''}\">\n"
        f"{skill.get('content') or ''}\n"
        "</skill>\n\n"
        "Supporting files:\n"
        f"{file_lines}\n\n"
        "User request:\n"
        f"{prompt}"
    )
```

- [ ] **Step 4: Add loader function using existing skill APIs**

Add to `agent_cli/skill_commands.py`:

```python
def load_skill_commands(*, built_in_names: set[str]) -> dict[str, SkillCommand]:
    from agent_tools.public.skills import _all_skills

    return build_skill_command_map(_all_skills(), built_in_names=built_in_names)


def load_skill_for_command(command: SkillCommand) -> SkillCommand:
    from agent_tools.public.skills import skill_view

    result = skill_view.invoke({"name": command.skill["name"]})
    artifact = getattr(result, "artifact", None) or {}
    data = artifact.get("data") if isinstance(artifact, dict) else {}
    if not isinstance(data, dict) or not data.get("content"):
        raise ValueError(f"Skill could not be loaded: {command.skill['name']}")
    merged = {**command.skill, **data}
    return SkillCommand(command=command.command, skill=merged)
```

- [ ] **Step 5: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_commands.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/skill_commands.py tests/test_agent_cli_commands.py
git commit -m "feat: add cli skill command registry"
```

---

## Task 6: Skill Commands in Completion, `/skills`, and REPL Routing

**Files:**
- Modify: `agent_cli/input.py`
- Modify: `agent_cli/repl.py`
- Modify: `agent_cli/commands.py`
- Test: `tests/test_agent_cli_input.py`
- Test: `tests/test_agent_cli_repl.py`

- [ ] **Step 1: Write failing tests for completion and routing**

Add to `tests/test_agent_cli_input.py`:

```python
def test_slash_completion_includes_dynamic_skill_commands(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    completer = SlashCommandCompleter(
        session_store=store,
        workdir=str(tmp_path),
        skill_commands_provider=lambda: {"python-debug": object()},
    )

    completions = _completion_texts(completer, "/python")

    assert "python-debug" in completions
```

Add to `tests/test_agent_cli_repl.py`:

```python
def test_handle_command_routes_dynamic_skill_to_agent(monkeypatch, tmp_path):
    from agent_cli.repl import AgentCLI
    from agent_cli.session_store import SessionStore
    from agent_cli.skill_commands import SkillCommand

    submitted = []
    command = SkillCommand(
        command="python-debug",
        skill={
            "name": "python-debug",
            "dir": "/repo/skills/python-debug",
            "content": "# Python Debug",
            "supporting_files": [],
        },
    )

    cli = AgentCLI(
        session_store=SessionStore(tmp_path / "cli.sqlite"),
        checkpointer=object(),
        agent_factory=lambda checkpointer: object(),
        runner=lambda agent, input_data, config: {"messages": [{"role": "assistant", "content": "ok"}]},
        workdir=str(tmp_path),
        model_name=None,
        skill_commands_provider=lambda: {"python-debug": command},
        skill_loader=lambda item: item,
    )
    monkeypatch.setattr(cli, "submit_message", lambda text: submitted.append(text) or "ok")

    assert cli.handle_command("/python-debug fix it") == "ok"
    assert "# Python Debug" in submitted[0]
    assert "User request:\nfix it" in submitted[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_input.py::test_slash_completion_includes_dynamic_skill_commands tests/test_agent_cli_repl.py::test_handle_command_routes_dynamic_skill_to_agent -v
```

Expected: FAIL because the constructor arguments are unsupported.

- [ ] **Step 3: Update input completer**

In `agent_cli/input.py`, update `SlashCommandCompleter.__init__`:

```python
        skill_commands_provider=None,
```

Store:

```python
        self.skill_commands_provider = skill_commands_provider or (lambda: {})
```

In command-name completion, include:

```python
        dynamic_commands = self.skill_commands_provider()
        for name in dynamic_commands:
            if name.startswith(prefix):
                yield Completion(name, start_position=start_position)
```

Update `build_prompt_session()` to accept and pass `skill_commands_provider`.

- [ ] **Step 4: Update REPL constructor and routing**

In `agent_cli/repl.py`, add constructor parameters:

```python
        default_title: str = "New session",
        skill_commands_provider: Callable[[], dict[str, Any]] | None = None,
        skill_loader: Callable[[Any], Any] | None = None,
```

Store:

```python
        self.skill_commands_provider = skill_commands_provider or (lambda: {})
        self.skill_loader = skill_loader
```

At the top of `handle_command()` after built-in `resolve_command()`:

```python
        if command is None:
            dynamic = self.skill_commands_provider()
            skill_name = parts[0].lstrip("/") if parts else ""
            if skill_name in dynamic:
                from agent_cli.skill_commands import build_skill_invocation_message, load_skill_for_command

                loader = self.skill_loader or load_skill_for_command
                loaded = loader(dynamic[skill_name])
                message = build_skill_invocation_message(loaded, arg)
                return self.submit_message(message)
            return f"Unknown command: {parts[0] if parts else raw}"
```

In `_prompt()`, pass `skill_commands_provider=self.skill_commands_provider`.

- [ ] **Step 5: Update `/skills` output and help note**

In `agent_cli/commands.py`, append to `render_help()`:

```python
    lines.append("")
    lines.append("Skill commands are available via /skills.")
```

In `AgentCLI._render_skills()`, use `self.skill_commands_provider()` and display:

```python
python-debug - Debug Python failures. (/python-debug)
```

For skills not registered, display:

```python
help - conflict description (use /skill help)
```

- [ ] **Step 6: Wire default provider in main**

In `agent_cli/main.py`, import:

```python
from agent_cli.commands import COMMAND_LOOKUP
from agent_cli.skill_commands import load_skill_commands
```

Pass to `AgentCLI`:

```python
        skill_commands_provider=lambda: load_skill_commands(
            built_in_names=set(COMMAND_LOOKUP)
        ),
```

- [ ] **Step 7: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_input.py tests/test_agent_cli_repl.py tests/test_agent_cli_commands.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add agent_cli/input.py agent_cli/repl.py agent_cli/commands.py agent_cli/main.py tests/test_agent_cli_input.py tests/test_agent_cli_repl.py tests/test_agent_cli_commands.py
git commit -m "feat: route cli skill slash commands"
```

---

## Task 7: Doctor and Logging Integration Checks

**Files:**
- Modify: `agent_cli/doctor.py`
- Modify: `agent_cli/main.py`
- Test: `tests/test_agent_cli_doctor.py`
- Test: `tests/test_agent_cli_main.py`

- [ ] **Step 1: Write failing tests for doctor exit code and config visibility**

Add to `tests/test_agent_cli_main.py`:

```python
def test_main_doctor_returns_1_when_health_check_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module

    class FakeResult:
        status = "FAIL"
        name = "config"
        message = "bad config"

    monkeypatch.setattr(main_module, "run_health_checks", lambda workdir: [FakeResult()])
    monkeypatch.setattr(main_module, "render_doctor_output", lambda results: "FAIL config")

    assert main_module.main(["doctor"]) == 1
    assert "FAIL config" in capsys.readouterr().out
```

Add to `tests/test_agent_cli_doctor.py`:

```python
def test_doctor_reports_config_and_dotenv_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.doctor import render_doctor_output, run_health_checks

    output = render_doctor_output(run_health_checks(workdir=str(tmp_path)))

    assert "config" in output.lower()
    assert ".env" in output
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_main.py::test_main_doctor_returns_1_when_health_check_fails tests/test_agent_cli_doctor.py::test_doctor_reports_config_and_dotenv_paths -v
```

Expected: FAIL because doctor always returns `0` and may not report config/dotenv.

- [ ] **Step 3: Implement doctor exit code**

In `agent_cli/main.py`, import `run_health_checks` and `render_doctor_output` at module level or keep monkeypatchable names:

```python
from agent_cli.doctor import render_doctor_output, run_health_checks
```

In doctor command branch:

```python
        results = run_health_checks(workdir=workdir)
        print(render_doctor_output(results))
        return 1 if any(item.status == "FAIL" for item in results) else 0
```

- [ ] **Step 4: Implement config/dotenv doctor checks**

In `agent_cli/doctor.py`, add checks using `get_cli_home()`:

```python
cli_home = get_cli_home()
config_path = cli_home / "config.yaml"
if config_path.exists():
    try:
        load_config_file(config_path)
    except ConfigError as exc:
        checks.append(HealthCheck("config", "FAIL", str(exc)))
    else:
        checks.append(HealthCheck("config", "OK", str(config_path)))
else:
    checks.append(HealthCheck("config", "OK", f"optional file missing: {config_path}"))

dotenv_paths = [cli_home / ".env", Path(workdir) / ".env"]
visible = [str(path) for path in dotenv_paths if path.exists()]
checks.append(HealthCheck("dotenv", "OK" if visible else "WARN", ", ".join(visible) or "no .env files found"))
```

- [ ] **Step 5: Run tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest tests/test_agent_cli_doctor.py tests/test_agent_cli_main.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_cli/doctor.py agent_cli/main.py tests/test_agent_cli_doctor.py tests/test_agent_cli_main.py
git commit -m "fix: report cli doctor failures"
```

---

## Task 8: Final Verification

**Files:**
- Verify: all CLI tests

- [ ] **Step 1: Run the full Agent CLI test suite**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_agent_cli_paths.py \
  tests/test_agent_cli_commands.py \
  tests/test_agent_cli_input.py \
  tests/test_agent_cli_interrupts.py \
  tests/test_agent_cli_repl.py \
  tests/test_agent_cli_main.py \
  tests/test_agent_cli_doctor.py \
  tests/test_agent_cli_logging.py \
  tests/test_agent_cli_session.py \
  tests/test_agent_cli_session_store.py \
  tests/test_agent_cli_checkpoints.py \
  tests/test_agent_cli_builders.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run adjacent regression tests**

Run:

```bash
/home/miku/miniforge3/envs/langchain/bin/python -m pytest \
  tests/test_policy_tool_middleware.py \
  tests/test_permissions_human_loop.py \
  tests/test_agent_tools_public_imports.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run CLI smoke tests with isolated home**

Run:

```bash
AGENT_CLI_HOME="$(mktemp -d)" /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli --help
AGENT_CLI_HOME="$(mktemp -d)" /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli doctor
AGENT_CLI_HOME="$(mktemp -d)" /home/miku/miniforge3/envs/langchain/bin/python -m agent_cli sessions
```

Expected:

- `--help` exits `0` and shows `--profile`
- `doctor` exits `0` or `1` depending on local warnings/failures, but does not crash
- `sessions` exits `0` and prints either `No sessions found.` or a session list

- [ ] **Step 4: Final status check**

Run:

```bash
git status --short
git log --oneline -8
```

Expected: clean worktree except intentional uncommitted files if the user requested them; recent commits show the Phase 2 task commits.

# Agent CLI Cron Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing cron system a first-class Agent CLI capability through natural-language tool use, `/cron` REPL commands, top-level `agent_cli cron` commands, scheduler lifecycle, and session notifications.

**Architecture:** Keep `cron/` as the scheduler and persistence layer. Add a thin shared cron action helper in `agent_tools.public.cronjob`, then route both `/cron` and top-level argparse through a new `agent_cli.cron_commands` service. `AgentCLI` owns REPL scheduler lifecycle and cron notification display/injection.

**Tech Stack:** Python, argparse, dataclasses, LangChain tools, LangGraph checkpointer config, pytest, existing `agent_cli` command handler registry.

---

## File Structure

- Modify `agent_cli/config_schema.py`: add `CronConfig`, cron validation, cron get/set/show support.
- Modify `agent_cli/config.py`: include cron values in runtime settings returned by `settings_from_config()`.
- Modify `agent_cli/config.example.yaml`: document default cron config.
- Modify `agent_tools/public/cronjob.py`: add `run_cronjob_action(action, origin_thread_id=None, **kwargs)` and make the LangChain tool call it.
- Create `agent_cli/cron_commands.py`: shared parsed-command service and renderers for list/create/edit/pause/resume/run/remove/status/tick.
- Create `agent_cli/command_handlers/cron.py`: `/cron` parser and handler.
- Modify `agent_cli/command_handlers/__init__.py`: register cron handlers.
- Modify `agent_cli/commands.py`: add `/cron` metadata.
- Modify `agent_cli/main.py`: add top-level `cron` argparse subcommands and dispatch before checkpointer creation; pass cron settings into `AgentCLI`.
- Modify `agent_cli/repl.py`: include cron tools in CLI agent factory; add scheduler lifecycle; add cron notification display and next-turn injection.
- Modify tests:
  - `tests/test_agent_cli_config.py`
  - `tests/test_cronjob_tool.py`
  - `tests/test_agent_cli_commands.py`
  - `tests/test_agent_cli_cron_commands.py`
  - `tests/test_agent_cli_main.py`
  - `tests/test_agent_cli_repl.py`

## Task 1: Add Cron Config Schema and Runtime Settings

**Files:**
- Modify: `agent_cli/config_schema.py`
- Modify: `agent_cli/config.py`
- Modify: `agent_cli/config.example.yaml`
- Test: `tests/test_agent_cli_config.py`

- [ ] **Step 1: Write failing config schema tests**

Add tests to `tests/test_agent_cli_config.py`:

```python
def test_parse_config_includes_cron_defaults():
    from agent_cli.config_schema import parse_config

    config = parse_config({})

    assert config.cron.enabled is True
    assert config.cron.interval_seconds == 60


def test_parse_config_accepts_cron_values():
    from agent_cli.config_schema import parse_config

    config = parse_config({"cron": {"enabled": False, "interval_seconds": 5}})

    assert config.cron.enabled is False
    assert config.cron.interval_seconds == 5


def test_parse_config_rejects_invalid_cron_interval():
    import pytest

    from agent_cli.config_schema import ConfigValidationError, parse_config

    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"cron": {"interval_seconds": 0}})

    assert str(exc.value) == "cron.interval_seconds: must be a positive integer"


def test_parse_config_rejects_unknown_cron_key():
    import pytest

    from agent_cli.config_schema import ConfigValidationError, parse_config

    with pytest.raises(ConfigValidationError) as exc:
        parse_config({"cron": {"weird": True}})

    assert str(exc.value) == "cron.weird: unknown config key"


def test_config_get_and_to_dict_include_cron():
    from agent_cli.config_schema import config_to_dict, get_config_path_value, parse_config

    config = parse_config({"cron": {"enabled": False, "interval_seconds": 12}})

    assert get_config_path_value(config, "cron.enabled") is False
    assert get_config_path_value(config, "cron.interval_seconds") == 12
    assert config_to_dict(config)["cron"] == {
        "enabled": False,
        "interval_seconds": 12,
    }
```

- [ ] **Step 2: Run config tests and verify failure**

Run:

```bash
pytest tests/test_agent_cli_config.py -q
```

Expected: FAIL because `AgentCLIConfig` has no `cron` field and cron paths are unknown.

- [ ] **Step 3: Implement cron schema**

In `agent_cli/config_schema.py`, add:

```python
@dataclass(frozen=True)
class CronConfig:
    enabled: bool = True
    interval_seconds: int = 60
```

Update `AgentCLIConfig`:

```python
@dataclass(frozen=True)
class AgentCLIConfig:
    display: DisplayConfig = DisplayConfig()
    model: ModelConfig = ModelConfig()
    session: SessionConfig = SessionConfig()
    cron: CronConfig = CronConfig()
```

Update allowed keys:

```python
ALLOWED_TOP_LEVEL = {"display", "model", "session", "cron"}
ALLOWED_CHILDREN = {
    "display": {"markdown", "theme"},
    "model": {"name"},
    "session": {"default_title"},
    "cron": {"enabled", "interval_seconds"},
}
```

In `parse_config()`, add:

```python
cron = _mapping(data.get("cron"), "cron")

cron_enabled_value = cron.get("enabled", True)
if isinstance(cron_enabled_value, bool):
    cron_enabled = cron_enabled_value
else:
    raise ConfigValidationError("cron.enabled", "must be a boolean")

interval_value = cron.get("interval_seconds", 60)
try:
    cron_interval_seconds = int(interval_value)
except (TypeError, ValueError):
    raise ConfigValidationError(
        "cron.interval_seconds", "must be a positive integer"
    ) from None
if cron_interval_seconds <= 0:
    raise ConfigValidationError(
        "cron.interval_seconds", "must be a positive integer"
    )
```

Return cron in `AgentCLIConfig`:

```python
return AgentCLIConfig(
    display=DisplayConfig(markdown=markdown, theme=theme),
    model=ModelConfig(name=model_name),
    session=SessionConfig(default_title=default_title),
    cron=CronConfig(
        enabled=cron_enabled,
        interval_seconds=cron_interval_seconds,
    ),
)
```

Update `config_to_dict()`:

```python
"cron": {
    "enabled": config.cron.enabled,
    "interval_seconds": config.cron.interval_seconds,
},
```

Update `get_config_path_value()` with:

```python
if path == "cron.enabled":
    return config.cron.enabled
if path == "cron.interval_seconds":
    return config.cron.interval_seconds
```

Update `set_config_path_value()` by adding cron paths to the existing literal set:

```python
if path not in {
    "display.markdown",
    "display.theme",
    "model.name",
    "session.default_title",
    "cron.enabled",
    "cron.interval_seconds",
}:
    raise ConfigValidationError(path, "unknown config key")
```

- [ ] **Step 4: Add runtime settings**

In `agent_cli/config.py`, find the runtime settings dataclass returned by `settings_from_config()`. Add fields:

```python
cron_enabled: bool = True
cron_interval_seconds: int = 60
```

In `settings_from_config()`, populate them from normalized config:

```python
cron_enabled=config.cron.enabled,
cron_interval_seconds=config.cron.interval_seconds,
```

- [ ] **Step 5: Document config example**

Append to `agent_cli/config.example.yaml`:

```yaml
cron:
  enabled: true
  interval_seconds: 60
```

If the file already has a root YAML object, place this at the same level as `display`, `model`, and `session`.

- [ ] **Step 6: Run config tests and commit**

Run:

```bash
pytest tests/test_agent_cli_config.py -q
```

Expected: PASS.

Commit:

```bash
git add agent_cli/config_schema.py agent_cli/config.py agent_cli/config.example.yaml tests/test_agent_cli_config.py
git commit -m "feat: add agent cli cron config"
```

## Task 2: Add Shared Cron Action Helper

**Files:**
- Modify: `agent_tools/public/cronjob.py`
- Test: `tests/test_cronjob_tool.py`

- [ ] **Step 1: Write failing helper tests**

Add to `tests/test_cronjob_tool.py`:

```python
def test_run_cronjob_action_accepts_explicit_origin_thread(monkeypatch):
    cronjob_tool = _cronjob_tool()
    captured = {}

    def fake_create_job(**kwargs):
        captured.update(kwargs)
        return {
            "id": "job-1",
            "name": "report",
            "prompt": "write report",
            "schedule_display": "30m",
            "repeat": {"times": 1, "completed": 0},
            "deliver": "origin",
            "next_run_at": "2026-05-27T12:00:00+08:00",
            "last_run_at": None,
            "last_status": None,
            "enabled": True,
            "state": "scheduled",
            "skills": [],
            "workdir": None,
        }

    monkeypatch.setattr(cronjob_tool, "create_job", fake_create_job)

    result = cronjob_tool.run_cronjob_action(
        "create",
        origin_thread_id="session-1",
        prompt="write report",
        schedule="30m",
        deliver="origin",
        name="report",
    )

    assert result["success"] is True
    assert captured["origin"] == {"thread_id": "session-1"}


def test_cronjob_tool_uses_shared_action_helper(monkeypatch):
    cronjob_tool = _cronjob_tool()
    calls = []

    def fake_action(action, *, origin_thread_id=None, **kwargs):
        calls.append((action, origin_thread_id, kwargs))
        return {"success": True, "message": "ok"}

    monkeypatch.setattr(cronjob_tool, "run_cronjob_action", fake_action)

    class Runtime:
        config = {"configurable": {"thread_id": "thread-1"}}

    message = cronjob_tool.cronjob.func(
        action="list",
        runtime=Runtime(),
        include_disabled=True,
    )

    assert calls[0][0] == "list"
    assert calls[0][1] == "thread-1"
    assert calls[0][2]["include_disabled"] is True
    assert "ok" in str(message.content)
```

- [ ] **Step 2: Run cronjob tests and verify failure**

Run:

```bash
pytest tests/test_cronjob_tool.py::test_run_cronjob_action_accepts_explicit_origin_thread tests/test_cronjob_tool.py::test_cronjob_tool_uses_shared_action_helper -q
```

Expected: FAIL because `run_cronjob_action` does not exist.

- [ ] **Step 3: Implement helper**

In `agent_tools/public/cronjob.py`, add:

```python
def run_cronjob_action(
    action: str,
    *,
    origin_thread_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return _cronjob_impl(
        action=action,
        runtime=None,
        origin_thread_id=origin_thread_id,
        **kwargs,
    )
```

Update `_cronjob_impl()` signature:

```python
def _cronjob_impl(
    action: str,
    runtime: ToolRuntime | None = None,
    origin_thread_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
```

In the create branch, replace:

```python
thread_id = _runtime_thread_id(runtime)
origin = {"thread_id": thread_id} if thread_id else None
```

with:

```python
thread_id = origin_thread_id or _runtime_thread_id(runtime)
origin = {"thread_id": thread_id} if thread_id else None
```

In the LangChain `cronjob()` function, replace the `_cronjob_impl(...)` call with:

```python
result = run_cronjob_action(
    action=action,
    origin_thread_id=_runtime_thread_id(runtime),
    job_id=job_id,
    prompt=prompt,
    schedule=schedule,
    name=name,
    repeat=repeat,
    deliver=deliver,
    include_disabled=include_disabled,
    skills=skills,
    model=model,
    provider=provider,
    base_url=base_url,
    reason=reason,
    script=script,
    context_from=context_from,
    enabled_toolsets=enabled_toolsets,
    workdir=workdir,
)
```

- [ ] **Step 4: Run cronjob tests and commit**

Run:

```bash
pytest tests/test_cronjob_tool.py -q
```

Expected: PASS.

Commit:

```bash
git add agent_tools/public/cronjob.py tests/test_cronjob_tool.py
git commit -m "feat: share cronjob action helper"
```

## Task 3: Add `agent_cli.cron_commands` Service

**Files:**
- Create: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing service tests**

Create `tests/test_agent_cli_cron_commands.py`:

```python
from __future__ import annotations

from types import SimpleNamespace


def test_create_defaults_to_origin_when_session_id_present(monkeypatch):
    import agent_cli.cron_commands as cron_commands

    calls = []

    def fake_action(action, *, origin_thread_id=None, **kwargs):
        calls.append((action, origin_thread_id, kwargs))
        return {
            "success": True,
            "job_id": "job-1",
            "job": {
                "job_id": "job-1",
                "name": "report",
                "schedule": "30m",
                "next_run_at": "soon",
                "skills": [],
            },
            "message": "created",
        }

    monkeypatch.setattr(cron_commands, "run_cronjob_action", fake_action)

    result = cron_commands.create_cron_job(
        schedule="30m",
        prompt="write report",
        session_id="session-1",
    )

    assert result.exit_code == 0
    assert "Created cron job job-1" in result.text
    assert calls[0][0] == "create"
    assert calls[0][1] == "session-1"
    assert calls[0][2]["deliver"] == "origin"


def test_top_level_create_defaults_to_local(monkeypatch):
    import agent_cli.cron_commands as cron_commands

    calls = []

    def fake_action(action, *, origin_thread_id=None, **kwargs):
        calls.append((action, origin_thread_id, kwargs))
        return {
            "success": True,
            "job_id": "job-1",
            "job": {"job_id": "job-1", "name": "report", "schedule": "30m"},
            "message": "created",
        }

    monkeypatch.setattr(cron_commands, "run_cronjob_action", fake_action)

    result = cron_commands.create_cron_job(
        schedule="30m",
        prompt="write report",
        top_level=True,
    )

    assert result.exit_code == 0
    assert calls[0][1] is None
    assert calls[0][2]["deliver"] == "local"


def test_top_level_origin_delivery_is_rejected():
    import agent_cli.cron_commands as cron_commands

    result = cron_commands.create_cron_job(
        schedule="30m",
        prompt="write report",
        deliver="origin",
        top_level=True,
    )

    assert result.exit_code == 2
    assert "origin delivery requires an active CLI session" in result.text


def test_status_renders_scheduler_and_paths(monkeypatch, tmp_path):
    import agent_cli.cron_commands as cron_commands

    monkeypatch.setattr(cron_commands, "is_cron_scheduler_running", lambda: True)
    monkeypatch.setattr(cron_commands, "display_cron_home", lambda: str(tmp_path))
    monkeypatch.setattr(cron_commands, "get_jobs_file", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cron_commands, "list_jobs", lambda include_disabled=True: [{"id": "a"}])

    result = cron_commands.cron_status()

    assert result.exit_code == 0
    assert "Scheduler: running" in result.text
    assert "Jobs: 1" in result.text


def test_tick_returns_failure_exit_when_job_fails(monkeypatch):
    import agent_cli.cron_commands as cron_commands

    tick_result = SimpleNamespace(
        due=1,
        ran=1,
        succeeded=0,
        failed=1,
        skipped=0,
        results=[SimpleNamespace(job_id="job-1", success=False, error="boom")],
    )
    monkeypatch.setattr(cron_commands, "cron_tick", lambda: tick_result)

    result = cron_commands.run_tick()

    assert result.exit_code == 1
    assert "failed=1" in result.text
    assert "job-1" in result.text
```

- [ ] **Step 2: Run service tests and verify failure**

Run:

```bash
pytest tests/test_agent_cli_cron_commands.py -q
```

Expected: FAIL because `agent_cli.cron_commands` does not exist.

- [ ] **Step 3: Implement service module**

Create `agent_cli/cron_commands.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_core.cron_lifecycle import is_cron_scheduler_running, tick as cron_tick
from agent_tools.public.cronjob import run_cronjob_action
from cron.jobs import get_job, list_jobs
from cron.paths import display_cron_home, get_jobs_file, get_output_dir, get_scripts_dir


@dataclass(frozen=True)
class CronCommandResult:
    text: str
    exit_code: int = 0


def _format_error(result: dict[str, Any]) -> CronCommandResult:
    return CronCommandResult(
        result.get("error", "Cron command failed."),
        exit_code=2,
    )


def _format_job_line(job: dict[str, Any]) -> str:
    return (
        f"{job.get('job_id') or job.get('id')}  "
        f"{job.get('state', '-'):<10}  "
        f"{job.get('schedule', job.get('schedule_display', '-'))}  "
        f"{job.get('name', '-')}"
    )


def list_cron_jobs(*, include_disabled: bool = False) -> CronCommandResult:
    result = run_cronjob_action("list", include_disabled=include_disabled)
    if not result.get("success"):
        return _format_error(result)
    jobs = result.get("jobs") or []
    if not jobs:
        return CronCommandResult("No cron jobs.")
    lines = ["Cron Jobs:"]
    lines.extend(f"  {_format_job_line(job)}" for job in jobs)
    return CronCommandResult("\n".join(lines))


def create_cron_job(
    *,
    schedule: str,
    prompt: str | None = None,
    session_id: str | None = None,
    top_level: bool = False,
    **kwargs: Any,
) -> CronCommandResult:
    deliver = kwargs.pop("deliver", None)
    if deliver == "origin" and not session_id:
        return CronCommandResult(
            "origin delivery requires an active CLI session",
            exit_code=2,
        )
    if deliver is None:
        deliver = "local" if top_level else "origin"
    if deliver == "origin" and top_level:
        return CronCommandResult(
            "origin delivery requires an active CLI session",
            exit_code=2,
        )
    origin_thread_id = session_id if deliver == "origin" else None
    result = run_cronjob_action(
        "create",
        origin_thread_id=origin_thread_id,
        schedule=schedule,
        prompt=prompt,
        deliver=deliver,
        **kwargs,
    )
    if not result.get("success"):
        return _format_error(result)
    job_id = result.get("job_id") or (result.get("job") or {}).get("job_id")
    job = result.get("job") or {}
    lines = [f"Created cron job {job_id}."]
    if job.get("name"):
        lines.append(f"Name: {job['name']}")
    if job.get("schedule"):
        lines.append(f"Schedule: {job['schedule']}")
    if job.get("next_run_at"):
        lines.append(f"Next run: {job['next_run_at']}")
    return CronCommandResult("\n".join(lines))


def update_cron_job(*, job_id: str, **kwargs: Any) -> CronCommandResult:
    result = run_cronjob_action("update", job_id=job_id, **kwargs)
    if not result.get("success"):
        return _format_error(result)
    job = result.get("job") or {}
    return CronCommandResult(f"Updated cron job {job.get('job_id', job_id)}.")


def simple_job_action(action: str, *, job_id: str, reason: str | None = None) -> CronCommandResult:
    kwargs: dict[str, Any] = {"job_id": job_id}
    if reason is not None:
        kwargs["reason"] = reason
    result = run_cronjob_action(action, **kwargs)
    if not result.get("success"):
        return _format_error(result)
    if action == "remove":
        return CronCommandResult(f"Removed cron job {job_id}.")
    job = result.get("job") or {}
    return CronCommandResult(f"{action.capitalize()}d cron job {job.get('job_id', job_id)}.")


def cron_status() -> CronCommandResult:
    jobs = list_jobs(include_disabled=True)
    lines = [
        f"Scheduler: {'running' if is_cron_scheduler_running() else 'stopped'}",
        f"Cron home: {display_cron_home()}",
        f"Jobs file: {get_jobs_file()}",
        f"Output dir: {get_output_dir()}",
        f"Scripts dir: {get_scripts_dir()}",
        f"Jobs: {len(jobs)}",
    ]
    return CronCommandResult("\n".join(lines))


def run_tick() -> CronCommandResult:
    result = cron_tick()
    lines = [
        (
            "Tick: "
            f"due={result.due} ran={result.ran} "
            f"succeeded={result.succeeded} failed={result.failed} skipped={result.skipped}"
        )
    ]
    for item in result.results:
        if not item.success:
            lines.append(f"  failed {item.job_id}: {item.error or '-'}")
    return CronCommandResult("\n".join(lines), exit_code=1 if result.failed else 0)


def existing_job_skills(job_id: str) -> list[str]:
    job = get_job(job_id)
    if not job:
        return []
    skills = list(job.get("skills") or [])
    skill = str(job.get("skill") or "").strip()
    if skill and skill not in skills:
        skills.append(skill)
    return skills
```

- [ ] **Step 4: Run service tests and commit**

Run:

```bash
pytest tests/test_agent_cli_cron_commands.py -q
```

Expected: PASS.

Commit:

```bash
git add agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: add agent cli cron command service"
```

## Task 4: Add `/cron` REPL Command

**Files:**
- Create: `agent_cli/command_handlers/cron.py`
- Modify: `agent_cli/command_handlers/__init__.py`
- Modify: `agent_cli/commands.py`
- Test: `tests/test_agent_cli_commands.py`

- [ ] **Step 1: Write failing command registry tests**

Add to `tests/test_agent_cli_commands.py`:

```python
def test_cron_command_is_registered_and_in_help():
    from agent_cli.commands import COMMAND_LOOKUP, render_help

    assert "cron" in COMMAND_LOOKUP
    help_text = render_help()
    assert "/cron" in help_text
    assert "Manage scheduled cron jobs." in help_text
```

Add this handler test to `tests/test_agent_cli_repl.py`, which already has a concrete `FakeStore` and direct `AgentCLI` construction:

```python
def test_handle_cron_list_dispatches_to_cron_handler(monkeypatch):
    import agent_cli.command_handlers.cron as cron_handler
    from agent_cli.repl import AgentCLI

    monkeypatch.setattr(
        cron_handler.cron_commands,
        "list_cron_jobs",
        lambda include_disabled=False: cron_handler.cron_commands.CronCommandResult(
            f"all={include_disabled}"
        ),
    )

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name="model",
    )

    assert cli.handle_command("/cron list --all") == "all=True"
```

- [ ] **Step 2: Run command tests and verify failure**

Run:

```bash
pytest tests/test_agent_cli_commands.py -q
```

Expected: FAIL because `/cron` is not registered.

- [ ] **Step 3: Add command metadata**

In `agent_cli/commands.py`, add to `COMMAND_REGISTRY` near Background or before Skills:

```python
CommandDef(
    "cron",
    "Manage scheduled cron jobs.",
    "Cron",
    args_hint="[subcommand]",
),
```

- [ ] **Step 4: Implement slash handler**

Create `agent_cli/command_handlers/cron.py`:

```python
from __future__ import annotations

import shlex

from agent_cli import cron_commands


def cron_handlers():
    return {"cron": handle_cron}


def _usage() -> str:
    return "\n".join(
        [
            "Usage:",
            "  /cron list [--all]",
            '  /cron add <schedule> [prompt] [--name NAME] [--deliver origin|local]',
            "  /cron edit <job_id> [--schedule S] [--prompt P]",
            "  /cron pause|resume|run|remove <job_id>",
            "  /cron status",
            "  /cron tick",
        ]
    )


def _parse_flags(tokens: list[str]) -> tuple[dict, list[str], str | None]:
    flags = {
        "name": None,
        "deliver": None,
        "repeat": None,
        "skills": [],
        "add_skills": [],
        "remove_skills": [],
        "clear_skills": False,
        "include_disabled": False,
        "prompt": None,
        "schedule": None,
        "script": None,
        "workdir": None,
    }
    positionals: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"--name", "--deliver", "--prompt", "--schedule", "--script", "--workdir"}:
            if i + 1 >= len(tokens):
                return flags, positionals, f"{token} requires a value"
            flags[token[2:].replace("-", "_")] = tokens[i + 1]
            i += 2
        elif token == "--repeat":
            if i + 1 >= len(tokens):
                return flags, positionals, "--repeat requires a value"
            try:
                flags["repeat"] = int(tokens[i + 1])
            except ValueError:
                return flags, positionals, "--repeat must be an integer"
            i += 2
        elif token == "--skill":
            if i + 1 >= len(tokens):
                return flags, positionals, "--skill requires a value"
            flags["skills"].append(tokens[i + 1])
            i += 2
        elif token == "--add-skill":
            if i + 1 >= len(tokens):
                return flags, positionals, "--add-skill requires a value"
            flags["add_skills"].append(tokens[i + 1])
            i += 2
        elif token == "--remove-skill":
            if i + 1 >= len(tokens):
                return flags, positionals, "--remove-skill requires a value"
            flags["remove_skills"].append(tokens[i + 1])
            i += 2
        elif token == "--clear-skills":
            flags["clear_skills"] = True
            i += 1
        elif token == "--all":
            flags["include_disabled"] = True
            i += 1
        else:
            positionals.append(token)
            i += 1
    return flags, positionals, None


def _compact_kwargs(flags: dict) -> dict:
    mapping = {
        "name": flags["name"],
        "deliver": flags["deliver"],
        "repeat": flags["repeat"],
        "skills": flags["skills"] or None,
        "script": flags["script"],
        "workdir": flags["workdir"],
    }
    return {key: value for key, value in mapping.items() if value is not None}


def handle_cron(ctx, arg, command):
    try:
        tokens = shlex.split(arg)
    except ValueError as exc:
        return f"Invalid /cron arguments: {exc}"
    if not tokens:
        return _usage() + "\n\n" + cron_commands.list_cron_jobs().text
    subcommand = tokens[0].lower()
    flags, positionals, error = _parse_flags(tokens[1:])
    if error:
        return f"{error}\n{_usage()}"

    if subcommand == "list":
        return cron_commands.list_cron_jobs(
            include_disabled=flags["include_disabled"]
        ).text
    if subcommand in {"add", "create"}:
        if not positionals and not flags["schedule"]:
            return _usage()
        schedule = flags["schedule"] or positionals[0]
        prompt = flags["prompt"] or (" ".join(positionals[1:]) if len(positionals) > 1 else None)
        session_id = ctx.ensure_session()
        return cron_commands.create_cron_job(
            schedule=schedule,
            prompt=prompt,
            session_id=session_id,
            **_compact_kwargs(flags),
        ).text
    if subcommand == "edit":
        if not positionals:
            return _usage()
        job_id = positionals[0]
        updates = _compact_kwargs(flags)
        if flags["schedule"] is not None:
            updates["schedule"] = flags["schedule"]
        if flags["prompt"] is not None:
            updates["prompt"] = flags["prompt"]
        if flags["clear_skills"]:
            updates["skills"] = []
        elif flags["skills"]:
            updates["skills"] = flags["skills"]
        elif flags["add_skills"] or flags["remove_skills"]:
            skills = cron_commands.existing_job_skills(job_id)
            remove = set(flags["remove_skills"])
            final_skills = [skill for skill in skills if skill not in remove]
            for skill in flags["add_skills"]:
                if skill not in final_skills:
                    final_skills.append(skill)
            updates["skills"] = final_skills
        return cron_commands.update_cron_job(job_id=job_id, **updates).text
    if subcommand in {"pause", "resume", "run", "remove", "rm", "delete"}:
        if not positionals:
            return _usage()
        action = "remove" if subcommand in {"remove", "rm", "delete"} else subcommand
        return cron_commands.simple_job_action(
            action,
            job_id=positionals[0],
            reason="paused from /cron" if action == "pause" else None,
        ).text
    if subcommand == "status":
        return cron_commands.cron_status().text
    if subcommand == "tick":
        return cron_commands.run_tick().text
    return f"Unknown /cron command: {subcommand}\n{_usage()}"
```

- [ ] **Step 5: Register handler group**

In `agent_cli/command_handlers/__init__.py`, import and include cron:

```python
from agent_cli.command_handlers.cron import cron_handlers
```

Add `cron_handlers()` to the tuple in `build_command_handlers()`:

```python
for group in (
    debug_handlers(),
    session_handlers(),
    background_handlers(),
    cron_handlers(),
    skills_handlers(),
    clipboard_handlers(),
):
```

- [ ] **Step 6: Run command tests and commit**

Run:

```bash
pytest tests/test_agent_cli_commands.py tests/test_agent_cli_cron_commands.py -q
```

Expected: PASS.

Commit:

```bash
git add agent_cli/commands.py agent_cli/command_handlers/__init__.py agent_cli/command_handlers/cron.py tests/test_agent_cli_commands.py
git commit -m "feat: add agent cli cron slash command"
```

## Task 5: Add Top-Level `agent_cli cron` Commands

**Files:**
- Modify: `agent_cli/main.py`
- Test: `tests/test_agent_cli_main.py`

- [ ] **Step 1: Write failing main tests**

Add to `tests/test_agent_cli_main.py`:

```python
def test_main_cron_list_does_not_create_checkpointer(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module
    from agent_cli.cron_commands import CronCommandResult

    monkeypatch.setattr(
        main_module.cron_commands,
        "list_cron_jobs",
        lambda include_disabled=False: CronCommandResult(f"all={include_disabled}"),
    )
    monkeypatch.setattr(
        main_module,
        "create_sqlite_checkpointer",
        lambda path: (_ for _ in ()).throw(AssertionError("no checkpointer")),
    )

    code = main_module.main(["cron", "list", "--all"])

    assert code == 0
    assert "all=True" in capsys.readouterr().out


def test_main_cron_create_rejects_origin(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module

    code = main_module.main(
        ["cron", "create", "30m", "write report", "--deliver", "origin"]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "origin delivery requires an active CLI session" in captured.out


def test_main_cron_tick_returns_service_exit_code(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    import agent_cli.main as main_module
    from agent_cli.cron_commands import CronCommandResult

    monkeypatch.setattr(
        main_module.cron_commands,
        "run_tick",
        lambda: CronCommandResult("failed=1", exit_code=1),
    )

    code = main_module.main(["cron", "tick"])

    assert code == 1
    assert "failed=1" in capsys.readouterr().out
```

- [ ] **Step 2: Run main tests and verify failure**

Run:

```bash
pytest tests/test_agent_cli_main.py::test_main_cron_list_does_not_create_checkpointer tests/test_agent_cli_main.py::test_main_cron_create_rejects_origin tests/test_agent_cli_main.py::test_main_cron_tick_returns_service_exit_code -q
```

Expected: FAIL because `cron` parser and `main_module.cron_commands` are missing.

- [ ] **Step 3: Import service**

In `agent_cli/main.py`, add:

```python
from agent_cli import cron_commands
```

- [ ] **Step 4: Add parser helpers**

In `agent_cli/main.py`, add helper functions near `build_parser()`:

```python
def _add_cron_create_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name")
    parser.add_argument("--deliver", choices=("origin", "local"))
    parser.add_argument("--repeat", type=int)
    parser.add_argument("--skill", dest="skills", action="append")
    parser.add_argument("--script")
    parser.add_argument("--workdir")


def _add_cron_edit_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--schedule")
    parser.add_argument("--prompt")
    parser.add_argument("--name")
    parser.add_argument("--deliver", choices=("origin", "local"))
    parser.add_argument("--repeat", type=int)
    parser.add_argument("--skill", dest="skills", action="append")
    parser.add_argument("--add-skill", dest="add_skills", action="append")
    parser.add_argument("--remove-skill", dest="remove_skills", action="append")
    parser.add_argument("--clear-skills", action="store_true")
    parser.add_argument("--script")
    parser.add_argument("--workdir")
```

- [ ] **Step 5: Add `cron` subparser**

In `build_parser()`, add:

```python
cron_parser = subparsers.add_parser(
    "cron",
    help="Manage scheduled cron jobs.",
    parents=[public_options],
)
cron_subparsers = cron_parser.add_subparsers(dest="cron_command")

cron_list = cron_subparsers.add_parser("list", parents=[public_options])
cron_list.add_argument("--all", action="store_true", dest="include_disabled")

cron_create = cron_subparsers.add_parser(
    "create",
    aliases=["add"],
    parents=[public_options],
)
cron_create.add_argument("schedule")
cron_create.add_argument("prompt", nargs="?")
_add_cron_create_flags(cron_create)

cron_edit = cron_subparsers.add_parser("edit", parents=[public_options])
cron_edit.add_argument("job_id")
_add_cron_edit_flags(cron_edit)

for name in ("pause", "resume", "run"):
    parser_for_action = cron_subparsers.add_parser(name, parents=[public_options])
    parser_for_action.add_argument("job_id")

cron_remove = cron_subparsers.add_parser(
    "remove",
    aliases=["rm", "delete"],
    parents=[public_options],
)
cron_remove.add_argument("job_id")

cron_subparsers.add_parser("status", parents=[public_options])
cron_subparsers.add_parser("tick", parents=[public_options])
```

- [ ] **Step 6: Add cron dispatch before checkpointer creation**

In `main()`, after settings load and `store = SessionStore(db_path)` but before resume/checkpointer handling, add:

```python
if command == "cron":
    result = _run_cron_command(args)
    print(result.text)
    return result.exit_code
```

Add helper:

```python
def _run_cron_command(args: argparse.Namespace):
    subcommand = getattr(args, "cron_command", None)
    if subcommand is None:
        return cron_commands.cron_status()
    if subcommand == "list":
        return cron_commands.list_cron_jobs(
            include_disabled=bool(getattr(args, "include_disabled", False))
        )
    if subcommand in {"create", "add"}:
        return cron_commands.create_cron_job(
            schedule=args.schedule,
            prompt=args.prompt,
            top_level=True,
            name=getattr(args, "name", None),
            deliver=getattr(args, "deliver", None),
            repeat=getattr(args, "repeat", None),
            skills=getattr(args, "skills", None),
            script=getattr(args, "script", None),
            workdir=getattr(args, "workdir", None),
        )
    if subcommand == "edit":
        updates = {
            "schedule": getattr(args, "schedule", None),
            "prompt": getattr(args, "prompt", None),
            "name": getattr(args, "name", None),
            "deliver": getattr(args, "deliver", None),
            "repeat": getattr(args, "repeat", None),
            "skills": getattr(args, "skills", None),
            "script": getattr(args, "script", None),
            "workdir": getattr(args, "workdir", None),
        }
        updates = {key: value for key, value in updates.items() if value is not None}
        if getattr(args, "clear_skills", False):
            updates["skills"] = []
        elif getattr(args, "add_skills", None) or getattr(args, "remove_skills", None):
            skills = cron_commands.existing_job_skills(args.job_id)
            remove = set(getattr(args, "remove_skills", None) or [])
            final_skills = [skill for skill in skills if skill not in remove]
            for skill in getattr(args, "add_skills", None) or []:
                if skill not in final_skills:
                    final_skills.append(skill)
            updates["skills"] = final_skills
        return cron_commands.update_cron_job(job_id=args.job_id, **updates)
    if subcommand in {"pause", "resume", "run"}:
        return cron_commands.simple_job_action(subcommand, job_id=args.job_id)
    if subcommand in {"remove", "rm", "delete"}:
        return cron_commands.simple_job_action("remove", job_id=args.job_id)
    if subcommand == "status":
        return cron_commands.cron_status()
    if subcommand == "tick":
        return cron_commands.run_tick()
    return cron_commands.CronCommandResult(f"Unknown cron command: {subcommand}", exit_code=2)
```

If argparse normalizes aliases to the canonical parser name differently, use `args.cron_command` as observed in the failing test output and update this helper to match.

- [ ] **Step 7: Run main tests and commit**

Run:

```bash
pytest tests/test_agent_cli_main.py -q
```

Expected: PASS.

Commit:

```bash
git add agent_cli/main.py tests/test_agent_cli_main.py
git commit -m "feat: add agent cli cron subcommands"
```

## Task 6: Add REPL Scheduler Lifecycle, Agent Cron Tool, and Notifications

**Files:**
- Modify: `agent_cli/main.py`
- Modify: `agent_cli/repl.py`
- Test: `tests/test_agent_cli_repl.py`
- Test: `tests/test_agent_cli_main.py`
- Test: `tests/test_agent_cli_builders.py` or `tests/test_cronjob_tool.py`

- [ ] **Step 1: Write failing default agent factory test**

Add to `tests/test_cronjob_tool.py` or `tests/test_agent_cli_builders.py`:

```python
def test_agent_cli_default_factory_includes_cronjob(monkeypatch):
    import agent_cli.repl as repl

    captured = {}

    def fake_build_agent(**kwargs):
        captured.update(kwargs)
        return "agent"

    monkeypatch.setattr("agent_core.builders.build_agent", fake_build_agent)

    assert repl.default_agent_factory("cp") == "agent"
    assert captured["checkpointer"] == "cp"
    assert captured["include_cron_tools"] is True
```

- [ ] **Step 2: Write failing lifecycle tests**

Add this helper to `tests/test_agent_cli_repl.py` near `FakeBackgroundRegistry`:

```python
def make_cli(**kwargs):
    params = {
        "session_store": FakeStore(),
        "checkpointer": "cp",
        "agent_factory": lambda checkpointer: "agent",
        "runner": lambda agent, input_data, config: {
            "messages": [{"role": "assistant", "content": "ok"}]
        },
        "workdir": "/repo",
        "model_name": "model",
        "show_banner": False,
    }
    params.update(kwargs)
    return AgentCLI(**params)
```

Add these tests to `tests/test_agent_cli_repl.py`:

```python
def test_run_repl_starts_and_stops_cron_scheduler(monkeypatch):
    import agent_cli.repl as repl

    calls = []
    monkeypatch.setattr(
        repl,
        "start_cron_scheduler",
        lambda interval_seconds: calls.append(("start", interval_seconds)) or True,
    )
    monkeypatch.setattr(
        repl,
        "stop_cron_scheduler",
        lambda timeout=None: calls.append(("stop", timeout)) or True,
    )

    cli = make_cli(cron_enabled=True, cron_interval_seconds=7)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: (_ for _ in ()).throw(EOFError()))

    assert cli.run_repl() == 0
    assert calls == [("start", 7), ("stop", 2.0)]


def test_run_repl_does_not_stop_scheduler_it_did_not_start(monkeypatch):
    import agent_cli.repl as repl

    calls = []
    monkeypatch.setattr(
        repl,
        "start_cron_scheduler",
        lambda interval_seconds: calls.append(("start", interval_seconds)) or False,
    )
    monkeypatch.setattr(
        repl,
        "stop_cron_scheduler",
        lambda timeout=None: calls.append(("stop", timeout)) or True,
    )

    cli = make_cli(cron_enabled=True, cron_interval_seconds=7)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: (_ for _ in ()).throw(EOFError()))

    assert cli.run_repl() == 0
    assert calls == [("start", 7)]


def test_run_repl_skips_cron_scheduler_when_disabled(monkeypatch):
    import agent_cli.repl as repl

    monkeypatch.setattr(
        repl,
        "start_cron_scheduler",
        lambda interval_seconds: (_ for _ in ()).throw(AssertionError("no start")),
    )

    cli = make_cli(cron_enabled=False)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: (_ for _ in ()).throw(EOFError()))

    assert cli.run_repl() == 0
```

- [ ] **Step 3: Write failing notification injection test**

Add to `tests/test_agent_cli_repl.py`:

```python
def test_cron_notifications_print_then_inject_next_turn(monkeypatch, capsys):
    import agent_cli.repl as repl

    event = {
        "job_id": "job-1",
        "job_name": "report",
        "status": "ok",
        "final_response": "daily report done",
        "output_path": "/tmp/out.md",
    }
    drained = {"done": False}

    def fake_drain(thread_id):
        if drained["done"]:
            return []
        drained["done"] = True
        return [event]

    monkeypatch.setattr(repl, "drain_cron_notifications_for_thread_id", fake_drain)
    monkeypatch.setattr(
        repl,
        "format_cron_notification_message",
        lambda events: "CRON UPDATE: " + events[0]["final_response"],
    )

    captured_input = {}

    def runner(agent, input_data, config):
        captured_input.update(input_data)
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = make_cli(runner=runner)
    cli.session_id = "session-1"

    cli._drain_cron_notifications()
    assert "cron report" in capsys.readouterr().out

    cli.submit_message("what changed?")

    message = captured_input["messages"][0]["content"]
    assert "CRON UPDATE: daily report done" in message
    assert "what changed?" in message
    assert cli.pending_cron_events == []
```

- [ ] **Step 4: Run targeted tests and verify failure**

Run:

```bash
pytest tests/test_agent_cli_repl.py tests/test_cronjob_tool.py::test_agent_cli_default_factory_includes_cronjob -q
```

Expected: FAIL because lifecycle and notification methods are missing and the default factory does not include cron tools.

- [ ] **Step 5: Import cron lifecycle and notification helpers**

In `agent_cli/repl.py`, add imports:

```python
from agent_core.cron_lifecycle import start_cron_scheduler, stop_cron_scheduler
from cron.notifications import (
    drain_cron_notifications_for_thread_id,
    format_cron_notification_message,
)
```

- [ ] **Step 6: Extend `AgentCLI.__init__()`**

Add parameters:

```python
cron_enabled: bool = True,
cron_interval_seconds: int = 60,
```

Set attributes:

```python
self.cron_enabled = cron_enabled
self.cron_interval_seconds = cron_interval_seconds
self._started_cron_scheduler = False
self.pending_cron_events: list[dict[str, Any]] = []
```

- [ ] **Step 7: Implement cron lifecycle helpers**

Add methods to `AgentCLI`:

```python
def _start_cron_scheduler_for_repl(self) -> None:
    if not self.cron_enabled:
        return
    try:
        self._started_cron_scheduler = start_cron_scheduler(
            interval_seconds=self.cron_interval_seconds
        )
    except Exception as exc:
        print(f"Warning: failed to start cron scheduler: {exc}", file=sys.stderr)
        self._started_cron_scheduler = False


def _stop_cron_scheduler_on_exit(self) -> None:
    if not self._started_cron_scheduler:
        return
    try:
        stop_cron_scheduler(timeout=2.0)
    except Exception as exc:
        print(f"Warning: failed to stop cron scheduler: {exc}", file=sys.stderr)
    finally:
        self._started_cron_scheduler = False
```

Update `run_repl()`:

```python
def run_repl(self) -> int:
    self.ensure_session()
    self._start_cron_scheduler_for_repl()
    try:
        if self.show_banner:
            self._print_banner()
        else:
            print(f"Session: {self.session_id}")
            print("Type /help for commands. Ctrl-D exits.")
        while True:
            try:
                self._drain_background_notifications()
                text = sanitize_terminal_input(self._prompt("> ")).strip()
            except EOFError:
                print()
                self._stop_active_background_tasks_on_exit()
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
                self._drain_background_notifications()
            except EOFError:
                self._stop_active_background_tasks_on_exit()
                return 0
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
                continue
    finally:
        self._stop_cron_scheduler_on_exit()
```

Because `run_repl()` currently returns inside exception branches, refactor the loop so EOF paths call `_stop_active_background_tasks_on_exit()` and then return, while the `finally` always stops cron. Keep existing KeyboardInterrupt behavior.

- [ ] **Step 8: Implement cron notification display**

Add or extend `_drain_cron_notifications()` in `AgentCLI`:

```python
def _drain_cron_notifications(self) -> None:
    if self.session_id:
        try:
            events = drain_cron_notifications_for_thread_id(self.session_id)
        except Exception as exc:
            print(f"Warning: failed to read cron notifications: {exc}", file=sys.stderr)
            events = []
        for event in events:
            self.pending_cron_events.append(event)
            job_name = event.get("job_name") or "(unnamed)"
            job_id = event.get("job_id") or "(unknown)"
            status = event.get("status") or "unknown"
            output_path = event.get("output_path") or "-"
            preview = " ".join(str(event.get("final_response") or event.get("error") or "").split())
            if len(preview) > 120:
                preview = preview[:117].rstrip() + "..."
            print(
                f"[cron {status}] {job_name} ({job_id}) output={output_path} {preview}".rstrip()
            )
    if self.background_registry is None:
        return
    for item in self.background_registry.drain_notifications():
        message = " ".join(str(item.message).split())
        if item.status == "completed":
            print(
                f"[background {item.kind}] {item.task_id} · completed "
                f"· session {item.session_id} · {message} "
                f"· resume with /resume {item.session_id}"
            )
        elif item.status == "failed":
            print(
                f"[background {item.kind}] {item.task_id} · failed "
                f"· session {item.session_id} · {message} "
                f"· inspect with /tasks {item.task_id}"
            )
        elif item.status == "stopped":
            print(
                f"[background {item.kind}] Stopped {item.task_id} "
                f"· session {item.session_id} · {message}"
            )
        else:
            print(
                f"[background {item.kind}] {item.task_id} · {item.status} "
                f"· session {item.session_id} · {message}"
            )
```

Preserve the existing background notification loop after the cron block.

- [ ] **Step 9: Inject pending cron events in `submit_message()`**

In `submit_message()`, after `processed = prepare_user_message(...)` and before setting `last_user_message`, add:

```python
if self.pending_cron_events:
    cron_update = format_cron_notification_message(self.pending_cron_events)
    if cron_update:
        processed = f"{cron_update}\n\n## User Message\n\n{processed}"
    self.pending_cron_events = []
```

If `format_cron_notification_message()` raises, catch and prepend a compact fallback:

```python
except Exception:
    fallback = "\n".join(
        f"- job_id={event.get('job_id')} status={event.get('status')}"
        for event in self.pending_cron_events
    )
    processed = f"[IMPORTANT: Cron job update]\n{fallback}\n\n## User Message\n\n{processed}"
    self.pending_cron_events = []
```

- [ ] **Step 10: Include cron tools in CLI default factory**

Update `agent_cli/repl.py`:

```python
def default_agent_factory(checkpointer: Any) -> Any:
    from agent_core.builders import build_agent

    return build_agent(include_cron_tools=True, checkpointer=checkpointer)
```

- [ ] **Step 11: Pass cron settings from `make_cli()`**

In `agent_cli/main.py`, when constructing `AgentCLI`, add:

```python
cron_enabled=settings.cron_enabled,
cron_interval_seconds=settings.cron_interval_seconds,
```

- [ ] **Step 12: Run targeted tests and commit**

Run:

```bash
pytest tests/test_agent_cli_repl.py tests/test_agent_cli_main.py tests/test_cronjob_tool.py -q
```

Expected: PASS.

Commit:

```bash
git add agent_cli/repl.py agent_cli/main.py tests/test_agent_cli_repl.py tests/test_agent_cli_main.py tests/test_cronjob_tool.py
git commit -m "feat: run cron in agent cli repl"
```

## Task 7: Final Integration Verification

**Files:**
- Modify: files touched by Tasks 1-6 only when verification exposes a concrete failing test, smoke failure, or whitespace issue.

- [ ] **Step 1: Run focused cron and CLI suite**

Run:

```bash
pytest \
  tests/test_agent_cli_config.py \
  tests/test_agent_cli_main.py \
  tests/test_agent_cli_commands.py \
  tests/test_agent_cli_cron_commands.py \
  tests/test_agent_cli_repl.py \
  tests/test_cronjob_tool.py \
  tests/test_cron_lifecycle.py \
  tests/test_cron_notifications.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run CLI smoke commands**

Run:

```bash
python -m agent_cli --help
python -m agent_cli cron status
python -m agent_cli cron list
```

Expected:

- `--help` exits 0 and includes `cron`.
- `cron status` exits 0 and prints `Scheduler:`.
- `cron list` exits 0 and prints either `No cron jobs.` or `Cron Jobs:`.

- [ ] **Step 3: Run full relevant test group**

Run:

```bash
pytest tests/test_agent_cli_*.py tests/test_cron*.py -q
```

Expected: PASS.

- [ ] **Step 4: Inspect git diff**

Run:

```bash
git diff --stat HEAD
git diff --check
```

Expected:

- `git diff --check` has no whitespace errors.
- Diff only touches planned files plus tests.

- [ ] **Step 5: Commit final fixes if any**

When Step 1-4 exposes a concrete failure, fix only the failing planned files and commit:

```bash
git add agent_cli agent_tools tests
git commit -m "fix: stabilize agent cli cron integration"
```

When Step 1-4 passes without changes, leave the branch as-is and do not create an empty commit.

## Self-Review

Spec coverage:

- Default chat scheduler startup: Task 6.
- `cron.enabled: false`: Task 1 and Task 6.
- CLI agent `cronjob` tool: Task 6.
- `/cron` commands: Task 4.
- Top-level `agent_cli cron`: Task 5.
- Shared service layer: Task 3.
- Origin delivery constraints: Task 3, Task 4, Task 5.
- Notification display and next-turn injection: Task 6.
- No daemon/background merge: preserved by using `agent_core.cron_lifecycle` and not changing `agent_cli.background`.
- Tests and smoke commands: Tasks 1-7.

Type consistency:

- Shared result type is `CronCommandResult(text: str, exit_code: int = 0)`.
- Shared helper is `run_cronjob_action(action, *, origin_thread_id=None, **kwargs)`.
- CLI runtime settings are `cron_enabled` and `cron_interval_seconds`.
- `AgentCLI` state is `pending_cron_events`.

Execution notes:

- Do not touch the untracked `exports/` directory.
- Do not change `build_agent()` default; only `agent_cli.repl.default_agent_factory()` opts into cron tools.
- Do not start scheduler for `ask`, `sessions`, `doctor`, `config`, or top-level `cron`.

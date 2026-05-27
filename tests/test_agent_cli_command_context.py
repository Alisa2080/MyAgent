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

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def get_or_create_session(self, *, session_id, workdir, model, title="New session"):
        existing = self.get_session(session_id)
        if existing is not None:
            return existing
        return self.create_session(
            workdir=workdir,
            model=model,
            title=title,
            session_id=session_id,
        )

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

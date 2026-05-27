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

    @property
    def default_title(self):
        return self.cli.default_title

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

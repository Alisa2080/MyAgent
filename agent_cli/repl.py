from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from agent_cli.commands import render_help, resolve_command
from agent_cli.interrupts import build_resume_value, extract_interrupt_requests, has_interrupt
from agent_cli.rendering import format_interrupt_summary, format_sessions, latest_ai_text
from agent_cli.session_store import SessionStore


AgentFactory = Callable[[Any], Any]
Runner = Callable[[Any, dict[str, Any] | Any, dict[str, Any]], Any]


def _title_from_message(
    session_store: SessionStore, message: str, *, max_length: int = 60
) -> str:
    title_fn = getattr(session_store, "title_from_message", None)
    if title_fn is not None:
        return title_fn(message, max_length=max_length)
    normalized = " ".join(message.split())
    if not normalized:
        return "New session"
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."


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
            _title_from_message(self.session_store, first_message)
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
            last_message_preview=_title_from_message(
                self.session_store, text, max_length=80
            ),
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
            self.session_store.touch_session(record.session_id)
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

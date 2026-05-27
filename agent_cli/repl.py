from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

from langgraph.types import Command

from agent_cli.commands import COMMAND_LOOKUP, render_help, resolve_command
from agent_cli.doctor import render_doctor_output, run_health_checks
from agent_cli.approval import collect_approval_decisions
from agent_cli.interrupts import (
    extract_interrupt_requests,
    extract_interrupt_review_requests,
    has_interrupt,
)
from agent_cli.rendering import format_sessions, latest_ai_text
from agent_cli.session import (
    Session,
    render_session_status,
    update_session_title,
    render_history,
    export_to_markdown,
)
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
        prompt_session: Any | None = None,
        default_title: str = "New session",
        skill_commands_provider: Callable[[], dict[str, Any]] | None = None,
        skill_discovery_provider: Callable[[], Any] | None = None,
        skill_loader: Callable[[Any], Any] | None = None,
        background_registry: Any | None = None,
        profile: str | None = None,
        cli_home: str | None = None,
        display_theme: str = "default",
        show_banner: bool = True,
    ):
        self.session_store = session_store
        self.checkpointer = checkpointer
        self.agent_factory = agent_factory
        self.runner = runner
        self.workdir = workdir
        self.model_name = model_name
        self.prompt_session = prompt_session
        self.default_title = default_title
        self.skill_commands_provider = skill_commands_provider or (lambda: {})
        self.skill_discovery_provider = skill_discovery_provider
        self.skill_loader = skill_loader
        self.background_registry = background_registry
        self.profile = profile
        self.cli_home = cli_home
        self.display_theme = display_theme
        self.show_banner = show_banner
        self._agent: Any | None = None
        # Initialize session after basic attributes are set
        if session_id:
            self.session = Session(
                session_store=session_store,
                session_id=session_id,
                model_name=model_name,
                session_store_for_checkpoints=checkpointer,
                workdir=workdir,
                profile=self.profile,
                display_theme=self.display_theme,
                cli_home=self.cli_home,
                db_path=str(session_store.db_path) if hasattr(session_store, "db_path") else None,
            )
        else:
            self.session = None
        # Legacy attribute for backward compatibility
        self.session_id = session_id

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
            else self.default_title
        )
        record = self.session_store.create_session(
            workdir=self.workdir,
            model=self.model_name,
            title=title,
        )
        self.session_id = record.session_id
        self.session = Session(
            session_store=self.session_store,
            session_id=self.session_id,
            model_name=self.model_name,
            session_store_for_checkpoints=self.checkpointer,
            workdir=self.workdir,
            profile=self.profile,
            display_theme=self.display_theme,
            cli_home=self.cli_home,
            db_path=str(self.session_store.db_path) if hasattr(self.session_store, "db_path") else None,
        )
        return self.session_id

    def submit_message(self, text: str) -> str:
        existing_session = self.session_id is not None
        if existing_session:
            session_id = self.session_id
            title = None
        else:
            session_id = self.session_store.new_session_id()
            title = _title_from_message(self.session_store, text)
        result = self.runner(
            self.agent,
            {"messages": [{"role": "user", "content": text}]},
            {"configurable": {"thread_id": session_id}},
        )
        result = self._handle_interrupts(result, session_id=session_id)
        if not existing_session:
            self.session_store.create_session(
                workdir=self.workdir,
                model=self.model_name,
                title=title or self.default_title,
                session_id=session_id,
            )
            self.session_id = session_id
        self.session_store.touch_session(
            session_id,
            last_message_preview=_title_from_message(
                self.session_store, text, max_length=80
            ),
        )
        return latest_ai_text(result)

    def _handle_interrupts(self, result: Any, *, session_id: str | None = None) -> Any:
        thread_id = session_id or self.session_id
        while has_interrupt(result):
            requests = extract_interrupt_review_requests(result)
            resume_value = collect_approval_decisions(requests)
            result = self.runner(
                self.agent,
                Command(resume=resume_value),
                {"configurable": {"thread_id": thread_id}},
            )
        return result

    def handle_command(self, raw: str) -> str | None:
        parts = raw.strip().split(maxsplit=1)
        command = resolve_command(parts[0] if parts else "")
        arg = parts[1].strip() if len(parts) > 1 else ""
        
        # Check for dynamic skill commands
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
        if command.name == "background":
            return self._handle_background(arg)
        if command.name == "tasks":
            return self._handle_tasks(active_only=False)
        if command.name == "queue":
            return self._handle_tasks(active_only=True)
        if command.name == "steer":
            return self._handle_steer(arg)
        if command.name == "stop":
            return self._handle_stop(arg)
        if command.name == "approve":
            return self._handle_approve(arg)
        if command.name == "help":
            return render_help()
        if command.name == "doctor":
            results = run_health_checks(workdir=self.workdir)
            return render_doctor_output(results)
        if command.name == "new":
            record = self.session_store.create_session(
                workdir=self.workdir,
                model=self.model_name,
                title=self.default_title,
            )
            self.session_id = record.session_id
            self.session = Session(
                session_store=self.session_store,
                session_id=self.session_id,
                model_name=self.model_name,
                session_store_for_checkpoints=self.checkpointer,
                workdir=self.workdir,
                profile=self.profile,
                display_theme=self.display_theme,
                cli_home=self.cli_home,
                db_path=str(self.session_store.db_path) if hasattr(self.session_store, "db_path") else None,
            )
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
            self.session = Session(
                session_store=self.session_store,
                session_id=self.session_id,
                model_name=self.model_name,
                session_store_for_checkpoints=self.checkpointer,
                workdir=self.workdir,
                profile=self.profile,
                display_theme=self.display_theme,
                cli_home=self.cli_home,
                db_path=str(self.session_store.db_path) if hasattr(self.session_store, "db_path") else None,
            )
            return f"Resumed session: {record.session_id}"
        if command.name == "status":
            if self.session is None:
                self.ensure_session()
            return render_session_status(self.session)
        if command.name == "title":
            if self.session is None:
                self.ensure_session()
            return update_session_title(self.session, arg)
        if command.name == "history":
            if self.session is None:
                self.ensure_session()
            limit = None
            if arg:
                try:
                    limit = int(arg)
                except ValueError:
                    return "Usage: /history [N]"
            return render_history(self.session, limit=limit)
        if command.name == "export":
            if self.session is None:
                self.ensure_session()
            if not arg:
                return "Usage: /export <path.md>\n"
            return export_to_markdown(self.session, arg)
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

    def _require_background_registry(self):
        if self.background_registry is None:
            raise RuntimeError("background tasks are not configured")
        return self.background_registry

    def _handle_background(self, arg: str) -> str:
        if not arg.strip():
            return "Usage: /background <prompt>"
        record = self._require_background_registry().start(arg)
        return f"Started background task {record.task_id} · session {record.session_id}"

    def _handle_tasks(self, *, active_only: bool = False) -> str:
        records = self._require_background_registry().list_tasks(active_only=active_only)
        if not records:
            return "No background tasks."
        lines = ["Background Tasks:"]
        for record in records:
            detail = (
                record.last_error
                or record.last_result_preview
                or getattr(record, "prompt_preview", None)
                or ""
            )
            lines.append(
                f"  {record.task_id}  {record.status:<16} {record.title} "
                f"session={record.session_id} steer={record.pending_steer_count} {detail}".rstrip()
            )
        return "\n".join(lines)

    def _handle_steer(self, arg: str) -> str:
        parts = arg.split(maxsplit=1)
        if len(parts) != 2:
            return "Usage: /steer <task_id> <message>"
        steer = self._require_background_registry().steer(parts[0], parts[1])
        return f"Queued steer {steer.id} for {steer.task_id}"

    def _handle_stop(self, arg: str) -> str:
        task_id = arg.strip()
        if not task_id:
            return "Usage: /stop <task_id>"
        record = self._require_background_registry().stop(task_id)
        if record.status in {"completing", "completed", "failed", "stopped"}:
            return f"Background task {record.task_id} is already {record.status}"
        return f"Stop requested for {record.task_id}"

    def _handle_approve(self, arg: str) -> str:
        task_id = arg.strip()
        if not task_id:
            return "Usage: /approve <task_id>"
        registry = self._require_background_registry()
        requests = registry.approval_requests(task_id)
        resume_value = collect_approval_decisions(requests)
        registry.approve(task_id, resume_value)
        return f"Approved background task {task_id}"

    def _render_skills(self) -> str:
        if self.skill_discovery_provider is not None:
            discovery = self.skill_discovery_provider()
        else:
            from agent_cli.skill_commands import load_skill_discovery

            discovery = load_skill_discovery(built_in_names=set(COMMAND_LOOKUP))
        if not discovery.entries:
            return "No skills found."
        lines = []
        for entry in discovery.entries:
            item = entry.skill
            line = f"{item['name']} - {item.get('description') or ''}".rstrip()
            if entry.command:
                line = f"{line} (/{entry.command})"
            elif entry.note:
                line = f"{line} ({entry.note})"
            lines.append(line)
        return "\n".join(lines)

    def _render_skill(self, name: str) -> str:
        if not name:
            return "Usage: /skill <name>"
        from agent_tools.public.skills import _find_skill

        meta = _find_skill(name)
        if not meta:
            return f"Skill not found: {name}"
        desc = meta.get("description") or ""
        return f"{meta['name']}\n{desc}".strip()

    def _drain_background_notifications(self) -> None:
        if self.background_registry is None:
            return
        for item in self.background_registry.drain_notifications():
            message = " ".join(str(item.message).split())
            print(
                f"[background {item.kind}] {item.task_id} · {item.status} "
                f"· session {item.session_id} · {message}"
            )

    def _stop_active_background_tasks_on_exit(self) -> None:
        if self.background_registry is None:
            return
        try:
            active = self.background_registry.list_tasks(active_only=True, limit=None)
        except TypeError:
            active = self.background_registry.list_tasks(active_only=True)
        if not active:
            return
        print(f"Stopping {len(active)} active background task(s)...")
        for record in active:
            try:
                self.background_registry.stop(record.task_id)
            except Exception as exc:
                print(f"Failed to stop {record.task_id}: {exc}", file=sys.stderr)
        join = getattr(self.background_registry, "join", None)
        if join is None:
            return
        for record in active:
            try:
                join(record.task_id, timeout=0.5)
                finalize = getattr(self.background_registry, "finalize_stopping", None)
                store = getattr(self.background_registry, "store", None)
                if finalize is not None and store is not None:
                    current = store.get_task(record.task_id)
                    if current is not None and current.status in {
                        "queued",
                        "running",
                        "waiting_approval",
                        "stopping",
                    }:
                        finalize(record.task_id)
            except Exception as exc:
                print(f"Failed to join {record.task_id}: {exc}", file=sys.stderr)

    def _print_banner(self) -> None:
        from shutil import get_terminal_size

        from agent_cli.banner import BannerContext, render_banner

        counts = (
            self.background_registry.summary_counts()
            if self.background_registry is not None
            else {}
        )
        context = BannerContext(
            workdir=self.workdir,
            profile=self.profile,
            home=self.cli_home,
            model=self.model_name,
            session=self.session_id or "",
            background_counts=counts,
        )
        width = get_terminal_size((100, 24)).columns
        print(render_banner(context, width=width, theme_name=self.display_theme))
        print("Type /help for commands. Ctrl-D exits.")

    def run_repl(self) -> int:
        self.ensure_session()
        if self.show_banner:
            self._print_banner()
        else:
            print(f"Session: {self.session_id}")
            print("Type /help for commands. Ctrl-D exits.")
        while True:
            try:
                self._drain_background_notifications()
                text = self._prompt("> ").strip()
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

    def _prompt(self, prompt_text: str) -> str:
        if self.prompt_session is None:
            from agent_cli.input import build_prompt_session
            from agent_cli.paths import ensure_cli_home

            self.prompt_session = build_prompt_session(
                history_path=ensure_cli_home() / "history.txt",
                session_store=self.session_store,
                workdir=self.workdir,
                skill_commands_provider=self.skill_commands_provider,
            )
        return self.prompt_session.prompt(prompt_text)


def default_agent_factory(checkpointer: Any) -> Any:
    from agent_core.builders import build_agent

    return build_agent(checkpointer=checkpointer)


def default_runner(agent: Any, input_data: dict[str, Any], config: dict[str, Any]) -> Any:
    from agent_core.agent_runner import invoke_agent_with_terminal_notifications

    return invoke_agent_with_terminal_notifications(agent, input_data, config)

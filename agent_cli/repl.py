from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any

from langgraph.types import Command

from agent_cli.command_context import CommandContext
from agent_cli.commands import COMMAND_LOOKUP, resolve_command
from agent_cli.approval import collect_approval_decisions
from agent_cli.command_handlers import build_command_handlers
from agent_cli.interrupts import (
    extract_interrupt_review_requests,
    has_interrupt,
)
from agent_cli.output_format import format_assistant_output
from agent_cli.rendering import latest_ai_text
from agent_cli.session import Session
from agent_cli.session_store import SessionStore
from agent_cli.text_input import (
    prepare_user_message,
    sanitize_terminal_input,
)
from agent_core.cron_lifecycle import start_cron_scheduler, stop_cron_scheduler
from cron.notifications import (
    drain_cron_notifications_for_thread_id,
    format_cron_notification_message,
)


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
        display_markdown: str = "render",
        dotenv_module: Any | None = None,
        show_banner: bool = True,
        cron_enabled: bool = True,
        cron_interval_seconds: int = 60,
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
        self.display_markdown = display_markdown
        self.dotenv_module = dotenv_module
        self.show_banner = show_banner
        self.cron_enabled = cron_enabled
        self.cron_interval_seconds = cron_interval_seconds
        self._started_cron_scheduler = False
        self.pending_cron_events: list[dict[str, Any]] = []
        self._agent: Any | None = None
        self._last_result: Any = None
        self.last_user_message: str | None = None
        self.assistant_replies: list[str] = []
        self.last_call_elapsed_seconds: float | None = None
        self.last_usage_metadata: dict[str, Any] | None = None
        self.command_context = CommandContext(self)
        self.command_handlers = build_command_handlers(self.command_context)
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
        self._set_session(record.session_id)
        return self.session_id

    def _set_session(self, session_id: str) -> None:
        self.session_id = session_id
        self.session = Session(
            session_store=self.session_store,
            session_id=self.session_id,
            model_name=self.model_name,
            session_store_for_checkpoints=self.checkpointer,
            workdir=self.workdir,
            profile=self.profile,
            display_theme=self.display_theme,
            cli_home=self.cli_home,
            db_path=str(self.session_store.db_path)
            if hasattr(self.session_store, "db_path")
            else None,
        )

    def submit_message(self, text: str) -> str:
        processed = prepare_user_message(
            text,
            workdir=self.workdir,
            cli_home=self._effective_cli_home(),
        )
        if self.pending_cron_events:
            try:
                cron_update = format_cron_notification_message(self.pending_cron_events)
                if cron_update:
                    processed = f"{cron_update}\n\n## User Message\n\n{processed}"
            except Exception:
                fallback = "\n".join(
                    f"- job_id={event.get('job_id')} status={event.get('status')}"
                    for event in self.pending_cron_events
                )
                processed = f"[IMPORTANT: Cron job update]\n{fallback}\n\n## User Message\n\n{processed}"
            self.pending_cron_events = []
        self.last_user_message = processed
        started_at = time.perf_counter()
        existing_session = self.session_id is not None
        if existing_session:
            session_id = self.session_id
            title = None
        else:
            session_id = self.session_store.new_session_id()
            title = _title_from_message(self.session_store, processed)
        try:
            result = self.runner(
                self.agent,
                {"messages": [{"role": "user", "content": processed}]},
                {"configurable": {"thread_id": session_id}},
            )
        finally:
            self.last_call_elapsed_seconds = time.perf_counter() - started_at
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
                self.session_store, processed, max_length=80
            ),
        )
        self._last_result = result
        self._capture_usage_metadata(result)
        output = latest_ai_text(result)
        if output:
            self.assistant_replies.append(output)
            return format_assistant_output(output, self.display_markdown)
        return output

    def _effective_cli_home(self):
        if self.cli_home:
            return self.cli_home
        from agent_cli.paths import ensure_cli_home

        return ensure_cli_home()

    def reload_runtime_settings(self) -> str:
        from pathlib import Path

        import agent_cli.config as config_module

        cli_home = Path(self._effective_cli_home())
        old = (
            self.model_name,
            self.default_title,
            self.display_theme,
            self.display_markdown,
        )
        try:
            config_module.load_dotenv_files(
                cli_home=cli_home,
                project_root=Path(self.workdir),
                dotenv_module=self.dotenv_module,
            )
            settings = config_module.settings_from_config(
                cli_home=cli_home,
                profile=self.profile,
                cli_model=None,
            )
        except Exception as exc:
            (
                self.model_name,
                self.default_title,
                self.display_theme,
                self.display_markdown,
            ) = old
            return f"Reload failed: {exc}"

        model_changed = settings.model_name != self.model_name
        self.model_name = settings.model_name
        self.default_title = settings.default_title
        self.display_theme = settings.display_theme
        self.display_markdown = settings.display_markdown
        if model_changed:
            self._agent = None
        return "Reloaded config and dotenv."

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
        raw = sanitize_terminal_input(raw)
        parts = raw.strip().split(maxsplit=1)
        token = parts[0] if parts else ""
        command = resolve_command(token)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command is None:
            dynamic = self.skill_commands_provider()
            skill_name = token.lstrip("/") if token else ""
            if skill_name in dynamic:
                from agent_cli.skill_commands import (
                    build_skill_invocation_message,
                )

                loader = self.command_context.load_skill
                loaded = loader(dynamic[skill_name])
                message = build_skill_invocation_message(loaded, arg)
                return self.command_context.submit_message(message)
            return f"Unknown command: {token if token else raw}"

        handler = self.command_handlers.get(command.effective_handler_key)
        if handler is None:
            return f"Unhandled command: /{command.name}"
        return handler(self.command_context, arg, command)

    def _require_background_registry(self):
        if self.background_registry is None:
            raise RuntimeError("background tasks are not configured")
        return self.background_registry

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

    def _drain_background_notifications(self) -> None:
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
        touched = False
        for record in active:
            try:
                updated = self.background_registry.stop(record.task_id)
                touched = True
                if getattr(updated, "status", None) in {
                    "completed",
                    "failed",
                    "stopped",
                    "completing",
                }:
                    print(f"Already terminal: {record.task_id} ({updated.status})")
                else:
                    print(f"Stop requested: {record.task_id}")
            except Exception as exc:
                print(f"Failed to stop {record.task_id}: {exc}", file=sys.stderr)
        join = getattr(self.background_registry, "join", None)
        if join is None:
            if touched:
                print("Inspect background task history with /tasks all")
            return
        for record in active:
            try:
                join(record.task_id, timeout=0.5)
                finalize = getattr(self.background_registry, "finalize_stopping", None)
                store = getattr(self.background_registry, "store", None)
                current = store.get_task(record.task_id) if store is not None else None
                if finalize is not None and current is not None:
                    if current.status in {
                        "queued",
                        "running",
                        "waiting_approval",
                        "stopping",
                    }:
                        finalize(record.task_id)
                        current = store.get_task(record.task_id)
                if current is not None:
                    if current.status in {
                        "completed",
                        "failed",
                        "stopped",
                    }:
                        print(f"Stopped after join: {record.task_id}")
                    else:
                        print(f"Still active after join: {record.task_id}")
            except Exception as exc:
                print(f"Failed to join {record.task_id}: {exc}", file=sys.stderr)
        if touched:
            print("Inspect background task history with /tasks all")

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
        self._start_cron_scheduler_for_repl()
        try:
            if self.show_banner:
                self._print_banner()
            else:
                print(f"Session: {self.session_id}")
                print("Type /help for commands. Ctrl-D exits.")
            while True:
                try:
                    self._drain_cron_notifications()
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
                    self._drain_cron_notifications()
                    self._drain_background_notifications()
                except EOFError:
                    self._stop_active_background_tasks_on_exit()
                    return 0
                except Exception as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    continue
        finally:
            self._stop_cron_scheduler_on_exit()

    def _prompt(self, prompt_text: str) -> str:
        if self.prompt_session is None:
            from agent_cli.input import build_prompt_session
            from agent_cli.paths import ensure_cli_home

            self.prompt_session = build_prompt_session(
                history_path=ensure_cli_home() / "history.txt",
                session_store=self.session_store,
                workdir=self.workdir,
                skill_commands_provider=self.skill_commands_provider,
                cli_home=self.cli_home or ensure_cli_home(),
            )
        return self.prompt_session.prompt(prompt_text)


def default_agent_factory(checkpointer: Any) -> Any:
    from agent_core.builders import build_agent

    return build_agent(include_cron_tools=True, checkpointer=checkpointer)


def default_runner(agent: Any, input_data: dict[str, Any], config: dict[str, Any]) -> Any:
    from agent_core.agent_runner import invoke_agent_with_terminal_notifications

    return invoke_agent_with_terminal_notifications(agent, input_data, config)

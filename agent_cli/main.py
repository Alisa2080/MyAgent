from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import dotenv
except ModuleNotFoundError:
    dotenv = None

from agent_cli.checkpoints import CheckpointDependencyError, create_sqlite_checkpointer
from agent_cli.background import BackgroundTaskRegistry, BackgroundTaskStore
from agent_cli.commands import COMMAND_LOOKUP
from agent_cli.config import (
    ConfigError,
    apply_profile_override,
    load_dotenv_files,
    settings_from_config,
)
from agent_cli.logging_config import setup_cli_logging
from agent_cli.paths import ensure_db_parent
from agent_cli.rendering import format_sessions
from agent_cli.repl import AgentCLI, default_agent_factory, default_runner
from agent_cli.session_store import SessionStore
from agent_cli.skill_commands import load_skill_commands, load_skill_discovery
from agent_core.terminal_lifecycle import interrupt_terminal_wait_for_thread_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_cli")
    parser.add_argument("--workdir", default=None, help="Workspace directory for the agent.")
    parser.add_argument("--model", default=None, help="Model display metadata for session list.")
    parser.add_argument("--profile", "-p", default=None, help="Use a named CLI profile.")

    subparsers = parser.add_subparsers(dest="command")

    chat = subparsers.add_parser("chat", help="Start interactive chat.")
    chat.add_argument("--resume", default=None, help="Resume an existing session id.")

    ask = subparsers.add_parser("ask", help="Ask one question and exit.")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--resume", default=None, help="Resume an existing session id.")

    subparsers.add_parser("sessions", help="List recent sessions.")
    subparsers.add_parser("doctor", help="Run CLI health checks.")
    return parser


def make_cli(
    *, args: argparse.Namespace, store: SessionStore, checkpointer, settings
) -> AgentCLI:
    workdir = str(Path(args.workdir).expanduser().resolve()) if args.workdir else os.getcwd()
    background_store = BackgroundTaskStore(store.db_path)

    def run_background(input_data, config):
        handle = create_sqlite_checkpointer(store.db_path)
        try:
            agent = default_agent_factory(handle.checkpointer)
            return default_runner(agent, input_data, config)
        finally:
            handle.close()

    def create_background_session(session_id: str, title: str) -> None:
        store.create_session(
            workdir=workdir,
            model=settings.model_name,
            title=title,
            session_id=session_id,
        )

    background_registry = BackgroundTaskRegistry(
        store=background_store,
        session_id_factory=store.new_session_id,
        session_record_creator=create_background_session,
        title_factory=lambda prompt: store.title_from_message(prompt),
        runner=run_background,
        stop_wait_interrupt=lambda session_id: interrupt_terminal_wait_for_thread_id(session_id),
    )
    return AgentCLI(
        session_store=store,
        checkpointer=checkpointer,
        agent_factory=default_agent_factory,
        runner=default_runner,
        workdir=workdir,
        model_name=settings.model_name,
        default_title=settings.default_title,
        session_id=getattr(args, "resume", None),
        background_registry=background_registry,
        profile=settings.profile,
        cli_home=str(settings.cli_home) if settings.cli_home else None,
        display_theme=settings.display_theme,
        skill_commands_provider=lambda: load_skill_commands(
            built_in_names=set(COMMAND_LOOKUP)
        ),
        skill_discovery_provider=lambda: load_skill_discovery(
            built_in_names=set(COMMAND_LOOKUP)
        ),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        profile_application = apply_profile_override(argv)
    except ValueError as exc:
        print(f"Invalid profile: {exc}", file=sys.stderr)
        return 2
    setup_cli_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "chat"

    db_path = ensure_db_parent()
    cli_home = db_path.parent

    load_dotenv_files(cli_home=cli_home, project_root=Path.cwd(), dotenv_module=dotenv)

    if command == "doctor":
        from agent_cli.doctor import render_doctor_output, run_health_checks

        workdir = str(Path(args.workdir).expanduser().resolve()) if args.workdir else os.getcwd()
        results = run_health_checks(workdir=workdir, cli_home=cli_home)
        print(render_doctor_output(results))
        return 1 if any(item.status == "FAIL" for item in results) else 0

    try:
        settings = settings_from_config(
            cli_home=cli_home,
            profile=profile_application.profile,
            cli_model=args.model,
        )
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    store = SessionStore(db_path)

    if command == "sessions":
        print(format_sessions(store.list_sessions()))
        return 0

    resume_id = getattr(args, "resume", None)
    if resume_id and store.get_session(resume_id) is None:
        print(f"Unknown session: {resume_id}", file=sys.stderr)
        return 2

    try:
        checkpointer_handle = create_sqlite_checkpointer(db_path)
    except CheckpointDependencyError as exc:
        print(
            f"agent_cli {command} requires SQLite checkpointing: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        cli = make_cli(
            args=args,
            store=store,
            checkpointer=checkpointer_handle.checkpointer,
            settings=settings,
        )

        if command == "ask":
            try:
                output = cli.submit_message(" ".join(args.question))
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            if output:
                print(output)
            return 0

        return cli.run_repl()
    finally:
        checkpointer_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())

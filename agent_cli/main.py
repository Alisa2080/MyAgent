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
from agent_cli.commands import COMMAND_LOOKUP
from agent_cli.config import ConfigError, load_dotenv_files, settings_from_config
from agent_cli.logging_config import setup_cli_logging
from agent_cli.paths import ensure_db_parent
from agent_cli.rendering import format_sessions
from agent_cli.repl import AgentCLI, default_agent_factory, default_runner
from agent_cli.session_store import SessionStore
from agent_cli.skill_commands import load_skill_commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_cli")
    parser.add_argument("--workdir", default=None, help="Workspace directory for the agent.")
    parser.add_argument("--model", default=None, help="Model display metadata for session list.")

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
    return AgentCLI(
        session_store=store,
        checkpointer=checkpointer,
        agent_factory=default_agent_factory,
        runner=default_runner,
        workdir=workdir,
        model_name=settings.model_name,
        default_title=settings.default_title,
        session_id=getattr(args, "resume", None),
        skill_commands_provider=lambda: load_skill_commands(
            built_in_names=set(COMMAND_LOOKUP)
        ),
    )


def main(argv: list[str] | None = None) -> int:
    setup_cli_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "chat"

    db_path = ensure_db_parent()
    cli_home = db_path.parent

    try:
        settings = settings_from_config(
            cli_home=cli_home,
            profile=None,
            cli_model=args.model,
        )
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    load_dotenv_files(cli_home=cli_home, project_root=Path.cwd(), dotenv_module=dotenv)

    store = SessionStore(db_path)

    if command == "sessions":
        print(format_sessions(store.list_sessions()))
        return 0

    if command == "doctor":
        from agent_cli.doctor import render_doctor_output, run_health_checks

        workdir = str(Path(args.workdir).expanduser().resolve()) if args.workdir else os.getcwd()
        results = run_health_checks(workdir=workdir)
        print(render_doctor_output(results))
        return 1 if any(item.status == "FAIL" for item in results) else 0

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

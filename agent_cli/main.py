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
from agent_cli.paths import ensure_db_parent
from agent_cli.rendering import format_sessions
from agent_cli.repl import AgentCLI, default_agent_factory, default_runner
from agent_cli.session_store import SessionStore


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
    return parser


def load_dotenv() -> None:
    if dotenv is None:
        return
    dotenv.load_dotenv(Path.cwd() / ".env")


def make_cli(*, args: argparse.Namespace, store: SessionStore, checkpointer) -> AgentCLI:
    workdir = str(Path(args.workdir).expanduser().resolve()) if args.workdir else os.getcwd()
    return AgentCLI(
        session_store=store,
        checkpointer=checkpointer,
        agent_factory=default_agent_factory,
        runner=default_runner,
        workdir=workdir,
        model_name=args.model,
        session_id=getattr(args, "resume", None),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "chat"
    load_dotenv()

    db_path = ensure_db_parent()
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

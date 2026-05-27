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
    load_normalized_config,
    save_config_file,
    settings_from_config,
)
from agent_cli.config_schema import (
    ConfigValidationError,
    get_config_path_value,
    parse_config,
    set_config_path_value,
    render_config_show,
    parse_config_value,
)
from agent_cli.logging_config import setup_cli_logging
from agent_cli.paths import ensure_db_parent, get_cli_home
from agent_cli.rendering import format_sessions
from agent_cli.repl import AgentCLI, default_agent_factory, default_runner
from agent_cli.session_store import SessionStore
from agent_cli.skill_commands import load_skill_commands, load_skill_discovery
from agent_core.terminal_lifecycle import interrupt_terminal_wait_for_thread_id


def build_public_options_parser(*, add_help: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=add_help)
    parser.add_argument("--workdir", default=None, help="Workspace directory for the agent.")
    parser.add_argument("--model", default=None, help="Model display metadata for session list.")
    parser.add_argument("--profile", "-p", default=None, help="Use a named CLI profile.")
    return parser


def _pre_parse_public_options(argv: list[str] | None) -> dict[str, str | None]:
    """Pre-parse public options to preserve them after subparser parsing.

    argparse has a quirk where subparsers with parents overwrite parent parser
    values. This workaround preserves values set before the subcommand.
    """
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--workdir", default=None)
    pre_parser.add_argument("--model", default=None)
    pre_parser.add_argument("--profile", "-p", default=None)
    args, _ = pre_parser.parse_known_args(argv or [])
    return {"workdir": args.workdir, "model": args.model, "profile": args.profile}


def build_parser() -> argparse.ArgumentParser:
    public_options = build_public_options_parser()
    parser = argparse.ArgumentParser(prog="agent_cli", parents=[public_options])

    subparsers = parser.add_subparsers(dest="command")

    chat = subparsers.add_parser(
        "chat",
        help="Start interactive chat.",
        parents=[public_options],
    )
    chat.add_argument("--resume", default=None, help="Resume an existing session id.")

    ask = subparsers.add_parser(
        "ask",
        help="Ask one question and exit.",
        parents=[public_options],
    )
    ask.add_argument("--resume", default=None, help="Resume an existing session id.")
    ask.add_argument("question", nargs="+")

    subparsers.add_parser(
        "sessions",
        help="List recent sessions.",
        parents=[public_options],
    )
    subparsers.add_parser(
        "doctor",
        help="Run CLI health checks.",
        parents=[public_options],
    )

    # Config subcommands
    config_parser = subparsers.add_parser(
        "config",
        help="Show or edit CLI config.",
        parents=[public_options],
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser(
        "show",
        help="Show normalized CLI config.",
        parents=[public_options],
    )
    config_get = config_subparsers.add_parser(
        "get",
        help="Get a config value.",
        parents=[public_options],
    )
    config_get.add_argument("path")
    config_set = config_subparsers.add_parser(
        "set",
        help="Set a config value.",
        parents=[public_options],
    )
    config_set.add_argument("path")
    config_set.add_argument("value")

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
        display_markdown=settings.display_markdown,
        dotenv_module=dotenv,
        skill_commands_provider=lambda: load_skill_commands(
            built_in_names=set(COMMAND_LOOKUP)
        ),
        skill_discovery_provider=lambda: load_skill_discovery(
            built_in_names=set(COMMAND_LOOKUP)
        ),
    )


def main(argv: list[str] | None = None) -> int:
    effective_argv = sys.argv[1:] if argv is None else argv
    try:
        profile_application = apply_profile_override(effective_argv)
    except ValueError as exc:
        print(f"Invalid profile: {exc}", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(effective_argv)
    command = args.command or "chat"

    # Workaround for argparse parent bug: restore values from pre-parse
    pre_parsed = _pre_parse_public_options(effective_argv)
    if pre_parsed["workdir"] is not None:
        args.workdir = pre_parsed["workdir"]
    if pre_parsed["model"] is not None:
        args.model = pre_parsed["model"]
    if pre_parsed["profile"] is not None:
        args.profile = pre_parsed["profile"]

    # Handle config commands without requiring checkpointer
    if command == "config":
        cli_home = get_cli_home()
        load_dotenv_files(cli_home=cli_home, project_root=Path.cwd(), dotenv_module=dotenv)

        try:
            config, raw, config_path = load_normalized_config(cli_home)
        except (ConfigError, ConfigValidationError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

        config_cmd = getattr(args, "config_command", None)

        if config_cmd == "show":
            print(render_config_show(config))
            return 0
        elif config_cmd == "get":
            try:
                value = get_config_path_value(config, args.path)
                print(value)
                return 0
            except (ConfigError, ConfigValidationError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
        elif config_cmd == "set":
            try:
                value = parse_config_value(args.value)
                updated_raw = set_config_path_value(raw, args.path, value)
                save_config_file(config_path, updated_raw)
                return 0
            except (ConfigError, ConfigValidationError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
        else:
            # Show config if no subcommand
            print(render_config_show(config))
            return 0

    if command == "doctor":
        from agent_cli.doctor import doctor_exit_code, render_doctor_output, run_health_checks

        cli_home = get_cli_home()
        workdir = str(Path(args.workdir).expanduser().resolve()) if args.workdir else os.getcwd()
        load_dotenv_files(cli_home=cli_home, project_root=Path.cwd(), dotenv_module=dotenv)
        results = run_health_checks(workdir=workdir, cli_home=cli_home)
        print(render_doctor_output(results))
        return doctor_exit_code(results)

    setup_cli_logging()
    db_path = ensure_db_parent()
    cli_home = db_path.parent

    load_dotenv_files(cli_home=cli_home, project_root=Path.cwd(), dotenv_module=dotenv)

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

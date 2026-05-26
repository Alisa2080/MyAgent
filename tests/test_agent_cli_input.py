from pathlib import Path

from prompt_toolkit.document import Document

from agent_cli.input import SlashCommandCompleter, build_prompt_session
from agent_cli.session_store import SessionStore


def _completion_texts(completer, text):
    document = Document(text=text, cursor_position=len(text))
    return [item.text for item in completer.get_completions(document, None)]


def test_slash_command_completion_uses_registry(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    completer = SlashCommandCompleter(session_store=store, workdir=str(tmp_path))

    completions = _completion_texts(completer, "/sta")

    assert "status" in completions


def test_resume_completion_uses_session_store(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    record = store.create_session(workdir="/repo", model=None, title="Known")
    completer = SlashCommandCompleter(session_store=store, workdir=str(tmp_path))

    completions = _completion_texts(completer, "/resume " + record.session_id[:6])

    assert record.session_id in completions


def test_export_completion_lists_paths(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    (tmp_path / "exports").mkdir()
    completer = SlashCommandCompleter(session_store=store, workdir=str(tmp_path))

    completions = _completion_texts(completer, "/export exp")

    assert "exports/" in completions


def test_build_prompt_session_uses_file_history(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    session = build_prompt_session(
        history_path=tmp_path / "history.txt",
        session_store=store,
        workdir=str(tmp_path),
    )

    assert session is not None
    assert Path(tmp_path / "history.txt").parent.exists()


def test_slash_completion_includes_dynamic_skill_commands(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    completer = SlashCommandCompleter(
        session_store=store,
        workdir=str(tmp_path),
        skill_commands_provider=lambda: {"python-debug": object()},
    )

    completions = _completion_texts(completer, "/python")

    assert "python-debug" in completions

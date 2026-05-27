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


from agent_cli.text_input import (
    collapse_large_paste,
    detect_file_drop,
    expand_paste_references,
    format_file_reference_message,
    format_osc52,
    prepare_user_message,
    sanitize_terminal_input,
    should_collapse_paste,
    estimate_tokens,
)


def test_sanitize_terminal_input_strips_paste_and_cpr_sequences():
    raw = "\x1b[200~hello\r\nworld\x1b[201~\x1b[12;34R^[[56;78R"
    assert sanitize_terminal_input(raw) == "hello\nworld"


def test_sanitize_terminal_input_strips_degraded_wrappers_at_boundaries():
    raw = "[200~first\nsecond[201~\n00~third01~"
    assert sanitize_terminal_input(raw) == "first\nsecond\nthird"


def test_should_collapse_paste_uses_five_line_threshold_and_skips_commands():
    assert should_collapse_paste("1\n2\n3\n4\n5") is True
    assert should_collapse_paste("/note\n1\n2\n3\n4") is False
    assert should_collapse_paste("1\n2\n3\n4") is False


def test_collapse_large_paste_writes_file_and_expand_reference(tmp_path):
    text = "a\r\nb\r\nc\r\nd\r\ne"
    result = collapse_large_paste(text, cli_home=tmp_path, counter=3)
    assert result.collapsed is True
    assert result.path is not None
    assert result.path.parent == tmp_path / "pastes"
    assert result.path.read_text(encoding="utf-8") == "a\nb\nc\nd\ne"
    assert result.placeholder.startswith("[Pasted text #3: 5 lines -> ")
    assert expand_paste_references(f"please read {result.placeholder}") == (
        "please read a\nb\nc\nd\ne"
    )


def test_collapse_large_paste_returns_sanitized_text_for_small_paste(tmp_path):
    result = collapse_large_paste("a\r\nb", cli_home=tmp_path, counter=1)
    assert result.collapsed is False
    assert result.placeholder == "a\nb"
    assert result.path is None


def test_detect_file_drop_supports_workdir_relative_path_and_trailing_text(tmp_path):
    target = tmp_path / "notes file.md"
    target.write_text("notes", encoding="utf-8")
    drop = detect_file_drop("notes\\ file.md summarize this", workdir=str(tmp_path))
    assert drop is not None
    assert drop.path == target.resolve()
    assert drop.remainder == "summarize this"
    assert drop.is_image is False
    assert format_file_reference_message(drop) == (
        f"[User referenced file: {target.resolve()}]\nsummarize this"
    )


def test_detect_file_drop_supports_quoted_file_url(tmp_path):
    target = tmp_path / "screen shot.png"
    target.write_text("image bytes", encoding="utf-8")
    drop = detect_file_drop(f'"{target.as_uri()}" describe', workdir=str(tmp_path))
    assert drop is not None
    assert drop.path == target.resolve()
    assert drop.remainder == "describe"
    assert drop.is_image is True


def test_detect_file_drop_returns_none_for_missing_file(tmp_path):
    assert detect_file_drop("missing.txt summarize", workdir=str(tmp_path)) is None


def test_prepare_user_message_sanitizes_expands_paste_and_rewrites_file(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("notes", encoding="utf-8")
    paste = collapse_large_paste("a\nb\nc\nd\ne", cli_home=tmp_path, counter=1)
    prepared = prepare_user_message(
        f"\x1b[200~{target.name} read this and {paste.placeholder}\x1b[201~",
        workdir=str(tmp_path),
    )
    assert prepared == (
        f"[User referenced file: {target.resolve()}]\n"
        "read this and a\nb\nc\nd\ne"
    )


def test_format_osc52_encodes_text_for_clipboard():
    assert format_osc52("hello") == "\x1b]52;c;aGVsbG8=\x07"


def test_estimate_tokens_is_deterministic_and_nonzero():
    assert estimate_tokens("one two three four five") == 2
    assert estimate_tokens("") == 0


def test_build_prompt_session_accepts_cli_home_for_paste_collapse(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")

    session = build_prompt_session(
        history_path=tmp_path / "history.txt",
        session_store=store,
        workdir=str(tmp_path),
        cli_home=tmp_path / "cli-home",
    )

    assert session is not None
    assert Path(tmp_path / "cli-home").exists()

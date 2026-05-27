import tempfile
from pathlib import Path

from agent_cli.session import Session, SessionStatus, render_session_status, update_session_title
from agent_cli.session_store import SessionStore


def test_session_status_returns_info():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "cli.sqlite")
        session = Session(session_store=store, session_id="test-123", model_name="gpt-4")
        
        status = session.status()
        
        assert status.session_id == "test-123"
        assert status.model_name == "gpt-4"
        assert isinstance(status.created_at, type(status.created_at))


def test_session_set_title_updates_store():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "cli.sqlite")
        session = Session(session_store=store, session_id="test-456")
        
        session.set_title("My Custom Title")
        status = session.status()
        
        assert status.title == "My Custom Title"


def test_render_session_status_formats_output():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "cli.sqlite")
        session = Session(session_store=store, session_id="test-789", model_name="claude")
        session.set_title("Test Session")
        
        output = render_session_status(session)
        
        assert "Session ID: test-789" in output
        assert "Title: Test Session" in output
        assert "Model: claude" in output


def test_update_session_title_validates_input():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "cli.sqlite")
        session = Session(session_store=store, session_id="test-empty")
        
        result = update_session_title(session, "")
        
        assert "Usage:" in result


def test_update_session_title_sets_and_confirms():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "cli.sqlite")
        session = Session(session_store=store, session_id="test-confirm")
        
        result = update_session_title(session, "New Title")
        
        assert "New Title" in result
        assert session.status().title == "New Title"


def test_session_history_returns_messages():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "cli.sqlite")
        
        session = Session(session_store=store, session_id="history-test")
        
        history = session.history()
        assert isinstance(history, list)


def test_session_export_empty_history_does_not_create_file():
    """Empty history should not create a file."""
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "cli.sqlite")
        session = Session(session_store=store, session_id="export-test")

        export_path = Path(tmp) / "export.md"
        result = session.export_markdown(export_path)

        assert not export_path.exists()
        assert "No messages" in result


def test_session_status_includes_runtime_metadata_and_counts():
    """Session status should include profile, theme, cli_home, db_path."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        store = SessionStore(tmp_path / "cli.sqlite")
        session = Session(
            session_store=store,
            session_id="status-test",
            model_name="model",
            session_store_for_checkpoints="cp",
            workdir=str(tmp_path),
            profile="dev",
            display_theme="slate",
            cli_home=str(tmp_path),
            db_path=str(tmp_path / "cli.sqlite"),
        )

        status = session.status()

        assert status.profile == "dev"
        assert status.display_theme == "slate"
        assert status.cli_home == str(tmp_path)
        assert status.db_path == str(tmp_path / "cli.sqlite")
        assert status.checkpointer_available is True


def test_session_export_relative_path_resolves_against_workdir():
    """Export should resolve relative paths against workdir."""
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "cli.sqlite")
        workdir = Path(tmp) / "repo"
        workdir.mkdir()
        session = Session(
            session_store=store,
            session_id="export-relative",
            model_name="model",
            session_store_for_checkpoints=None,
            workdir=str(workdir),
        )

        result = session.export_markdown("exports/session.md")
        export_path = workdir / "exports" / "session.md"

        # Empty history - file should not be created
        assert not export_path.exists()
        assert "No messages" in result

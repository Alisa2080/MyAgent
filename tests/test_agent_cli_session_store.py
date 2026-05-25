from agent_cli.session_store import SessionStore


class FixedTimeSessionStore(SessionStore):
    @staticmethod
    def now() -> str:
        return "2026-05-25T00:00:00+00:00"


def test_create_session_persists_metadata(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")

    session = store.create_session(workdir="/repo", model="test-model", title="Hello")

    loaded = store.get_session(session.session_id)
    assert loaded is not None
    assert loaded.session_id == session.session_id
    assert loaded.title == "Hello"
    assert loaded.workdir == "/repo"
    assert loaded.model == "test-model"
    assert loaded.status == "active"


def test_list_sessions_orders_by_updated_at(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")
    first = store.create_session(workdir="/repo", model=None, title="First")
    second = store.create_session(workdir="/repo", model=None, title="Second")
    store.touch_session(first.session_id, last_message_preview="later")

    sessions = store.list_sessions(limit=10)

    assert [item.session_id for item in sessions[:2]] == [first.session_id, second.session_id]


def test_list_sessions_orders_equal_timestamps_by_recency(tmp_path):
    store = FixedTimeSessionStore(tmp_path / "cli.sqlite")
    first = store.create_session(workdir="/repo", model=None, title="First")
    second = store.create_session(workdir="/repo", model=None, title="Second")
    store.touch_session(second.session_id, last_message_preview="later")

    sessions = store.list_sessions(limit=10)

    assert [item.session_id for item in sessions[:2]] == [second.session_id, first.session_id]


def test_resume_unknown_session_returns_none(tmp_path):
    store = SessionStore(tmp_path / "cli.sqlite")

    assert store.get_session("missing") is None


def test_title_from_message_truncates_and_normalizes_whitespace():
    assert SessionStore.title_from_message("  hello\nworld  ", max_length=20) == "hello world"
    assert SessionStore.title_from_message("x" * 80, max_length=10) == "xxxxxxx..."

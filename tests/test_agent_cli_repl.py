from types import SimpleNamespace

from agent_cli.repl import AgentCLI


class FakeStore:
    def __init__(self):
        self.created = []
        self.touched = []
        self.sessions = {}

    def create_session(self, *, workdir, model, title="New session", session_id=None):
        sid = session_id or f"s{len(self.created) + 1}"
        record = SimpleNamespace(
            session_id=sid,
            title=title,
            workdir=workdir,
            model=model,
            updated_at="now",
            last_message_preview=None,
        )
        self.created.append(record)
        self.sessions[sid] = record
        return record

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def list_sessions(self, limit=20):
        return list(self.sessions.values())

    def touch_session(self, session_id, **kwargs):
        self.touched.append((session_id, kwargs))

    def new_session_id(self):
        return f"s{len(self.created) + 1}"

    def title_from_message(self, message, max_length=60):
        normalized = " ".join(message.split())
        if not normalized:
            return "New session"
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3].rstrip() + "..."


def test_submit_message_invokes_runner_with_thread_id(monkeypatch):
    calls = []

    def fake_runner(agent, input_data, config):
        calls.append((agent, input_data, config))
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name="model",
    )

    output = cli.submit_message("hello")

    assert output == "ok"
    assert calls[0][0] == "agent"
    assert calls[0][1] == {"messages": [{"role": "user", "content": "hello"}]}
    assert calls[0][2] == {"configurable": {"thread_id": "s1"}}


def test_fresh_submit_message_does_not_write_metadata_when_runner_raises():
    store = FakeStore()

    def fake_runner(agent, input_data, config):
        raise RuntimeError("agent failed")

    cli = AgentCLI(
        session_store=store,
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name="model",
    )

    try:
        cli.submit_message("hello")
    except RuntimeError as exc:
        assert str(exc) == "agent failed"
    else:
        raise AssertionError("expected submit_message to raise")

    assert store.created == []
    assert store.touched == []
    assert cli.session_id is None


def test_fresh_submit_message_creates_session_after_success_with_runner_thread_id():
    store = FakeStore()
    calls = []

    def fake_runner(agent, input_data, config):
        calls.append((agent, input_data, config))
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = AgentCLI(
        session_store=store,
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name="model",
    )

    output = cli.submit_message("hello")

    assert output == "ok"
    assert calls[0][2] == {"configurable": {"thread_id": "s1"}}
    assert cli.session_id == "s1"
    assert [record.session_id for record in store.created] == ["s1"]
    assert store.created[0].title == "hello"
    assert store.touched == [("s1", {"last_message_preview": "hello"})]


def test_run_repl_prints_error_and_continues_after_message_failure(
    monkeypatch, capsys
):
    entries = iter(["boom", EOFError])
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: (_ for _ in ()).throw(
            RuntimeError("agent failed")
        ),
        workdir="/repo",
        model_name=None,
    )

    def fake_input(prompt):
        entry = next(entries)
        if entry is EOFError:
            raise EOFError
        return entry

    monkeypatch.setattr("builtins.input", fake_input)

    code = cli.run_repl()

    captured = capsys.readouterr()
    assert code == 0
    assert "Error: agent failed" in captured.err


def test_handle_command_new_switches_session():
    store = FakeStore()
    cli = AgentCLI(
        session_store=store,
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    cli.ensure_session()
    assert cli.session_id == "s1"

    result = cli.handle_command("/new")

    assert "Started session" in result
    assert cli.session_id == "s2"


def test_handle_command_resume_rejects_unknown_session():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    assert cli.handle_command("/resume missing") == "Unknown session: missing"


def test_handle_command_resume_touches_session_metadata():
    store = FakeStore()
    store.create_session(
        workdir="/repo",
        model=None,
        title="Known session",
        session_id="known",
    )
    cli = AgentCLI(
        session_store=store,
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    result = cli.handle_command("/resume known")

    assert result == "Resumed session: known"
    assert cli.session_id == "known"
    assert store.touched == [("known", {})]

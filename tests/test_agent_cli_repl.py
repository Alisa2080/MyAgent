from types import SimpleNamespace

from agent_cli.repl import AgentCLI


class FakeStore:
    def __init__(self):
        self.created = []
        self.touched = []
        self.sessions = {}
        self._session_counter = 0

    def create_session(self, *, workdir, model, title="New session", session_id=None):
        sid = session_id or self.new_session_id()
        record = SimpleNamespace(
            session_id=sid,
            title=title,
            workdir=workdir,
            model=model,
            updated_at="now",
            created_at="now",
            last_message_preview=None,
        )
        self.created.append(record)
        self.sessions[sid] = record  # Store under the actual sid, not session_id param
        return record

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def get_or_create_session(self, *, session_id, workdir, model, title="New session"):
        existing = self.get_session(session_id)
        if existing is not None:
            return existing
        return self.create_session(
            workdir=workdir,
            model=model,
            title=title,
            session_id=session_id,
        )

    def update_session(self, session_id, *, title=None):
        session = self.get_session(session_id)
        if session:
            session.title = title if title is not None else session.title

    def list_sessions(self, limit=20):
        return list(self.sessions.values())

    def touch_session(self, session_id, **kwargs):
        self.touched.append((session_id, kwargs))

    def new_session_id(self):
        self._session_counter += 1
        return f"s{self._session_counter}"

    def title_from_message(self, message, max_length=60):
        normalized = " ".join(message.split())
        if not normalized:
            return "New session"
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3].rstrip() + "..."


class FakeBackgroundRegistry:
    def __init__(self):
        self.started = []
        self.steers = []
        self.stopped = []
        self.approved = []
        self.notifications = []
        self.records = {}
        self.list_active_only_calls = []

    def start(self, prompt):
        record = SimpleNamespace(
            task_id="bg_12345678",
            session_id="session-bg",
            title="Background task",
            status="queued",
            pending_steer_count=0,
            last_result_preview=None,
            last_error=None,
            updated_at="now",
            created_at="now",
        )
        self.started.append(prompt)
        self.records[record.task_id] = record
        return record

    def list_tasks(self, active_only=False):
        self.list_active_only_calls.append(active_only)
        return list(self.records.values())

    def steer(self, task_id, message):
        self.steers.append((task_id, message))
        return SimpleNamespace(id=1, task_id=task_id, message=message)

    def stop(self, task_id):
        self.stopped.append(task_id)
        return self.records[task_id]

    def approval_requests(self, task_id):
        from agent_cli.approval import ApprovalRequest

        return [ApprovalRequest({"name": "terminal", "args": {"command": "pwd"}}, {})]

    def approve(self, task_id, resume_value):
        self.approved.append((task_id, resume_value))

    def drain_notifications(self):
        items = self.notifications
        self.notifications = []
        return items


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


def test_run_repl_prints_error_and_continues_after_message_failure(capsys):
    entries = iter(["boom", EOFError])

    class FakePrompt:
        def prompt(self, prompt_text):
            entry = next(entries)
            if entry is EOFError:
                raise EOFError
            return entry

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: (_ for _ in ()).throw(
            RuntimeError("agent failed")
        ),
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
    )

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


def test_handle_background_command_starts_task():
    registry = FakeBackgroundRegistry()
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    output = cli.handle_command("/background fix tests")

    assert registry.started == ["fix tests"]
    assert "Started background task bg_12345678" in output
    assert "session-bg" in output


def test_handle_tasks_and_queue_render_background_tasks():
    registry = FakeBackgroundRegistry()
    registry.start("fix tests")
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    assert "bg_12345678" in cli.handle_command("/tasks")
    assert "bg_12345678" in cli.handle_command("/queue")
    assert registry.list_active_only_calls == [False, True]


def test_handle_steer_stop_and_approve(monkeypatch):
    registry = FakeBackgroundRegistry()
    registry.start("fix tests")
    monkeypatch.setattr(
        "agent_cli.repl.collect_approval_decisions",
        lambda requests: {"decisions": [{"type": "approve"}]},
    )
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    assert "Queued steer" in cli.handle_command("/steer bg_12345678 retry now")
    assert "Stop requested" in cli.handle_command("/stop bg_12345678")
    assert "Approved background task" in cli.handle_command("/approve bg_12345678")

    assert registry.steers == [("bg_12345678", "retry now")]
    assert registry.stopped == ["bg_12345678"]
    assert registry.approved == [("bg_12345678", {"decisions": [{"type": "approve"}]})]


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


def test_run_repl_uses_prompt_adapter(monkeypatch, capsys):
    entries = iter(["/help", EOFError])
    prompts = []

    class FakePrompt:
        def prompt(self, prompt_text):
            prompts.append(prompt_text)
            entry = next(entries)
            if entry is EOFError:
                raise EOFError
            return entry

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
    )

    code = cli.run_repl()

    captured = capsys.readouterr()
    assert code == 0
    assert prompts == ["> ", "> "]
    assert "Available commands:" in captured.out


def test_handle_command_history_renders_empty_history():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )
    cli.ensure_session()

    result = cli.handle_command("/history")

    assert "No messages" in result


def test_handle_command_history_with_limit():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )
    cli.ensure_session()

    result = cli.handle_command("/history 10")

    assert "History" in result or "No messages" in result


def test_handle_command_export_usage_error():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )
    cli.ensure_session()

    result = cli.handle_command("/export")

    assert "Usage" in result
    assert "path.md" in result


def test_run_repl_handles_history_command(monkeypatch, capsys):
    entries = iter(["/history", EOFError])

    class FakePrompt:
        def prompt(self, prompt_text):
            entry = next(entries)
            if entry is EOFError:
                raise EOFError
            return entry

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
    )

    code = cli.run_repl()

    assert code == 0
    captured = capsys.readouterr()
    assert "History" in captured.out or "No messages" in captured.out


def test_run_repl_handles_export_command(monkeypatch, capsys, tmp_path):
    export_file = tmp_path / "export.md"
    entries = iter([f"/export {export_file}", EOFError])

    class FakePrompt:
        def prompt(self, prompt_text):
            entry = next(entries)
            if entry is EOFError:
                raise EOFError
            return entry

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir=str(tmp_path),
        model_name=None,
        prompt_session=FakePrompt(),
    )

    code = cli.run_repl()

    assert code == 0
    captured = capsys.readouterr()
    # With empty history, should report "No messages"
    assert "No messages" in captured.out or "export.md" in captured.out or "Exported" in captured.out


def test_handle_interrupts_uses_collected_decisions(monkeypatch, tmp_path):
    from agent_cli.repl import AgentCLI
    from agent_cli.session_store import SessionStore

    calls = []

    class FakeAgent:
        pass

    def runner(agent, input_data, config):
        calls.append(input_data)
        if len(calls) == 1:
            return {
                "__interrupt__": [
                    {
                        "value": {
                            "action_requests": [{"name": "terminal", "args": {"command": "pwd"}}],
                            "review_configs": [{"description": "review"}],
                        }
                    }
                ]
            }
        return {"messages": [{"role": "assistant", "content": "done"}]}

    monkeypatch.setattr(
        "agent_cli.repl.collect_approval_decisions",
        lambda requests: {"decisions": [{"type": "reject", "message": "no"}]},
    )

    cli = AgentCLI(
        session_store=SessionStore(tmp_path / "cli.sqlite"),
        checkpointer=object(),
        agent_factory=lambda checkpointer: FakeAgent(),
        runner=runner,
        workdir=str(tmp_path),
        model_name=None,
    )

    assert cli.submit_message("hi") == "done"
    assert calls[1].resume == {"decisions": [{"type": "reject", "message": "no"}]}


def test_handle_command_routes_dynamic_skill_to_agent(monkeypatch, tmp_path):
    from agent_cli.repl import AgentCLI
    from agent_cli.skill_commands import SkillCommand

    submitted = []
    command = SkillCommand(
        command="python-debug",
        skill={
            "name": "python-debug",
            "dir": "/repo/skills/python-debug",
            "content": "# Python Debug",
            "supporting_files": [],
        },
    )

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=object(),
        agent_factory=lambda checkpointer: object(),
        runner=lambda agent, input_data, config: {"messages": [{"role": "assistant", "content": "ok"}]},
        workdir=str(tmp_path),
        model_name=None,
        skill_commands_provider=lambda: {"python-debug": command},
        skill_loader=lambda item: item,
    )
    monkeypatch.setattr(cli, "submit_message", lambda text: submitted.append(text) or "ok")

    assert cli.handle_command("/python-debug fix it") == "ok"
    assert "# Python Debug" in submitted[0]
    assert "User request:\nfix it" in submitted[0]


def test_render_skills_shows_dynamic_command_and_conflict(monkeypatch):
    from agent_cli.skill_commands import SkillCommand, SkillDiscovery, SkillEntry

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=object(),
        agent_factory=lambda checkpointer: object(),
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        skill_discovery_provider=lambda: SkillDiscovery(
            commands={
                "python-debug": SkillCommand(
                    command="python-debug",
                    skill={
                        "name": "python-debug",
                        "description": "Debug Python.",
                    },
                )
            },
            entries=[
                SkillEntry(
                    skill={"name": "python-debug", "description": "Debug Python."},
                    command="python-debug",
                    note="",
                ),
                SkillEntry(
                    skill={"name": "help", "description": "Builtin conflict."},
                    command=None,
                    note="conflicts with built-in command /help",
                ),
            ],
        ),
    )

    output = cli.handle_command("/skills")

    assert "python-debug - Debug Python. (/python-debug)" in output
    assert "help - Builtin conflict. (conflicts with built-in command /help)" in output

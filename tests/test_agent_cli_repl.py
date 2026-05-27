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
        self.list_limit_calls = []
        self.joined = []

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

    def list_tasks(self, active_only=False, limit=20):
        self.list_active_only_calls.append(active_only)
        self.list_limit_calls.append(limit)
        return list(self.records.values())

    def steer(self, task_id, message):
        self.steers.append((task_id, message))
        return SimpleNamespace(id=1, task_id=task_id, message=message)

    def stop(self, task_id):
        self.stopped.append(task_id)
        return self.records[task_id]

    def join(self, task_id, timeout=None):
        self.joined.append((task_id, timeout))

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


def test_handle_stop_reports_completing_as_already_done():
    registry = FakeBackgroundRegistry()
    record = registry.start("fix tests")
    record.status = "completing"
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    assert cli.handle_command("/stop bg_12345678") == (
        "Background task bg_12345678 is already completing"
    )


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


def test_run_repl_drains_background_notifications_before_prompt(capsys):
    from agent_cli.background import BackgroundNotification

    class Registry(FakeBackgroundRegistry):
        def __init__(self):
            super().__init__()
            self.notifications = [
                BackgroundNotification(
                    kind="done",
                    task_id="bg_12345678",
                    session_id="session-bg",
                    status="completed",
                    message="done",
                )
            ]

    class FakePrompt:
        def prompt(self, prompt_text):
            raise EOFError

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
        background_registry=Registry(),
        show_banner=False,
    )

    assert cli.run_repl() == 0
    captured = capsys.readouterr()
    assert "[background done] bg_12345678" in captured.out


def test_run_repl_sanitizes_background_notification_messages(capsys):
    from agent_cli.background import BackgroundNotification

    class Registry(FakeBackgroundRegistry):
        def __init__(self):
            super().__init__()
            self.notifications = [
                BackgroundNotification(
                    kind="done",
                    task_id="bg_12345678",
                    session_id="session-bg",
                    status="completed",
                    message="first line\nsecond line",
                )
            ]

    class FakePrompt:
        def prompt(self, prompt_text):
            raise EOFError

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
        background_registry=Registry(),
        show_banner=False,
    )

    assert cli.run_repl() == 0
    captured = capsys.readouterr()
    assert "first line second line" in captured.out
    assert "first line\nsecond line" not in captured.out


def test_run_repl_stops_active_background_tasks_on_exit(capsys):
    class Registry(FakeBackgroundRegistry):
        def list_tasks(self, active_only=False, limit=20):
            self.list_active_only_calls.append(active_only)
            self.list_limit_calls.append(limit)
            return [
                SimpleNamespace(
                    task_id="bg_12345678",
                    session_id="session-bg",
                    title="Task",
                    status="running",
                    pending_steer_count=0,
                    last_result_preview=None,
                    last_error=None,
                    prompt_preview="Task",
                    updated_at="now",
                    created_at="now",
                )
            ]

        def stop(self, task_id):
            self.stopped.append(task_id)
            return SimpleNamespace(task_id=task_id, status="stopping")

    class FakePrompt:
        def prompt(self, prompt_text):
            raise EOFError

    registry = Registry()
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
        background_registry=registry,
        show_banner=False,
    )

    assert cli.run_repl() == 0
    assert registry.stopped == ["bg_12345678"]
    assert registry.joined == [("bg_12345678", 0.5)]
    assert registry.list_limit_calls == [None]


def test_run_repl_stops_all_active_background_tasks_on_exit(capsys):
    class Registry(FakeBackgroundRegistry):
        def list_tasks(self, active_only=False, limit=20):
            self.list_active_only_calls.append(active_only)
            self.list_limit_calls.append(limit)
            return [
                SimpleNamespace(
                    task_id=f"bg_{index:08d}",
                    session_id=f"session-bg-{index}",
                    title="Task",
                    status="running",
                    pending_steer_count=0,
                    last_result_preview=None,
                    last_error=None,
                    prompt_preview="Task",
                    updated_at="now",
                    created_at="now",
                )
                for index in range(25)
            ]

        def stop(self, task_id):
            self.stopped.append(task_id)
            return SimpleNamespace(task_id=task_id, status="stopping")

    class FakePrompt:
        def prompt(self, prompt_text):
            raise EOFError

    registry = Registry()
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
        background_registry=registry,
        show_banner=False,
    )

    assert cli.run_repl() == 0
    assert len(registry.stopped) == 25
    assert registry.list_limit_calls == [None]


def test_run_repl_requests_all_stops_before_joining_on_exit(capsys):
    class Registry(FakeBackgroundRegistry):
        def __init__(self):
            super().__init__()
            self.events = []

        def list_tasks(self, active_only=False, limit=20):
            return [
                SimpleNamespace(
                    task_id=f"bg_{index:08d}",
                    session_id=f"session-bg-{index}",
                    title="Task",
                    status="running",
                    pending_steer_count=0,
                    last_result_preview=None,
                    last_error=None,
                    prompt_preview="Task",
                    updated_at="now",
                    created_at="now",
                )
                for index in range(3)
            ]

        def stop(self, task_id):
            self.events.append(("stop", task_id))
            self.stopped.append(task_id)
            return SimpleNamespace(task_id=task_id, status="stopping")

        def join(self, task_id, timeout=None):
            self.events.append(("join", task_id))
            self.joined.append((task_id, timeout))

    class FakePrompt:
        def prompt(self, prompt_text):
            raise EOFError

    registry = Registry()
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
        background_registry=registry,
        show_banner=False,
    )

    assert cli.run_repl() == 0
    assert registry.events == [
        ("stop", "bg_00000000"),
        ("stop", "bg_00000001"),
        ("stop", "bg_00000002"),
        ("join", "bg_00000000"),
        ("join", "bg_00000001"),
        ("join", "bg_00000002"),
    ]


def test_run_repl_finalizes_still_active_tasks_after_exit_join(capsys):
    active_record = SimpleNamespace(
        task_id="bg_12345678",
        session_id="session-bg",
        title="Task",
        status="stopping",
        pending_steer_count=0,
        last_result_preview=None,
        last_error=None,
        prompt_preview="Task",
        updated_at="now",
        created_at="now",
    )

    class Store:
        def get_task(self, task_id):
            return active_record

    class Registry(FakeBackgroundRegistry):
        def __init__(self):
            super().__init__()
            self.store = Store()
            self.finalized = []

        def list_tasks(self, active_only=False, limit=20):
            return [active_record]

        def stop(self, task_id):
            self.stopped.append(task_id)
            return active_record

        def finalize_stopping(self, task_id):
            self.finalized.append(task_id)
            active_record.status = "stopped"
            return active_record

    class FakePrompt:
        def prompt(self, prompt_text):
            raise EOFError

    registry = Registry()
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
        background_registry=registry,
        show_banner=False,
    )

    assert cli.run_repl() == 0
    assert registry.finalized == ["bg_12345678"]
    assert active_record.status == "stopped"


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


def test_handle_command_history_rejects_invalid_limit():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    result = cli.handle_command("/history nope")

    assert result == "Usage: /history [N]"


def test_handle_copy_supports_nth_recent_assistant_reply(capsys):
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )
    cli.assistant_replies.extend(["first", "second"])

    output = cli.handle_command("/copy 2")

    captured = capsys.readouterr()
    assert "Zmlyc3Q=" in captured.out
    assert output == "Copied assistant reply 2."


def test_handle_copy_validates_args_and_empty_state():
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    assert cli.handle_command("/copy nope") == "Usage: /copy [N]"
    assert cli.handle_command("/copy 0") == "Usage: /copy [N]"
    assert cli.handle_command("/copy") == "No assistant reply available to copy."


def test_handle_retry_resubmits_last_attempted_user_message_after_failure():
    calls = []

    def fake_runner(agent, input_data, config):
        calls.append(input_data["messages"][0]["content"])
        if len(calls) == 1:
            raise RuntimeError("transient")
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name=None,
    )

    try:
        cli.submit_message("hello")
    except RuntimeError:
        pass

    assert cli.handle_command("/retry") == "ok"
    assert calls == ["hello", "hello"]


def test_handle_usage_renders_local_session_stats(tmp_path):
    class FakeCheckpointer:
        def get_tuple(self, config):
            return {
                "checkpoint": {
                    "channel_values": {
                        "messages": [
                            {"role": "user", "content": "hello there"},
                            {"role": "assistant", "content": "hi"},
                        ]
                    }
                }
            }

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=FakeCheckpointer(),
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir=str(tmp_path),
        model_name="model-x",
    )
    cli.ensure_session()
    cli.last_call_elapsed_seconds = 1.234
    cli.assistant_replies.append("assistant text")

    output = cli.handle_command("/usage")

    assert "Usage:" in output
    assert "Session ID: s1" in output
    assert "Model: model-x" in output
    assert "Messages: 2" in output
    assert "Turns: 1" in output
    assert "Estimated tokens:" in output
    assert "Checkpointer: available" in output
    assert "Last call: 1.234s" in output
    assert "Assistant replies tracked: 1" in output


def test_run_repl_sanitizes_input_before_command_detection(capsys):
    entries = iter(["\x1b[200~/help\x1b[201~", EOFError])

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
            AssertionError("command should not reach runner")
        ),
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
    )

    assert cli.run_repl() == 0
    captured = capsys.readouterr()
    assert "Available commands:" in captured.out


def test_handle_command_dispatches_through_handler_registry():
    calls = []
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    def fake_handler(cli_arg, arg, command):
        calls.append((cli_arg, arg, command.name))
        return "handled"

    cli.command_handlers = {"help": fake_handler}

    assert cli.handle_command("/help extra") == "handled"
    assert calls == [(cli, "extra", "help")]


def test_all_builtin_commands_have_registered_handlers():
    from agent_cli.command_handlers import build_command_handlers
    from agent_cli.commands import COMMAND_REGISTRY

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )
    handlers = build_command_handlers(cli)

    missing = [
        command.name
        for command in COMMAND_REGISTRY
        if command.effective_handler_key not in handlers
    ]
    assert missing == []

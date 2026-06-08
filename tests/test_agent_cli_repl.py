import importlib
import sys
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
            prompt_preview=prompt,
            updated_at="now",
            created_at="now",
            started_at=None,
            finished_at=None,
            cancel_requested=False,
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

    def get_task(self, task_id):
        return self.records.get(task_id)

    def get_steers(self, task_id, limit=20):
        return [
            SimpleNamespace(
                id=index + 1,
                task_id=task_id,
                message=message,
                status="pending",
                created_at="now",
                consumed_at=None,
            )
            for index, (stored_task_id, message) in enumerate(self.steers)
            if stored_task_id == task_id
        ][-limit:]


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


def test_run_repl_ctrl_c_exits_instead_of_looping(capsys):
    class FakePrompt:
        def __init__(self):
            self.calls = 0

        def prompt(self, prompt_text):
            self.calls += 1
            raise KeyboardInterrupt

    prompt = FakePrompt()
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {"messages": []},
        workdir="/repo",
        model_name=None,
        prompt_session=prompt,
        show_banner=False,
    )

    assert cli.run_repl() == 130
    assert prompt.calls == 1
    captured = capsys.readouterr()
    assert captured.out.endswith("\n\n")
    assert "Type /help for commands. Ctrl-D exits." in captured.out
    assert captured.err == ""


def test_run_repl_ctrl_c_stops_background_tasks_and_returns_130(capsys):
    class Registry(FakeBackgroundRegistry):
        def __init__(self):
            super().__init__()
            self.events = []

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
                    started_at=None,
                    finished_at=None,
                    cancel_requested=False,
                )
            ]

        def stop(self, task_id):
            self.events.append(("stop", task_id))
            self.stopped.append(task_id)
            return SimpleNamespace(task_id=task_id, status="stopping")

        def join(self, task_id, timeout=None):
            self.events.append(("join", task_id, timeout))
            self.joined.append((task_id, timeout))

    class FakePrompt:
        def prompt(self, prompt_text):
            raise KeyboardInterrupt

    registry = Registry()
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {"messages": []},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
        background_registry=registry,
        show_banner=False,
    )

    assert cli.run_repl() == 130
    captured = capsys.readouterr()
    assert registry.events == [
        ("stop", "bg_12345678"),
        ("join", "bg_12345678", 0.5),
    ]
    assert registry.stopped == ["bg_12345678"]
    assert registry.joined == [("bg_12345678", 0.5)]
    assert registry.list_limit_calls == [None]
    assert "Stop requested: bg_12345678" in captured.out
    assert "Inspect background task history with /tasks all" in captured.out


def test_run_repl_ctrl_c_finalizes_stopping_background_tasks(capsys):
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
        started_at=None,
        finished_at=None,
        cancel_requested=False,
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
            raise KeyboardInterrupt

    registry = Registry()
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {"messages": []},
        workdir="/repo",
        model_name=None,
        prompt_session=FakePrompt(),
        background_registry=registry,
        show_banner=False,
    )

    assert cli.run_repl() == 130
    captured = capsys.readouterr()
    assert registry.stopped == ["bg_12345678"]
    assert registry.finalized == ["bg_12345678"]
    assert active_record.status == "stopped"
    assert "Stop requested: bg_12345678" in captured.out
    assert "Stopped after join: bg_12345678" in captured.out


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
        "agent_cli.command_handlers.background.collect_approval_decisions",
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
    assert "resume with /resume session-bg" in captured.out


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


def test_background_failure_notification_includes_inspect_action(capsys):
    from agent_cli.background import BackgroundNotification

    registry = FakeBackgroundRegistry()
    registry.notifications = [
        BackgroundNotification(
            kind="failed",
            task_id="bg_12345678",
            session_id="session-bg",
            status="failed",
            message="boom",
        )
    ]
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    cli._drain_background_notifications()

    captured = capsys.readouterr()
    assert "inspect with /tasks bg_12345678" in captured.out


def test_background_stopped_notification_includes_session(capsys):
    from agent_cli.background import BackgroundNotification

    registry = FakeBackgroundRegistry()
    registry.notifications = [
        BackgroundNotification(
            kind="stopped",
            task_id="bg_12345678",
            session_id="session-bg",
            status="stopped",
            message="stopped",
        )
    ]
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    cli._drain_background_notifications()

    captured = capsys.readouterr()
    assert "Stopped bg_12345678" in captured.out
    assert "session session-bg" in captured.out


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
    captured = capsys.readouterr()
    assert registry.stopped == ["bg_12345678"]
    assert registry.joined == [("bg_12345678", 0.5)]
    assert registry.list_limit_calls == [None]
    assert "Stop requested: bg_12345678" in captured.out
    assert "Inspect background task history with /tasks all" in captured.out


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
    captured = capsys.readouterr()
    assert registry.finalized == ["bg_12345678"]
    assert active_record.status == "stopped"
    assert "Stopped after join: bg_12345678" in captured.out


def test_run_repl_reports_tasks_still_active_after_exit_join(capsys):
    active_record = SimpleNamespace(
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
        started_at=None,
        finished_at=None,
        cancel_requested=False,
    )

    class Store:
        def get_task(self, task_id):
            return active_record

    class Registry(FakeBackgroundRegistry):
        def __init__(self):
            super().__init__()
            self.store = Store()

        def list_tasks(self, active_only=False, limit=20):
            return [active_record]

        def stop(self, task_id):
            self.stopped.append(task_id)
            active_record.status = "running"
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
    captured = capsys.readouterr()
    assert "Still active after join: bg_12345678" in captured.out


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

    class FakeCommand:
        def __init__(self, *, resume):
            self.resume = resume

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
    monkeypatch.setattr("agent_cli.repl.Command", FakeCommand)

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
    from agent_cli.command_context import CommandContext

    calls = []
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
    )

    def fake_handler(ctx_arg, arg, command):
        calls.append((ctx_arg, arg, command.name))
        return "handled"

    cli.command_handlers = {"help": fake_handler}

    assert cli.handle_command("/help extra") == "handled"
    assert isinstance(calls[0][0], CommandContext)
    assert calls[0][0].cli is cli
    assert calls[0][1:] == ("extra", "help")


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
    handlers = build_command_handlers(cli.command_context)

    missing = [
        command.name
        for command in COMMAND_REGISTRY
        if command.effective_handler_key not in handlers
    ]
    assert missing == []


def test_agent_cli_no_long_legacy_command_dispatch():
    assert not hasattr(AgentCLI, "_legacy_handle_command")


def test_reload_updates_runtime_settings_and_clears_agent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "display:\n  markdown: strip\n  theme: slate\n"
        "model:\n  name: new-model\n"
        "session:\n  default_title: Reloaded\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_CLI_HOME", str(home))

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir=str(tmp_path),
        model_name="old-model",
        cli_home=str(home),
        display_theme="default",
    )
    cli._agent = "old-agent"

    output = cli.handle_command("/reload")

    assert "Reloaded" in output
    assert cli.model_name == "new-model"
    assert cli.default_title == "Reloaded"
    assert cli.display_theme == "slate"
    assert cli.display_markdown == "strip"
    assert cli._agent is None


def test_display_markdown_strip_formats_live_assistant_output():
    def fake_runner(agent, input_data, config):
        return {"messages": [{"role": "assistant", "content": "**hello** `world`"}]}

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=fake_runner,
        workdir="/repo",
        model_name="model",
        display_markdown="strip",
    )

    assert cli.submit_message("hi") == "hello world"


def test_tasks_with_task_id_renders_detail_and_resume_hint():
    registry = FakeBackgroundRegistry()
    record = registry.start("do work")
    record.status = "completed"
    record.last_result_preview = "done"
    record.last_error = None
    record.started_at = "started"
    record.finished_at = "finished"
    record.cancel_requested = True
    record.pending_steer_count = 2
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    output = cli.handle_command(f"/tasks {record.task_id}")

    assert record.task_id in output
    assert "completed" in output
    assert "Created: now" in output
    assert "Updated: now" in output
    assert "Started: started" in output
    assert "Finished: finished" in output
    assert "Cancel requested: yes" in output
    assert "Pending steers: 2" in output
    assert "Prompt: do work" in output
    assert "Result: done" in output
    assert "Error: -" in output
    assert f"/resume {record.session_id}" in output


def test_tail_renders_result_error_and_steers():
    registry = FakeBackgroundRegistry()
    record = registry.start("do work")
    record.last_result_preview = "latest result"
    record.last_error = "latest error"
    registry.steer(record.task_id, "extra instruction")
    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer=None,
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name=None,
        background_registry=registry,
    )

    output = cli.handle_command(f"/tail {record.task_id}")

    assert "latest result" in output
    assert "latest error" in output
    assert "extra instruction" in output


def test_command_handlers_do_not_depend_on_agent_cli_private_helpers():
    from pathlib import Path

    handler_dir = Path("agent_cli/command_handlers")
    forbidden = (
        "._set_session",
        "._require_background_registry",
        "._effective_cli_home",
        "from agent_cli.repl import AgentCLI",
    )

    offenders = {}
    for path in handler_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        found = [item for item in forbidden if item in text]
        if found:
            offenders[str(path)] = found

    assert offenders == {}


def test_handle_cron_list_dispatches_to_cron_handler(monkeypatch):
    import agent_cli.command_handlers.cron as cron_handler
    from agent_cli.repl import AgentCLI

    monkeypatch.setattr(
        cron_handler.cron_commands,
        "list_cron_jobs",
        lambda include_disabled=False: cron_handler.cron_commands.CronCommandResult(
            f"all={include_disabled}"
        ),
    )

    cli = AgentCLI(
        session_store=FakeStore(),
        checkpointer="cp",
        agent_factory=lambda checkpointer: "agent",
        runner=lambda agent, input_data, config: {},
        workdir="/repo",
        model_name="model",
    )

    assert cli.handle_command("/cron list --all") == "all=True"


def make_cli(**kwargs):
    params = {
        "session_store": FakeStore(),
        "checkpointer": "cp",
        "agent_factory": lambda checkpointer: "agent",
        "runner": lambda agent, input_data, config: {
            "messages": [{"role": "assistant", "content": "ok"}]
        },
        "workdir": "/repo",
        "model_name": "model",
        "show_banner": False,
    }
    params.update(kwargs)
    return AgentCLI(**params)


def test_repl_does_not_start_cron_scheduler(monkeypatch, tmp_path):
    import agent_cli.repl as repl_module

    calls = []
    monkeypatch.setattr(
        repl_module,
        "start_cron_scheduler",
        lambda *args, **kwargs: calls.append((args, kwargs)),
        raising=False,
    )

    cli = repl_module.AgentCLI(
        session_store=FakeStore(),
        checkpointer=object(),
        agent_factory=lambda config: object(),
        runner=lambda agent, messages, config: {"messages": []},
        workdir=str(tmp_path),
        model_name=None,
        cron_enabled=True,
    )

    assert cli is not None
    assert calls == []


def test_repl_import_does_not_import_cron_lifecycle():
    import agent_cli

    module_names = (
        "agent_cli.repl",
        "agent_cli.command_handlers",
        "agent_cli.command_handlers.cron",
        "agent_cli.cron_commands",
        "agent_core.cron_lifecycle",
    )
    attr_names = ("repl", "command_handlers", "cron_commands")
    saved_modules = {name: sys.modules.get(name) for name in module_names}
    saved_attrs = {
        name: getattr(agent_cli, name)
        for name in attr_names
        if hasattr(agent_cli, name)
    }
    try:
        for module_name in module_names:
            sys.modules.pop(module_name, None)
        for attr_name in attr_names:
            if hasattr(agent_cli, attr_name):
                delattr(agent_cli, attr_name)

        importlib.import_module("agent_cli.repl")

        assert "agent_core.cron_lifecycle" not in sys.modules
    finally:
        for module_name in module_names:
            if saved_modules[module_name] is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = saved_modules[module_name]
        for attr_name in attr_names:
            if attr_name in saved_attrs:
                setattr(agent_cli, attr_name, saved_attrs[attr_name])
            elif hasattr(agent_cli, attr_name):
                delattr(agent_cli, attr_name)


def test_run_repl_does_not_start_or_stop_cron_scheduler(monkeypatch):
    import agent_cli.repl as repl

    calls = []
    monkeypatch.setattr(
        repl,
        "start_cron_scheduler",
        lambda *args, **kwargs: calls.append(("start", args, kwargs)) or True,
        raising=False,
    )
    monkeypatch.setattr(
        repl,
        "stop_cron_scheduler",
        lambda *args, **kwargs: calls.append(("stop", args, kwargs)) or True,
        raising=False,
    )

    cli = make_cli(cron_enabled=True, cron_interval_seconds=7)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: (_ for _ in ()).throw(EOFError()))

    assert cli.run_repl() == 0
    assert calls == []


def test_run_repl_accepts_disabled_cron_compatibility_arg(monkeypatch):
    import agent_cli.repl as repl

    calls = []
    monkeypatch.setattr(
        repl,
        "start_cron_scheduler",
        lambda *args, **kwargs: calls.append(("start", args, kwargs)) or True,
        raising=False,
    )

    cli = make_cli(cron_enabled=False)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: (_ for _ in ()).throw(EOFError()))

    assert cli.run_repl() == 0
    assert calls == []


def test_run_repl_does_not_log_cron_scheduler_start_failure(monkeypatch, caplog):
    import logging

    import agent_cli.repl as repl

    monkeypatch.setattr(
        repl,
        "start_cron_scheduler",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cron boom")),
        raising=False,
    )

    cli = make_cli(cron_enabled=True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: (_ for _ in ()).throw(EOFError()))

    with caplog.at_level(logging.ERROR, logger="agent_cli.repl"):
        assert cli.run_repl() == 0

    assert "Failed to start cron scheduler." not in caplog.text


def test_cron_notifications_print_then_inject_next_turn(monkeypatch, capsys):
    import agent_cli.repl as repl

    event = {
        "job_id": "job-1",
        "job_name": "report",
        "status": "ok",
        "final_response": "daily report done",
        "output_path": "/tmp/out.md",
    }
    drained = {"done": False}

    def fake_drain(thread_id):
        if drained["done"]:
            return []
        drained["done"] = True
        return [event]

    monkeypatch.setattr(repl, "drain_cron_notifications_for_thread_id", fake_drain)
    monkeypatch.setattr(
        repl,
        "format_cron_notification_message",
        lambda events: "CRON UPDATE: " + events[0]["final_response"],
    )

    captured_input = {}

    def runner(agent, input_data, config):
        captured_input.update(input_data)
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = make_cli(runner=runner)
    cli.session_id = "session-1"

    cli._drain_cron_notifications()
    assert "cron ok" in capsys.readouterr().out

    cli.submit_message("what changed?")

    message = captured_input["messages"][0]["content"]
    assert "CRON UPDATE: daily report done" in message
    assert "what changed?" in message
    assert cli.pending_cron_events == []


def test_cron_notifications_are_restored_when_runner_raises(monkeypatch):
    import agent_cli.repl as repl

    event = {
        "job_id": "job-1",
        "job_name": "report",
        "status": "ok",
        "final_response": "daily report done",
        "output_path": "/tmp/out.md",
    }

    monkeypatch.setattr(
        repl,
        "format_cron_notification_message",
        lambda events: "CRON UPDATE: " + events[0]["final_response"],
    )

    def runner(agent, input_data, config):
        raise RuntimeError("agent failed")

    cli = make_cli(runner=runner)
    cli.pending_cron_events = [event]

    try:
        cli.submit_message("what changed?")
    except RuntimeError as exc:
        assert str(exc) == "agent failed"
    else:
        raise AssertionError("expected submit_message to raise")

    assert cli.pending_cron_events == [event]


def test_cron_notification_fallback_includes_output_path(monkeypatch):
    import agent_cli.repl as repl

    event = {
        "job_id": "job-1",
        "job_name": "report",
        "status": "ok",
        "final_response": "daily report done",
        "output_path": "/tmp/out.md",
    }

    monkeypatch.setattr(
        repl,
        "format_cron_notification_message",
        lambda events: (_ for _ in ()).throw(RuntimeError("format failed")),
    )

    captured_input = {}

    def runner(agent, input_data, config):
        captured_input.update(input_data)
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    cli = make_cli(runner=runner)
    cli.pending_cron_events = [event]

    cli.submit_message("what changed?")

    message = captured_input["messages"][0]["content"]
    assert "job_id=job-1" in message
    assert "status=ok" in message
    assert "output_path=/tmp/out.md" in message

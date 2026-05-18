from types import SimpleNamespace


def test_default_background_quota(monkeypatch):
    from agent_core import terminal_process_policy as policy

    monkeypatch.delenv("HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK", raising=False)

    assert policy.max_background_processes_per_task() == 3


def test_background_quota_can_be_configured(monkeypatch):
    from agent_core import terminal_process_policy as policy

    monkeypatch.setenv("HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK", "5")

    assert policy.max_background_processes_per_task() == 5


def test_invalid_background_quota_falls_back_to_default(monkeypatch):
    from agent_core import terminal_process_policy as policy

    for value in ("", "abc", "0", "-2"):
        monkeypatch.setenv("HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK", value)
        assert policy.max_background_processes_per_task() == 3


def test_count_running_background_processes_for_task():
    from agent_core import terminal_process_policy as policy

    sessions = [
        SimpleNamespace(task_id="task-a", exited=False),
        SimpleNamespace(task_id="task-a", exited=True),
        SimpleNamespace(task_id="task-b", exited=False),
    ]

    class FakeRegistry:
        _running = {f"proc-{idx}": session for idx, session in enumerate(sessions)}
        _lock = None

    assert policy.count_running_background_processes("task-a", registry=FakeRegistry()) == 1


def test_background_quota_available_reports_current_and_limit(monkeypatch):
    from agent_core import terminal_process_policy as policy

    sessions = [
        SimpleNamespace(task_id="task-a", exited=False),
        SimpleNamespace(task_id="task-a", exited=False),
    ]

    class FakeRegistry:
        _running = {f"proc-{idx}": session for idx, session in enumerate(sessions)}
        _lock = None

    monkeypatch.setenv("HERMES_MAX_BACKGROUND_PROCESSES_PER_TASK", "2")

    assert policy.background_quota_available("task-a", registry=FakeRegistry()) == (False, 2, 2)


def test_background_quota_guard_serializes_same_task():
    import threading

    from agent_core import terminal_process_policy as policy

    entered = threading.Event()
    release = threading.Event()
    second_finished = threading.Event()
    events = []

    def first_worker():
        with policy.background_quota_guard("task-a"):
            events.append("first-entered")
            entered.set()
            release.wait(timeout=5)
            events.append("first-exiting")

    def second_worker():
        entered.wait(timeout=5)
        with policy.background_quota_guard("task-a"):
            events.append("second-entered")
        second_finished.set()

    first = threading.Thread(target=first_worker)
    second = threading.Thread(target=second_worker)
    first.start()
    second.start()

    assert entered.wait(timeout=5)
    assert not second_finished.wait(timeout=0.05)

    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert events == ["first-entered", "first-exiting", "second-entered"]

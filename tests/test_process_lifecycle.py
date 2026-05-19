import pytest
import threading
import time


@pytest.fixture(autouse=True)
def reset_process_lifecycle_state(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_installed", False)
    monkeypatch.setattr(lifecycle, "_cleanup_done", False)
    monkeypatch.setattr(lifecycle, "_shutdown_requested", False)
    monkeypatch.setattr(lifecycle, "_previous_signal_handlers", {})


def test_parse_signal_grace_defaults_to_one_point_five(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    monkeypatch.delenv(lifecycle.SIGTERM_GRACE_ENV, raising=False)

    assert lifecycle.signal_grace_seconds() == 1.5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0.0),
        ("2.25", 2.25),
        ("-1", 0.0),
        ("invalid", 1.5),
    ],
)
def test_parse_signal_grace_env(monkeypatch, raw, expected):
    import agent_core.process_lifecycle as lifecycle

    monkeypatch.setenv(lifecycle.SIGTERM_GRACE_ENV, raw)

    assert lifecycle.signal_grace_seconds() == expected


def test_signal_handler_interrupts_sleeps_then_raises(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(
        lifecycle,
        "interrupt_all_terminal_waits",
        lambda reason="process_signal": calls.append(("interrupt", reason))
        or {"interrupted": True},
    )
    monkeypatch.setattr(lifecycle, "signal_grace_seconds", lambda: 0.25)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    with pytest.raises(KeyboardInterrupt):
        lifecycle._signal_handler(15, None)

    assert calls == [("interrupt", "received_signal_15"), ("sleep", 0.25)]


def test_signal_handler_skips_sleep_when_grace_is_zero(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(
        lifecycle,
        "interrupt_all_terminal_waits",
        lambda reason="process_signal": calls.append(("interrupt", reason))
        or {"interrupted": True},
    )
    monkeypatch.setattr(lifecycle, "signal_grace_seconds", lambda: 0.0)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    with pytest.raises(KeyboardInterrupt):
        lifecycle._signal_handler(1, None)

    assert calls == [("interrupt", "received_signal_1")]


def test_signal_handler_marks_process_shutdown_requested(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "interrupt_all_terminal_waits", lambda reason="process_signal": {})
    monkeypatch.setattr(lifecycle, "signal_grace_seconds", lambda: 0.0)

    assert lifecycle.is_process_shutdown_requested() is False
    with pytest.raises(KeyboardInterrupt):
        lifecycle._signal_handler(15, None)

    assert lifecycle.is_process_shutdown_requested() is True


def test_signal_handler_swallows_interrupt_errors_without_logging(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    def fail_interrupt(reason="process_signal"):
        raise RuntimeError("interrupt failed")

    def fail_logging(*args, **kwargs):
        raise AssertionError("signal handler should not log")

    monkeypatch.setattr(lifecycle, "interrupt_all_terminal_waits", fail_interrupt)
    monkeypatch.setattr(lifecycle.logger, "exception", fail_logging)

    with pytest.raises(KeyboardInterrupt):
        lifecycle._signal_handler(15, None)

    assert lifecycle.is_process_shutdown_requested() is True


def test_install_unregisters_direct_cleanup_and_registers_lifecycle_cleanup(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(
        lifecycle.signal,
        "signal",
        lambda signum, handler: calls.append(("signal", signum, handler)) or lifecycle.signal.SIG_DFL,
    )
    monkeypatch.setattr(
        lifecycle.atexit,
        "unregister",
        lambda callback: calls.append(("unregister", callback)),
    )
    monkeypatch.setattr(
        lifecycle.atexit,
        "register",
        lambda callback: calls.append(("register", callback)),
    )

    assert lifecycle.install_process_signal_handlers() is True

    assert ("unregister", lifecycle.cleanup_all_environments) in calls
    assert ("register", lifecycle._atexit_cleanup) in calls


def test_signal_handler_invokes_previous_callable_after_interrupt_and_grace(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []

    def previous_handler(signum, frame):
        calls.append(("previous", signum, frame))

    monkeypatch.setattr(lifecycle, "_previous_signal_handlers", {15: previous_handler})
    monkeypatch.setattr(
        lifecycle,
        "interrupt_all_terminal_waits",
        lambda reason="process_signal": calls.append(("interrupt", reason))
        or {"interrupted": True},
    )
    monkeypatch.setattr(lifecycle, "signal_grace_seconds", lambda: 0.25)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    with pytest.raises(KeyboardInterrupt):
        lifecycle._signal_handler(15, None)

    assert calls == [
        ("interrupt", "received_signal_15"),
        ("sleep", 0.25),
        ("previous", 15, None),
    ]


def test_previous_signal_handler_exception_does_not_block_keyboard_interrupt(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []

    def previous_handler(signum, frame):
        calls.append(("previous", signum, frame))
        raise RuntimeError("previous failed")

    def fail_logging(*args, **kwargs):
        raise AssertionError("signal handler should not log previous handler failures")

    monkeypatch.setattr(lifecycle, "_previous_signal_handlers", {15: previous_handler})
    monkeypatch.setattr(
        lifecycle,
        "interrupt_all_terminal_waits",
        lambda reason="process_signal": calls.append(("interrupt", reason)) or {},
    )
    monkeypatch.setattr(lifecycle, "signal_grace_seconds", lambda: 0.0)
    monkeypatch.setattr(lifecycle.logger, "exception", fail_logging)

    with pytest.raises(KeyboardInterrupt):
        lifecycle._signal_handler(15, None)

    assert calls == [("interrupt", "received_signal_15"), ("previous", 15, None)]


def test_run_process_shutdown_cleanup_kills_processes_before_environments(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_cleanup_done", False)
    monkeypatch.setattr(lifecycle.process_registry, "kill_all", lambda: calls.append("kill_all") or 2)
    monkeypatch.setattr(lifecycle, "cleanup_all_environments", lambda: calls.append("cleanup_envs") or 3)

    result = lifecycle.run_process_shutdown_cleanup(reason="test")

    assert result == {
        "cleaned": True,
        "reason": "test",
        "killed_processes": 2,
        "cleaned_environments": 3,
        "errors": [],
    }
    assert calls == ["kill_all", "cleanup_envs"]


def test_run_process_shutdown_cleanup_runs_once(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "_cleanup_done", False)
    monkeypatch.setattr(lifecycle.process_registry, "kill_all", lambda: calls.append("kill_all") or 1)
    monkeypatch.setattr(lifecycle, "cleanup_all_environments", lambda: calls.append("cleanup_envs") or 1)

    first = lifecycle.run_process_shutdown_cleanup(reason="first")
    second = lifecycle.run_process_shutdown_cleanup(reason="second")

    assert first["cleaned"] is True
    assert second == {"cleaned": False, "reason": "second", "already_done": True}
    assert calls == ["kill_all", "cleanup_envs"]


def test_run_process_shutdown_cleanup_reports_errors_and_continues(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_cleanup_done", False)

    def fail_kill_all():
        raise RuntimeError("kill failed")

    monkeypatch.setattr(lifecycle.process_registry, "kill_all", fail_kill_all)
    monkeypatch.setattr(lifecycle, "cleanup_all_environments", lambda: 4)

    result = lifecycle.run_process_shutdown_cleanup(reason="test")

    assert result["cleaned"] is True
    assert result["killed_processes"] == 0
    assert result["cleaned_environments"] == 4
    assert result["errors"] == ["process_registry.kill_all: kill failed"]


def test_install_process_signal_handlers_registers_sigterm_sighup_and_atexit(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    signal_calls = []
    unregister_calls = []
    atexit_calls = []
    monkeypatch.setattr(lifecycle, "_installed", False)
    monkeypatch.setattr(
        lifecycle.signal,
        "signal",
        lambda signum, handler: signal_calls.append((signum, handler)) or f"previous-{signum}",
    )
    monkeypatch.setattr(
        lifecycle.atexit,
        "unregister",
        lambda handler: unregister_calls.append(handler),
    )
    monkeypatch.setattr(lifecycle.atexit, "register", lambda handler: atexit_calls.append(handler))

    installed = lifecycle.install_process_signal_handlers()

    assert installed is True
    assert (lifecycle.signal.SIGTERM, lifecycle._signal_handler) in signal_calls
    assert lifecycle._previous_signal_handlers[lifecycle.signal.SIGTERM] == (
        f"previous-{lifecycle.signal.SIGTERM}"
    )
    if hasattr(lifecycle.signal, "SIGHUP"):
        assert (lifecycle.signal.SIGHUP, lifecycle._signal_handler) in signal_calls
        assert lifecycle._previous_signal_handlers[lifecycle.signal.SIGHUP] == (
            f"previous-{lifecycle.signal.SIGHUP}"
        )
    assert unregister_calls == [lifecycle.cleanup_all_environments]
    assert atexit_calls == [lifecycle._atexit_cleanup]


def test_install_process_signal_handlers_is_idempotent(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    signal_calls = []
    unregister_calls = []
    atexit_calls = []
    monkeypatch.setattr(lifecycle, "_installed", False)
    monkeypatch.setattr(
        lifecycle.signal,
        "signal",
        lambda signum, handler: signal_calls.append((signum, handler)) or lifecycle.signal.SIG_DFL,
    )
    monkeypatch.setattr(
        lifecycle.atexit,
        "unregister",
        lambda handler: unregister_calls.append(handler),
    )
    monkeypatch.setattr(lifecycle.atexit, "register", lambda handler: atexit_calls.append(handler))

    assert lifecycle.install_process_signal_handlers() is True
    assert lifecycle.install_process_signal_handlers() is False
    assert len(atexit_calls) == 1
    assert len(unregister_calls) == 1
    assert len([call for call in signal_calls if call[0] == lifecycle.signal.SIGTERM]) == 1
    if hasattr(lifecycle.signal, "SIGHUP"):
        assert len([call for call in signal_calls if call[0] == lifecycle.signal.SIGHUP]) == 1


def test_signal_handler_grace_window_allows_foreground_wait_to_kill_process(monkeypatch):
    import agent_core.process_lifecycle as process_lifecycle
    from agent_core.terminal_lifecycle import terminal_execution_scope
    from agent_tools.hermes_terminal_toolkit.environments.local import LocalEnvironment

    env = LocalEnvironment()
    result_holder = {}
    ready = threading.Event()

    def run_command():
        with terminal_execution_scope("signal-thread"):
            ready.set()
            result_holder["result"] = env.execute("sleep 30", timeout=5)

    worker = threading.Thread(target=run_command)
    try:
        worker.start()
        assert ready.wait(timeout=5)
        time.sleep(0.3)

        monkeypatch.setattr(process_lifecycle, "signal_grace_seconds", lambda: 0.8)

        with pytest.raises(KeyboardInterrupt):
            process_lifecycle._signal_handler(15, None)

        worker.join(timeout=5)
        assert not worker.is_alive()
        assert result_holder["result"]["returncode"] == 130
        assert "[Command interrupted]" in result_holder["result"]["output"]
    finally:
        if worker.is_alive():
            process_lifecycle.interrupt_all_terminal_waits(reason="test_cleanup")
            worker.join(timeout=2)

import pytest


@pytest.fixture(autouse=True)
def reset_process_lifecycle_state(monkeypatch):
    import agent_core.process_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_installed", False)
    monkeypatch.setattr(lifecycle, "_cleanup_done", False)
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

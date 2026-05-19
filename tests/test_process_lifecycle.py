import pytest


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

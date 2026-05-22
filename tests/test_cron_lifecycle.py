import pytest


@pytest.fixture(autouse=True)
def reset_cron_lifecycle_state(monkeypatch):
    import agent_core.cron_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_thread", None)
    lifecycle._stop_event.set()


def test_start_cron_scheduler_is_idempotent(monkeypatch):
    import agent_core.cron_lifecycle as lifecycle

    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.daemon = kwargs.get("daemon")

        def start(self):
            started.append(self)

        def is_alive(self):
            return True

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(lifecycle.threading, "Thread", lambda **kwargs: FakeThread(**kwargs))

    assert lifecycle.start_cron_scheduler(interval_seconds=1) is True
    assert lifecycle.start_cron_scheduler(interval_seconds=1) is False
    assert len(started) == 1
    assert started[0].kwargs["name"] == "cron-ticker"
    assert started[0].daemon is True


def test_stop_cron_scheduler_sets_event_and_joins(monkeypatch):
    import agent_core.cron_lifecycle as lifecycle

    stopped = []

    class FakeThread:
        def is_alive(self):
            return False

        def join(self, timeout=None):
            stopped.append(timeout)

    monkeypatch.setattr(lifecycle, "_thread", FakeThread())
    lifecycle._stop_event.clear()

    assert lifecycle.stop_cron_scheduler(timeout=0.1) is True
    assert lifecycle._stop_event.is_set()
    assert stopped == [0.1]
    assert lifecycle._thread is None


def test_stop_cron_scheduler_returns_false_when_no_thread():
    import agent_core.cron_lifecycle as lifecycle

    lifecycle._stop_event.clear()

    assert lifecycle.stop_cron_scheduler(timeout=0.1) is False
    assert lifecycle._stop_event.is_set()


def test_stop_cron_scheduler_returns_false_when_thread_still_alive(monkeypatch):
    import agent_core.cron_lifecycle as lifecycle

    class FakeThread:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            return None

    thread = FakeThread()
    monkeypatch.setattr(lifecycle, "_thread", thread)

    assert lifecycle.stop_cron_scheduler(timeout=0.1) is False
    assert lifecycle._thread is thread


def test_stop_cron_scheduler_preserves_thread_restarted_during_join(monkeypatch):
    import agent_core.cron_lifecycle as lifecycle

    created = []

    class NewThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

        def start(self):
            return None

        def is_alive(self):
            return True

    class OldThread:
        def is_alive(self):
            return False

        def join(self, timeout=None):
            assert lifecycle.start_cron_scheduler(interval_seconds=1) is True

    monkeypatch.setattr(lifecycle.threading, "Thread", lambda **kwargs: NewThread(**kwargs))
    old_thread = OldThread()
    monkeypatch.setattr(lifecycle, "_thread", old_thread)
    lifecycle._stop_event.clear()

    assert lifecycle.stop_cron_scheduler(timeout=0.1) is True
    assert lifecycle._thread is created[0]
    assert lifecycle.is_cron_scheduler_running() is True


def test_is_cron_scheduler_running_reflects_thread_alive_state(monkeypatch):
    import agent_core.cron_lifecycle as lifecycle

    class FakeThread:
        def __init__(self, alive):
            self.alive = alive

        def is_alive(self):
            return self.alive

    monkeypatch.setattr(lifecycle, "_thread", None)
    assert lifecycle.is_cron_scheduler_running() is False

    monkeypatch.setattr(lifecycle, "_thread", FakeThread(False))
    assert lifecycle.is_cron_scheduler_running() is False

    monkeypatch.setattr(lifecycle, "_thread", FakeThread(True))
    assert lifecycle.is_cron_scheduler_running() is True


def test_ticker_loop_calls_tick_and_continues_after_tick_exception(monkeypatch):
    import agent_core.cron_lifecycle as lifecycle

    calls = []

    class FakeStopEvent:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits >= 2

        def wait(self, interval):
            calls.append(("wait", interval))
            self.waits += 1

    def fake_tick():
        calls.append(("tick", None))
        if calls.count(("tick", None)) == 1:
            raise RuntimeError("tick failed")

    monkeypatch.setattr(lifecycle, "_stop_event", FakeStopEvent())
    monkeypatch.setattr(lifecycle.cron.scheduler, "tick", fake_tick)

    lifecycle._ticker_loop(3)

    assert calls == [
        ("tick", None),
        ("wait", 3),
        ("tick", None),
        ("wait", 3),
    ]


def test_start_cron_scheduler_clamps_interval_below_one(monkeypatch):
    import agent_core.cron_lifecycle as lifecycle

    created = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

        def start(self):
            return None

        def is_alive(self):
            return True

    monkeypatch.setattr(lifecycle.threading, "Thread", lambda **kwargs: FakeThread(**kwargs))

    assert lifecycle.start_cron_scheduler(interval_seconds=0.01) is True
    assert created[0].kwargs["args"] == (1.0,)

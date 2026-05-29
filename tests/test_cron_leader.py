from __future__ import annotations


def test_leader_lease_acquire_renew_and_release(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.leader import SchedulerLeaderLease
    from cron.state_store import StateStore

    store = StateStore()
    lease = SchedulerLeaderLease(store=store, name="scheduler", lease_seconds=30)

    first = lease.try_acquire_or_renew(
        owner_id="host:1:first",
        pid=1,
        hostname="host",
        now_text="2026-05-29T10:00:00+00:00",
    )
    assert first.is_leader is True
    assert first.owner_id == "host:1:first"
    assert first.expires_at == "2026-05-29T10:00:30+00:00"

    renewed = lease.try_acquire_or_renew(
        owner_id="host:1:first",
        pid=1,
        hostname="host",
        now_text="2026-05-29T10:00:10+00:00",
    )
    assert renewed.is_leader is True
    assert renewed.owner_id == "host:1:first"
    assert renewed.expires_at == "2026-05-29T10:00:40+00:00"

    assert lease.release("host:2:other") is False
    assert lease.current().owner_id == "host:1:first"
    assert lease.release("host:1:first") is True
    assert lease.current() is None


def test_leader_lease_blocks_follower_until_expired(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.leader import SchedulerLeaderLease
    from cron.state_store import StateStore

    lease = SchedulerLeaderLease(store=StateStore(), name="scheduler", lease_seconds=30)

    lease.try_acquire_or_renew(
        owner_id="host:1:first",
        pid=1,
        hostname="host",
        now_text="2026-05-29T10:00:00+00:00",
    )
    follower = lease.try_acquire_or_renew(
        owner_id="host:2:second",
        pid=2,
        hostname="host",
        now_text="2026-05-29T10:00:10+00:00",
    )
    assert follower.is_leader is False
    assert follower.owner_id == "host:1:first"

    stolen = lease.try_acquire_or_renew(
        owner_id="host:2:second",
        pid=2,
        hostname="host",
        now_text="2026-05-29T10:00:31+00:00",
    )
    assert stolen.is_leader is True
    assert stolen.owner_id == "host:2:second"

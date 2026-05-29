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


def test_leader_lease_steals_malformed_existing_expiry(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.leader import SchedulerLeaderLease
    from cron.state_store import StateStore

    store = StateStore()
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_leases (
                name, owner_id, pid, hostname, acquired_at, heartbeat_at, expires_at
            ) VALUES (
                'scheduler', 'host:1:first', 1, 'host',
                '2026-05-29T10:00:00+00:00',
                '2026-05-29T10:00:00+00:00',
                'not-a-time'
            )
            """
        )

    lease = SchedulerLeaderLease(store=store, name="scheduler", lease_seconds=30)
    stolen = lease.try_acquire_or_renew(
        owner_id="host:2:second",
        pid=2,
        hostname="host",
        now_text="2026-05-29T10:00:10+00:00",
    )

    assert stolen.is_leader is True
    assert stolen.owner_id == "host:2:second"


def test_leader_lease_compares_existing_expiry_by_instant(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.leader import SchedulerLeaderLease
    from cron.state_store import StateStore

    store = StateStore()
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_leases (
                name, owner_id, pid, hostname, acquired_at, heartbeat_at, expires_at
            ) VALUES (
                'scheduler', 'host:1:first', 1, 'host',
                '2026-05-29T09:59:00+02:00',
                '2026-05-29T09:59:00+02:00',
                '2026-05-29T10:00:30+02:00'
            )
            """
        )

    lease = SchedulerLeaderLease(store=store, name="scheduler", lease_seconds=30)
    stolen = lease.try_acquire_or_renew(
        owner_id="host:2:second",
        pid=2,
        hostname="host",
        now_text="2026-05-29T08:00:31+00:00",
    )

    assert stolen.is_leader is True
    assert stolen.owner_id == "host:2:second"


def test_leader_lease_malformed_now_does_not_steal_valid_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    store.try_acquire_scheduler_lease(
        "scheduler",
        "host:1:first",
        1,
        "host",
        "2026-05-29T10:00:00+00:00",
        "2026-05-29T10:00:30+00:00",
    )

    existing = store.try_acquire_scheduler_lease(
        "scheduler",
        "host:2:second",
        2,
        "host",
        "not-a-time",
        "2026-05-29T10:00:30+00:00",
    )

    assert existing["owner_id"] == "host:1:first"

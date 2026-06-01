from __future__ import annotations


def test_unsupported_platform_install_returns_exit_code_2(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_manager import install_service

    monkeypatch.setattr("cron.service_manager.detect_platform", lambda: None)

    result = install_service(interval_seconds=60, lease_seconds=180, force=False)

    assert result.exit_code == 2
    assert "user-level cron service is not supported" in result.message
    assert "agent cron serve" in result.message


def test_missing_linux_adapter_install_returns_exit_code_2(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_manager import install_service, platform_module
    from cron.service_platforms.systemd_user import SystemdUserCronService

    monkeypatch.setattr(platform_module, "system", lambda: "Linux")
    monkeypatch.setattr(SystemdUserCronService, "supported", lambda self: False)

    result = install_service(interval_seconds=60, lease_seconds=180, force=False)

    assert result.exit_code == 2
    assert "user-level cron service is not supported" in result.message
    assert "agent cron serve" in result.message


def test_service_status_combines_platform_and_fresh_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import write_service_status
    from cron.service_manager import ServiceRuntimeStatus, compose_service_status

    write_service_status(
        {
            "process_state": "running",
            "pid": 123,
            "leader_state": "leader",
            "last_heartbeat_at": "2026-06-01T10:00:00+00:00",
            "last_tick": {
                "due": 1,
                "ran": 1,
                "succeeded": 1,
                "failed": 0,
                "skipped": 0,
            },
            "last_error": None,
        }
    )

    status = compose_service_status(
        ServiceRuntimeStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
        ),
        now_text="2026-06-01T10:01:00+00:00",
    )

    assert status.platform == "systemd-user"
    assert status.installed is True
    assert status.active is True
    assert status.pid == 123
    assert status.status_pid == 123
    assert status.heartbeat_fresh is True
    assert status.process_state == "running"
    assert status.leader_state == "leader"
    assert status.last_tick == {
        "due": 1,
        "ran": 1,
        "succeeded": 1,
        "failed": 0,
        "skipped": 0,
    }


def test_service_status_marks_stale_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.service_state import write_service_status
    from cron.service_manager import ServiceRuntimeStatus, compose_service_status

    write_service_status(
        {
            "process_state": "running",
            "pid": 123,
            "leader_state": "leader",
            "last_heartbeat_at": "2026-06-01T09:00:00+00:00",
        }
    )

    status = compose_service_status(
        ServiceRuntimeStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=123,
            detail="active",
        ),
        now_text="2026-06-01T10:01:00+00:00",
    )

    assert status.heartbeat_fresh is False
    assert status.last_heartbeat_at == "2026-06-01T09:00:00+00:00"

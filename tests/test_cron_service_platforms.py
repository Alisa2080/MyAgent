from __future__ import annotations


def test_systemd_install_writes_unit_and_enables(monkeypatch, tmp_path):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import SystemdUserCronService

    calls = []

    def fake_run(args):
        calls.append(args)
        return 0, "ok", ""

    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: tmp_path / "langchain-agent-cron.service",
    )
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "cron-home"))

    service = SystemdUserCronService(command_runner=fake_run)
    result = service.install(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            force=False,
            python_executable="/usr/bin/python3",
        )
    )

    unit = (tmp_path / "langchain-agent-cron.service").read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert (
        "ExecStart=/usr/bin/python3 -m agent_cli.main cron serve --interval 30 "
        "--lease-seconds 90"
    ) in unit
    assert 'Environment="AGENT_CRON_HOME=' in unit
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "langchain-agent-cron.service"],
    ]


def test_systemd_install_existing_requires_force(monkeypatch, tmp_path):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import SystemdUserCronService

    unit_path = tmp_path / "langchain-agent-cron.service"
    unit_path.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: unit_path,
    )

    result = SystemdUserCronService(command_runner=lambda args: (0, "", "")).install(
        ServiceInstallConfig(interval_seconds=60, lease_seconds=180, force=False)
    )

    assert result.exit_code == 2
    assert "--force" in result.message


def test_systemd_status_parses_active_enabled(monkeypatch):
    from cron.service_platforms.systemd_user import SystemdUserCronService

    def fake_run(args):
        if args[:3] == ["systemctl", "--user", "is-enabled"]:
            return 0, "enabled\n", ""
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return 0, "active\n", ""
        if args[:3] == ["systemctl", "--user", "show"]:
            return 0, "MainPID=456\nExecMainStatus=0\nResult=success\n", ""
        raise AssertionError(args)

    status = SystemdUserCronService(command_runner=fake_run).status()

    assert status.platform == "systemd-user"
    assert status.supported is True
    assert status.installed is True
    assert status.enabled is True
    assert status.active is True
    assert status.pid == 456


def test_systemd_logs_use_journalctl():
    from cron.service_platforms.systemd_user import SystemdUserCronService

    calls = []

    def fake_run(args):
        calls.append(args)
        return 0, "line1\nline2\n", ""

    result = SystemdUserCronService(command_runner=fake_run).logs(lines=2)

    assert result.exit_code == 0
    assert result.message == "line1\nline2"
    assert calls == [
        [
            "journalctl",
            "--user",
            "-u",
            "langchain-agent-cron.service",
            "-n",
            "2",
            "--no-pager",
        ]
    ]

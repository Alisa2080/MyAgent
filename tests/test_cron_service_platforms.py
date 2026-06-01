from __future__ import annotations


def test_systemd_supported_requires_user_systemd(monkeypatch):
    from cron.service_platforms.systemd_user import SystemdUserCronService

    calls = []
    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.shutil.which",
        lambda name: "/usr/bin/systemctl",
    )

    def fake_run(args):
        calls.append(args)
        return 0, "PATH=/usr/bin\n", ""

    service = SystemdUserCronService(command_runner=fake_run)

    assert service.supported() is True
    assert calls == [["systemctl", "--user", "show-environment"]]


def test_systemd_supported_rejects_missing_user_systemd(monkeypatch):
    from cron.service_platforms.systemd_user import SystemdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.shutil.which",
        lambda name: "/usr/bin/systemctl",
    )

    service = SystemdUserCronService(command_runner=lambda args: (1, "", "no bus"))

    assert service.supported() is False


def test_systemd_supported_rejects_user_systemd_probe_exception(monkeypatch):
    from cron.service_platforms.systemd_user import SystemdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.shutil.which",
        lambda name: "/usr/bin/systemctl",
    )

    def fake_run(args):
        raise OSError("no user bus")

    assert SystemdUserCronService(command_runner=fake_run).supported() is False


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


def test_systemd_unit_escapes_execstart_and_environment(monkeypatch):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import render_unit

    monkeypatch.setenv("AGENT_CLI_HOME", r'/tmp/home "quoted" \path 100%')
    monkeypatch.setenv("AGENT_CRON_HOME", "/tmp/cron\nhome")
    monkeypatch.setenv("AGENT_RUNTIME_PROFILE", "prod%blue")

    unit = render_unit(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            python_executable="/opt/My Python/py$prod/bin/python%3",
        )
    )

    assert (
        'ExecStart="/opt/My Python/py$$prod/bin/python%%3" -m agent_cli.main cron serve '
        "--interval 30 --lease-seconds 90"
    ) in unit
    assert (
        r'Environment="AGENT_CLI_HOME=/tmp/home \"quoted\" \\path 100%%"'
        in unit
    )
    assert 'Environment="AGENT_RUNTIME_PROFILE=prod%%blue"' in unit
    assert "AGENT_CRON_HOME" not in unit


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
            return (
                0,
                "LoadState=loaded\n"
                "UnitFileState=enabled\n"
                "MainPID=456\n"
                "ExecMainStatus=0\n"
                "Result=success\n",
                "",
            )
        raise AssertionError(args)

    status = SystemdUserCronService(command_runner=fake_run).status()

    assert status.platform == "systemd-user"
    assert status.supported is True
    assert status.installed is True
    assert status.enabled is True
    assert status.active is True
    assert status.pid == 456


def test_systemd_status_parses_inactive_installed_unit():
    from cron.service_platforms.systemd_user import SystemdUserCronService

    def fake_run(args):
        if args[:3] == ["systemctl", "--user", "is-enabled"]:
            return 1, "disabled\n", ""
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return 3, "inactive\n", ""
        if args[:3] == ["systemctl", "--user", "show"]:
            return (
                0,
                "LoadState=loaded\n"
                "UnitFileState=disabled\n"
                "MainPID=0\n"
                "ExecMainStatus=0\n"
                "Result=success\n",
                "",
            )
        raise AssertionError(args)

    status = SystemdUserCronService(command_runner=fake_run).status()

    assert status.installed is True
    assert status.enabled is False
    assert status.active is False
    assert status.pid is None
    assert status.detail == "inactive"


def test_systemd_status_parses_not_found_unit_as_not_installed():
    from cron.service_platforms.systemd_user import SystemdUserCronService

    def fake_run(args):
        if args[:3] == ["systemctl", "--user", "is-enabled"]:
            return 1, "", "Failed to get unit file state"
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return 3, "inactive\n", ""
        if args[:3] == ["systemctl", "--user", "show"]:
            return (
                0,
                "LoadState=not-found\n"
                "UnitFileState=\n"
                "MainPID=0\n"
                "ExecMainStatus=0\n"
                "Result=success\n",
                "",
            )
        raise AssertionError(args)

    status = SystemdUserCronService(command_runner=fake_run).status()

    assert status.installed is False
    assert status.enabled is False
    assert status.active is False
    assert status.pid is None


def test_systemd_status_parses_failed_unit():
    from cron.service_platforms.systemd_user import SystemdUserCronService

    def fake_run(args):
        if args[:3] == ["systemctl", "--user", "is-enabled"]:
            return 0, "enabled\n", ""
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return 3, "failed\n", ""
        if args[:3] == ["systemctl", "--user", "show"]:
            return (
                0,
                "LoadState=loaded\n"
                "UnitFileState=enabled\n"
                "MainPID=0\n"
                "ExecMainStatus=1\n"
                "Result=exit-code\n",
                "",
            )
        raise AssertionError(args)

    status = SystemdUserCronService(command_runner=fake_run).status()

    assert status.installed is True
    assert status.enabled is True
    assert status.active is False
    assert status.pid is None
    assert status.detail == "failed; result=exit-code; exit_status=1"


def test_systemd_install_removes_new_unit_when_daemon_reload_fails(monkeypatch, tmp_path):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import SystemdUserCronService

    calls = []
    unit_path = tmp_path / "langchain-agent-cron.service"
    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: unit_path,
    )

    def fake_run(args):
        calls.append(args)
        if args == ["systemctl", "--user", "daemon-reload"] and len(calls) == 1:
            return 1, "", "reload failed"
        return 0, "", ""

    result = SystemdUserCronService(command_runner=fake_run).install(
        ServiceInstallConfig(interval_seconds=60, lease_seconds=180, force=False)
    )

    assert result.exit_code == 1
    assert "reload failed" in result.message
    assert not unit_path.exists()
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_systemd_install_restores_existing_unit_when_enable_fails(monkeypatch, tmp_path):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import SystemdUserCronService

    calls = []
    unit_path = tmp_path / "langchain-agent-cron.service"
    unit_path.write_text("previous unit", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: unit_path,
    )

    def fake_run(args):
        calls.append(args)
        if args == ["systemctl", "--user", "enable", "langchain-agent-cron.service"]:
            return 1, "", "enable failed"
        return 0, "", ""

    result = SystemdUserCronService(command_runner=fake_run).install(
        ServiceInstallConfig(interval_seconds=60, lease_seconds=180, force=True)
    )

    assert result.exit_code == 1
    assert "enable failed" in result.message
    assert unit_path.read_text(encoding="utf-8") == "previous unit"
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "langchain-agent-cron.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_systemd_install_cleans_temp_file_when_replace_fails(monkeypatch, tmp_path):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import SystemdUserCronService

    unit_path = tmp_path / "langchain-agent-cron.service"
    tmp_path_for_unit = tmp_path / ".langchain-agent-cron.service.tmp"
    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: unit_path,
    )

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("cron.service_platforms.systemd_user.atomic_replace", fail_replace)

    result = SystemdUserCronService(command_runner=lambda args: (0, "", "")).install(
        ServiceInstallConfig(interval_seconds=60, lease_seconds=180, force=False)
    )

    assert result.exit_code == 2
    assert "replace failed" in result.message
    assert not unit_path.exists()
    assert not tmp_path_for_unit.exists()


def test_systemd_uninstall_removes_unit_after_disable_failure(monkeypatch, tmp_path):
    from cron.service_platforms.systemd_user import SystemdUserCronService

    calls = []
    unit_path = tmp_path / "langchain-agent-cron.service"
    unit_path.write_text("unit", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: unit_path,
    )

    def fake_run(args):
        calls.append(args)
        if args == ["systemctl", "--user", "disable", "langchain-agent-cron.service"]:
            return 1, "", "permission denied"
        return 0, "", ""

    result = SystemdUserCronService(command_runner=fake_run).uninstall()

    assert result.exit_code == 1
    assert "permission denied" in result.message
    assert not unit_path.exists()
    assert calls == [
        ["systemctl", "--user", "disable", "langchain-agent-cron.service"],
        ["systemctl", "--user", "stop", "langchain-agent-cron.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_systemd_uninstall_removes_unit_after_stop_failure(monkeypatch, tmp_path):
    from cron.service_platforms.systemd_user import SystemdUserCronService

    calls = []
    unit_path = tmp_path / "langchain-agent-cron.service"
    unit_path.write_text("unit", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: unit_path,
    )

    def fake_run(args):
        calls.append(args)
        if args == ["systemctl", "--user", "stop", "langchain-agent-cron.service"]:
            return 1, "", "stop failed"
        return 0, "", ""

    result = SystemdUserCronService(command_runner=fake_run).uninstall()

    assert result.exit_code == 1
    assert "stop failed" in result.message
    assert not unit_path.exists()
    assert calls == [
        ["systemctl", "--user", "disable", "langchain-agent-cron.service"],
        ["systemctl", "--user", "stop", "langchain-agent-cron.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]


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

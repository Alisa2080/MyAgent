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


def test_systemd_install_auto_injects_subprocess_for_prod(monkeypatch, tmp_path):
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
    monkeypatch.delenv("AGENT_CRON_RUNNER_MODE", raising=False)

    service = SystemdUserCronService(command_runner=fake_run)
    result = service.install(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            force=False,
            python_executable="/usr/bin/python3",
            environment_overrides={"AGENT_CRON_RUNNER_MODE": "subprocess"},
        )
    )

    unit = (tmp_path / "langchain-agent-cron.service").read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert 'Environment="AGENT_CRON_RUNNER_MODE=subprocess"' in unit


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


def test_systemd_start_stop_restart_require_installed_unit(monkeypatch, tmp_path):
    from cron.service_platforms.systemd_user import SystemdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.systemd_user.systemd_unit_path",
        lambda: tmp_path / "missing.service",
    )

    def fail_run(args):
        raise AssertionError(f"systemctl should not be called: {args}")

    service = SystemdUserCronService(command_runner=fail_run)

    for action in (service.start, service.stop, service.restart):
        result = action()
        assert result.exit_code == 2
        assert "agent cron service install" in result.message


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


def test_launchd_install_writes_plist(monkeypatch, tmp_path):
    import plistlib

    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    plist_path = tmp_path / "LaunchAgents" / f"{LABEL}.plist"
    stdout_path = tmp_path / "logs" / "cron.out.log"
    stderr_path = tmp_path / "logs" / "cron.err.log"
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_log_paths",
        lambda: (stdout_path, stderr_path),
    )

    result = LaunchdUserCronService(command_runner=lambda args: (0, "", "")).install(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            force=False,
            python_executable="/usr/bin/python3",
        )
    )

    payload = plistlib.loads(plist_path.read_bytes())
    assert result.exit_code == 0
    assert payload["Label"] == "ai.langchain.agent.cron"
    assert payload["RunAtLoad"] is True
    assert payload["ProgramArguments"] == [
        "/usr/bin/python3",
        "-m",
        "agent_cli.main",
        "cron",
        "serve",
        "--interval",
        "30",
        "--lease-seconds",
        "90",
    ]
    assert payload["StandardOutPath"] == str(stdout_path)
    assert payload["StandardErrorPath"] == str(stderr_path)
    assert stdout_path.parent.is_dir()
    assert "agent cron service start" in result.message


def test_launchd_install_auto_injects_subprocess_for_prod(monkeypatch, tmp_path):
    import plistlib

    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: tmp_path / "ai.langchain.agent.cron.plist",
    )
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_log_paths",
        lambda: (tmp_path / "out.log", tmp_path / "err.log"),
    )
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "cron-home"))
    monkeypatch.delenv("AGENT_CRON_RUNNER_MODE", raising=False)

    service = LaunchdUserCronService(command_runner=lambda args: (0, "ok", ""))
    result = service.install(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            force=False,
            python_executable="/usr/bin/python3",
            environment_overrides={"AGENT_CRON_RUNNER_MODE": "subprocess"},
        )
    )

    payload = plistlib.loads(
        (tmp_path / "ai.langchain.agent.cron.plist").read_bytes()
    )
    assert result.exit_code == 0
    assert payload["EnvironmentVariables"]["AGENT_CRON_RUNNER_MODE"] == "subprocess"


def test_launchd_supported_probes_gui_domain(monkeypatch):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    calls = []
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.shutil.which",
        lambda name: "/bin/launchctl",
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    def fake_run(args):
        calls.append(args)
        return 0, "domain", ""

    assert LaunchdUserCronService(command_runner=fake_run).supported() is True
    assert calls == [["launchctl", "print", "gui/501"]]


def test_launchd_supported_rejects_missing_gui_domain(monkeypatch):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.shutil.which",
        lambda name: "/bin/launchctl",
    )

    service = LaunchdUserCronService(command_runner=lambda args: (113, "", "no gui"))

    assert service.supported() is False


def test_launchd_supported_rejects_probe_exception(monkeypatch):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.shutil.which",
        lambda name: "/bin/launchctl",
    )

    def fake_run(args):
        raise OSError("no bootstrap namespace")

    assert LaunchdUserCronService(command_runner=fake_run).supported() is False


def test_launchd_start_bootstraps_and_kickstarts(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    calls = []
    plist_path = tmp_path / f"{LABEL}.plist"
    plist_path.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    def fake_run(args):
        calls.append(args)
        if args[:2] == ["launchctl", "bootstrap"]:
            return 5, "", "service already loaded"
        if args == ["launchctl", "print", "gui/501/ai.langchain.agent.cron"]:
            return 0, "service info", ""
        return 0, "started", ""

    result = LaunchdUserCronService(command_runner=fake_run).start()

    assert result.exit_code == 0
    assert calls == [
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
        ["launchctl", "print", "gui/501/ai.langchain.agent.cron"],
        ["launchctl", "kickstart", "-k", "gui/501/ai.langchain.agent.cron"],
    ]


def test_launchd_start_rejects_non_benign_bootstrap_code_5(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    calls = []
    plist_path = tmp_path / f"{LABEL}.plist"
    plist_path.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    def fake_run(args):
        calls.append(args)
        if args[:2] == ["launchctl", "bootstrap"]:
            return 5, "", "permission denied"
        if args == ["launchctl", "print", "gui/501/ai.langchain.agent.cron"]:
            return 113, "", "not found"
        raise AssertionError(args)

    result = LaunchdUserCronService(command_runner=fake_run).start()

    assert result.exit_code == 1
    assert result.message == "permission denied"
    assert calls == [
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
        ["launchctl", "print", "gui/501/ai.langchain.agent.cron"],
    ]


def test_launchd_start_accepts_generic_code_5_when_service_printable(
    monkeypatch, tmp_path
):
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    calls = []
    plist_path = tmp_path / f"{LABEL}.plist"
    plist_path.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    def fake_run(args):
        calls.append(args)
        if args[:2] == ["launchctl", "bootstrap"]:
            return 5, "", "Input/output error"
        if args == ["launchctl", "print", "gui/501/ai.langchain.agent.cron"]:
            return 0, "service info", ""
        if args[:2] == ["launchctl", "kickstart"]:
            return 0, "started", ""
        raise AssertionError(args)

    result = LaunchdUserCronService(command_runner=fake_run).start()

    assert result.exit_code == 0
    assert calls == [
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
        ["launchctl", "print", "gui/501/ai.langchain.agent.cron"],
        ["launchctl", "kickstart", "-k", "gui/501/ai.langchain.agent.cron"],
    ]


def test_launchd_start_rejects_generic_code_5_when_service_not_printable(
    monkeypatch, tmp_path
):
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    calls = []
    plist_path = tmp_path / f"{LABEL}.plist"
    plist_path.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    def fake_run(args):
        calls.append(args)
        if args[:2] == ["launchctl", "bootstrap"]:
            return 5, "", "Input/output error"
        if args == ["launchctl", "print", "gui/501/ai.langchain.agent.cron"]:
            return 113, "", "not found"
        raise AssertionError(args)

    result = LaunchdUserCronService(command_runner=fake_run).start()

    assert result.exit_code == 1
    assert result.message == "Input/output error"
    assert calls == [
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
        ["launchctl", "print", "gui/501/ai.langchain.agent.cron"],
    ]


def test_launchd_start_requires_installed_plist(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: tmp_path / "missing.plist",
    )

    result = LaunchdUserCronService(command_runner=lambda args: (0, "", "")).start()

    assert result.exit_code == 2
    assert "agent cron service install" in result.message


def test_launchd_stop_and_restart_require_installed_plist(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: tmp_path / "missing.plist",
    )

    def fail_run(args):
        raise AssertionError(f"launchctl should not be called: {args}")

    service = LaunchdUserCronService(command_runner=fail_run)

    for action in (service.stop, service.restart):
        result = action()
        assert result.exit_code == 2
        assert "agent cron service install" in result.message


def test_launchd_stop_boots_out_plist_from_domain(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    calls = []
    plist_path = tmp_path / f"{LABEL}.plist"
    plist_path.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    def fake_run(args):
        calls.append(args)
        if args[:2] == ["launchctl", "bootout"]:
            return 5, "", "not loaded"
        if args == ["launchctl", "print", "gui/501/ai.langchain.agent.cron"]:
            return 113, "", "not found"
        raise AssertionError(args)

    result = LaunchdUserCronService(command_runner=fake_run).stop()

    assert result.exit_code == 0
    assert calls == [
        ["launchctl", "bootout", "gui/501", str(plist_path)],
        ["launchctl", "print", "gui/501/ai.langchain.agent.cron"],
    ]


def test_launchd_stop_rejects_non_benign_code_5(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    plist_path = tmp_path / f"{LABEL}.plist"
    plist_path.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    def fake_run(args):
        if args[:2] == ["launchctl", "bootout"]:
            return 5, "", "permission denied"
        if args == ["launchctl", "print", "gui/501/ai.langchain.agent.cron"]:
            return 0, "still loaded", ""
        raise AssertionError(args)

    result = LaunchdUserCronService(command_runner=fake_run).stop()

    assert result.exit_code == 1
    assert result.message == "permission denied"


def test_launchd_stop_accepts_generic_code_5_when_service_not_printable(
    monkeypatch, tmp_path
):
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    calls = []
    plist_path = tmp_path / f"{LABEL}.plist"
    plist_path.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    def fake_run(args):
        calls.append(args)
        if args[:2] == ["launchctl", "bootout"]:
            return 5, "", "Input/output error"
        if args == ["launchctl", "print", "gui/501/ai.langchain.agent.cron"]:
            return 113, "", "not found"
        raise AssertionError(args)

    result = LaunchdUserCronService(command_runner=fake_run).stop()

    assert result.exit_code == 0
    assert calls == [
        ["launchctl", "bootout", "gui/501", str(plist_path)],
        ["launchctl", "print", "gui/501/ai.langchain.agent.cron"],
    ]


def test_launchd_stop_rejects_generic_code_5_when_service_still_printable(
    monkeypatch, tmp_path
):
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    calls = []
    plist_path = tmp_path / f"{LABEL}.plist"
    plist_path.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    def fake_run(args):
        calls.append(args)
        if args[:2] == ["launchctl", "bootout"]:
            return 5, "", "Input/output error"
        if args == ["launchctl", "print", "gui/501/ai.langchain.agent.cron"]:
            return 0, "still loaded", ""
        raise AssertionError(args)

    result = LaunchdUserCronService(command_runner=fake_run).stop()

    assert result.exit_code == 1
    assert result.message == "Input/output error"
    assert calls == [
        ["launchctl", "bootout", "gui/501", str(plist_path)],
        ["launchctl", "print", "gui/501/ai.langchain.agent.cron"],
    ]


def test_launchd_status_reports_loaded_detail(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    plist_path = tmp_path / f"{LABEL}.plist"
    plist_path.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )
    monkeypatch.setattr("cron.service_platforms.launchd_user._uid", lambda: 501)

    status = LaunchdUserCronService(
        command_runner=lambda args: (0, "random first line\n    pid = 789\n", "")
    ).status()

    assert status.installed is True
    assert status.enabled is True
    assert status.active is True
    assert status.pid == 789
    assert status.detail == "loaded"


def test_launchd_status_printable_without_pid_is_not_active(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import (
        LABEL,
        LaunchdUserCronService,
    )

    plist_path = tmp_path / f"{LABEL}.plist"
    plist_path.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: plist_path,
    )

    status = LaunchdUserCronService(
        command_runner=lambda args: (0, "state = waiting\n", "")
    ).status()

    assert status.installed is True
    assert status.enabled is True
    assert status.active is False
    assert status.pid is None
    assert status.detail == "state = waiting"
    assert status.error is None


def test_launchd_status_reports_not_loaded_detail(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_plist_path",
        lambda: tmp_path / "missing.plist",
    )

    status = LaunchdUserCronService(
        command_runner=lambda args: (113, "", "Could not find service")
    ).status()

    assert status.installed is False
    assert status.enabled is False
    assert status.active is False
    assert status.pid is None
    assert status.detail == "not loaded"
    assert status.error == "Could not find service"


def test_launchd_logs_read_stdout_and_stderr(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    stdout_path = tmp_path / "cron.out.log"
    stderr_path = tmp_path / "cron.err.log"
    stdout_path.write_text("out1\nout2\nout3\n", encoding="utf-8")
    stderr_path.write_text("err1\nerr2\n", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_log_paths",
        lambda: (stdout_path, stderr_path),
    )

    result = LaunchdUserCronService(command_runner=lambda args: (0, "", "")).logs(
        lines=2
    )

    assert result.exit_code == 0
    assert result.message == (
        "==> stdout <==\n"
        "out2\n"
        "out3\n\n"
        "==> stderr <==\n"
        "err1\n"
        "err2"
    )


def test_launchd_logs_use_bounded_tail_without_read_text(monkeypatch, tmp_path):
    from pathlib import Path

    from cron.service_platforms.launchd_user import LaunchdUserCronService

    stdout_path = tmp_path / "cron.out.log"
    stderr_path = tmp_path / "cron.err.log"
    stdout_path.write_text("out1\nout2\nout3\n", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_log_paths",
        lambda: (stdout_path, stderr_path),
    )

    def fail_read_text(self, *args, **kwargs):
        raise AssertionError("logs should not read entire file")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    result = LaunchdUserCronService(command_runner=lambda args: (0, "", "")).logs(
        lines=2
    )

    assert result.exit_code == 0
    assert result.message == "==> stdout <==\nout2\nout3"


def test_launchd_logs_tail_large_single_line_from_bounded_suffix(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    stdout_path = tmp_path / "cron.out.log"
    stderr_path = tmp_path / "cron.err.log"
    stdout_path.write_text("a" * 20000 + "TAIL", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_log_paths",
        lambda: (stdout_path, stderr_path),
    )

    result = LaunchdUserCronService(command_runner=lambda args: (0, "", "")).logs(
        lines=1
    )

    assert result.exit_code == 0
    assert result.message == "==> stdout <==\n" + "a" * 8188 + "TAIL"


def test_launchd_logs_report_when_no_logs(monkeypatch, tmp_path):
    from cron.service_platforms.launchd_user import LaunchdUserCronService

    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_log_paths",
        lambda: (tmp_path / "missing.out.log", tmp_path / "missing.err.log"),
    )

    result = LaunchdUserCronService(command_runner=lambda args: (0, "", "")).logs()

    assert result.exit_code == 0
    assert result.message == "No cron service logs yet."


def test_systemd_unit_includes_workdir_pythonpath_and_env_file(tmp_path):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import render_unit

    unit = render_unit(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            python_executable="/usr/bin/python3",
            working_directory=tmp_path / "repo",
            pythonpath=f"{tmp_path / 'repo'}:/extra",
            service_env_file=tmp_path / "home" / "cron" / "service.env",
            environment_overrides={"FEISHU_APP_SECRET": "must-not-leak"},
        )
    )

    assert f"WorkingDirectory={tmp_path / 'repo'}" in unit
    assert f'Environment="PYTHONPATH={tmp_path / "repo"}:/extra"' in unit
    assert f"EnvironmentFile=-{tmp_path / 'home' / 'cron' / 'service.env'}" in unit
    assert "FEISHU_APP_SECRET" not in unit


def test_systemd_unit_quotes_workdir_and_env_file_with_spaces(tmp_path):
    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.systemd_user import render_unit

    workdir = tmp_path / "repo with spaces $prod"
    env_file = tmp_path / "home with spaces $prod" / "cron" / "service.env"

    unit = render_unit(
        ServiceInstallConfig(
            interval_seconds=30,
            lease_seconds=90,
            working_directory=workdir,
            pythonpath=str(workdir),
            service_env_file=env_file,
        )
    )

    assert f'WorkingDirectory="{workdir}"' in unit
    assert f'EnvironmentFile=-"{env_file}"' in unit
    assert "$$prod" not in unit


def test_launchd_plist_includes_workdir_pythonpath_and_service_env(monkeypatch, tmp_path):
    import plistlib

    from cron.service_manager import ServiceInstallConfig
    from cron.service_platforms.launchd_user import render_plist

    env_path = tmp_path / "home" / "cron" / "service.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("FEISHU_APP_ID=cli_123\nFEISHU_APP_SECRET=secret\n", encoding="utf-8")
    monkeypatch.setattr(
        "cron.service_platforms.launchd_user.launchd_log_paths",
        lambda: (tmp_path / "out.log", tmp_path / "err.log"),
    )

    plist = plistlib.loads(
        render_plist(
            ServiceInstallConfig(
                interval_seconds=30,
                lease_seconds=90,
                working_directory=tmp_path / "repo",
                pythonpath=f"{tmp_path / 'repo'}:/extra",
                service_env_file=env_path,
            )
        )
    )

    assert plist["WorkingDirectory"] == str(tmp_path / "repo")
    env = plist["EnvironmentVariables"]
    assert env["PYTHONPATH"] == f"{tmp_path / 'repo'}:/extra"
    assert env["FEISHU_APP_ID"] == "cli_123"
    assert env["FEISHU_APP_SECRET"] == "secret"

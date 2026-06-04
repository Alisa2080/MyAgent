from __future__ import annotations

import pytest


def test_gateway_service_env_commands(monkeypatch, tmp_path):
    from gateway import service_manager

    env_file = tmp_path / "service.env"
    monkeypatch.setattr(service_manager, "get_service_env_file", lambda: env_file)

    set_result = service_manager.service_env_set("FEISHU_APP_ID", "cli_x")
    list_result = service_manager.service_env_list()
    unset_result = service_manager.service_env_unset("FEISHU_APP_ID")

    assert set_result.exit_code == 0
    assert "FEISHU_APP_ID" in list_result.message
    assert unset_result.exit_code == 0


def test_gateway_service_env_invalid_key_returns_error(monkeypatch, tmp_path):
    from gateway import service_manager

    env_file = tmp_path / "service.env"
    monkeypatch.setattr(service_manager, "get_service_env_file", lambda: env_file)

    result = service_manager.service_env_set("BAD-KEY", "x")

    assert result.exit_code == 2
    assert "invalid service env key" in result.message


def test_gateway_service_env_set_mentions_launchd_reinstall(monkeypatch, tmp_path):
    from gateway import service_manager

    env_file = tmp_path / "service.env"
    monkeypatch.setattr(service_manager, "get_service_env_file", lambda: env_file)

    result = service_manager.service_env_set("FEISHU_APP_ID", "cli_x")

    assert "install --force" in result.message


def test_gateway_service_install_unsupported_platform(monkeypatch):
    from gateway import service_manager

    monkeypatch.setattr(service_manager, "detect_platform", lambda: "unsupported")

    result = service_manager.install_service(transport="feishu-ws", force=False)

    assert result.exit_code == 2
    assert "unsupported" in result.message


def test_gateway_service_install_systemd_writes_unit(monkeypatch, tmp_path):
    from gateway import service_manager

    monkeypatch.setattr(service_manager, "detect_platform", lambda: "systemd-user")
    monkeypatch.setattr(service_manager, "systemd_unit_dir", lambda: tmp_path)
    commands = []
    monkeypatch.setattr(service_manager, "run_command", lambda args: commands.append(args) or (0, ""))

    result = service_manager.install_service(transport="feishu-ws", force=True)

    assert result.exit_code == 0
    assert (tmp_path / "agent-gateway.service").exists()
    assert any("daemon-reload" in args for args in commands)
    assert ["systemctl", "--user", "enable", "agent-gateway.service"] in commands


def test_gateway_detect_platform_requires_supported_systemd(monkeypatch):
    from gateway import service_manager

    monkeypatch.setattr(service_manager.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service_manager, "run_command", lambda args: (1, "no user bus"))

    assert service_manager.detect_platform() == "unsupported"


def test_gateway_service_install_systemd_rolls_back_on_enable_failure(monkeypatch, tmp_path):
    from gateway import service_manager

    existing = tmp_path / "agent-gateway.service"
    existing.write_text("previous", encoding="utf-8")
    commands = []

    def fake_run(args):
        commands.append(args)
        if args == ["systemctl", "--user", "enable", "agent-gateway.service"]:
            return 1, "enable failed"
        return 0, ""

    monkeypatch.setattr(service_manager, "detect_platform", lambda: "systemd-user")
    monkeypatch.setattr(service_manager, "systemd_unit_dir", lambda: tmp_path)
    monkeypatch.setattr(service_manager, "run_command", fake_run)

    result = service_manager.install_service(transport="feishu-ws", force=True)

    assert result.exit_code == 2
    assert "enable failed" in result.message
    assert existing.read_text(encoding="utf-8") == "previous"
    assert commands[-1] == ["systemctl", "--user", "daemon-reload"]


def test_gateway_run_command_times_out(monkeypatch):
    import subprocess

    from gateway import service_manager

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, timeout=15)

    monkeypatch.setattr(service_manager.subprocess, "run", fake_run)

    code, output = service_manager.run_command(["systemctl", "--user", "status", "agent-gateway.service"])

    assert code == 1
    assert "timed out" in output
    assert calls[0][1]["timeout"] == 15


def test_gateway_service_install_launchd_writes_plist(monkeypatch, tmp_path):
    from gateway import service_manager

    monkeypatch.setattr(service_manager, "detect_platform", lambda: "launchd-user")
    monkeypatch.setattr(service_manager, "launchd_plist_path", lambda: tmp_path / "com.agent.gateway.plist")
    commands = []
    monkeypatch.setattr(service_manager, "run_command", lambda args: commands.append(args) or (0, ""))

    result = service_manager.install_service(transport="feishu-ws", force=True)

    assert result.exit_code == 0
    assert (tmp_path / "com.agent.gateway.plist").exists()


def test_gateway_service_start_dispatches_systemd_command(monkeypatch, tmp_path):
    from gateway import service_manager

    monkeypatch.setattr(service_manager, "detect_platform", lambda: "systemd-user")
    monkeypatch.setattr(service_manager, "systemd_unit_dir", lambda: tmp_path)
    (tmp_path / "agent-gateway.service").write_text("[Service]\n", encoding="utf-8")
    commands = []
    monkeypatch.setattr(service_manager, "run_command", lambda args: commands.append(args) or (0, "started\n"))

    result = service_manager.start_service()

    assert result.exit_code == 0
    assert commands == [["systemctl", "--user", "start", "agent-gateway.service"]]


def test_gateway_service_start_reports_missing_install(monkeypatch, tmp_path):
    from gateway import service_manager

    monkeypatch.setattr(service_manager, "detect_platform", lambda: "systemd-user")
    monkeypatch.setattr(service_manager, "systemd_unit_dir", lambda: tmp_path)

    result = service_manager.start_service()

    assert result.exit_code == 2
    assert "not installed" in result.message


def test_gateway_service_logs_dispatches_launchd_tail(monkeypatch, tmp_path):
    from gateway import service_manager

    plist = tmp_path / "com.agent.gateway.plist"
    plist.write_bytes(b"plist")
    monkeypatch.setattr(service_manager, "detect_platform", lambda: "launchd-user")
    monkeypatch.setattr(service_manager, "launchd_plist_path", lambda: plist)
    commands = []
    monkeypatch.setattr(service_manager, "run_command", lambda args: commands.append(args) or (0, "log line\n"))

    result = service_manager.service_logs(lines=20)

    assert result.exit_code == 0
    assert commands == [["tail", "-n", "20", str(tmp_path / "agent-gateway.out.log"), str(tmp_path / "agent-gateway.err.log")]]


def test_gateway_service_start_launchd_continues_when_already_loaded(monkeypatch, tmp_path):
    from gateway import service_manager

    plist = tmp_path / "com.agent.gateway.plist"
    plist.write_bytes(b"plist")
    monkeypatch.setattr(service_manager, "detect_platform", lambda: "launchd-user")
    monkeypatch.setattr(service_manager, "launchd_plist_path", lambda: plist)
    commands = []

    def fake_run(args):
        commands.append(args)
        if args[:2] == ["launchctl", "bootstrap"]:
            return 5, "already loaded"
        if args[:2] == ["launchctl", "print"]:
            return 0, "loaded"
        return 0, "started"

    monkeypatch.setattr(service_manager, "run_command", fake_run)

    result = service_manager.start_service()

    assert result.exit_code == 0
    assert commands == [
        ["launchctl", "bootstrap", f"gui/{service_manager._uid()}", str(plist)],
        ["launchctl", "print", f"gui/{service_manager._uid()}/com.agent.gateway"],
        ["launchctl", "kickstart", "-k", f"gui/{service_manager._uid()}/com.agent.gateway"],
    ]

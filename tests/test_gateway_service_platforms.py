from __future__ import annotations


def test_systemd_user_unit_contains_feishu_ws(tmp_path):
    from gateway.service_context import GatewayServiceRuntimeContext
    from gateway.service_platforms.systemd_user import render_unit

    context = GatewayServiceRuntimeContext(
        project_root=tmp_path,
        python_executable="/opt/python/bin/python",
        service_env_file=tmp_path / "gateway" / "service.env",
    )

    unit = render_unit(context, transport="feishu-ws")

    assert "ExecStart=/opt/python/bin/python -m agent_cli.main gateway feishu-ws" in unit
    assert f"WorkingDirectory={tmp_path}" in unit
    assert f"EnvironmentFile=-{tmp_path / 'gateway' / 'service.env'}" in unit
    assert f"PYTHONPATH={tmp_path}" in unit


def test_systemd_user_unit_quotes_paths_with_spaces():
    from pathlib import Path

    from gateway.service_context import GatewayServiceRuntimeContext
    from gateway.service_platforms.systemd_user import render_unit

    context = GatewayServiceRuntimeContext(
        project_root=Path("/tmp/project root"),
        python_executable="/opt/python env/bin/python",
        service_env_file=Path("/tmp/project root/gateway/service.env"),
    )

    unit = render_unit(context, transport="feishu-ws")

    assert 'WorkingDirectory="/tmp/project root"' in unit
    assert 'EnvironmentFile=-"/tmp/project root/gateway/service.env"' in unit
    assert 'ExecStart="/opt/python env/bin/python" -m agent_cli.main gateway feishu-ws' in unit


def test_launchd_plist_contains_feishu_ws(tmp_path):
    from gateway.service_context import GatewayServiceRuntimeContext
    from gateway.service_platforms.launchd_user import render_plist

    context = GatewayServiceRuntimeContext(
        project_root=tmp_path,
        python_executable="/opt/python/bin/python",
        service_env_file=tmp_path / "gateway" / "service.env",
    )

    plist = render_plist(context, transport="feishu-ws", service_env={"FEISHU_APP_ID": "cli_x"})

    assert b"com.agent.gateway" in plist
    assert b"feishu-ws" in plist
    assert str(tmp_path).encode() in plist
    assert b"FEISHU_APP_ID" in plist


def test_launchd_plist_runs_as_background_service(tmp_path):
    import plistlib

    from gateway.service_context import GatewayServiceRuntimeContext
    from gateway.service_platforms.launchd_user import render_plist

    context = GatewayServiceRuntimeContext(
        project_root=tmp_path,
        python_executable="/opt/python/bin/python",
        service_env_file=tmp_path / "gateway" / "service.env",
    )

    payload = plistlib.loads(render_plist(context, transport="feishu-ws"))

    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}

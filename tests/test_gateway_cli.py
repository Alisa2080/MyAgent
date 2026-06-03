from __future__ import annotations


def test_gateway_status_reports_not_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_cli.command_handlers.gateway import gateway_status

    exit_code = gateway_status(home=tmp_path)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Gateway service is not running" in output


def test_gateway_status_reports_running(tmp_path, capsys):
    from agent_cli.command_handlers.gateway import gateway_status
    from gateway.service_state import write_gateway_status

    write_gateway_status(tmp_path, {"process_state": "running", "platforms": ["feishu"]})

    exit_code = gateway_status(home=tmp_path)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Gateway service is running" in output
    assert "feishu" in output


def test_feishu_gateway_doctor_reports_missing_env():
    from agent_cli.doctor import check_feishu_gateway_config

    errors = check_feishu_gateway_config({})

    assert errors == [
        "missing Feishu gateway environment variables: FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CALLBACK_TOKEN"
    ]
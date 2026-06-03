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


def test_gateway_status_treats_stale_running_status_as_not_running(tmp_path, capsys):
    import json
    from datetime import datetime, timedelta, timezone

    from agent_cli.command_handlers.gateway import gateway_status
    from gateway.service_state import gateway_status_path, write_gateway_status

    write_gateway_status(tmp_path, {"process_state": "running", "platforms": ["feishu"]})
    path = gateway_status_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = gateway_status(home=tmp_path)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Gateway service is not running" in output


def test_gateway_serve_runs_callback_server_and_clears_status(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    import agent_cli.command_handlers.gateway as gateway_handler
    from gateway.service_state import read_gateway_status

    calls = []

    def fake_default_home():
        return tmp_path

    def fake_serve(app, *, host, port):
        calls.append((app, host, port))

    monkeypatch.setattr(gateway_handler, "_default_home", fake_default_home)
    monkeypatch.setattr(gateway_handler, "serve_callback_http", fake_serve)

    exit_code = gateway_handler.gateway_serve(SimpleNamespace(host="0.0.0.0", port=9999))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert calls and calls[0][1:] == ("0.0.0.0", 9999)
    assert "Gateway service listening on 0.0.0.0:9999" in output
    assert read_gateway_status(tmp_path)["process_state"] == "exited"


def test_gateway_serve_uses_explicit_home_for_status(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import agent_cli.command_handlers.gateway as gateway_handler
    from gateway.service_state import read_gateway_status

    monkeypatch.setattr(gateway_handler, "serve_callback_http", lambda app, *, host, port: None)

    gateway_handler.gateway_serve(SimpleNamespace(host="127.0.0.1", port=8765), home=tmp_path)

    assert read_gateway_status(tmp_path)["process_state"] == "exited"


def test_gateway_default_home_resolves_cli_home(tmp_path, monkeypatch):
    import agent_cli.command_handlers.gateway as gateway_handler

    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    assert gateway_handler._default_home() == tmp_path.resolve()


def test_feishu_gateway_doctor_reports_missing_env():
    from agent_cli.doctor import check_feishu_gateway_config

    errors = check_feishu_gateway_config({})

    assert errors == [
        "missing Feishu gateway environment variables: FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CALLBACK_TOKEN"
    ]

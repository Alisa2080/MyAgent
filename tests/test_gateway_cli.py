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


def test_gateway_serve_preserves_http_transport_on_exit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import agent_cli.command_handlers.gateway as gateway_handler
    from gateway.service_state import read_gateway_status

    monkeypatch.setattr(gateway_handler, "serve_callback_http", lambda app, *, host, port: None)

    gateway_handler.gateway_serve(SimpleNamespace(host="127.0.0.1", port=8765), home=tmp_path)

    assert read_gateway_status(tmp_path)["transport"] == "http"


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


def test_gateway_feishu_ws_requires_feishu_env(monkeypatch, tmp_path):
    from argparse import Namespace
    from agent_cli.command_handlers.gateway import gateway_feishu_ws

    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    result = gateway_feishu_ws(Namespace(), home=tmp_path)

    assert result == 2


def test_gateway_feishu_ws_invokes_transport(monkeypatch, tmp_path):
    from argparse import Namespace
    import agent_cli.command_handlers.gateway as gateway_commands

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    calls = []
    monkeypatch.setattr(gateway_commands, "serve_feishu_ws_gateway", lambda **kwargs: calls.append(kwargs))

    result = gateway_commands.gateway_feishu_ws(Namespace(), home=tmp_path)

    assert result == 0
    assert calls[0]["home"] == tmp_path


def test_gateway_feishu_ws_marks_running_status(monkeypatch, tmp_path):
    from argparse import Namespace
    import agent_cli.command_handlers.gateway as gateway_commands
    from gateway.service_state import read_gateway_status

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setattr(gateway_commands, "serve_feishu_ws_gateway", lambda **kwargs: None)

    result = gateway_commands.gateway_feishu_ws(Namespace(), home=tmp_path)

    assert result == 0
    status = read_gateway_status(tmp_path)
    assert status["process_state"] == "exited"
    assert status["transport"] == "feishu-ws"


def test_gateway_status_shows_transport(monkeypatch, tmp_path, capsys):
    from agent_cli.command_handlers.gateway import gateway_status
    from gateway.service_state import write_gateway_status

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    write_gateway_status(tmp_path, {"process_state": "running", "transport": "feishu-ws", "platforms": ["feishu"]})

    exit_code = gateway_status(home=tmp_path)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Transport: feishu-ws" in output


def test_gateway_feishu_ws_refuses_when_http_running(monkeypatch, tmp_path, capsys):
    import agent_cli.command_handlers.gateway as gateway_commands
    from argparse import Namespace
    from gateway.service_state import write_gateway_status

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    write_gateway_status(tmp_path, {"process_state": "running", "transport": "http", "platforms": ["feishu"]})
    monkeypatch.setattr(gateway_commands, "serve_feishu_ws_gateway", lambda **kwargs: None)

    exit_code = gateway_commands.gateway_feishu_ws(Namespace(), home=tmp_path)

    assert exit_code == 2
    assert "already running with transport http" in capsys.readouterr().out


def test_gateway_serve_refuses_when_feishu_ws_running(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    import agent_cli.command_handlers.gateway as gateway_handler
    from gateway.service_state import write_gateway_status

    monkeypatch.setattr(gateway_handler, "serve_callback_http", lambda app, *, host, port: None)
    write_gateway_status(tmp_path, {"process_state": "running", "transport": "feishu-ws", "platforms": ["feishu"]})

    exit_code = gateway_handler.gateway_serve(SimpleNamespace(host="127.0.0.1", port=8765), home=tmp_path)

    assert exit_code == 2
    assert "already running with transport feishu-ws" in capsys.readouterr().out


def test_gateway_status_shows_inbox_stats(monkeypatch, tmp_path, capsys):
    from pathlib import Path

    from gateway.contracts import InboundEvent
    from gateway.inbox_store import GatewayInboxStore
    from gateway.service_state import write_gateway_status
    from agent_cli.command_handlers.gateway import gateway_status

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    store = GatewayInboxStore(tmp_path / "gateway" / "gateway.sqlite")
    store.enqueue(
        InboundEvent(
            platform="feishu",
            event_id="evt-1",
            event_type="im.message.receive_v1",
            chat_id="oc",
            text="hello",
            timestamp="2026-06-03T00:00:00+00:00",
            thread_id="mid",
            raw={},
        )
    )
    write_gateway_status(tmp_path, {"process_state": "running", "transport": "feishu-ws", "platforms": ["feishu"]})

    assert gateway_status(home=tmp_path) == 0
    assert "Inbox: pending=1" in capsys.readouterr().out


def test_gateway_feishu_ws_warns_when_cron_service_not_running(monkeypatch, tmp_path, capsys):
    from argparse import Namespace

    import agent_cli.command_handlers.gateway as gateway_handler
    from cron.service_manager import ServiceRuntimeStatus

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    def fake_compose_service_status(runtime=None, *, now_text=None, stale_after_seconds=180):
        from cron.service_manager import compose_service_status

        return compose_service_status(
            ServiceRuntimeStatus(
                platform="systemd-user",
                supported=True,
                installed=True,
                enabled=True,
                active=False,
                pid=None,
                detail="inactive",
            ),
            now_text="2026-06-04T00:00:00+00:00",
            stale_after_seconds=stale_after_seconds,
        )

    def fake_serve(**kwargs):
        pass

    monkeypatch.setattr(gateway_handler, "serve_feishu_ws_gateway", fake_serve)
    monkeypatch.setattr(gateway_handler, "compose_service_status", fake_compose_service_status)

    gateway_handler.gateway_feishu_ws(Namespace(), home=tmp_path)

    output = capsys.readouterr().out
    assert "WARN Cron service is not running" in output
    assert "scheduled cron jobs will not run automatically" in output
    assert "python3 -m agent_cli.main cron service start" in output


def test_gateway_feishu_ws_warns_with_cron_serve_on_unsupported_platform(monkeypatch, tmp_path, capsys):
    from argparse import Namespace

    import agent_cli.command_handlers.gateway as gateway_handler
    from cron.service_manager import ServiceRuntimeStatus

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    def fake_compose_service_status(runtime=None, *, now_text=None, stale_after_seconds=180):
        from cron.service_manager import compose_service_status
        return compose_service_status(
            ServiceRuntimeStatus(
                platform="unsupported",
                supported=False,
                installed=False,
                enabled=False,
                active=False,
                pid=None,
                detail="run `python3 -m agent_cli.main cron serve` for foreground scheduling",
            ),
            now_text="2026-06-04T00:00:00+00:00",
            stale_after_seconds=stale_after_seconds,
        )

    def fake_serve(**kwargs):
        pass

    monkeypatch.setattr(gateway_handler, "serve_feishu_ws_gateway", fake_serve)
    monkeypatch.setattr(gateway_handler, "compose_service_status", fake_compose_service_status)

    gateway_handler.gateway_feishu_ws(Namespace(), home=tmp_path)

    output = capsys.readouterr().out
    assert "WARN Cron service is not available on this platform" in output
    assert "scheduled cron jobs will not run automatically" in output
    assert "python3 -m agent_cli.main cron serve" in output


def test_gateway_feishu_ws_does_not_warn_when_cron_service_is_healthy(monkeypatch, tmp_path, capsys):
    from argparse import Namespace

    import agent_cli.command_handlers.gateway as gateway_handler
    from cron.service_manager import ServiceRuntimeStatus
    from cron.service_state import write_service_status

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    write_service_status(
        {"process_state": "running", "pid": 999, "last_heartbeat_at": "2026-06-04T00:00:00+00:00"},
    )

    def fake_compose_service_status(runtime=None, *, now_text=None, stale_after_seconds=180):
        from cron.service_manager import compose_service_status
        return compose_service_status(
            ServiceRuntimeStatus(
                platform="systemd-user",
                supported=True,
                installed=True,
                enabled=True,
                active=True,
                pid=999,
                detail="active",
            ),
            now_text="2026-06-04T00:00:00+00:00",
            stale_after_seconds=stale_after_seconds,
        )

    def fake_serve(**kwargs):
        pass

    monkeypatch.setattr(gateway_handler, "serve_feishu_ws_gateway", fake_serve)
    monkeypatch.setattr(gateway_handler, "compose_service_status", fake_compose_service_status)

    gateway_handler.gateway_feishu_ws(Namespace(), home=tmp_path)

    output = capsys.readouterr().out
    assert "WARN" not in output
    assert "Cron service" not in output

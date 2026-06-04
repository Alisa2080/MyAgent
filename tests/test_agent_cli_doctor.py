from agent_cli.doctor import (
    HealthCheck,
    check_dependencies,
    check_openai_api_key,
    check_python_version,
    check_workdir,
    doctor_exit_code,
    render_doctor_output,
    run_health_checks,
)


def test_check_python_version():
    result = check_python_version()
    assert "Python" in result.message
    assert result.status == "OK"


def test_check_dependencies():
    results = check_dependencies()
    # Each dependency is a separate HealthCheck
    assert len(results) >= 3
    # At least prompt_toolkit, langchain, langgraph should be present
    names = [r.name for r in results]
    for dep in ["prompt_toolkit", "langchain", "langgraph"]:
        assert dep in names


def test_check_workdir_exists():
    result = check_workdir("/home/miku/projects/langchain")
    assert result.status == "OK"
    assert "langchain" in result.message


def test_check_workdir_missing():
    result = check_workdir("/nonexistent/path")
    assert result.status == "FAIL"


def test_run_health_checks_returns_results():
    results = run_health_checks(workdir="/home/miku/projects/langchain")
    assert len(results) >= 3
    names = [r.name for r in results]
    assert "Python Version" in names
    # Dependencies are now individual checks
    assert "Platform" in names


def test_render_doctor_output_uses_stable_status_columns():
    results = [
        HealthCheck("Python Version", "OK", "Python 3.11"),
        HealthCheck("OPENAI_API_KEY", "WARN", "not set"),
        HealthCheck("SQLite DB", "FAIL", "cannot open"),
    ]

    output = render_doctor_output(results)

    assert "OK    Python Version" in output
    assert "WARN  OPENAI_API_KEY" in output
    assert "FAIL  SQLite DB" in output
    assert "✓" not in output
    assert "✗" not in output


def test_doctor_exit_code_is_zero_for_ok_and_warn():
    checks = [
        HealthCheck("python", "OK", "ok"),
        HealthCheck("key", "WARN", "missing"),
    ]

    assert doctor_exit_code(checks) == 0


def test_doctor_exit_code_is_one_for_failures():
    checks = [
        HealthCheck("python", "OK", "ok"),
        HealthCheck("db", "FAIL", "bad"),
    ]

    assert doctor_exit_code(checks) == 1


def test_check_openai_api_key_warns_when_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = check_openai_api_key()

    assert result.status == "WARN"
    assert result.name == "OPENAI_API_KEY"


def test_check_openai_api_key_ok_when_present(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    result = check_openai_api_key()

    assert result.status == "OK"
    assert "set" in result.message


def test_run_health_checks_reports_storage_and_openai_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)
    by_name = {item.name: item for item in results}

    assert by_name["CLI Home"].status == "OK"
    assert by_name["SQLite DB"].status == "OK"
    assert by_name["Logs"].status == "OK"
    assert by_name["OPENAI_API_KEY"].status == "WARN"


def test_run_health_checks_reports_feishu_gateway_config(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("FEISHU_CALLBACK_TOKEN", raising=False)

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)
    by_name = {item.name: item for item in results}

    assert by_name["Feishu Gateway"].status == "WARN"
    assert "FEISHU_APP_ID" in by_name["Feishu Gateway"].message


def test_run_health_checks_uses_explicit_cli_home_for_database(tmp_path, monkeypatch):
    ambient_home = tmp_path / "ambient"
    explicit_home = tmp_path / "explicit"
    monkeypatch.setenv("AGENT_CLI_HOME", str(ambient_home))

    results = run_health_checks(workdir=str(tmp_path), cli_home=explicit_home)
    by_name = {item.name: item for item in results}

    assert by_name["SQLite DB"].status == "OK"
    assert by_name["SQLite DB"].message == str(explicit_home / "cli.sqlite")
    assert by_name["Background Tasks"].status in {"OK", "WARN"}
    assert (explicit_home / "cli.sqlite").exists()
    assert not (ambient_home / "cli.sqlite").exists()


def test_doctor_reports_stale_background_task_as_warn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.background import BackgroundTaskStore

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.create_task(
        task_id="bg_stale",
        session_id="session-stale",
        title="Stale",
        prompt_preview="work",
        owner_id=None,
    )

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)
    background = next(item for item in results if item.name == "Background Tasks")

    assert background.status == "WARN"
    assert "stale" in background.message.lower() or "owner" in background.message.lower()


def test_doctor_reports_dead_background_owner_as_warn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))

    from agent_cli.background import BackgroundTaskStore

    store = BackgroundTaskStore(tmp_path / "cli.sqlite")
    store.register_owner("dead-owner", process_id=99999999)
    store.create_task(
        task_id="bg_dead",
        session_id="session-dead",
        title="Dead",
        prompt_preview="work",
        owner_id="dead-owner",
    )

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)
    background = next(item for item in results if item.name == "Background Tasks")

    assert background.status == "WARN"
    assert "dead" in background.message.lower() or "stale" in background.message.lower()


def test_doctor_reports_config_and_dotenv_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model: [", encoding="utf-8")

    from agent_cli.doctor import run_health_checks

    results = run_health_checks(workdir=str(tmp_path))
    config = next(item for item in results if item.name == "Config")

    assert config.status == "FAIL"
    assert str(tmp_path / "config.yaml") in config.message


def test_doctor_fails_semantically_invalid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "display:\n  markdown: weird\n",
        encoding="utf-8",
    )

    from agent_cli.doctor import run_health_checks

    results = run_health_checks(workdir=str(tmp_path))
    config = next(item for item in results if item.name == "Config")

    assert config.status == "FAIL"
    assert "display.markdown" in config.message


def test_doctor_fails_invalid_display_theme(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "display:\n  theme: weird\n",
        encoding="utf-8",
    )

    from agent_cli.doctor import run_health_checks

    results = run_health_checks(workdir=str(tmp_path))
    config = next(item for item in results if item.name == "Config")

    assert config.status == "FAIL"
    assert "display.theme" in config.message


def test_doctor_reports_config_schema_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("display:\n  markdown: weird\n", encoding="utf-8")

    from agent_cli.doctor import run_health_checks

    results = run_health_checks(workdir=str(tmp_path), cli_home=home)
    config = next(result for result in results if result.name == "Config")

    assert config.status == "FAIL"
    assert "display.markdown" in config.message


def test_doctor_warns_missing_feishu_ws_config(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_cli.doctor import run_health_checks

    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    results = run_health_checks(workdir=str(tmp_path))

    ws_check = next((c for c in results if c.name == "Feishu WebSocket"), None)
    assert ws_check is not None
    assert ws_check.status in ("WARN", "FAIL")


def test_doctor_runs_feishu_ws_check(tmp_path, monkeypatch):
    from agent_cli.doctor import run_health_checks

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)

    names = [c.name for c in results]
    assert "Feishu WebSocket" in names


def test_doctor_warns_gateway_service_not_installed(tmp_path, monkeypatch):
    import agent_cli.doctor as doctor

    monkeypatch.setattr(doctor, "get_cli_home", lambda: tmp_path)
    monkeypatch.setattr("gateway.service_manager.detect_platform", lambda: "systemd-user")
    monkeypatch.setattr("gateway.service_manager.systemd_unit_dir", lambda: tmp_path / "systemd")

    results = doctor.run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)

    service_check = next((check for check in results if check.name == "Gateway Service"), None)
    assert service_check is not None
    assert service_check.status == "WARN"
    assert "not installed" in service_check.message


# --- Task 2: Cron/gateway/Feishu doctor helpers ---


def test_format_counts_returns_key_values(monkeypatch):
    from agent_cli.doctor import _format_counts

    result = _format_counts({"pending": 2, "failed": 1, "succeeded": 5})
    assert "pending=2" in result
    assert "failed=1" in result
    assert "succeeded=5" in result


def test_format_counts_empty(monkeypatch):
    from agent_cli.doctor import _format_counts

    result = _format_counts({})
    assert result == ""


def test_check_cron_service_reports_ok_when_active(monkeypatch):
    from agent_cli.doctor import check_cron_service
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "agent_cli.doctor.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=True,
            pid=12345,
            detail="ok",
            error=None,
            heartbeat_fresh=True,
            process_state="running",
            leader_state="leader",
            last_heartbeat_at="2026-06-04T00:00:00+00:00",
            last_tick={"jobs_ran": 1},
            last_error=None,
            exit_reason=None,
            status_pid=12345,
        ),
    )

    result = check_cron_service()

    assert result.status == "OK"
    assert "cron service" in result.name.lower()


def test_check_cron_service_warns_when_inactive(monkeypatch):
    from agent_cli.doctor import check_cron_service
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "agent_cli.doctor.compose_service_status",
        lambda: ServiceStatus(
            platform="systemd-user",
            supported=True,
            installed=True,
            enabled=True,
            active=False,
            pid=None,
            detail=None,
            error=None,
            heartbeat_fresh=False,
            process_state=None,
            leader_state=None,
            last_heartbeat_at=None,
            last_tick=None,
            last_error=None,
            exit_reason=None,
            status_pid=None,
        ),
    )

    result = check_cron_service()

    assert result.status == "WARN"


def test_check_cron_service_warns_when_unsupported(monkeypatch):
    from agent_cli.doctor import check_cron_service
    from cron.service_manager import ServiceStatus

    monkeypatch.setattr(
        "agent_cli.doctor.compose_service_status",
        lambda: ServiceStatus(
            platform="unknown",
            supported=False,
            installed=None,
            enabled=None,
            active=None,
            pid=None,
            detail=None,
            error=None,
            heartbeat_fresh=False,
            process_state=None,
            leader_state=None,
            last_heartbeat_at=None,
            last_tick=None,
            last_error=None,
            exit_reason=None,
            status_pid=None,
        ),
    )

    result = check_cron_service()

    assert result.status == "WARN"


def test_check_feishu_token_ok_when_token_obtained(monkeypatch):
    from agent_cli.doctor import check_feishu_token
    from gateway.contracts import SendResult

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    class FakeAdapter:
        key = "feishu"

        def token_smoke(self):
            return SendResult(True)

    monkeypatch.setattr(
        "agent_cli.doctor.default_gateway_registry",
        lambda **kwargs: type("R", (), {"get": lambda self, k: FakeAdapter() if k == "feishu" else None})(),
    )

    result = check_feishu_token()

    assert result.status == "OK"


def test_check_feishu_token_warns_when_env_missing(monkeypatch):
    from agent_cli.doctor import check_feishu_token

    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    result = check_feishu_token()

    assert result.status == "WARN"
    assert "FEISHU_APP_ID" in result.message or "FEISHU_APP_SECRET" in result.message


def test_check_feishu_token_warns_when_adapter_absent(monkeypatch):
    from agent_cli.doctor import check_feishu_token

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    monkeypatch.setattr(
        "agent_cli.doctor.default_gateway_registry",
        lambda **kwargs: type("R", (), {"get": lambda self, k: None})(),
    )

    result = check_feishu_token()

    assert result.status == "WARN"
    assert "feishu" in result.message.lower()


def test_check_feishu_token_warns_when_token_fails(monkeypatch):
    from agent_cli.doctor import check_feishu_token
    from gateway.contracts import SendResult

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    class FakeAdapter:
        key = "feishu"

        def token_smoke(self):
            return SendResult(False, error="token request failed")

    monkeypatch.setattr(
        "agent_cli.doctor.default_gateway_registry",
        lambda **kwargs: type("R", (), {"get": lambda self, k: FakeAdapter() if k == "feishu" else None})(),
    )

    result = check_feishu_token()

    assert result.status == "WARN"
    assert "token" in result.message.lower() or "failed" in result.message.lower()


def test_check_cron_feishu_delivery_ok_when_adapters_present(monkeypatch):
    from agent_cli.doctor import check_cron_feishu_delivery
    from gateway.contracts import SendResult

    class FakeDeliveryRegistry:
        def get(self, key):
            return True

        def active_adapter_keys(self):
            return ["feishu", "origin"]

        def adapter_keys(self):
            return ["feishu", "origin", "local"]

    class FakeGatewayAdapter:
        def validate_target(self, target):
            return SendResult(True)

    class FakeGatewayRegistry:
        def get(self, key):
            return FakeGatewayAdapter() if key == "feishu" else None

    monkeypatch.setattr(
        "agent_cli.doctor.default_delivery_registry",
        lambda: FakeDeliveryRegistry(),
    )
    monkeypatch.setattr(
        "agent_cli.doctor.default_gateway_registry",
        lambda **kwargs: FakeGatewayRegistry(),
    )

    result = check_cron_feishu_delivery()

    assert result.status == "OK"


def test_check_cron_feishu_delivery_warns_when_cron_feishu_missing(monkeypatch):
    from agent_cli.doctor import check_cron_feishu_delivery

    monkeypatch.setattr(
        "agent_cli.doctor.default_delivery_registry",
        lambda: type("R", (), {
            "get": lambda self, k: None,
            "active_adapter_keys": lambda self: ["origin"],
            "adapter_keys": lambda self: ["origin", "local"],
        })(),
    )
    monkeypatch.setattr(
        "agent_cli.doctor.default_gateway_registry",
        lambda **kwargs: type("R", (), {"get": lambda self, k: True if k == "feishu" else None})(),
    )

    result = check_cron_feishu_delivery()

    assert result.status == "WARN"
    assert "feishu" in result.message.lower()


def test_check_cron_feishu_delivery_warns_when_gateway_feishu_missing(monkeypatch):
    from agent_cli.doctor import check_cron_feishu_delivery

    monkeypatch.setattr(
        "agent_cli.doctor.default_delivery_registry",
        lambda: type("R", (), {
            "get": lambda self, k: True,
            "active_adapter_keys": lambda self: ["feishu", "origin"],
            "adapter_keys": lambda self: ["feishu", "origin"],
        })(),
    )
    monkeypatch.setattr(
        "agent_cli.doctor.default_gateway_registry",
        lambda **kwargs: type("R", (), {"get": lambda self, k: None})(),
    )

    result = check_cron_feishu_delivery()

    assert result.status == "WARN"


def test_check_cron_feishu_delivery_warns_when_origin_adapter_inactive(monkeypatch):
    from agent_cli.doctor import check_cron_feishu_delivery
    from gateway.contracts import SendResult

    class FakeGatewayAdapter:
        def validate_target(self, target):
            return SendResult(True)

    monkeypatch.setattr(
        "agent_cli.doctor.default_delivery_registry",
        lambda: type("R", (), {
            "get": lambda self, k: True,
            "active_adapter_keys": lambda self: ["feishu"],
            "adapter_keys": lambda self: ["feishu", "origin"],
        })(),
    )
    monkeypatch.setattr(
        "agent_cli.doctor.default_gateway_registry",
        lambda **kwargs: type("GR", (), {"get": lambda self, k: FakeGatewayAdapter() if k == "feishu" else None})(),
    )

    result = check_cron_feishu_delivery()

    assert result.status == "WARN"
    assert "origin" in result.message.lower()


def test_check_cron_feishu_delivery_warns_when_target_validation_fails(monkeypatch):
    from agent_cli.doctor import check_cron_feishu_delivery
    from gateway.contracts import SendResult

    class FakeGatewayAdapter:
        def validate_target(self, target):
            return SendResult(False, error="missing required Feishu environment variables")

    monkeypatch.setattr(
        "agent_cli.doctor.default_delivery_registry",
        lambda: type("R", (), {
            "get": lambda self, k: True,
            "active_adapter_keys": lambda self: ["feishu", "origin"],
            "adapter_keys": lambda self: ["feishu", "origin"],
        })(),
    )
    monkeypatch.setattr(
        "agent_cli.doctor.default_gateway_registry",
        lambda **kwargs: type("GR", (), {"get": lambda self, k: FakeGatewayAdapter() if k == "feishu" else None})(),
    )

    result = check_cron_feishu_delivery()

    assert result.status == "WARN"
    assert "missing required" in result.message


def test_check_gateway_inbox_ok_when_no_failures(monkeypatch):
    from agent_cli.doctor import check_gateway_inbox
    from pathlib import Path

    # No gateway.sqlite at all
    result = check_gateway_inbox(Path("/nonexistent"))

    assert result.status == "OK"


def test_check_gateway_inbox_warns_on_failed_events(tmp_path, monkeypatch):
    from agent_cli.doctor import check_gateway_inbox
    from gateway.contracts import InboundEvent
    from gateway.inbox_store import GatewayInboxStore

    inbox_path = tmp_path / "gateway" / "gateway.sqlite"
    store = GatewayInboxStore(inbox_path)
    row_id = store.enqueue(
        InboundEvent(
            platform="feishu",
            event_id="evt-dead",
            event_type="im.message.receive_v1",
            chat_id="oc_123",
            text="hello",
            timestamp="2026-06-04T00:00:00+00:00",
            raw={},
        )
    )
    store.fail(row_id, "dispatch failed", max_attempts=1)

    result = check_gateway_inbox(tmp_path)

    assert result.status == "WARN"
    assert "dead=1" in result.message


def test_check_gateway_inbox_reports_counts(tmp_path, monkeypatch):
    from agent_cli.doctor import check_gateway_inbox

    result = check_gateway_inbox(tmp_path)

    assert result.status == "OK"
    assert "pending" in result.message or "inbox" in result.name.lower()


def test_check_cron_delivery_queue_reports_stats(monkeypatch):
    from agent_cli.doctor import check_cron_delivery_queue

    monkeypatch.setattr(
        "agent_cli.doctor.DeliveryStore",
        lambda: type("DS", (), {"stats": lambda self: {"pending": 0, "delivering": 0, "failed": 0, "dead": 0, "delivered": 0}})(),
    )

    result = check_cron_delivery_queue()

    assert result.status == "OK"
    assert "pending" in result.message


def test_check_cron_delivery_queue_warns_on_failures(monkeypatch):
    from agent_cli.doctor import check_cron_delivery_queue

    monkeypatch.setattr(
        "agent_cli.doctor.DeliveryStore",
        lambda: type("DS", (), {"stats": lambda self: {"pending": 0, "delivering": 0, "failed": 2, "dead": 1, "delivered": 10}})(),
    )

    result = check_cron_delivery_queue()

    assert result.status == "WARN"
    assert "failed" in result.message or "dead" in result.message


def test_check_cron_delivery_queue_warns_on_pending_when_cron_unhealthy(monkeypatch):
    from agent_cli.doctor import check_cron_delivery_queue

    monkeypatch.setattr(
        "agent_cli.doctor.DeliveryStore",
        lambda: type("DS", (), {"stats": lambda self: {"pending": 2, "delivering": 0, "failed": 0, "dead": 0, "delivered": 0}})(),
    )

    result = check_cron_delivery_queue(cron_service_healthy=False)

    assert result.status == "WARN"
    assert "pending" in result.message
    assert "cron service" in result.message.lower()


def test_run_health_checks_includes_new_cron_checks(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")

    from agent_cli.doctor import run_health_checks

    monkeypatch.setattr(
        "agent_cli.doctor.compose_service_status",
        lambda: type("SS", (), {
            "platform": "systemd-user",
            "supported": True,
            "installed": True,
            "enabled": True,
            "active": True,
            "pid": 12345,
            "detail": "ok",
            "error": None,
            "heartbeat_fresh": True,
            "process_state": "running",
            "leader_state": "leader",
            "last_heartbeat_at": "2026-06-04T00:00:00+00:00",
            "last_tick": {"jobs_ran": 0},
            "last_error": None,
            "exit_reason": None,
            "status_pid": 12345,
        })(),
    )
    monkeypatch.setattr(
        "agent_cli.doctor.default_delivery_registry",
        lambda: type("R", (), {"get": lambda self, k: True, "platform_keys": lambda self: ["feishu"]})(),
    )
    monkeypatch.setattr(
        "agent_cli.doctor.default_gateway_registry",
        lambda **kwargs: type("GR", (), {"get": lambda self, k: True if k == "feishu" else None})(),
    )

    results = run_health_checks(workdir=str(tmp_path), cli_home=tmp_path)
    names = [c.name for c in results]

    assert "Cron Service" in names
    assert "Feishu Token" in names
    assert "Cron Feishu Delivery" in names
    assert "Gateway Inbox" in names
    assert "Cron Delivery Queue" in names

from __future__ import annotations

import os


def test_service_env_set_list_unset_masks_secrets(monkeypatch, tmp_path):
    from cron import service_env

    path = tmp_path / "service.env"
    monkeypatch.setattr(service_env, "get_service_env_file", lambda: path)

    service_env.set_service_env("FEISHU_APP_ID", "cli_123")
    service_env.set_service_env("FEISHU_APP_SECRET", "secret-value")

    values = service_env.read_service_env()
    assert values == {
        "FEISHU_APP_ID": "cli_123",
        "FEISHU_APP_SECRET": "secret-value",
    }
    assert service_env.masked_service_env() == {
        "FEISHU_APP_ID": "********",
        "FEISHU_APP_SECRET": "********",
    }

    removed = service_env.unset_service_env("FEISHU_APP_SECRET")

    assert removed is True
    assert service_env.read_service_env() == {"FEISHU_APP_ID": "cli_123"}


def test_service_env_rejects_invalid_key_and_multiline_value(monkeypatch, tmp_path):
    from cron import service_env

    monkeypatch.setattr(service_env, "get_service_env_file", lambda: tmp_path / "service.env")

    for key in ("feishu", "FEISHU-APP", "1FEISHU"):
        try:
            service_env.set_service_env(key, "value")
        except ValueError as exc:
            assert "invalid service env key" in str(exc)
        else:
            raise AssertionError(f"{key} should be rejected")

    try:
        service_env.set_service_env("FEISHU_APP_SECRET", "a\nb")
    except ValueError as exc:
        assert "single-line" in str(exc)
    else:
        raise AssertionError("multiline values should be rejected")


def test_service_env_file_permissions_are_restricted(monkeypatch, tmp_path):
    from cron import service_env

    path = tmp_path / "service.env"
    monkeypatch.setattr(service_env, "get_service_env_file", lambda: path)

    service_env.set_service_env("FEISHU_APP_ID", "cli_123")

    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_service_env_permission_status_detects_broad_file(monkeypatch, tmp_path):
    from cron import service_env

    path = tmp_path / "service.env"
    path.write_text("FEISHU_APP_ID=cli_123\n", encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(service_env, "get_service_env_file", lambda: path)

    status = service_env.inspect_service_env_file()

    assert status.exists is True
    if os.name != "nt":
        assert status.permissions_ok is False

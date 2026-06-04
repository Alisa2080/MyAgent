def test_gateway_service_env_set_list_unset(tmp_path):
    from gateway.service_env import masked_service_env, read_service_env, set_service_env, unset_service_env

    path = tmp_path / "service.env"
    set_service_env("FEISHU_APP_ID", "cli_x", path=path)
    set_service_env("FEISHU_APP_SECRET", "secret", path=path)

    assert read_service_env(path=path)["FEISHU_APP_ID"] == "cli_x"
    assert masked_service_env(path=path)["FEISHU_APP_SECRET"] != "secret"

    assert unset_service_env("FEISHU_APP_SECRET", path=path) is True
    assert "FEISHU_APP_SECRET" not in read_service_env(path=path)


def test_gateway_service_env_file_is_private(tmp_path):
    import os

    from gateway.service_env import set_service_env

    path = tmp_path / "gateway" / "service.env"
    set_service_env("FEISHU_APP_SECRET", "secret", path=path)

    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0


def test_gateway_service_env_rejects_invalid_key(tmp_path):
    from gateway.service_env import set_service_env

    try:
        set_service_env("BAD-KEY", "x", path=tmp_path / "service.env")
    except ValueError as exc:
        assert "invalid service env key" in str(exc)
    else:
        raise AssertionError("expected invalid key")

from __future__ import annotations

from pathlib import Path


def test_detect_project_root_walks_to_pyproject(tmp_path):
    from cron.service_context import detect_project_root

    root = tmp_path / "repo"
    package = root / "agent_cli"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    module_path = package / "main.py"
    module_path.write_text("x = 1\n", encoding="utf-8")

    assert detect_project_root(module_path) == root


def test_build_service_runtime_context_includes_project_pythonpath(monkeypatch, tmp_path):
    from cron.service_context import build_service_runtime_context

    root = tmp_path / "repo"
    (root / "agent_cli").mkdir(parents=True)
    (root / "cron").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    module_path = root / "agent_cli" / "main.py"
    module_path.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PYTHONPATH", "/extra/path")

    context = build_service_runtime_context(module_path=module_path)

    assert context.project_root == root
    assert context.working_directory == root
    assert context.service_env_file == (tmp_path / "home" / "cron" / "service.env").resolve()
    assert context.pythonpath == f"{root}:/extra/path"


def test_build_service_install_config_carries_runtime_context(monkeypatch, tmp_path):
    from cron import service_context
    from cron.service_manager import build_service_install_config

    root = tmp_path / "repo"
    env_file = tmp_path / "home" / "cron" / "service.env"
    monkeypatch.setattr(
        service_context,
        "build_service_runtime_context",
        lambda: service_context.ServiceRuntimeContext(
            project_root=root,
            working_directory=root,
            pythonpath=str(root),
            service_env_file=env_file,
        ),
    )

    config, _detail = build_service_install_config(
        interval_seconds=30,
        lease_seconds=90,
        force=True,
    )

    assert config.working_directory == root
    assert config.pythonpath == str(root)
    assert config.service_env_file == env_file

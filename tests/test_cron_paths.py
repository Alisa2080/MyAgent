import importlib.util
import sys
from pathlib import Path


def load_paths_module():
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    module_path = repo_root / "cron" / "paths.py"
    spec = importlib.util.spec_from_file_location("cron_paths_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_get_cron_home_prefers_hermes_home(monkeypatch, tmp_path):
    paths = load_paths_module()

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert paths.get_cron_home() == hermes_home
    assert paths.get_cron_dir() == hermes_home / "cron"
    assert paths.get_jobs_file() == hermes_home / "cron" / "jobs.json"
    assert paths.get_output_dir() == hermes_home / "cron" / "output"
    assert paths.get_scripts_dir() == hermes_home / "scripts"


def test_get_cron_home_uses_toolkit_parent_when_no_hermes_home(monkeypatch, tmp_path):
    paths = load_paths_module()

    toolkit_home = tmp_path / "toolkit"
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(paths, "get_toolkit_home", lambda: toolkit_home)

    assert paths.get_cron_home() == toolkit_home


def test_ensure_cron_dirs_creates_secure_dirs(monkeypatch, tmp_path):
    paths = load_paths_module()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    paths.ensure_cron_dirs()

    assert (tmp_path / "cron").is_dir()
    assert (tmp_path / "cron" / "output").is_dir()
    assert (tmp_path / "scripts").is_dir()


def test_atomic_write_json_sets_owner_only_permissions(monkeypatch, tmp_path):
    paths = load_paths_module()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    target = paths.get_jobs_file()

    paths.atomic_write_json(target, {"jobs": []})

    assert target.exists()
    assert '"jobs": []' in target.read_text()
    if hasattr(target, "stat"):
        assert oct(target.stat().st_mode & 0o777) == "0o600"

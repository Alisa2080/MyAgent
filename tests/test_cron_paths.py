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


def test_get_cron_home_prefers_agent_cron_home(monkeypatch, tmp_path):
    paths = load_paths_module()

    cron_home = tmp_path / "cron-home"
    monkeypatch.setenv("AGENT_CRON_HOME", str(cron_home))

    assert paths.get_cron_home() == cron_home
    assert paths.get_cron_dir() == cron_home / "cron"
    assert paths.get_jobs_file() == cron_home / "cron" / "jobs.json"
    assert paths.get_output_dir() == cron_home / "cron" / "output"
    assert paths.get_scripts_dir() == cron_home / "scripts"


def test_get_cron_home_uses_toolkit_home_when_no_agent_cron_home(monkeypatch, tmp_path):
    paths = load_paths_module()

    toolkit_home = tmp_path / "toolkit"
    monkeypatch.delenv("AGENT_CRON_HOME", raising=False)
    monkeypatch.setattr(paths, "get_toolkit_home", lambda: toolkit_home)

    assert paths.get_cron_home() == toolkit_home


def test_ensure_cron_dirs_creates_secure_dirs(monkeypatch, tmp_path):
    paths = load_paths_module()

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    paths.ensure_cron_dirs()

    assert (tmp_path / "cron").is_dir()
    assert (tmp_path / "cron" / "output").is_dir()
    assert (tmp_path / "scripts").is_dir()
    assert (tmp_path / "cron" / "runner-tmp").is_dir()


def test_atomic_write_json_sets_owner_only_permissions(monkeypatch, tmp_path):
    paths = load_paths_module()

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    target = paths.get_jobs_file()

    paths.atomic_write_json(target, {"jobs": []})

    assert target.exists()
    assert '"jobs": []' in target.read_text()
    if hasattr(target, "stat"):
        assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_get_runner_tmp_dir(monkeypatch, tmp_path):
    paths = load_paths_module()

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    assert paths.get_runner_tmp_dir() == tmp_path / "cron" / "runner-tmp"
    assert paths.RUNNER_TMP_DIR_NAME == "runner-tmp"

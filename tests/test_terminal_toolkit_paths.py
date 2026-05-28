import importlib.util
from pathlib import Path

import pytest


def _load_paths_module():
    module_path = Path(__file__).resolve().parents[1] / "agent_tools" / "terminal_toolkit" / "paths.py"
    spec = importlib.util.spec_from_file_location("terminal_toolkit_paths_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def paths():
    return _load_paths_module()


def test_get_toolkit_home_tries_all_candidates_in_priority_order(paths, monkeypatch, tmp_path):
    custom_home = tmp_path / "custom-home"
    fake_home = tmp_path / "home"
    fake_cwd = tmp_path / "cwd"

    monkeypatch.setenv("TERMINAL_TOOLKIT_HOME", str(custom_home))
    monkeypatch.setattr(paths.Path, "home", lambda: fake_home)
    monkeypatch.setattr(paths.Path, "cwd", lambda: fake_cwd)

    expected = [
        custom_home,
        fake_home / ".terminal-toolkit",
        fake_cwd / ".terminal-toolkit",
        Path("/tmp") / ".terminal-toolkit",
    ]
    calls = []

    def fake_mkdir(self, parents=False, exist_ok=False):
        calls.append(self)
        assert parents is True
        assert exist_ok is True
        if self != expected[-1]:
            raise OSError("candidate unavailable")

    monkeypatch.setattr(paths.Path, "mkdir", fake_mkdir)

    assert paths.get_toolkit_home() == expected[-1]
    assert calls == expected


def test_get_toolkit_home_starts_with_home_when_env_candidates_unset(paths, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_cwd = tmp_path / "cwd"
    expected_home_candidate = fake_home / ".terminal-toolkit"
    calls = []

    monkeypatch.delenv("TERMINAL_TOOLKIT_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: fake_home)
    monkeypatch.setattr(paths.Path, "cwd", lambda: fake_cwd)

    def fake_mkdir(self, parents=False, exist_ok=False):
        calls.append(self)
        assert parents is True
        assert exist_ok is True

    monkeypatch.setattr(paths.Path, "mkdir", fake_mkdir)

    assert paths.get_toolkit_home() == expected_home_candidate
    assert calls == [expected_home_candidate]


def test_get_toolkit_home_ignores_empty_env_candidates(paths, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_cwd = tmp_path / "cwd"
    expected_home_candidate = fake_home / ".terminal-toolkit"
    calls = []

    monkeypatch.setenv("TERMINAL_TOOLKIT_HOME", "")
    monkeypatch.setattr(paths.Path, "home", lambda: fake_home)
    monkeypatch.setattr(paths.Path, "cwd", lambda: fake_cwd)

    def fake_mkdir(self, parents=False, exist_ok=False):
        calls.append(self)
        assert parents is True
        assert exist_ok is True

    monkeypatch.setattr(paths.Path, "mkdir", fake_mkdir)

    assert paths.get_toolkit_home() == expected_home_candidate
    assert calls == [expected_home_candidate]


def test_get_toolkit_home_does_not_evaluate_later_candidates_after_success(paths, monkeypatch, tmp_path):
    custom_home = tmp_path / "custom-home"
    calls = []

    monkeypatch.setenv("TERMINAL_TOOLKIT_HOME", str(custom_home))
    monkeypatch.setattr(
        paths.Path,
        "home",
        lambda: (_ for _ in ()).throw(AssertionError("Path.home evaluated")),
    )
    monkeypatch.setattr(
        paths.Path,
        "cwd",
        lambda: (_ for _ in ()).throw(AssertionError("Path.cwd evaluated")),
    )

    def fake_mkdir(self, parents=False, exist_ok=False):
        calls.append(self)
        assert self == custom_home
        assert parents is True
        assert exist_ok is True

    monkeypatch.setattr(paths.Path, "mkdir", fake_mkdir)

    assert paths.get_toolkit_home() == custom_home
    assert calls == [custom_home]


def test_get_toolkit_home_raises_after_all_candidates_fail(paths, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_cwd = tmp_path / "cwd"
    calls = []

    monkeypatch.delenv("TERMINAL_TOOLKIT_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: fake_home)
    monkeypatch.setattr(paths.Path, "cwd", lambda: fake_cwd)

    expected = [
        fake_home / ".terminal-toolkit",
        fake_cwd / ".terminal-toolkit",
        Path("/tmp") / ".terminal-toolkit",
    ]

    def fake_mkdir(self, parents=False, exist_ok=False):
        calls.append(self)
        assert parents is True
        assert exist_ok is True
        raise OSError("candidate unavailable")

    monkeypatch.setattr(paths.Path, "mkdir", fake_mkdir)

    with pytest.raises(OSError, match="Unable to create a writable toolkit home directory"):
        paths.get_toolkit_home()

    assert calls == expected


def test_get_subprocess_home_uses_fallback_toolkit_home(paths, monkeypatch, tmp_path):
    custom_file = tmp_path / "custom-home-is-a-file"
    fallback_home = tmp_path / "fallback-home"
    custom_file.write_text("not a directory")

    monkeypatch.setenv("TERMINAL_TOOLKIT_HOME", str(custom_file))
    monkeypatch.setenv("TERMINAL_TOOLKIT_ISOLATE_HOME", "1")
    monkeypatch.delenv("TERMINAL_TOOLKIT_SUBPROCESS_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: fallback_home)

    subprocess_home = paths.get_subprocess_home()

    assert subprocess_home == str(fallback_home / ".terminal-toolkit" / "home")
    assert Path(subprocess_home).is_dir()


def test_get_subprocess_home_prefers_explicit_subprocess_home(paths, monkeypatch, tmp_path):
    explicit_home = tmp_path / "explicit-subprocess-home"
    unused_toolkit_home = tmp_path / "unused-toolkit-home-is-a-file"
    unused_toolkit_home.write_text("not a directory")

    monkeypatch.setenv("TERMINAL_TOOLKIT_SUBPROCESS_HOME", str(explicit_home))
    monkeypatch.setenv("TERMINAL_TOOLKIT_ISOLATE_HOME", "1")
    monkeypatch.setenv("TERMINAL_TOOLKIT_HOME", str(unused_toolkit_home))

    subprocess_home = paths.get_subprocess_home()

    assert subprocess_home == str(explicit_home)
    assert explicit_home.is_dir()

import json

from agent_tools import hermes_shell_adapter


def test_adapter_forwards_task_id_to_hermes(monkeypatch, tmp_path):
    calls = []

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "ok", "exit_code": 0, "error": None})

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(hermes_shell_adapter, "run_terminal", fake_run_terminal)

    result = hermes_shell_adapter.run_foreground_command(
        "python -c \"print('ok')\"",
        workdir=str(tmp_path),
        task_id="lg_abc123",
    )

    assert result["exit_code"] == 0
    assert calls[0]["task_id"] == "lg_abc123"


def test_adapter_replaces_empty_task_id_with_default(monkeypatch, tmp_path):
    calls = []

    def fake_run_terminal(**kwargs):
        calls.append(kwargs)
        return json.dumps({"output": "ok", "exit_code": 0, "error": None})

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(hermes_shell_adapter, "run_terminal", fake_run_terminal)

    result = hermes_shell_adapter.run_foreground_command(
        "python -c \"print('ok')\"",
        workdir=str(tmp_path),
        task_id="",
    )

    assert result["exit_code"] == 0
    assert calls[0]["task_id"] == "default"

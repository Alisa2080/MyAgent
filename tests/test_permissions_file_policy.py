from pathlib import Path


def test_workspace_write_is_allowed(tmp_path, monkeypatch):
    import agent_core.permissions.file_policy as policy

    monkeypatch.setattr(policy, "WORKDIR", tmp_path)

    decision = policy.classify_file_write("notes.txt", task_id="task-local")

    assert decision.outcome == "allow"
    assert decision.reason == "workspace_write"


def test_ordinary_workspace_escape_requires_review(tmp_path, monkeypatch):
    import agent_core.permissions.file_policy as policy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(policy, "WORKDIR", workspace)

    decision = policy.classify_file_write(str(tmp_path / "report.txt"), task_id="task-local")

    assert decision.outcome == "review"
    assert decision.reason == "writes_outside_workspace"
    assert "writes_outside_workspace" in decision.risk_tags


def test_sensitive_host_path_is_denied(tmp_path, monkeypatch):
    import agent_core.permissions.file_policy as policy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(policy, "WORKDIR", workspace)
    monkeypatch.setattr(policy.Path, "home", lambda: home)

    decision = policy.classify_file_write(str(home / ".ssh" / "id_rsa"), task_id="task-local")

    assert decision.outcome == "deny"
    assert decision.reason == "sensitive_path"
    assert "sensitive_path" in decision.risk_tags


def test_sensitive_container_path_is_denied():
    from agent_core.permissions.file_policy import classify_file_write

    decision = classify_file_write("/root/.aws/credentials", task_id="task-docker")

    assert decision.outcome == "deny"
    assert decision.reason == "sensitive_path"
    assert "sensitive_path" in decision.risk_tags


def test_low_level_sensitive_system_paths_are_denied():
    from agent_core.permissions.file_policy import classify_file_write

    for path in (
        "/boot/vmlinuz",
        "/usr/lib/systemd/system/example.service",
        "/private/etc/hosts",
        "/private/var/db/example",
    ):
        decision = classify_file_write(path, task_id="task-local")

        assert decision.outcome == "deny"
        assert decision.reason == "sensitive_path"
        assert "sensitive_path" in decision.risk_tags


def test_docker_workspace_path_is_allowed(monkeypatch):
    import agent_core.permissions.file_policy as policy

    monkeypatch.setattr(policy, "allowed_workspace_roots_for_task", lambda task_id: ["/workspace"])
    monkeypatch.setattr(policy, "resolve_path_for_policy", lambda path, task_id: path)

    decision = policy.classify_file_write("/workspace/app.py", task_id="task-docker")

    assert decision.outcome == "allow"
    assert decision.reason == "workspace_write"


def test_non_local_helper_roots_do_not_allow_host_workspace(tmp_path, monkeypatch):
    import agent_core.permissions.file_policy as policy

    host_path = tmp_path / "probe.txt"
    monkeypatch.setattr(policy, "WORKDIR", tmp_path)
    monkeypatch.setattr(policy, "allowed_workspace_roots_for_task", lambda task_id: ["/workspace"])
    monkeypatch.setattr(policy, "resolve_path_for_policy", lambda path, task_id: str(host_path))

    decision = policy.classify_file_write("probe.txt", task_id="task-docker")

    assert decision.outcome == "review"
    assert decision.reason == "writes_outside_workspace"
    assert "writes_outside_workspace" in decision.risk_tags

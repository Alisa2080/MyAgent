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


def test_github_cli_credential_paths_are_denied(tmp_path, monkeypatch):
    import agent_core.permissions.file_policy as policy

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(policy.Path, "home", lambda: home)

    host_decision = policy.classify_file_write(str(home / ".config" / "gh" / "hosts.yml"), task_id="task-local")
    container_decision = policy.classify_file_write("/root/.config/gh/hosts.yml", task_id="task-docker")

    assert host_decision.outcome == "deny"
    assert container_decision.outcome == "deny"
    assert "sensitive_path" in host_decision.risk_tags
    assert "sensitive_path" in container_decision.risk_tags


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


def test_backend_realpath_escape_to_sensitive_path_is_denied(monkeypatch):
    import agent_core.permissions.file_policy as policy

    monkeypatch.setattr(policy, "allowed_workspace_roots_for_task", lambda task_id: ["/workspace"])

    decision = policy.classify_file_write(
        "/workspace/link/id_rsa",
        task_id="task-docker",
        resolved_path="/root/.ssh/id_rsa",
    )

    assert decision.outcome == "deny"
    assert "sensitive_path" in decision.risk_tags


def test_backend_realpath_escape_to_ordinary_path_requires_review(monkeypatch):
    import agent_core.permissions.file_policy as policy

    monkeypatch.setattr(policy, "allowed_workspace_roots_for_task", lambda task_id: ["/workspace"])

    decision = policy.classify_file_write(
        "/workspace/link/report.txt",
        task_id="task-docker",
        resolved_path="/tmp/report.txt",
    )

    assert decision.outcome == "review"
    assert "writes_outside_workspace" in decision.risk_tags


def test_unresolved_backend_realpath_is_denied():
    import agent_core.permissions.file_policy as policy

    decision = policy.classify_file_write(
        "/workspace/link/report.txt",
        task_id="task-docker",
        resolved_path=policy.UNRESOLVED_BACKEND_WRITE_PATH,
    )

    assert decision.outcome == "deny"
    assert "path_resolution_failed" in decision.risk_tags


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

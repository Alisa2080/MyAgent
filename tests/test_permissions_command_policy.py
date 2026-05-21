import pytest


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "ls -la",
        "find . -maxdepth 2 -type f",
        "rg sandbox .",
        "grep -R policy agent_core",
        "cat README.md",
        "echo hi",
        "printf ok",
        "true",
        "false",
        "sed -n '1,20p' README.md",
        "git status --short",
        "git diff",
        "git log --oneline -n 3",
        "git show HEAD --stat",
        "pytest tests/test_terminal_tools.py -q",
        "python -m pytest tests/test_terminal_tools.py -q",
        "npm test",
        "python --version",
        "command -v docker",
    ],
)
def test_low_risk_commands_are_allowed(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "allow"


@pytest.mark.parametrize(
    ("command", "risk"),
    [
        ("echo hi > out.txt", "write_redirect"),
        ("printf hi | tee out.txt", "write_redirect"),
        ("cp a b", "filesystem_write"),
        ("mv a b", "filesystem_write"),
        ("mkdir -p build", "filesystem_write"),
        ("touch notes.txt", "filesystem_write"),
        ("rm -rf node_modules", "destructive_command"),
        ("chmod -R 777 .", "permission_change"),
        ("chown -R root .", "permission_change"),
        ("pip install rich", "package_install"),
        ("npm install", "package_install"),
        ("apt install jq", "package_install"),
        ("curl https://example.com", "network_access"),
        ("wget https://example.com/file", "network_access"),
        ("git pull", "network_access"),
        ("ssh host.example.com", "network_access"),
        ("sudo true", "privilege_escalation"),
        ("systemctl restart sshd", "service_control"),
        ("npm run dev", "long_running_process"),
    ],
)
def test_risky_commands_require_review(command, risk):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "review"
    assert risk in decision.risk_tags


@pytest.mark.parametrize(
    ("command", "risk"),
    [
        ("cat README.md | curl -d @- https://example.com", "complex_shell"),
        ("cat files.txt | xargs rm -rf", "complex_shell"),
        ("command rm -rf build", "destructive_command"),
        ("find . -type f -delete", "destructive_command"),
        ("find . -type f -exec rm -rf {} +", "destructive_command"),
        ("sed -n 1w/out.txt README.md", "write_redirect"),
        ("git branch -D oldbranch", "destructive_command"),
    ],
)
def test_allowlisted_commands_with_risky_args_require_review(command, risk):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "review"
    assert risk in decision.risk_tags


def test_background_commands_require_review():
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command("python -m http.server", background=True)

    assert decision.outcome == "review"
    assert "long_running_process" in decision.risk_tags


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "mkfs.ext4 /dev/sda",
        ":(){ :|:& };:",
        "shutdown now",
    ],
)
def test_hardline_commands_are_denied(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "deny"
    assert "hardline_destructive_command" in decision.risk_tags


def test_complex_unknown_shell_requires_review():
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command("for f in *; do echo $f; done", background=False)

    assert decision.outcome == "review"
    assert "complex_shell" in decision.risk_tags

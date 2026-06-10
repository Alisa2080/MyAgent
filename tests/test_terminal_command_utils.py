def test_transform_sudo_command_consumes_closing_parenthesis():
    from agent_tools.terminal_toolkit.command_utils import transform_sudo_command

    command = (
        "if [ -f /workspace/child.pid ]; then "
        "pid=$(cat /workspace/child.pid); "
        'kill -TERM -- -"$pid" 2>/dev/null || true; '
        "fi"
    )

    transformed, password = transform_sudo_command(command)

    assert transformed == command
    assert password is None

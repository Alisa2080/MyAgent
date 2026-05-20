from agent_tools.file_toolkit.file_operations import ShellFileOperations


class RecordingEnv:
    cwd = "/workspace"

    def __init__(self, responses):
        self.responses = list(responses)
        self.commands = []

    def execute(self, command, cwd=None, **kwargs):
        self.commands.append((command, cwd, kwargs))
        return self.responses.pop(0)


def test_stat_mtime_uses_backend_shell_and_parses_epoch_seconds():
    env = RecordingEnv([{"output": "1716200000\n", "returncode": 0}])
    file_ops = ShellFileOperations(env)

    assert file_ops.stat_mtime("/workspace/notes.txt") == 1716200000.0

    command, cwd, kwargs = env.commands[0]
    assert "stat -c '%Y %y'" in command
    assert "stat -f '%m'" in command
    assert "'/workspace/notes.txt'" in command
    assert cwd == "/workspace"
    assert kwargs == {"timeout": 10}


def test_stat_mtime_parses_fractional_gnu_output():
    env = RecordingEnv(
        [
            {
                "output": "1716200000 2024-05-20 12:26:40.123456789 +0000\n",
                "returncode": 0,
            }
        ]
    )
    file_ops = ShellFileOperations(env)

    assert file_ops.stat_mtime("/workspace/notes.txt") == 1716200000.123456789


def test_stat_mtime_returns_none_when_backend_stat_fails():
    env = RecordingEnv([{"output": "", "returncode": 1}])
    file_ops = ShellFileOperations(env)

    assert file_ops.stat_mtime("/workspace/missing.txt") is None


def test_stat_mtime_expands_tilde_on_backend():
    env = RecordingEnv(
        [
            {"output": "/home/remote\n", "returncode": 0},
            {"output": "1716200123\n", "returncode": 0},
        ]
    )
    file_ops = ShellFileOperations(env)

    assert file_ops.stat_mtime("~/notes.txt") == 1716200123.0

    stat_command = env.commands[1][0]
    assert "'/home/remote/notes.txt'" in stat_command

import pytest

from agent_tools.terminal_toolkit.command_utils import rewrite_compound_background


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "python -m pip install -e . && python -m http.server &",
            "python -m pip install -e . && { python -m http.server & }",
        ),
        (
            "cd /tmp && python3 -m http.server 8123 > /tmp/server.log 2>&1 &",
            "cd /tmp && { python3 -m http.server 8123 > /tmp/server.log 2>&1 & }",
        ),
        (
            "test -f app.py || python -m http.server &",
            "test -f app.py || { python -m http.server & }",
        ),
        (
            "setup && server_one &\nprepare || server_two &",
            "setup && { server_one & }\nprepare || { server_two & }",
        ),
        ("cd web; npm run dev &", "cd web; npm run dev &"),
        ("python -m http.server &", "python -m http.server &"),
        (
            "printf 'a && b &'\npython -m http.server &",
            "printf 'a && b &'\npython -m http.server &",
        ),
        ('printf "a && b &"', 'printf "a && b &"'),
        (
            "echo ok && { python -m http.server & }",
            "echo ok && { python -m http.server & }",
        ),
        (
            "echo ok && python -m http.server > server.log &",
            "echo ok && { python -m http.server > server.log & }",
        ),
        (
            "echo ok && python -m http.server &> server.log",
            "echo ok && python -m http.server &> server.log",
        ),
        (
            "echo ok && python -m http.server 2>&1",
            "echo ok && python -m http.server 2>&1",
        ),
        (
            "echo ok && python -m http.server & # keep serving",
            "echo ok && { python -m http.server & } # keep serving",
        ),
        (
            "echo ok && python -m http.server &# keep serving",
            "echo ok && { python -m http.server & } # keep serving",
        ),
        (
            "echo ok && python -m http.server &\necho done",
            "echo ok && { python -m http.server & }\necho done",
        ),
        (
            "echo ok | grep ok && python -m http.server &",
            "echo ok | grep ok && { python -m http.server & }",
        ),
        (
            "echo ok && (python -m http.server &)",
            "echo ok && (python -m http.server &)",
        ),
        (
            "echo $(setup && python -m http.server &)",
            "echo $(setup && python -m http.server &)",
        ),
        (
            "echo ok && echo done | python -m http.server &",
            "echo ok && echo done | python -m http.server &",
        ),
        (
            "echo ok && python -m http.server & echo done",
            "echo ok && python -m http.server & echo done",
        ),
    ],
)
def test_rewrite_compound_background(command, expected):
    assert rewrite_compound_background(command) == expected


def test_rewrite_compound_background_is_idempotent():
    command = "echo ok && { python -m http.server & }"

    assert rewrite_compound_background(rewrite_compound_background(command)) == command

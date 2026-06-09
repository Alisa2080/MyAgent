from __future__ import annotations

import io


def test_terminal_observer_streams_tokens_and_marks_output():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ModelStartEvent, TokenDeltaEvent, TurnCompleteEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream)

    observer.emit(ModelStartEvent(thread_id="t1"))
    observer.emit(TokenDeltaEvent("hello", thread_id="t1"))
    observer.emit(TokenDeltaEvent(" world", thread_id="t1"))
    observer.emit(TurnCompleteEvent(thread_id="t1", streamed_output=True))

    output = stream.getvalue()
    assert "waiting for model..." in output
    assert "hello world" in output
    assert observer.streamed_output is True


def test_terminal_observer_formats_terminal_tool_summary():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ToolCompleteEvent, ToolStartEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream)
    result = {
        "stdout": "\nfirst useful line\nsecond line",
        "stderr": "",
        "exit_code": 0,
    }

    observer.emit(ToolStartEvent("terminal", {"command": "rg submit_message agent_cli"}, "call-1"))
    observer.emit(ToolCompleteEvent("terminal", {"command": "rg submit_message agent_cli"}, result, 1234, "call-1"))

    output = stream.getvalue()
    assert '> terminal: rg submit_message agent_cli' in output
    assert '< terminal done 1.2s' in output
    assert 'first useful line' in output


def test_terminal_observer_hides_write_file_content():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ToolCompleteEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream)

    observer.emit(
        ToolCompleteEvent(
            "write_file",
            {"path": "agent_cli/repl.py", "content": "secret long content"},
            "wrote 2000 bytes",
            5,
            "call-1",
        )
    )

    output = stream.getvalue()
    assert "agent_cli/repl.py" in output
    assert "secret long content" not in output


def test_terminal_observer_caps_progress_lines():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ToolStartEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream, max_progress_lines=2)

    observer.emit(ToolStartEvent("terminal", {"command": "one"}, "1"))
    observer.emit(ToolStartEvent("terminal", {"command": "two"}, "2"))
    observer.emit(ToolStartEvent("terminal", {"command": "three"}, "3"))
    observer.emit(ToolStartEvent("terminal", {"command": "four"}, "4"))

    output = stream.getvalue()
    assert "> terminal: one" in output
    assert "> terminal: two" in output
    assert "... more progress hidden" in output
    assert "four" not in output


def test_terminal_observer_defaults_to_stderr(monkeypatch):
    import sys

    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ModelStartEvent

    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)

    observer = TerminalProgressObserver()
    observer.emit(ModelStartEvent(thread_id="thread-1"))

    assert "waiting for model..." in stderr.getvalue()


def test_terminal_observer_separates_progress_after_partial_token():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import TokenDeltaEvent, ToolStartEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream)

    observer.emit(TokenDeltaEvent("partial"))
    observer.emit(ToolStartEvent("terminal", {"command": "pwd"}, "call"))

    output = stream.getvalue()
    assert "partial\n> terminal: pwd" in output
    assert "partial> terminal" not in output


def test_terminal_observer_hides_patch_content_on_start():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ToolStartEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream)

    observer.emit(ToolStartEvent("patch", {"mode": "patch", "patch": "secret patch body"}, "call"))

    output = stream.getvalue()
    assert "secret patch body" not in output
    assert "patch content hidden" in output


def test_terminal_observer_hides_patch_result_body_on_complete():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ToolCompleteEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream)

    observer.emit(ToolCompleteEvent("patch", {"mode": "patch"}, "SECRET_RESULT_BODY", 1, "call"))

    output = stream.getvalue()
    assert "SECRET_RESULT_BODY" not in output
    assert "patch content hidden" in output


def test_terminal_observer_hides_write_file_result_body_without_path():
    from agent_cli.progress import TerminalProgressObserver
    from agent_core.progress import ToolCompleteEvent

    stream = io.StringIO()
    observer = TerminalProgressObserver(stream=stream)

    observer.emit(ToolCompleteEvent("write_file", {"mode": "write"}, "SECRET_WRITE_BODY", 1, "call"))

    output = stream.getvalue()
    assert "SECRET_WRITE_BODY" not in output
    assert "write content hidden" in output

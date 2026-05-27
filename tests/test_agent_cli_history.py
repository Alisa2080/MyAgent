from types import SimpleNamespace

from agent_cli.history import (
    TranscriptMessage,
    content_to_text,
    filter_transcript_messages,
    render_history_markdown,
    render_history_text,
    transcript_stats,
)


def test_content_to_text_handles_strings_dicts_and_text_blocks():
    assert content_to_text("hello") == "hello"
    assert content_to_text({"text": "hello"}) == "hello"
    assert content_to_text([{"type": "text", "text": "hello"}, "world"]) == "hello\nworld"


def test_filter_transcript_messages_supports_dicts_and_objects():
    messages = [
        {"role": "system", "content": "ignore"},
        {"role": "user", "content": "hi"},
        SimpleNamespace(type="ai", content=[{"type": "text", "text": "hello"}]),
        {"role": "tool", "content": "ignore"},
    ]

    transcript = filter_transcript_messages(messages)

    assert transcript == [
        TranscriptMessage(role="user", content="hi"),
        TranscriptMessage(role="assistant", content="hello"),
    ]


def test_transcript_stats_counts_messages_and_user_turns():
    transcript = [
        TranscriptMessage(role="user", content="one"),
        TranscriptMessage(role="assistant", content="two"),
        TranscriptMessage(role="user", content="three"),
    ]

    stats = transcript_stats(transcript)

    assert stats.message_count == 3
    assert stats.turn_count == 2


def test_render_history_text_uses_filtered_roles():
    transcript = [
        TranscriptMessage(role="user", content="hi"),
        TranscriptMessage(role="assistant", content="hello"),
    ]

    output = render_history_text(transcript)

    assert "User:" in output
    assert "Assistant:" in output
    assert "hello" in output


def test_render_history_markdown_includes_metadata():
    transcript = [TranscriptMessage(role="user", content="hi")]

    output = render_history_markdown(
        transcript,
        session_id="s1",
        title="Title",
        workdir="/repo",
        model="model",
        exported_at="2026-05-27T00:00:00+00:00",
    )

    assert "# Session s1" in output
    assert "- Title: Title" in output
    assert "- Workdir: /repo" in output
    assert "- Model: model" in output
    assert "- Messages: 1" in output
    assert "- Turns: 1" in output
    assert "## User" in output

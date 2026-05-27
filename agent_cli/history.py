from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TranscriptMessage:
    role: str
    content: str


@dataclass(frozen=True)
class TranscriptStats:
    message_count: int
    turn_count: int


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or message.get("type") or "")
    return str(getattr(message, "role", "") or getattr(message, "type", ""))


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", "")


def normalize_role(role: str) -> str | None:
    value = role.lower()
    if value in {"user", "human"}:
        return "user"
    if value in {"assistant", "ai"}:
        return "assistant"
    return None


def content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [content_to_text(item) for item in content]
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("content"), str):
            return content["content"]
        return ""
    return str(content)


def filter_transcript_messages(messages: list[Any]) -> list[TranscriptMessage]:
    transcript: list[TranscriptMessage] = []
    for message in messages:
        role = normalize_role(_message_role(message))
        if role is None:
            continue
        text = content_to_text(_message_content(message))
        if text:
            transcript.append(TranscriptMessage(role=role, content=text))
    return transcript


def transcript_stats(transcript: list[TranscriptMessage]) -> TranscriptStats:
    return TranscriptStats(
        message_count=len(transcript),
        turn_count=sum(1 for message in transcript if message.role == "user"),
    )


def render_history_text(transcript: list[TranscriptMessage]) -> str:
    if not transcript:
        return "No messages in session history.\n"
    lines = ["Session History:", ""]
    for message in transcript:
        label = "User" if message.role == "user" else "Assistant"
        lines.append(f"{label}:")
        lines.append(message.content)
        lines.append("")
    return "\n".join(lines)


def render_history_markdown(
    transcript: list[TranscriptMessage],
    *,
    session_id: str,
    title: str,
    workdir: str,
    model: str | None,
    exported_at: str,
) -> str:
    stats = transcript_stats(transcript)
    lines = [
        f"# Session {session_id}",
        "",
        f"- Title: {title}",
        f"- Workdir: {workdir}",
        f"- Model: {model or 'default'}",
        f"- Exported at: {exported_at}",
        f"- Messages: {stats.message_count}",
        f"- Turns: {stats.turn_count}",
        "",
    ]
    for message in transcript:
        heading = "User" if message.role == "user" else "Assistant"
        lines.extend([f"## {heading}", "", message.content, ""])
    return "\n".join(lines)


def load_thread_transcript(checkpointer: Any, thread_id: str) -> list[TranscriptMessage]:
    from agent_cli.checkpoints import extract_messages_from_checkpoints

    return filter_transcript_messages(
        extract_messages_from_checkpoints(checkpointer, thread_id)
    )

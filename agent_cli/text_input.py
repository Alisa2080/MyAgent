from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse


IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg", ".ico"}
)

PASTE_REFERENCE_RE = re.compile(r"\[Pasted text #\d+: \d+ lines -> (.+?)\]")
_DSR_CPR_ESC_RE = re.compile(r"\x1b\[\d+;\d+R")
_DSR_CPR_VISIBLE_RE = re.compile(r"\^\[\[\d+;\d+R")


@dataclass(frozen=True)
class PasteCollapse:
    collapsed: bool
    placeholder: str
    path: Path | None = None


@dataclass(frozen=True)
class FileDrop:
    path: Path
    remainder: str
    is_image: bool


def sanitize_terminal_input(text: str) -> str:
    if not text:
        return text
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = (
        value.replace("\x1b[200~", "")
        .replace("\x1b[201~", "")
        .replace("^[[200~", "")
        .replace("^[[201~", "")
    )
    value = re.sub(r"(^|[\s\n>:\]\)])\[200~", r"\1", value)
    value = re.sub(r"\[201~(?=$|[\s\n<\[\(\):;.,!?])", "", value)
    value = re.sub(r"(^|[\s\n>:\]\)])00~", r"\1", value)
    value = re.sub(r"01~(?=$|[\s\n<\[\(\):;.,!?])", "", value)
    value = _DSR_CPR_ESC_RE.sub("", value)
    value = _DSR_CPR_VISIBLE_RE.sub("", value)
    return value


def should_collapse_paste(text: str, *, min_lines: int = 5) -> bool:
    sanitized = sanitize_terminal_input(text)
    if sanitized.lstrip().startswith("/"):
        return False
    return len(sanitized.splitlines()) >= min_lines


def collapse_large_paste(text: str, *, cli_home: str | Path, counter: int) -> PasteCollapse:
    sanitized = sanitize_terminal_input(text)
    if not should_collapse_paste(sanitized):
        return PasteCollapse(collapsed=False, placeholder=sanitized)
    paste_dir = Path(cli_home) / "pastes"
    paste_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    path = paste_dir / f"paste_{counter}_{stamp}.txt"
    path.write_text(sanitized, encoding="utf-8")
    line_count = len(sanitized.splitlines())
    placeholder = f"[Pasted text #{counter}: {line_count} lines -> {path}]"
    return PasteCollapse(collapsed=True, placeholder=placeholder, path=path)


def _is_allowed_paste_path(path: Path, *, cli_home: str | Path | None) -> bool:
    if cli_home is None:
        return False
    try:
        resolved = path.resolve()
        paste_dir = (Path(cli_home) / "pastes").resolve()
        resolved.relative_to(paste_dir)
    except (OSError, ValueError):
        return False
    return bool(re.fullmatch(r"paste_\d+_\d{6}\.txt", resolved.name))


def expand_paste_references(text: str, *, cli_home: str | Path | None = None) -> str:
    def replace(match: re.Match[str]) -> str:
        path = Path(match.group(1))
        if not _is_allowed_paste_path(path, cli_home=cli_home):
            return match.group(0)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return match.group(0)

    return PASTE_REFERENCE_RE.sub(replace, text)


def split_path_input(raw: str) -> tuple[str, str]:
    value = str(raw or "").strip()
    if not value:
        return "", ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        pos = 1
        while pos < len(value):
            ch = value[pos]
            if ch == "\\" and pos + 1 < len(value):
                pos += 2
                continue
            if ch == quote:
                return value[1:pos], value[pos + 1 :].strip()
            pos += 1
        return value[1:], ""
    pos = 0
    while pos < len(value):
        ch = value[pos]
        if ch == "\\" and pos + 1 < len(value) and value[pos + 1] == " ":
            pos += 2
        elif ch == " ":
            break
        else:
            pos += 1
    return value[:pos].replace("\\ ", " "), value[pos:].strip()


def resolve_file_path(raw_path: str, *, workdir: str | Path) -> Path | None:
    token = str(raw_path or "").strip()
    if not token:
        return None
    if (token.startswith('"') and token.endswith('"')) or (
        token.startswith("'") and token.endswith("'")
    ):
        token = token[1:-1].strip()
    token = token.replace("\\ ", " ")
    expanded = token
    if token.startswith("file://"):
        parsed = urlparse(token)
        expanded = unquote(parsed.path or "")
        if parsed.netloc and os.name == "nt":
            expanded = f"//{parsed.netloc}{expanded}"
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    path = Path(expanded)
    if not path.is_absolute():
        path = Path(workdir) / path
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if not resolved.exists() or not resolved.is_file():
        return None
    return resolved


def _starts_like_path(value: str) -> bool:
    return (
        value.startswith("/")
        or value.startswith("~")
        or value.startswith("./")
        or value.startswith("../")
        or value.startswith("file://")
        or value.startswith('"')
        or value.startswith("'")
        or bool(re.match(r"^[^\s/]+(?:\\ |\.[A-Za-z0-9])[^\s]*", value))
    )


def detect_file_drop(text: str, *, workdir: str | Path) -> FileDrop | None:
    value = sanitize_terminal_input(str(text or "")).strip()
    if not value or not _starts_like_path(value):
        return None
    direct = resolve_file_path(value, workdir=workdir)
    if direct is not None:
        return FileDrop(direct, "", direct.suffix.lower() in IMAGE_EXTENSIONS)
    token, remainder = split_path_input(value)
    path = resolve_file_path(token, workdir=workdir)
    if path is None and " " in value and value[0] not in {"'", '"'}:
        for pos in [idx for idx, ch in enumerate(value) if ch == " "][::-1]:
            candidate = value[:pos].rstrip()
            resolved = resolve_file_path(candidate, workdir=workdir)
            if resolved is not None:
                path = resolved
                remainder = value[pos + 1 :].strip()
                break
    if path is None:
        return None
    return FileDrop(path, remainder, path.suffix.lower() in IMAGE_EXTENSIONS)


def format_file_reference_message(drop: FileDrop) -> str:
    reference = f"[User referenced file: {drop.path}]"
    if drop.remainder:
        return f"{reference}\n{drop.remainder}"
    return reference


def prepare_user_message(
    text: str, *, workdir: str | Path, cli_home: str | Path | None = None
) -> str:
    value = expand_paste_references(
        sanitize_terminal_input(text), cli_home=cli_home
    ).strip()
    drop = detect_file_drop(value, workdir=workdir)
    if drop is not None:
        return format_file_reference_message(drop)
    return value


def format_osc52(text: str) -> str:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\x1b]52;c;{payload}\x07"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    word_count = len(text.split())
    return max(1, (word_count + 3) // 4)


def render_usage_summary(
    *,
    session_id: str,
    model_name: str | None,
    message_count: int,
    turn_count: int,
    transcript_text: str,
    checkpointer_available: bool,
    last_call_elapsed_seconds: float | None,
    assistant_reply_count: int,
    usage_metadata: dict[str, object] | None = None,
) -> str:
    lines = [
        "Usage:",
        f"  Session ID: {session_id}",
        f"  Model: {model_name or 'default'}",
        f"  Messages: {message_count}",
        f"  Turns: {turn_count}",
        f"  Estimated tokens: {estimate_tokens(transcript_text)}",
        f"  Checkpointer: {'available' if checkpointer_available else 'unavailable'}",
        f"  Checkpoint message bytes: {len(transcript_text.encode('utf-8'))}",
        f"  Assistant replies tracked: {assistant_reply_count}",
    ]
    if last_call_elapsed_seconds is None:
        lines.append("  Last call: n/a")
    else:
        lines.append(f"  Last call: {last_call_elapsed_seconds:.3f}s")
    if usage_metadata:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage_metadata:
                label = key.replace("_", " ").title()
                lines.append(f"  {label}: {usage_metadata[key]}")
    return "\n".join(lines)

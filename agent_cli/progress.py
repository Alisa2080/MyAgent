from __future__ import annotations

import json
import sys
from typing import Any, Mapping, TextIO

from agent_core.progress import (
    FallbackEvent,
    ModelStartEvent,
    ProgressEvent,
    ProgressHiddenEvent,
    TokenDeltaEvent,
    ToolCompleteEvent,
    ToolErrorEvent,
    ToolStartEvent,
    TurnCompleteEvent,
)


DEFAULT_MAX_SUMMARY_CHARS = 160
DEFAULT_MAX_PROGRESS_LINES = 80


class TerminalProgressObserver:
    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
        max_progress_lines: int = DEFAULT_MAX_PROGRESS_LINES,
    ) -> None:
        self.stream = stream or sys.stderr
        self.max_summary_chars = max_summary_chars
        self.max_progress_lines = max_progress_lines
        self.progress_lines = 0
        self.hidden_count = 0
        self.hidden_notice_printed = False
        self.streamed_output = False
        self._token_started = False
        self._token_at_line_start = True

    def emit(self, event: ProgressEvent) -> None:
        if isinstance(event, ModelStartEvent):
            self._write_progress("waiting for model...")
        elif isinstance(event, TokenDeltaEvent):
            self._write_token(event.text)
        elif isinstance(event, ToolStartEvent):
            self._write_progress(
                f"> {event.tool_name}: {format_tool_args(event.tool_name, event.args, self.max_summary_chars)}"
            )
        elif isinstance(event, ToolCompleteEvent):
            status = "blocked" if event.blocked else "done"
            self._write_progress(f"< {event.tool_name} {status} {_format_duration(event.duration_ms)}")
            summary = summarize_tool_result(
                event.tool_name,
                event.args,
                event.result,
                self.max_summary_chars,
            )
            if summary:
                self._write_progress(f"  {summary}")
        elif isinstance(event, ToolErrorEvent):
            reason = _truncate(event.error_message, self.max_summary_chars)
            self._write_progress(
                f"< {event.tool_name} failed {_format_duration(event.duration_ms)}: {reason}"
            )
        elif isinstance(event, TurnCompleteEvent):
            if event.streamed_output:
                self.streamed_output = True
            self.flush()
        elif isinstance(event, FallbackEvent):
            self._write_progress(f"waiting for model... ({event.reason})")
        elif isinstance(event, ProgressHiddenEvent):
            self._write_progress(f"... {event.hidden_count} more progress events hidden")

    def flush(self) -> None:
        try:
            self.stream.flush()
        except Exception:
            pass

    def _write_token(self, text: str) -> None:
        if not text:
            return
        if not self._token_started:
            self._token_started = True
            self.streamed_output = True
            self._raw_write("\n")
        self._raw_write(text)
        self._token_at_line_start = text.endswith("\n")
        self.flush()

    def _write_progress(self, line: str) -> None:
        self._separate_from_partial_token()
        if self.progress_lines >= self.max_progress_lines:
            self.hidden_count += 1
            if not self.hidden_notice_printed:
                self.hidden_notice_printed = True
                self._raw_write("... more progress hidden\n")
            return
        self.progress_lines += 1
        self._raw_write(f"{line}\n")
        self.flush()

    def _raw_write(self, text: str) -> None:
        self.stream.write(text)

    def _separate_from_partial_token(self) -> None:
        if self._token_started and not self._token_at_line_start:
            self._raw_write("\n")
            self._token_at_line_start = True


def format_tool_args(
    tool_name: str,
    args: Mapping[str, Any],
    max_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
) -> str:
    if not isinstance(args, Mapping):
        return ""
    if tool_name in {"write_file", "patch"}:
        path = _path_arg(args)
        if path:
            return _truncate(path, max_chars)
        return f"{tool_name.removesuffix('_file')} content hidden"
    for key in ("command", "path", "query", "pattern", "session_id"):
        value = args.get(key)
        if value:
            return _truncate(str(value), max_chars)
    compact = json.dumps(dict(args), ensure_ascii=False, sort_keys=True, default=str)
    return _truncate(compact, max_chars)


def summarize_tool_result(
    tool_name: str,
    args: Mapping[str, Any],
    result: Any,
    max_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
) -> str:
    if tool_name in {"write_file", "patch"}:
        return _write_summary(tool_name, args, max_chars)
    if tool_name == "terminal":
        return _terminal_summary(result, max_chars)
    if tool_name == "process":
        return _process_summary(result, max_chars)
    if tool_name in {"read_file", "search", "grep", "rg"}:
        return _first_informative_line(_result_text(result), max_chars)
    return ""


def _write_summary(tool_name: str, args: Mapping[str, Any], max_chars: int) -> str:
    path = _path_arg(args)
    if path:
        return _truncate(f"updated {path}", max_chars)
    return f"{tool_name.removesuffix('_file')} content hidden"


def _path_arg(args: Mapping[str, Any]) -> str:
    value = args.get("path") or args.get("file") or args.get("filename")
    if value:
        return str(value)
    paths = args.get("paths")
    if isinstance(paths, (list, tuple)) and paths:
        return ", ".join(str(path) for path in paths)
    if paths:
        return str(paths)
    return ""


def _terminal_summary(result: Any, max_chars: int) -> str:
    if isinstance(result, Mapping):
        text = _first_informative_line(str(result.get("stdout") or ""), max_chars)
        if text:
            return text
        text = _first_informative_line(str(result.get("stderr") or ""), max_chars)
        if text:
            return text
        if "exit_code" in result:
            return f"exit_code={result.get('exit_code')}"
    return _first_informative_line(_result_text(result), max_chars)


def _process_summary(result: Any, max_chars: int) -> str:
    if isinstance(result, Mapping):
        for key in ("status", "session_id", "output"):
            value = result.get(key)
            if value:
                return _truncate(f"{key}={value}", max_chars)
    return _first_informative_line(_result_text(result), max_chars)


def _result_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if content is not None:
        return str(content)
    return "" if result is None else str(result)


def _first_informative_line(text: str, max_chars: int) -> str:
    for line in str(text).splitlines():
        normalized = " ".join(line.split())
        if normalized:
            return _truncate(normalized, max_chars)
    normalized = " ".join(str(text).split())
    return _truncate(normalized, max_chars) if normalized else ""


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 3:
        return text[:max_chars]
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _format_duration(duration_ms: int) -> str:
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    return f"{duration_ms / 1000:.1f}s"

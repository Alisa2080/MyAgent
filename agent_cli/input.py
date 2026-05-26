from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory

from agent_cli.commands import commands_for_completion, resolve_command
from agent_cli.session_store import SessionStore


class SlashCommandCompleter(Completer):
    def __init__(self, *, session_store: SessionStore, workdir: str):
        self.session_store = session_store
        self.workdir = Path(workdir)

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        parts = text.split(maxsplit=1)
        if len(parts) == 1 and not text.endswith(" "):
            word = parts[0][1:].lower()
            for command in commands_for_completion():
                names = (command.name, *command.aliases)
                for name in names:
                    if name.startswith(word):
                        yield Completion(
                            name,
                            start_position=-len(word),
                            display=f"/{name}",
                            display_meta=command.description,
                        )
            return

        command = resolve_command(parts[0])
        if command is None:
            return
        arg = parts[1] if len(parts) > 1 else ""
        if command.completion == "session":
            yield from self._session_completions(arg)
        elif command.completion == "skill":
            yield from self._skill_completions(arg)
        elif command.completion == "path":
            yield from self._path_completions(arg)

    def _session_completions(self, prefix: str) -> Iterable[Completion]:
        for record in self.session_store.list_sessions(limit=50):
            if record.session_id.startswith(prefix):
                yield Completion(
                    record.session_id,
                    start_position=-len(prefix),
                    display=record.session_id,
                    display_meta=record.title,
                )

    def _skill_completions(self, prefix: str) -> Iterable[Completion]:
        try:
            from agent_tools.public.skills import _all_skills
        except Exception:
            return
        prefix_lower = prefix.lower()
        for item in _all_skills():
            name = str(item.get("name") or "")
            if name.lower().startswith(prefix_lower):
                yield Completion(
                    name,
                    start_position=-len(prefix),
                    display=name,
                    display_meta=str(item.get("description") or ""),
                )

    def _path_completions(self, prefix: str) -> Iterable[Completion]:
        raw = prefix or "."
        expanded = Path(os.path.expanduser(raw))
        if not expanded.is_absolute():
            expanded = self.workdir / expanded
        directory = expanded if raw.endswith("/") else expanded.parent
        basename = "" if raw.endswith("/") else expanded.name
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries[:100]:
            if basename and not entry.name.startswith(basename):
                continue
            suffix = "/" if entry.is_dir() else ""
            text = entry.name + suffix
            yield Completion(text, start_position=-len(basename), display=text)


def build_prompt_session(
    *,
    history_path: str | Path,
    session_store: SessionStore,
    workdir: str,
) -> PromptSession:
    path = Path(history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=FileHistory(str(path)),
        completer=SlashCommandCompleter(session_store=session_store, workdir=workdir),
    )

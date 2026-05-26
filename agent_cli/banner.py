from __future__ import annotations

from dataclasses import dataclass, field

from agent_cli.theme import get_theme


@dataclass(frozen=True, init=False)
class BannerContext:
    workdir: str
    profile: str | None
    home: str | None
    model: str | None
    session: str
    background_counts: dict[str, int] = field(default_factory=dict)

    def __init__(
        self,
        *,
        workdir: str,
        profile: str | None,
        background_counts: dict[str, int],
        home: str | None = None,
        model: str | None = None,
        session: str | None = None,
        cli_home: str | None = None,
        model_name: str | None = None,
        session_id: str | None = None,
    ):
        object.__setattr__(self, "workdir", workdir)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "home", home if home is not None else cli_home)
        object.__setattr__(self, "model", model if model is not None else model_name)
        object.__setattr__(
            self, "session", session if session is not None else session_id or ""
        )
        object.__setattr__(self, "background_counts", dict(background_counts))


def _shorten(value: str | None, width: int) -> str:
    text = value or "-"
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return "..." + text[-(width - 3) :]


def _line(label: str, value: str, inner_width: int) -> str:
    content = f" {label:<8} {value}"
    return "│" + content[:inner_width].ljust(inner_width) + "│"


def _section(title: str, inner_width: int) -> str:
    label = f" {title} "
    return "├" + label + "─" * max(0, inner_width - len(label)) + "┤"


def render_banner(
    context: BannerContext, *, width: int, theme_name: str = "default"
) -> str:
    theme = get_theme(theme_name)
    counts = context.background_counts
    if width < 72:
        return "\n".join(
            [
                "Agent CLI",
                f"cwd {context.workdir}",
                f"profile {context.profile or '-'}",
                f"home {context.home or '-'}",
                f"model {context.model or 'default'}",
                f"session {context.session}",
                (
                    "background "
                    f"run={counts.get('running', 0)} "
                    f"wait={counts.get('waiting_approval', 0)} "
                    f"queued={counts.get('queued', 0)} "
                    f"done={counts.get('completed', 0)}"
                ),
            ]
        )

    box_width = min(width, 96)
    inner_width = box_width - 2
    top_label = " Agent CLI "
    top = "┌" + top_label + "─" * max(0, inner_width - len(top_label)) + "┐"
    bottom = "└" + "─" * inner_width + "┘"
    value_width = inner_width - 11
    background = (
        f"RUN {counts.get('running', 0)}   "
        f"WAIT {counts.get('waiting_approval', 0)}   "
        f"QUEUED {counts.get('queued', 0)}   "
        f"DONE {counts.get('completed', 0)}"
    )
    lines = [
        top,
        _line("cwd", _shorten(context.workdir, value_width), inner_width),
        _line("profile", context.profile or "-", inner_width),
        _line("home", _shorten(context.home, value_width), inner_width),
        _line("model", context.model or "default", inner_width),
        _line("session", context.session, inner_width),
        _section("Commands", inner_width),
        _line("", "/background /tasks /steer /stop /approve", inner_width),
        _line("", "/status /history /export /skills /doctor", inner_width),
        _section("Background", inner_width),
        _line("", background, inner_width),
        bottom,
    ]
    if theme.name == "mono":
        return "\n".join(lines)
    return "\n".join(theme.paint(line, theme.border) for line in lines)

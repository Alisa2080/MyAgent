from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    color_enabled: bool
    border: str
    label: str
    success: str
    warning: str
    error: str
    reset: str = "\033[0m"

    def paint(self, text: str, color: str) -> str:
        if not self.color_enabled or not color:
            return text
        return f"{color}{text}{self.reset}"


SUPPORTED_THEMES: dict[str, Theme] = {
    "default": Theme(
        name="default",
        color_enabled=True,
        border="\033[38;5;67m",
        label="\033[38;5;111m",
        success="\033[38;5;71m",
        warning="\033[38;5;178m",
        error="\033[38;5;167m",
    ),
    "mono": Theme(
        name="mono",
        color_enabled=False,
        border="",
        label="",
        success="",
        warning="",
        error="",
    ),
    "slate": Theme(
        name="slate",
        color_enabled=True,
        border="\033[38;5;103m",
        label="\033[38;5;110m",
        success="\033[38;5;109m",
        warning="\033[38;5;179m",
        error="\033[38;5;168m",
    ),
}


def get_theme(name: str | None) -> Theme:
    key = "default" if name is None else name
    try:
        return SUPPORTED_THEMES[key]
    except KeyError as exc:
        allowed = ", ".join(sorted(SUPPORTED_THEMES))
        raise ValueError(f"display.theme must be one of: {allowed}") from exc

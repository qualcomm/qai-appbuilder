"""``apps.cli._render_theme`` — shared Rich theme + icon constants.

Single source of truth for CLI terminal styling (cli-render-redesign plan,
Technical Design decision 4bis / 组件说明). Imported by ``_render.py`` and,
in later delivery steps, by ``_repl.py``/``commands/build.py``/
``commands/app.py`` so ``qai build`` and ``qai app`` share identical colour
and icon semantics instead of drifting independently.
"""

from __future__ import annotations

from typing import TextIO

from rich.console import Console
from rich.theme import Theme

__all__ = ["THEME", "icon", "build_console"]

#: Semantic styles shared by every CLI renderer. Kept intentionally small —
#: one style per meaning (agent speech, tool activity, outcome, severity) so
#: callers never need to hand-roll ANSI combinations.
THEME = Theme(
    {
        "agent": "bold magenta",
        "tool": "cyan",
        "tool.arg": "dim cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "dim": "dim",
        "heading": "bold",
    }
)

#: Emoji glyph per semantic name, used when ``RenderOptions.emoji`` is True.
_EMOJI_ICONS: dict[str, str] = {
    "agent": "🧞",
    "tool": "⚙",
    "success": "✓",
    "error": "✗",
    "warning": "⚠",
    "audio": "🔊",
    "image": "🖼",
    "ocr": "📄",
    "asr": "📝",
    "predict": "🏷",
    "detect": "🎯",
}

#: ASCII fallback per semantic name, used when emoji is disabled (non-TTY).
_PLAIN_ICONS: dict[str, str] = {
    "agent": "",
    "tool": "»",
    "success": "[ok]",
    "error": "[x]",
    "warning": "!",
    "audio": "",
    "image": "",
    "ocr": "",
    "asr": "",
    "predict": "",
    "detect": "",
}


def icon(name: str, *, emoji: bool) -> str:
    """Return the glyph for *name*, honouring the emoji/plain switch.

    Unknown names return ``""`` so callers may compose freely without a
    ``KeyError`` risk.
    """
    table = _EMOJI_ICONS if emoji else _PLAIN_ICONS
    return table.get(name, "")


def build_console(*, color: bool, emoji: bool, stream: TextIO) -> Console:
    """Build the shared themed console bound to *stream*.

    Single construction site for every ``qai build``/``qai app`` terminal
    surface (``_render.py``, ``_repl.py``, ``commands/build.py``,
    ``commands/app.py``) so they never re-implement or drift from this
    ``Console(...)`` call. ``no_color`` fully disables ANSI when *color* is
    False; when *color* is True, colour is forced on so a non-TTY sink (e.g.
    a test ``io.StringIO``) still renders deterministic styling.
    ``legacy_windows=False`` opts every console into plain ANSI escapes
    instead of raw Win32 console API calls — this project already enables
    VT100 processing on the real console at startup
    (``_console_ctrl._enable_vt_mode``), and the Win32 path writes to the OS
    console handle directly, bypassing whatever ``stream`` was injected
    (breaking both piping and tests).
    """
    return Console(
        file=stream,
        theme=THEME,
        emoji=emoji,
        highlight=False,
        no_color=not color,
        force_terminal=color,
        color_system="standard" if color else None,
        legacy_windows=False,
        soft_wrap=True,
    )

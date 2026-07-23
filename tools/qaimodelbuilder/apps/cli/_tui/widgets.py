"""``apps.cli._tui.widgets`` — Textual widgets for the full-screen REPL shell.

Step 1 of the cli-textual-repl revamp only needs the branded splash banner
shown by :class:`apps.cli._tui.app.QaiReplApp` on mount. Step 2 adds the
``/``-triggered command menu and the themed chat input. Step 3 adds
:class:`ChatTranscript` (the scrollable widget-backed
:class:`apps.cli._render.TranscriptSink` implementation) and
:class:`TranscriptStream` (the ``sys.stdout``/``sys.stderr`` redirection
target), so ``QaiReplApp`` becomes the persistent whole-session shell
instead of just a splash + ephemeral input round.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, OptionList, ProgressBar, RichLog, Static
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from apps.cli._repl import SlashCommand

__all__ = [
    "Banner",
    "CommandMenu",
    "ReplInput",
    "ChatTranscript",
    "TranscriptStream",
    "LogPanel",
    "ProgressPanel",
]

#: Compact ASCII wordmark — intentionally small (a handful of lines), not a
#: giant multi-line font block.
_WORDMARK = r"""
  ___   _   ___
 / _ \ / \ |_ _|
| | | / _ \ | |
 \_\_\/_/ \_\|_|
"""


class Banner(Static):
    """Branded splash screen: wordmark + version + session id + shortcuts.

    Colours mirror ``apps.cli._render_theme.THEME``'s semantic styles
    (``agent`` = bold magenta, ``dim`` = dim, ``tool`` = cyan) via plain Rich
    markup, matching the rest of the CLI's visual language without needing a
    ``Console`` bound to that ``Theme``.
    """

    def __init__(
        self,
        *,
        session_id: str,
        version: str,
        extra_lines: list[str] | None = None,
    ) -> None:
        legend = (
            "[cyan]/[/cyan] 命令菜单   "
            "[cyan]Ctrl+C[/cyan] 中断当前回合 / 两次退出   "
            "[cyan]↑[/cyan]/[cyan]↓[/cyan] 输入历史"
        )
        text = (
            f"[bold magenta]{_WORDMARK}[/bold magenta]"
            f"[bold]QAIModelBuilder[/bold] [dim]v{version}[/dim]\n"
            f"[dim]会话 id: {session_id}[/dim]\n\n"
            f"{legend}"
        )
        if extra_lines:
            text += "\n\n[dim]" + "\n".join(extra_lines) + "[/dim]"
        super().__init__(text, markup=True)
        #: Raw markup source, kept as a plain public attribute so tests can
        #: assert on the banner's content without depending on ``Static``'s
        #: internal renderable/``Content`` caching, which differs across
        #: Textual versions.
        self.banner_text = text


def _option_text(cmd: "SlashCommand") -> Text:
    """Render one command as ``/name  help  (alias, ...)`` markup.

    Uses literal Rich style names (``cyan``/``dim``), mirroring
    :class:`Banner`'s approach — ``apps.cli._render_theme.THEME``'s semantic
    names (``tool``/``dim``) only resolve on a ``Console`` bound to that
    ``Theme``, which Textual's own rendering pipeline is not.
    """
    alias_suffix = ""
    if cmd.aliases:
        alias_suffix = " [dim](%s)[/dim]" % ", ".join(f"/{a}" for a in cmd.aliases)
    return Text.from_markup(f"[cyan]/{cmd.name}[/cyan]  {cmd.help}{alias_suffix}")


class CommandMenu(Vertical):
    """Popup panel listing slash commands, filterable by a typed prefix.

    Takes a plain ``list[SlashCommand]`` at construction time — this widget
    has no knowledge of :class:`apps.cli._repl.SlashDispatcher`; the caller
    must source that list from ``dispatcher.commands()`` so the menu and
    ``/help`` never drift from each other.
    """

    DEFAULT_CSS = """
    CommandMenu {
        border: round magenta;
        height: auto;
        max-height: 10;
        padding: 0 1;
    }
    CommandMenu > OptionList {
        border: none;
        height: auto;
        max-height: 8;
    }
    CommandMenu > .command-menu-footer {
        color: $text-muted;
        text-align: right;
        height: 1;
    }
    """

    def __init__(self, commands: list["SlashCommand"], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._all_commands = list(commands)
        self._filtered: list["SlashCommand"] = []
        self._option_list = OptionList()
        self._footer = Static("", classes="command-menu-footer")

    def compose(self):
        yield self._option_list
        yield self._footer

    def on_mount(self) -> None:
        self.set_filter("")

    def set_filter(self, prefix: str) -> None:
        """Narrow the list to commands whose name/alias starts with *prefix*."""
        needle = prefix.lower()
        self._filtered = [
            cmd
            for cmd in self._all_commands
            if cmd.name.lower().startswith(needle)
            or any(alias.lower().startswith(needle) for alias in cmd.aliases)
        ]
        self._option_list.clear_options()
        for cmd in self._filtered:
            self._option_list.add_option(Option(_option_text(cmd)))
        if self._filtered:
            self._option_list.highlighted = 0
        self._update_footer()

    def move_highlight_up(self) -> None:
        self._option_list.action_cursor_up()
        self._update_footer()

    def move_highlight_down(self) -> None:
        self._option_list.action_cursor_down()
        self._update_footer()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        self._update_footer()

    def _update_footer(self) -> None:
        total = len(self._filtered)
        if total == 0:
            self._footer.update("0/0")
            return
        index = self._option_list.highlighted
        if index is None:
            index = 0
        self._footer.update(f"{index + 1}/{total}")

    @property
    def highlighted_command(self) -> "SlashCommand | None":
        index = self._option_list.highlighted
        if index is None or not (0 <= index < len(self._filtered)):
            return None
        return self._filtered[index]

    @property
    def visible_commands(self) -> list["SlashCommand"]:
        """Commands currently shown (post-filter), for test observability."""
        return list(self._filtered)


class ReplInput(Input):
    """Themed single-line chat input — styling only, no new behaviour."""

    DEFAULT_CSS = """
    ReplInput {
        border: round magenta;
        background: $surface;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("placeholder", "输入消息，或 / 查看命令…")
        super().__init__(**kwargs)


class ChatTranscript(Vertical):
    """Scrollable chat transcript — a widget-backed
    :class:`apps.cli._render.TranscriptSink` (duck-typed; this widget
    implements the protocol directly, no explicit inheritance needed).

    Built on :class:`~textual.widgets.RichLog` for "one call = one new
    block" content (append-only via ``write()`` — verified empirically
    against the installed ``textual==0.89.1``: ``RichLog`` exposes no way
    to update its own last line in place), plus one tracked in-progress
    :class:`~textual.widgets.Static` row for the currently-streaming chunk
    line, "frozen" into the ``RichLog`` history on :meth:`break_line`.
    """

    DEFAULT_CSS = """
    ChatTranscript {
        height: 1fr;
    }
    ChatTranscript > RichLog {
        height: 1fr;
    }
    ChatTranscript > Static {
        height: auto;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._log = RichLog(wrap=True, markup=False, auto_scroll=True)
        self._pending = Static("")
        self._pending_text = ""

    def compose(self) -> ComposeResult:
        yield self._log
        yield self._pending

    def write_chunk(self, text: str) -> None:
        self._pending_text += text
        self._pending.update(self._pending_text)

    def break_line(self) -> None:
        if self._pending_text:
            self._log.write(Text(self._pending_text))
            self._pending_text = ""
            self._pending.update("")

    def print_block(self, renderable: object) -> None:
        self.break_line()
        self._log.write(renderable)


class TranscriptStream:
    """File-like ``sys.stdout``/``sys.stderr`` replacement forwarding whole
    writes into a :class:`ChatTranscript` (or any ``print_block``-capable
    sink) as complete blocks.

    Used only while the persistent Textual REPL owns the terminal, so
    existing ``_out_console(opts).print(...)``-style calls scattered across
    ``commands/chat.py``'s dispatcher handlers/farewell messages land in the
    transcript widget with zero changes to those call sites — mirrors
    ``apps.cli._session_log._TeeStream``'s save-original/restore idiom, just
    simpler (no file involved, and no "also write to the original stream"
    since the transcript widget *is* the terminal here).
    """

    __slots__ = ("_sink",)

    def __init__(self, sink: object) -> None:
        self._sink = sink

    def write(self, s: str) -> int:
        if s.strip():
            # ``Console.print()`` always ends with exactly one trailing
            # newline; strip it so it doesn't render as a visible blank row.
            text = s[:-1] if s.endswith("\n") else s
            self._sink.print_block(Text.from_ansi(text))
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class LogPanel:
    """Thin wrapper surfacing WARNING+ structlog records as non-disruptive toasts.

    Built on Textual's own built-in :meth:`~textual.app.App.notify` toast
    system — no persistent widget markup needed, just a method
    :func:`apps.cli._tui.logging_bridge.make_warning_notifier`'s processor
    calls. Maps structlog's ``WARNING``/``ERROR``/``CRITICAL`` levels onto
    ``App.notify``'s ``"warning"``/``"error"`` severities.
    """

    __slots__ = ("_app",)

    _SEVERITY = {"WARNING": "warning", "ERROR": "error", "CRITICAL": "error"}

    def __init__(self, app: App) -> None:
        self._app = app

    def show(self, level: str, message: str) -> None:
        severity = self._SEVERITY.get(level.upper(), "warning")
        self._app.notify(message, severity=severity)


class ProgressPanel(Vertical):
    """One-shot pack-run progress indicator (``qai app``'s REPL).

    Duck-typed :class:`apps.cli._render.ProgressSink` implementation — the
    same "no explicit inheritance needed" precedent :class:`ChatTranscript`
    uses for :class:`~apps.cli._render.TranscriptSink`. Built on
    :class:`~textual.widgets.ProgressBar` (verified empirically against the
    installed ``textual==0.89.1``: mutated via
    ``ProgressBar.update(total=, progress=)``, no separate "status text"
    concept of its own), plus a plain :class:`~textual.widgets.Static` row
    for the stage/status text the bar itself cannot show.

    Hidden (``display = False``) until the first status/progress update of
    a run, and hidden again on :meth:`stop` — there is nothing to show
    between runs.
    """

    DEFAULT_CSS = """
    ProgressPanel {
        height: auto;
        border: round cyan;
        padding: 0 1;
    }
    ProgressPanel > Static {
        height: 1;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._status = Static("")
        self._bar = ProgressBar(total=100, show_eta=False)
        self.display = False

    def compose(self) -> ComposeResult:
        yield self._status
        yield self._bar

    def set_status(self, text: str) -> None:
        self.display = True
        self._status.update(text)

    def set_progress(self, stage: str, percent: float | None) -> None:
        self.display = True
        if stage:
            self._status.update(stage)
        if percent is not None:
            self._bar.update(total=100, progress=percent)

    def stop(self) -> None:
        self.display = False
        self._status.update("")
        self._bar.update(total=100, progress=0)

"""``apps.cli._tui.app`` — the Textual full-screen REPL shell.

Step 1 of the cli-textual-repl revamp added the branded :class:`Banner`
splash. Step 2 added an ephemeral per-input-round app
(``QaiInputApp``/``read_line_tui``) that collected one line of input with a
``/``-triggered command menu, then handed it back to the plain-print
``_repl_loop``.

Step 3 supersedes Step 2's ephemeral-app-per-round model: :class:`QaiReplApp`
is now ONE persistent :class:`~textual.app.App` instance that lives for the
whole real-TTY session and renders the entire conversation (not just the
input box) — turn streaming, slash-command output and farewell/warning
messages all land in :class:`~apps.cli._tui.widgets.ChatTranscript` via
:class:`~apps.cli._tui.widgets.TranscriptStream` (installed over
``sys.stdout``/``sys.stderr`` for the app's lifetime) and a
:class:`~apps.cli._render.StreamFrameRenderer` bound directly to the
transcript widget as its sink.

Step 5 generalizes this shell so it is reusable across REPL commands
(``qai chat``, ``qai build``, and later ``qai app``): the shell itself owns
no command-specific dispatch/turn logic — each command module supplies its
own ``dispatcher_factory``/``run_turn`` closures (see the constructor),
capturing whatever extra state (session objects, model hints, ...) it needs.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Input

from apps.cli._render import RenderOptions, StreamFrameRenderer
from apps.cli._repl import InterruptController, SlashDispatcher, is_slash_command
from apps.cli._tui.screens import ExitConfirmScreen
from apps.cli._tui.widgets import (
    Banner,
    ChatTranscript,
    CommandMenu,
    LogPanel,
    ReplInput,
    TranscriptStream,
)

__all__ = ["QaiReplApp"]


class QaiReplApp(App):
    """Persistent full-screen shell for a real-TTY REPL session.

    Owns the terminal for the session's entire lifetime: mounts the branded
    banner as the transcript's first block, then drives the same
    read-line/slash-dispatch/turn-streaming/interrupt/permission discipline
    each command module's own ``_repl_loop`` uses for its non-TTY fallback
    path — adapted to Textual's event-driven model (``on_input_submitted``
    instead of an ``await async_read_line()`` loop, a Textual worker instead
    of a bare ``asyncio.ensure_future`` for the in-flight turn so Ctrl+C keeps
    being processed while a turn streams).

    Generic across command modules: ``dispatcher_factory`` builds the
    session's :class:`~apps.cli._repl.SlashDispatcher` (called once, after
    ``self.renderer`` exists), and ``run_turn`` drives one natural-language
    turn (called per submitted line). Each command module supplies its own
    closures wrapping its own dispatcher/turn functions.
    """

    CSS = """
    ReplInput {
        dock: bottom;
    }
    CommandMenu {
        dock: bottom;
        offset-y: -3;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "中断", show=False),
        Binding("up", "menu_up", "上一个", show=False),
        Binding("down", "menu_down", "下一个", show=False),
        Binding("escape", "dismiss_menu", "关闭菜单", show=False),
    ]

    def __init__(
        self,
        *,
        c: Any,
        conversation_id: Any,
        tab_id: Any,
        interrupts: InterruptController,
        perm_bridge: Any,
        opts: RenderOptions,
        session_id: str,
        version: str,
        dispatcher_factory: Callable[["QaiReplApp"], SlashDispatcher],
        run_turn: Callable[["QaiReplApp", str], Awaitable[None]],
        has_provider: bool = True,
        banner_extra_lines: list[str] | None = None,
        extra_widgets: Sequence[Widget] = (),
    ) -> None:
        super().__init__()
        self._c = c
        self._conversation_id = conversation_id
        self._tab_id = tab_id
        self._interrupts = interrupts
        self._perm_bridge = perm_bridge
        self._opts = opts
        self.session_id = session_id
        self.version = version
        self.has_provider = has_provider
        self._banner_extra_lines = banner_extra_lines
        self._run_turn = run_turn
        self._extra_widgets = list(extra_widgets)

        self._transcript = ChatTranscript()
        self._input = ReplInput()
        self.renderer = StreamFrameRenderer(opts, sink=self._transcript)
        self.log_panel = LogPanel(self)

        self.dispatcher: SlashDispatcher = dispatcher_factory(self)
        self._menu = CommandMenu(self.dispatcher.commands())
        self._menu.display = False

        self._turn_worker: Any = None
        self._allow_set: set[str] = set()
        self._suppress_reopen = False
        self._orig_stdout: Any = None
        self._orig_stderr: Any = None

    def compose(self) -> ComposeResult:
        yield self._transcript
        yield from self._extra_widgets
        yield self._input
        yield self._menu

    def on_mount(self) -> None:
        banner = Banner(
            session_id=self.session_id,
            version=self.version,
            extra_lines=self._banner_extra_lines,
        )
        self._transcript.print_block(Text.from_markup(banner.banner_text))

        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        stream = TranscriptStream(self._transcript)
        sys.stdout = stream
        sys.stderr = stream

        self._input.focus()

    def on_unmount(self) -> None:
        if self._orig_stdout is not None:
            sys.stdout = self._orig_stdout
        if self._orig_stderr is not None:
            sys.stderr = self._orig_stderr

    # -- input / command menu -----------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._suppress_reopen:
            self._suppress_reopen = False
            self._menu.display = False
            return
        if event.value.startswith("/"):
            self._menu.display = True
            self._menu.set_filter(event.value[1:])
        else:
            self._menu.display = False

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._menu.display:
            cmd = self._menu.highlighted_command
            if cmd is not None:
                self._suppress_reopen = True
                self._input.value = f"/{cmd.name} "
                self._input.cursor_position = len(self._input.value)
            self._menu.display = False
            return

        line = event.value
        self._input.value = ""
        if not line.strip():
            return

        if is_slash_command(line):
            _handled, keep = await self.dispatcher.dispatch(line)
            if not keep:
                self._transcript.print_block(Text("再见。", style="dim"))
                self.exit()
            return

        if self._turn_worker is not None:
            # A turn is already in flight; ignore the overlapping submission.
            return
        self._input.disabled = True
        self._turn_worker = self.run_worker(self._run_one_turn(line), exclusive=True)

    async def _run_one_turn(self, line: str) -> None:
        try:
            await self._run_turn(self, line)
        except Exception as exc:  # noqa: BLE001 — a bad turn must not crash the shell
            self._transcript.print_block(
                Text(f"回合失败: {type(exc).__name__}: {exc}", style="error")
            )
        finally:
            self._turn_worker = None
            self._input.disabled = False
            self._input.focus()

    def action_menu_up(self) -> None:
        if self._menu.display:
            self._menu.move_highlight_up()

    def action_menu_down(self) -> None:
        if self._menu.display:
            self._menu.move_highlight_down()

    def action_dismiss_menu(self) -> None:
        if self._menu.display:
            self._menu.display = False

    def action_interrupt(self) -> None:
        if self._turn_worker is not None:
            self._turn_worker.cancel()
            return
        if self._interrupts.signal():
            if isinstance(self.screen, ExitConfirmScreen):
                with contextlib.suppress(Exception):
                    self.pop_screen()
            self._transcript.print_block(Text("再见。", style="dim"))
            self.exit()
        else:
            self.push_screen(
                ExitConfirmScreen(window_seconds=self._interrupts.window_seconds)
            )

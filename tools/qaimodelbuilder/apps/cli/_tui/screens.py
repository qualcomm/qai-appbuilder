"""``apps.cli._tui.screens`` — modal screens for the persistent REPL shell.

Step 3 of the cli-textual-repl revamp: while :class:`~apps.cli._tui.app.QaiReplApp`
owns the whole real-TTY ``qai chat`` session, ``/show`` can no longer hand
off to the ``prompt_toolkit``-based full-screen pager
(:func:`apps.cli._pager.show_pager`) — a second full-screen application
would fight the already-running Textual app over the terminal. This module
provides the in-app equivalent as a :class:`~textual.screen.ModalScreen`.
"""

from __future__ import annotations

import contextlib

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

__all__ = ["FoldedContentScreen", "ExitConfirmScreen"]


class FoldedContentScreen(ModalScreen[None]):
    """Full-screen scrollable view of one folded ``/show`` body.

    ``q``/``escape`` dismiss (:meth:`~textual.screen.Screen.dismiss`, the
    convention every Textual modal in this codebase shares — verified
    empirically against the installed ``textual==0.89.1``). Unlike
    ``show_pager``, this doesn't need incremental search — ``/show`` bodies
    are already folded/registered content the operator is re-reading, not
    hunting through.
    """

    DEFAULT_CSS = """
    FoldedContentScreen {
        align: center middle;
    }
    FoldedContentScreen > VerticalScroll {
        border: round magenta;
        width: 90%;
        height: 90%;
        padding: 1 2;
    }
    FoldedContentScreen > VerticalScroll > .folded-content-title {
        text-align: center;
        color: $text-muted;
        height: 1;
    }
    """

    BINDINGS = [
        Binding("q", "dismiss_screen", "关闭", show=False),
        Binding("escape", "dismiss_screen", "关闭", show=False),
    ]

    def __init__(self, text: str, *, title: str = "") -> None:
        super().__init__()
        self._text = text
        self._title = title

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(self._title, classes="folded-content-title")
            yield Static(self._text)

    def action_dismiss_screen(self) -> None:
        self.dismiss()


class ExitConfirmScreen(ModalScreen[None]):
    """Informational modal shown after a first Ctrl+C with no turn in flight.

    Auto-dismisses itself after ``window_seconds`` so it stays in sync with
    :class:`~apps.cli._repl.InterruptController`'s own exit window
    (:attr:`~apps.cli._repl.InterruptController.window_seconds`) instead of
    duplicating that constant.

    Verified empirically against the installed ``textual==0.89.1``: a
    ``ModalScreen`` truncates non-priority key-binding resolution at itself
    (``App._modal_binding_chain``) regardless of whether it defines that
    key, so the App-level ``ctrl+c`` binding never reaches
    :meth:`~apps.cli._tui.app.QaiReplApp.action_interrupt` while this screen
    is on top. This screen therefore binds ``ctrl+c`` itself and forwards
    straight to the App's own action — the two-stage timing state machine
    (:meth:`~apps.cli._repl.InterruptController.signal`) is untouched, only
    which node's binding fires first changes.
    """

    DEFAULT_CSS = """
    ExitConfirmScreen {
        align: center middle;
    }
    ExitConfirmScreen > Static {
        border: round yellow;
        padding: 1 2;
        width: auto;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "confirm_exit", show=False),
    ]

    def __init__(self, *, window_seconds: float) -> None:
        super().__init__()
        self._window_seconds = window_seconds

    def compose(self) -> ComposeResult:
        yield Static(f"再次按 Ctrl+C 退出（{self._window_seconds:g} 秒内）")

    def on_mount(self) -> None:
        self.set_timer(self._window_seconds, self._auto_dismiss)

    def _auto_dismiss(self) -> None:
        with contextlib.suppress(Exception):
            self.dismiss()

    def action_confirm_exit(self) -> None:
        self.app.action_interrupt()

"""``apps.cli._tui`` — Textual-based full-screen REPL shell.

:class:`apps.cli._tui.app.QaiReplApp` is the persistent, real-TTY-only
full-screen shell (banner, chat transcript, ``/``-triggered command menu,
turn streaming, two-stage Ctrl+C, modal screens) each REPL command module
(``qai chat``/``qai build``/``qai app``) drives via its own
``dispatcher_factory``/``run_turn`` closures; the non-TTY/pipe/CI path keeps
using the existing print+``prompt_toolkit`` based REPL unchanged.
"""

from __future__ import annotations

from apps.cli._tui.app import QaiReplApp

__all__ = ["QaiReplApp"]

"""``apps.cli._tui.logging_bridge`` — structlog → LogPanel bridge.

Step 4 of the cli-textual-repl revamp: ``commands/chat.py::_run_chat``'s
real-TTY branch redirects ``configure_logging(stream=...)`` at the session
log file (see that module's docstring), so raw ``WARNING``+ structlog
records no longer break into the terminal. This module supplies the
``extra_processors`` hook :func:`~qai.platform.logging.configure_logging`
threads through the renderer chain: it forwards a short summary of every
``WARNING``+ record to the live :class:`~apps.cli._tui.app.QaiReplApp`'s
``LogPanel`` (see :class:`apps.cli._tui.widgets.LogPanel`) so the operator
still sees it — as a non-disruptive toast, not a line torn out of the
transcript — while the full rendered line still lands in the log file
afterward via the normal ``ConsoleRenderer``.
"""

from __future__ import annotations

from typing import Any

from structlog.types import EventDict, Processor

__all__ = ["make_warning_notifier"]

_NOTIFY_LEVELS = {"WARNING", "ERROR", "CRITICAL"}


def make_warning_notifier(app: Any) -> Processor:
    """Build a structlog processor forwarding WARNING+ records to ``app.log_panel``.

    A structlog processor must always return the event dict unchanged — this
    only adds a side effect (the toast notification), it never transforms the
    log line the ``ConsoleRenderer`` renders afterward. Runs synchronously in
    the same asyncio event loop as the rest of the app (no background thread
    is involved on the chat-turn-streaming path, so no ``call_from_thread``
    hop is needed here).
    """

    def _notify(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
        level = str(event_dict.get("level", "")).upper()
        if level in _NOTIFY_LEVELS:
            message = str(event_dict.get("event", ""))
            app.log_panel.show(level, message)
        return event_dict

    return _notify

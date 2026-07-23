"""Unit tests for the WARNING+ log → ``LogPanel`` bridge (cli-textual-repl
revamp, Step 4).

Focused coverage of :func:`apps.cli._tui.logging_bridge.make_warning_notifier`
plus its wiring through :func:`qai.platform.logging.configure_logging`: a
``WARNING``-level structlog record must (a) still render into the given
``stream`` (the session log file, in production), and (b) forward a message
derived from the log event to the app's ``LogPanel``. A plain ``INFO`` log
must do (a) only, never (b). Not an end-to-end TTY session test — see
``test_tui_chat_session.py`` for that.
"""

from __future__ import annotations

import io

import pytest

from apps.cli._tui.logging_bridge import make_warning_notifier
from apps.cli._tui.widgets import LogPanel
from qai.platform.logging import configure_logging, get_logger


class _FakeApp:
    """Minimal app-like stand-in recording ``notify()`` calls."""

    def __init__(self) -> None:
        self.notify_calls: list[tuple[str, str]] = []
        self.log_panel = LogPanel(self)

    def notify(self, message: str, *, severity: str = "information") -> None:
        self.notify_calls.append((message, severity))


@pytest.fixture(autouse=True)
def _restore_logging():
    yield
    configure_logging(level="INFO")


def test_warning_log_writes_to_stream_and_notifies_log_panel() -> None:
    stream = io.StringIO()
    app = _FakeApp()
    configure_logging(
        level="INFO",
        fmt="console",
        stream=stream,
        extra_processors=[make_warning_notifier(app)],
    )

    get_logger("test_tui_log_isolation").warning("something_happened", foo="bar")

    assert "something_happened" in stream.getvalue()
    assert len(app.notify_calls) == 1
    message, severity = app.notify_calls[0]
    assert "something_happened" in message
    assert severity == "warning"


def test_error_log_notifies_with_error_severity() -> None:
    stream = io.StringIO()
    app = _FakeApp()
    configure_logging(
        level="INFO",
        fmt="console",
        stream=stream,
        extra_processors=[make_warning_notifier(app)],
    )

    get_logger("test_tui_log_isolation").error("boom")

    assert "boom" in stream.getvalue()
    assert len(app.notify_calls) == 1
    _message, severity = app.notify_calls[0]
    assert severity == "error"


def test_info_log_writes_to_stream_but_does_not_notify() -> None:
    stream = io.StringIO()
    app = _FakeApp()
    configure_logging(
        level="INFO",
        fmt="console",
        stream=stream,
        extra_processors=[make_warning_notifier(app)],
    )

    get_logger("test_tui_log_isolation").info("just_info")

    assert "just_info" in stream.getvalue()
    assert app.notify_calls == []

"""``apps.cli._session_log`` — REPL session transcript logging.

``qai build`` / ``qai app`` REPL sessions tee every themed-Console write
(both the streaming transcript renderer and the banner/slash-command
feedback unified in cli-render-redesign Step 2) into a per-session log file
under ``DataPaths.cli_sessions_dir``, so a session can be inspected after the
terminal scrolls away.

Every existing ``build_console(...)`` call site reads ``sys.stdout`` /
``sys.stderr`` at call time (see ``_out_console``/``_err_console`` in
``commands/build.py``/``commands/app.py``); wrapping those two module
globals for the session's duration tees them all for free, instead of
threading a new sink parameter through every print call site.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Any, TextIO

from qai.platform.config import DataPaths

from apps.cli._console_ctrl import restore_terminal

__all__ = ["SessionLog", "cleanup_repl_session"]


class _TeeStream:
    """Write to *log* in addition to the real *stream*."""

    __slots__ = ("_stream", "_log")

    def __init__(self, stream: TextIO, log: TextIO) -> None:
        self._stream = stream
        self._log = log

    def write(self, text: str) -> int:
        try:
            self._log.write(text)
        except OSError:
            pass
        return self._stream.write(text)

    def flush(self) -> None:
        try:
            self._log.flush()
        except OSError:
            pass
        self._stream.flush()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


class SessionLog:
    """Open ``<cli_sessions_dir>/<session_id>.log`` and tee stdout/stderr into it.

    Constructing installs the tee immediately (over whatever ``sys.stdout`` /
    ``sys.stderr`` currently are) — so build it as early in the REPL session
    as the session id is known, before the first themed-console print.
    :meth:`close` restores the originals and flushes/closes the file; safe to
    call more than once.
    """

    __slots__ = ("_file", "_orig_stdout", "_orig_stderr")

    def __init__(self, data_paths: DataPaths, session_id: str) -> None:
        path = data_paths.ensure(data_paths.cli_sessions_dir) / f"{session_id}.log"
        self._file: TextIO | None = path.open("a", encoding="utf-8")
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = _TeeStream(self._orig_stdout, self._file)
        sys.stderr = _TeeStream(self._orig_stderr, self._file)

    def close(self) -> None:
        if self._file is None:
            return
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        try:
            self._file.flush()
            self._file.close()
        finally:
            self._file = None


def cleanup_repl_session(session_log: SessionLog, *, active_renderer: Any = None) -> None:
    """Best-effort REPL exit cleanup, shared by ``qai build``/``qai app``.

    Call from a REPL's outer ``finally`` on every exit path (normal return,
    Ctrl+C, uncaught exception): stops a ``RunFrameRenderer``/``Progress``
    that may still be live (if one is passed), shows the terminal cursor
    again (``_console_ctrl.restore_terminal``), then flushes/closes the
    session log.
    """
    if active_renderer is not None:
        with contextlib.suppress(Exception):
            active_renderer.stop()
    restore_terminal()
    session_log.close()

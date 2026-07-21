# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Windows-friendly console Ctrl+C handling for the ``apps.cli.serve`` supervisor.

V1 parity: the legacy ``start_server.py`` (V1) gave the launcher a polished
Ctrl+C experience on Windows:

* a Win32 ``SetConsoleCtrlHandler`` intercepts ``CTRL_C_EVENT`` so cmd.exe
  does **not** pop the ``Terminate batch job (Y/N)?`` prompt and the supervisor
  decides what to do (V1 ``start_server.py:97-108``);
* on Ctrl+C an interactive arrow-key menu asks *Yes (exit) / No (keep running)*
  (V1 ``start_server.py:134-207``).

When the user chooses to exit, the supervisor terminates via ``os._exit`` (V1
``start_server.py:666``), which bypasses cmd.exe's batch-interrupt check so no
second Y/N prompt appears - V1 never needed to inject a ``Y`` keystroke.

This module isolates all of that Win32 / ``msvcrt`` / ``ctypes`` machinery from
the supervisor (``serve.py``), which stays a small, platform-neutral reboot
loop. Everything here is ``sys.platform``-guarded with a graceful POSIX
fallback (cross-platform neutrality per AGENTS.md):

* on non-Windows there is no ``SetConsoleCtrlHandler`` / ``msvcrt``; the
  supervisor falls back to the ordinary ``signal.SIGINT`` handler it already
  installs, and :func:`show_exit_menu` returns ``True`` (a Ctrl+C means exit).
"""

from __future__ import annotations

import sys
from collections.abc import Callable

__all__ = [
    "ANSI",
    "ConsoleCtrlInterceptor",
    "show_exit_menu",
]

_IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# ANSI escape sequences (V1 start_server.py:63-74)
# ---------------------------------------------------------------------------


class ANSI:
    """ANSI escape sequences used for the interactive menu rendering."""

    ESC = "\033"
    RESET = f"{ESC}[0m"
    BOLD = f"{ESC}[1m"
    DIM = f"{ESC}[2m"
    CYAN = f"{ESC}[96m"
    GREEN = f"{ESC}[92m"
    YELLOW = f"{ESC}[93m"
    RED = f"{ESC}[91m"
    HIDE_CURSOR = f"{ESC}[?25l"
    SHOW_CURSOR = f"{ESC}[?25h"
    UP_CLEAR = f"{ESC}[A{ESC}[2K"  # move up one line and clear it


def _write(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Win32 lazy init - only touched on Windows
# ---------------------------------------------------------------------------


def _enable_vt_mode() -> None:
    """Enable VT100 processing so ANSI escapes render in cmd.exe.

    No-op on non-Windows or if the console handle is unavailable.
    """

    if not _IS_WINDOWS:
        return
    try:
        import ctypes
        import ctypes.wintypes

        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT = 0x0001
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        STD_OUTPUT_HANDLE = ctypes.c_ulong(-11)
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(
                handle,
                mode.value
                | ENABLE_VIRTUAL_TERMINAL_PROCESSING
                | ENABLE_PROCESSED_OUTPUT,
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Console control handler (SetConsoleCtrlHandler) - V1 start_server.py:97-108
# ---------------------------------------------------------------------------


class ConsoleCtrlInterceptor:
    """Intercepts Ctrl+C at the Win32 console layer.

    On Windows this registers a ``SetConsoleCtrlHandler`` callback that fires
    on a dedicated OS thread - crucially **not** blocked by the supervisor's
    main thread sitting in ``proc.wait()``. The callback returns ``True`` to
    tell Windows the event was handled (so cmd.exe does not terminate the batch
    job), and signals the supervisor via ``on_ctrl_c``.

    On non-Windows this is a no-op: the supervisor's plain ``signal.SIGINT``
    handler remains in charge (graceful POSIX fallback).
    """

    def __init__(
        self,
        on_ctrl_c: Callable[[], None],
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._on_ctrl_c = on_ctrl_c
        # Fired for CTRL_CLOSE_EVENT / CTRL_LOGOFF_EVENT / CTRL_SHUTDOWN_EVENT
        # (the console window was closed, or the OS is logging off / shutting
        # down). Unlike Ctrl+C — which offers the interactive keep-running menu
        # — these events mean the supervisor MUST tear the worker down promptly
        # (Windows gives a console process only a few seconds after
        # CTRL_CLOSE_EVENT before killing it). This is the reliable native
        # mechanism that fixes the "close the server window → orphaned backend"
        # case for the Start.bat launch path (the supervisor is attached to the
        # same console as cmd.exe, so it receives this event directly —
        # independent of any intermediate python-stub in the process tree that
        # makes parent-PID watchdogs unreliable).
        self._on_close = on_close
        self._installed = False
        # Keep a strong reference to the ctypes callback so it is not GC'd
        # while still registered with the OS.
        self._handler_ref: object | None = None

    def install(self) -> bool:
        """Install the console handler. Returns True if active (Windows only)."""

        if not _IS_WINDOWS:
            return False
        try:
            import ctypes
            import ctypes.wintypes

            _enable_vt_mode()
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handler_type = ctypes.WINFUNCTYPE(
                ctypes.wintypes.BOOL, ctypes.wintypes.DWORD
            )

            @handler_type
            def _handler(ctrl_type: int) -> bool:  # pragma: no cover - OS thread
                # CTRL_C_EVENT = 0, CTRL_BREAK_EVENT = 1 → interactive menu.
                if ctrl_type in (0, 1):
                    try:
                        self._on_ctrl_c()
                    except Exception:
                        pass
                    return True
                # CTRL_CLOSE_EVENT = 2 (window closed), CTRL_LOGOFF_EVENT = 5,
                # CTRL_SHUTDOWN_EVENT = 6 → the console is going away; stop the
                # worker NOW (no menu). Returning True marks it handled; the
                # callback blocks just long enough to request the graceful stop
                # so the worker is torn down + the Job Object reaps any
                # remainder when the supervisor exits. (Windows allows a few
                # seconds of handler runtime before force-killing the process.)
                if ctrl_type in (2, 5, 6):
                    if self._on_close is not None:
                        try:
                            self._on_close()
                        except Exception:
                            pass
                    return True
                return False

            self._handler_ref = _handler
            if kernel32.SetConsoleCtrlHandler(_handler, True):
                self._installed = True
        except Exception:
            self._installed = False
        return self._installed

    @property
    def active(self) -> bool:
        return self._installed

    def uninstall(self) -> None:
        """Uninstall the console handler (Windows only, no-op elsewhere).

        Calls ``SetConsoleCtrlHandler(handler, False)`` to deregister the
        callback and releases the strong ctypes reference so the callback
        object can be garbage-collected. Safe to call multiple times.
        """
        if not _IS_WINDOWS or not self._installed or self._handler_ref is None:
            return
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetConsoleCtrlHandler(self._handler_ref, False)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        finally:
            self._installed = False
            self._handler_ref = None


# ---------------------------------------------------------------------------
# Interactive exit menu (V1 start_server.py:134-207)
# ---------------------------------------------------------------------------


_OPTIONS = (
    ("Yes", "Exit now    "),
    ("No ", "Keep running"),
)
_PROMPT = "  Server is running. What would you like to do?"


def _render_menu(selected: int, *, update: bool) -> None:
    total_lines = len(_OPTIONS) + 2
    if update:
        for _ in range(total_lines):
            sys.stdout.write(ANSI.UP_CLEAR)
    sys.stdout.write(f"{ANSI.BOLD}{ANSI.YELLOW}{_PROMPT}{ANSI.RESET}\n")
    for i, (label, desc) in enumerate(_OPTIONS):
        if i == selected:
            sys.stdout.write(
                f"  {ANSI.CYAN}{ANSI.BOLD}> {i + 1}. {label}"
                f"{ANSI.RESET}{ANSI.CYAN} - {desc}{ANSI.RESET}\n"
            )
        else:
            sys.stdout.write(
                f"  {ANSI.DIM}  {i + 1}. {label} - {desc}{ANSI.RESET}\n"
            )
    sys.stdout.write(
        f"{ANSI.DIM}  Up/Down to select  -  Enter to confirm  "
        f"-  Esc to exit{ANSI.RESET}\n"
    )
    sys.stdout.flush()


def _clear_menu() -> None:
    total_lines = len(_OPTIONS) + 2
    for _ in range(total_lines):
        sys.stdout.write(ANSI.UP_CLEAR)
    sys.stdout.flush()


def _read_key_windows() -> str:
    """Read one keypress and normalise it (Windows ``msvcrt``)."""

    import msvcrt  # type: ignore[import-not-found]

    key = msvcrt.getch()
    if key == b"\xe0":  # arrow-key prefix
        key2 = msvcrt.getch()
        if key2 == b"H":
            return "UP"
        if key2 == b"P":
            return "DOWN"
        return f"EXT:{key2!r}"
    if key in (b"\r", b"\n"):
        return "ENTER"
    if key == b"\x1b":
        return "ESC"
    if key == b"\x03":
        return "CTRL_C"
    return key.decode("utf-8", errors="replace")


def show_exit_menu() -> bool:
    """Show the interactive Yes/No exit menu. Returns True if the user chose exit.

    On non-Windows (no ``msvcrt``) this returns ``True`` immediately - a Ctrl+C
    on POSIX means "stop the server" without an arrow-key prompt.
    """

    if not _IS_WINDOWS:
        return True

    try:
        import msvcrt  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:  # pragma: no cover - non-Windows safety
        return True

    selected = 0
    _write("\n")
    _write(ANSI.HIDE_CURSOR)
    try:
        _render_menu(selected, update=False)
        while True:
            key = _read_key_windows()
            if key == "UP":
                selected = (selected - 1) % len(_OPTIONS)
                _render_menu(selected, update=True)
            elif key == "DOWN":
                selected = (selected + 1) % len(_OPTIONS)
                _render_menu(selected, update=True)
            elif key == "ENTER":
                break
            elif key in ("ESC", "CTRL_C"):
                selected = 0  # Esc / a second Ctrl+C means exit
                break
    finally:
        _clear_menu()
        _write(ANSI.SHOW_CURSOR)
    return selected == 0

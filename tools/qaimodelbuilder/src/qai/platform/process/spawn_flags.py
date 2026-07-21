# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Cross-context subprocess ``creationflags`` helper (platform shared kernel).

When the desktop shell (a GUI process with no attached console) spawns a child
console program, Windows allocates a brand-new console window for it that flashes
on screen — a black box that pops up and vanishes for every ``exec`` / streamed
command the agent runs. ``CREATE_NO_WINDOW`` tells Windows not to allocate that
console, so the child runs invisibly (its stdout/stderr are still captured via
the pipes we attach). This is purely a desktop-UX fix; it does not change how the
child runs or how we read its output.

This lives under ``qai.platform.*`` (the shared kernel), NOT under any single
context, so both ``qai.ai_coding`` and ``qai.tools`` can import it without
crossing the ``context-isolation`` import-linter contract.

Cross-platform posture (AGENTS.md): ``CREATE_NO_WINDOW`` only exists on Windows,
so :func:`no_window_creationflags` returns ``0`` on every non-Windows platform
(a no-op flag) behind a ``sys.platform`` guard — it never references a
Windows-only attribute off-Windows and never raises.
"""

from __future__ import annotations

import subprocess
import sys

__all__ = ["no_window_creationflags"]


def no_window_creationflags() -> int:
    """Return ``CREATE_NO_WINDOW`` on Windows, ``0`` elsewhere.

    Pass the result as the ``creationflags`` argument to
    ``asyncio.create_subprocess_exec`` / ``...shell`` / ``subprocess.Popen`` so a
    spawned console child does not flash a console window when launched from the
    GUI desktop shell. On POSIX there is no such flag, so ``0`` is returned (the
    caller passes a harmless no-op).
    """
    if sys.platform == "win32":
        # CREATE_NO_WINDOW is a Windows-only subprocess constant; guarded above.
        return subprocess.CREATE_NO_WINDOW
    return 0  # type: ignore[unreachable]  # POSIX fallback (win32-typed checker)

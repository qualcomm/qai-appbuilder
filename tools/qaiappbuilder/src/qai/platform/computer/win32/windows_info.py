# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Window geometry the model needs on EVERY turn, read from the shell.

Why this is bundled with a screenshot instead of offered as a query
------------------------------------------------------------------
A model driving the desktop must know two things before it can click anything
safely, and it does not know to ask for either:

* **Which window owns the pixel it is aiming at.** A coordinate that looks right
  in the image belongs to whatever window is on top there. Acting without that
  knowledge is how a gesture meant for the desktop lands in a browser instead.
* **Which window has focus.** Many surfaces (notably the Explorer desktop) only
  react to input while they are foreground.

Both are a handful of integers, so they ride along with the screenshot rather
than waiting for the model to realise it needs them.

What is deliberately NOT here
-----------------------------
Arbitrary in-window controls (buttons, menu items, text boxes). Those need
UIAutomation, whose tree can run to thousands of nodes and must be filtered per
task — a caller that knows what it is looking for should query it directly
(see the ``computer-automation`` skill). Caption buttons are also excluded:
``WM_GETTITLEBARINFOEX`` only answers for classic Win32 frames and returns
garbage for the WinUI / Electron / Chromium windows that dominate a modern
desktop, so reporting it would be worse than reporting nothing.

Coordinates are DPI-aware physical screen pixels — the same space as a
native-resolution screenshot and the input primitives. Every failure degrades to
an empty result; window metadata is an assist and must never break a tool call.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

__all__ = [
    "describe_windows",
    "foreground_window",
    "taskbar_rect",
    "window_at",
    "window_rect",
]

#: Ignore windows too small to be a meaningful click target (tooltips, 0-size
#: message sinks, tray helpers). Keeps the payload to windows a model may act on.
_MIN_EDGE_PX = 120
#: Cap the reported list: a desktop with dozens of windows would otherwise crowd
#: out the rest of the tool result. Foreground first, then largest.
_MAX_WINDOWS = 12

_GA_ROOT = 2


def _rect_tuple(rect: wintypes.RECT) -> tuple[int, int, int, int]:
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _win32() -> ctypes.WinDLL | None:
    if sys.platform != "win32":
        return None
    from . import capture as _capture

    # Shared, idempotent: geometry must be in the same space as the pixels.
    _capture.set_process_dpi_aware()
    return ctypes.WinDLL("user32", use_last_error=True)


def _title(user32: ctypes.WinDLL, hwnd: int) -> str:
    length = int(user32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(user32: ctypes.WinDLL, hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(160)
    user32.GetClassNameW(hwnd, buf, 160)
    return buf.value


def describe_windows() -> list[dict[str, Any]]:
    """Visible top-level windows, foreground first.

    Each entry: ``{title, class_name, x, y, width, height, is_foreground}``.
    Returns ``[]`` on any failure or off-Windows.
    """
    user32 = _win32()
    if user32 is None:
        return []
    try:
        foreground = int(user32.GetForegroundWindow())
        found: list[dict[str, Any]] = []

        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _visit(hwnd, _lparam):  # noqa: ANN001 — ctypes callback
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _title(user32, hwnd)
            if not title:
                return True  # untitled windows are not user-actionable targets
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            left, top, right, bottom = _rect_tuple(rect)
            width, height = right - left, bottom - top
            if width < _MIN_EDGE_PX or height < _MIN_EDGE_PX:
                return True
            found.append({
                # Exposed so the model can target this window by id for
                # window-relative coordinates or background delivery.
                "hwnd": int(hwnd),
                "title": title,
                "class_name": _class_name(user32, hwnd),
                "x": left,
                "y": top,
                "width": width,
                "height": height,
                "is_foreground": int(hwnd) == foreground,
            })
            return True

        user32.EnumWindows(proc_type(_visit), 0)
        # Foreground first (the model's most common question), then by area so
        # the truncation keeps the windows most likely to be acted upon.
        found.sort(key=lambda w: (not w["is_foreground"], -(w["width"] * w["height"])))
        return found[:_MAX_WINDOWS]
    except Exception:  # noqa: BLE001 — best-effort assist
        return []


def taskbar_rect() -> dict[str, int] | None:
    """The taskbar's rectangle, or ``None`` when unavailable.

    Worth reporting because it is both a common target (tray, Start, window
    buttons) and a common obstacle — a "bottom of the screen" coordinate often
    lands on it by accident.
    """
    user32 = _win32()
    if user32 is None:
        return None
    try:
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(tray, ctypes.byref(rect)):
            return None
        left, top, right, bottom = _rect_tuple(rect)
        if right <= left or bottom <= top:
            return None
        return {"x": left, "y": top, "width": right - left, "height": bottom - top}
    except Exception:  # noqa: BLE001
        return None


def window_at(x: int, y: int) -> dict[str, Any] | None:
    """The top-level window owning screen pixel ``(x, y)``.

    This is the "am I about to click what I think I am?" check: the answer is
    whatever window is topmost at that point, which is not necessarily the one
    the model saw when the screenshot was taken.
    """
    user32 = _win32()
    if user32 is None:
        return None
    try:
        class _POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        # Handles are pointer-sized; a bare ctypes call marshals them as c_int
        # and a real 64-bit handle then raises OverflowError.
        user32.WindowFromPoint.argtypes = [_POINT]
        user32.WindowFromPoint.restype = ctypes.c_void_p
        hwnd = user32.WindowFromPoint(_POINT(int(x), int(y)))
        if not hwnd:
            return None
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        root = user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
        rect = wintypes.RECT()
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetWindowRect(root, ctypes.byref(rect))
        left, top, right, bottom = _rect_tuple(rect)
        return {
            # The handle itself: callers that need to post a message to this
            # window (background delivery) cannot re-derive it from a title.
            "hwnd": int(root),
            "title": _title(user32, root),
            "class_name": _class_name(user32, root),
            "x": left,
            "y": top,
            "width": right - left,
            "height": bottom - top,
            "is_foreground": int(root) == int(user32.GetForegroundWindow()),
        }
    except Exception:  # noqa: BLE001
        return None


def foreground_window() -> int | None:
    """The handle of the window currently receiving input, or ``None``.

    This is the window a foreground-delivered keystroke will actually reach, so
    it is also the sensible default target for an accessibility snapshot.
    """
    user32 = _win32()
    if user32 is None:
        return None
    try:
        hwnd = int(user32.GetForegroundWindow())
        return hwnd or None
    except Exception:  # noqa: BLE001 — best-effort assist
        return None


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """``(left, top, right, bottom)`` of a window, or ``None`` if it is gone.

    Read live rather than cached: window-relative coordinates are only useful
    because they are resolved against where the window is at action time.
    """
    user32 = _win32()
    if user32 is None:
        return None
    try:
        handle = ctypes.c_void_p(int(hwnd))
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        if not user32.IsWindow(handle):
            return None
        rect = wintypes.RECT()
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if not user32.GetWindowRect(handle, ctypes.byref(rect)):
            return None
        return _rect_tuple(rect)
    except Exception:  # noqa: BLE001
        return None

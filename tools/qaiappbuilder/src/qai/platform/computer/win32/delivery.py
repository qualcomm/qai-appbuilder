# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Background input delivery: post messages to a window, don't drive the mouse.

The default input path (``SendInput`` in :mod:`.input`) drives the SYSTEM
cursor and keyboard, which has two consequences the model cannot control: the
target window must be in the foreground to receive the keystrokes, and the
user's pointer physically jumps. If the foreground window changed between the
screenshot and the action — a notification stealing focus, the user clicking
something — the input lands in the wrong application.

Posting messages straight to a window's message queue avoids both problems: the
target receives the input regardless of z-order or focus, the global cursor is
never moved, and the user can keep working. Verified on this machine by posting
``WM_CHAR`` into a Notepad++ Scintilla view while a different application held
the foreground: the buffer grew by exactly the posted character count and
``GetForegroundWindow`` was unchanged throughout.

The trade-off is real and the caller must choose deliberately, which is why
delivery is an explicit per-action option rather than a global default:

* **Coordinates are CLIENT-relative.** A posted mouse message carries its
  position in the target's client area, so the caller converts from screen
  space. :func:`screen_to_client` does that.
* **Not universally honoured.** A window that reads the raw input queue
  (``GetAsyncKeyState``), most full-screen games, and anything using DirectInput
  will ignore posted messages. There is no reliable way to detect this, so a
  silent no-op is possible; foreground delivery remains the default for that
  reason.
* **No modifier state.** Posted messages do not go through the keyboard state
  machine, so ``WM_CHAR`` types a character but a chord like ``CTRL+S`` needs
  explicit ``WM_KEYDOWN``/``WM_KEYUP`` with the modifier's virtual key.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from .keymap import resolve_key

__all__ = [
    "click_background",
    "double_click_background",
    "keypress_background",
    "screen_to_client",
    "scroll_background",
    "type_text_background",
]

# Window messages
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_MBUTTONDOWN = 0x0207
_WM_MBUTTONUP = 0x0208
_WM_MOUSEWHEEL = 0x020A
_WM_MOUSEHWHEEL = 0x020E
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_CHAR = 0x0102
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105

# wParam button flags
_MK_LBUTTON = 0x0001
_MK_RBUTTON = 0x0002
_MK_MBUTTON = 0x0010
_MK_SHIFT = 0x0004
_MK_CONTROL = 0x0008

_WHEEL_DELTA = 120
_VK_MENU = 0x12

#: Brief pauses so the target's message loop observes a real gesture rather
#: than a same-tick down/up pair; mirrors the dwell used for synthetic input.
_PRESS_DWELL_S = 0.03
_MOVE_SETTLE_S = 0.01

#: Modifier virtual keys, posted as real key messages around the main action
#: because posted messages bypass the keyboard state machine.
_MODIFIER_VKS: dict[str, int] = {
    "CTRL": 0x11,
    "SHIFT": 0x10,
    "ALT": 0x12,
    "META": 0x5B,
}

_BUTTON_MESSAGES: dict[str, tuple[int, int, int]] = {
    # button -> (down message, up message, wParam mask while held)
    "left": (_WM_LBUTTONDOWN, _WM_LBUTTONUP, _MK_LBUTTON),
    "right": (_WM_RBUTTONDOWN, _WM_RBUTTONUP, _MK_RBUTTON),
    "wheel": (_WM_MBUTTONDOWN, _WM_MBUTTONUP, _MK_MBUTTON),
}


class BackgroundDeliveryError(Exception):
    """A posted message could not be delivered to the target window."""


def _user32() -> ctypes.WinDLL:
    if sys.platform != "win32":
        raise BackgroundDeliveryError("background delivery requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    return user32


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    """Convert a virtual-desktop point to ``hwnd``'s client coordinates.

    Posted mouse messages address the client area, so a screen coordinate the
    model derived from a screenshot or an AX rectangle has to be translated
    before it can be posted.
    """
    user32 = _user32()
    point = _POINT(int(x), int(y))
    if not user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(point)):
        raise BackgroundDeliveryError(
            f"ScreenToClient failed for window 0x{hwnd:X}"
        )
    return int(point.x), int(point.y)


def _lparam_xy(x: int, y: int) -> int:
    """Pack a client-area point into an ``LPARAM`` (low word x, high word y).

    Negative coordinates are legal (a point above/left of the client origin),
    so both halves are masked to 16 bits rather than assumed non-negative.
    """
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def _post(
    user32: ctypes.WinDLL, hwnd: int, message: int, wparam: int, lparam: int
) -> None:
    if not user32.PostMessageW(
        wintypes.HWND(hwnd),
        wintypes.UINT(message),
        wintypes.WPARAM(wparam),
        wintypes.LPARAM(lparam),
    ):
        raise BackgroundDeliveryError(
            f"PostMessage(0x{message:04X}) to 0x{hwnd:X} failed "
            f"(error {ctypes.get_last_error()})"
        )


def _validate(hwnd: int) -> ctypes.WinDLL:
    user32 = _user32()
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        raise BackgroundDeliveryError(f"not a window: 0x{hwnd:X}")
    return user32

#: Child classes that host text but are only shells around the real editor.
#: Modern (WinUI) apps nest the actual text control several levels down, so a
#: window's own handle is usually NOT where keystrokes belong.
_SHELL_CLASSES = frozenset(
    {
        "Microsoft.UI.Content.DesktopChildSiteBridge",
        "InputSiteWindowClass",
        "InputNonClientPointerSource",
    }
)


def keyboard_target(hwnd: int) -> int:
    """The descendant of ``hwnd`` that keyboard messages must be posted to.

    A top-level window does NOT forward ``WM_CHAR`` to its children, so posting
    to the frame silently does nothing whenever the real text control is a
    child. Verified on Windows Notepad: its text lives in a ``RichEditD2DPT``
    child, and characters posted to the ``Notepad`` frame never appeared.

    Resolution order:

    1. Whatever the target's own UI thread reports as focused — the most
       accurate answer, available because ``GetGUIThreadInfo`` works on any
       thread rather than only the foreground one.
    2. Otherwise the deepest child that accepts text, skipping the WinUI
       plumbing windows that merely host content.
    3. Otherwise the window itself (classic Win32 apps handle their own input).
    """
    user32 = _user32()

    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", ctypes.c_void_p),
            ("hwndFocus", ctypes.c_void_p),
            ("hwndCapture", ctypes.c_void_p),
            ("hwndMenuOwner", ctypes.c_void_p),
            ("hwndMoveSize", ctypes.c_void_p),
            ("hwndCaret", ctypes.c_void_p),
            ("rcCaret", wintypes.RECT),
        ]

    try:
        thread_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p
        ]
        tid = user32.GetWindowThreadProcessId(
            ctypes.c_void_p(hwnd), ctypes.byref(thread_id)
        )
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        if user32.GetGUIThreadInfo(wintypes.DWORD(tid), ctypes.byref(info)):
            focused = int(info.hwndFocus or 0)
            if focused and _is_descendant(user32, hwnd, focused):
                return focused
    except (AttributeError, OSError):
        pass

    deepest = _deepest_text_child(user32, hwnd)
    return deepest or hwnd


def _is_descendant(user32: ctypes.WinDLL, ancestor: int, candidate: int) -> bool:
    """Whether ``candidate`` is ``ancestor`` or sits inside it."""
    if candidate == ancestor:
        return True
    user32.GetParent.argtypes = [ctypes.c_void_p]
    user32.GetParent.restype = ctypes.c_void_p
    current = candidate
    for _ in range(32):  # bounded: window trees are shallow
        current = int(user32.GetParent(ctypes.c_void_p(current)) or 0)
        if not current:
            return False
        if current == ancestor:
            return True
    return False


def _deepest_text_child(user32: ctypes.WinDLL, hwnd: int) -> int:
    """The largest child that reports text content, ignoring WinUI shells."""
    best = 0
    best_area = 0
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _visit(child, _lparam):
        name = ctypes.create_unicode_buffer(160)
        user32.GetClassNameW(child, name, 160)
        if name.value in _SHELL_CLASSES:
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(child, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        nonlocal best, best_area
        if area > best_area:
            best, best_area = int(child), area
        return True

    try:
        user32.EnumChildWindows(ctypes.c_void_p(hwnd), proc(_visit), 0)
    except (AttributeError, OSError):
        return 0
    return best


def _modifier_mask(mods: tuple[str, ...] | list[str] | None) -> int:
    """The ``wParam`` bits for modifiers that mouse messages carry natively."""
    mask = 0
    for mod in mods or ():
        if mod == "SHIFT":
            mask |= _MK_SHIFT
        elif mod == "CTRL":
            mask |= _MK_CONTROL
    return mask


def _press_modifiers(
    user32: ctypes.WinDLL, hwnd: int, mods: tuple[str, ...] | list[str] | None
) -> list[int]:
    """Post key-down for each modifier; returns the VKs to release later."""
    pressed: list[int] = []
    for mod in mods or ():
        vk = _MODIFIER_VKS.get(mod)
        if vk is None:
            continue
        # ALT travels as a system key; anything else is an ordinary key.
        message = _WM_SYSKEYDOWN if vk == _VK_MENU else _WM_KEYDOWN
        _post(user32, hwnd, message, vk, 0)
        pressed.append(vk)
    return pressed


def _release_modifiers(
    user32: ctypes.WinDLL, hwnd: int, vks: list[int]
) -> None:
    """Post key-up for each held modifier, in reverse press order."""
    for vk in reversed(vks):
        message = _WM_SYSKEYUP if vk == _VK_MENU else _WM_KEYUP
        _post(user32, hwnd, message, vk, 0)


def click_background(
    hwnd: int,
    x: int,
    y: int,
    button: str = "left",
    *,
    modifiers: tuple[str, ...] | list[str] | None = None,
) -> None:
    """Post a single click at screen point ``(x, y)`` into ``hwnd``.

    The window neither gains focus nor receives the system cursor; the user's
    pointer stays exactly where it is.
    """
    messages = _BUTTON_MESSAGES.get(button)
    if messages is None:
        raise BackgroundDeliveryError(
            f"background delivery cannot post button {button!r}"
        )
    down, up, mask = messages
    user32 = _validate(hwnd)
    cx, cy = screen_to_client(hwnd, x, y)
    lparam = _lparam_xy(cx, cy)
    held = _press_modifiers(user32, hwnd, modifiers)
    try:
        wparam = mask | _modifier_mask(modifiers)
        _post(user32, hwnd, _WM_MOUSEMOVE, _modifier_mask(modifiers), lparam)
        time.sleep(_MOVE_SETTLE_S)
        _post(user32, hwnd, down, wparam, lparam)
        time.sleep(_PRESS_DWELL_S)
        _post(user32, hwnd, up, _modifier_mask(modifiers), lparam)
    finally:
        _release_modifiers(user32, hwnd, held)


def double_click_background(
    hwnd: int,
    x: int,
    y: int,
    *,
    modifiers: tuple[str, ...] | list[str] | None = None,
) -> None:
    """Post a left double-click at screen point ``(x, y)`` into ``hwnd``.

    Sends down/up, then ``WM_LBUTTONDBLCLK`` + up, which is the sequence a real
    double-click produces; posting two plain clicks is often treated as two
    separate clicks because the posted messages carry no timing information.
    """
    user32 = _validate(hwnd)
    cx, cy = screen_to_client(hwnd, x, y)
    lparam = _lparam_xy(cx, cy)
    mask = _modifier_mask(modifiers)
    held = _press_modifiers(user32, hwnd, modifiers)
    try:
        _post(user32, hwnd, _WM_MOUSEMOVE, mask, lparam)
        time.sleep(_MOVE_SETTLE_S)
        _post(user32, hwnd, _WM_LBUTTONDOWN, _MK_LBUTTON | mask, lparam)
        time.sleep(_PRESS_DWELL_S)
        _post(user32, hwnd, _WM_LBUTTONUP, mask, lparam)
        _post(user32, hwnd, _WM_LBUTTONDBLCLK, _MK_LBUTTON | mask, lparam)
        time.sleep(_PRESS_DWELL_S)
        _post(user32, hwnd, _WM_LBUTTONUP, mask, lparam)
    finally:
        _release_modifiers(user32, hwnd, held)


def scroll_background(
    hwnd: int,
    x: int,
    y: int,
    steps_x: int,
    steps_y: int,
    *,
    modifiers: tuple[str, ...] | list[str] | None = None,
) -> None:
    """Post wheel scrolling at screen point ``(x, y)`` into ``hwnd``.

    Wheel messages are unusual: their position is in SCREEN coordinates, not
    client coordinates, so no conversion is applied here.

    ``steps_y > 0`` means "content moves DOWN" and ``steps_x > 0`` means
    "content moves RIGHT" — the SAME contract the foreground
    :func:`qai.platform.computer.win32.input.scroll` honours, so one
    ``scroll_y`` scrolls the same way under either ``delivery`` mode.
    """
    user32 = _validate(hwnd)
    lparam = _lparam_xy(int(x), int(y))
    mask = _modifier_mask(modifiers)
    held = _press_modifiers(user32, hwnd, modifiers)
    try:
        if steps_y:
            # ``steps_y > 0`` means "content moves DOWN" (the tool contract —
            # see ``computer/tool_schemas.py`` and ``input.py:402-404``).  Win32
            # ``WM_MOUSEWHEEL`` positive is "away from the user" = content UP,
            # so the vertical sign is negated here, identical to the foreground
            # ``input.py`` path (book 04 §4.5).  Without this negation
            # ``delivery="background"`` scrolled the OPPOSITE way from
            # ``delivery="foreground"`` for the same ``scroll_y``.
            # Horizontal ``WM_MOUSEHWHEEL`` positive already means "content
            # moves RIGHT", so ``steps_x`` stays unnegated in both paths.
            delta = -int(steps_y) * _WHEEL_DELTA
            _post(
                user32, hwnd, _WM_MOUSEWHEEL, (delta << 16) | mask, lparam
            )
        if steps_x:
            delta = int(steps_x) * _WHEEL_DELTA
            _post(
                user32, hwnd, _WM_MOUSEHWHEEL, (delta << 16) | mask, lparam
            )
    finally:
        _release_modifiers(user32, hwnd, held)


def type_text_background(hwnd: int, text: str) -> None:
    """Post ``text`` character by character into ``hwnd``.

    Uses ``WM_CHAR``, so the target receives the literal characters without any
    dependence on keyboard layout — the same reason the foreground path uses
    Unicode injection. Astral characters are posted as their two UTF-16
    surrogate code units, which is what a real keyboard driver does.

    The characters go to :func:`keyboard_target`, not to ``hwnd`` itself: a
    top-level window does not forward ``WM_CHAR`` to its children, so posting
    to the frame is a silent no-op for any app whose editor is a child control.
    """
    user32 = _validate(hwnd)
    target = keyboard_target(hwnd)
    data = text.encode("utf-16-le")
    for index in range(0, len(data), 2):
        code_unit = data[index] | (data[index + 1] << 8)
        # lParam repeat-count of 1: a plain single keystroke.
        _post(user32, target, _WM_CHAR, code_unit, 1)


def keypress_background(hwnd: int, chords: list[str] | tuple[str, ...]) -> None:
    """Post key chords (e.g. ``["CTRL+S"]``) into ``hwnd``.

    Each chord's keys are pressed in order and released in reverse, matching
    the foreground path. Because posted messages bypass the keyboard state
    machine, modifiers are posted as explicit key messages rather than relying
    on a global shift state.

    A key the keymap can only express as a character (a symbol with no reliable
    virtual key) is posted as ``WM_CHAR``. That is correct for a bare key but
    cannot carry modifiers, so combining one with a modifier is rejected rather
    than posted as a chord the target would silently misread.
    """
    user32 = _validate(hwnd)
    # Chords address the focused child for the same reason literal text does.
    target = keyboard_target(hwnd)
    for chord in chords:
        parts = [p for p in str(chord).replace("-", "+").split("+") if p]
        if not parts:
            raise BackgroundDeliveryError(f"empty chord: {chord!r}")
        resolved = [resolve_key(part) for part in parts]
        chars = [r for r in resolved if r.is_unicode]
        if chars and len(resolved) > 1:
            raise BackgroundDeliveryError(
                f"background delivery cannot post chord {chord!r}: "
                f"{chars[0].char!r} has no virtual key. Use foreground "
                "delivery for this chord."
            )
        if chars:
            # Bare symbol key: post it as a character, reusing the same
            # UTF-16 code-unit path as literal typing.
            type_text_background(hwnd, chars[0].char or "")
            continue
        pressed: list[int] = []
        try:
            for item in resolved:
                vk = int(item.vk or 0)
                message = _WM_SYSKEYDOWN if vk == _VK_MENU else _WM_KEYDOWN
                _post(user32, target, message, vk, 0)
                pressed.append(vk)
            time.sleep(_PRESS_DWELL_S)
        finally:
            for vk in reversed(pressed):
                message = _WM_SYSKEYUP if vk == _VK_MENU else _WM_KEYUP
                _post(user32, target, message, vk, 0)

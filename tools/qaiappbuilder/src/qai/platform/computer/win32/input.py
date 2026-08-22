# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Mouse / keyboard input via Win32 ``SendInput`` (ctypes only).

All pointer coordinates handed to this module are ALREADY mapped to
absolute logical screen coordinates by the session layer; this module
only normalises them to the ``0..65535`` range ``SendInput`` expects and
emits the event structs. Keyboard text uses ``KEYEVENTF_UNICODE`` (layout
independent, handles surrogate pairs); chords use VK press/release.

Book 04 §4 is the authoritative spec for every flag and ordering rule.
"""

from __future__ import annotations

import ctypes
import math
import sys
import time
from ctypes import wintypes

from ..types import DesktopError
from .keymap import resolve_key

__all__ = [
    "click",
    "double_click",
    "drag",
    "keypress",
    "move",
    "press_keys",
    "release_keys",
    "scroll",
    "type_text",
]


# INPUT.type
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

# MOUSEINPUT.dwFlags
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_XDOWN = 0x0080
_MOUSEEVENTF_XUP = 0x0100
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_MOUSEEVENTF_ABSOLUTE = 0x8000

_XBUTTON1 = 0x0001
_XBUTTON2 = 0x0002
_WHEEL_DELTA = 120

# KEYBDINPUT.dwFlags
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _user32() -> "ctypes.WinDLL":
    if sys.platform != "win32":
        raise DesktopError("input only available on Windows", name="InputFailed")
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    u.SendInput.restype = wintypes.UINT
    return u


def _send(inputs: list[_INPUT]) -> None:
    if not inputs:
        return
    user32 = _user32()
    n = len(inputs)
    arr = (_INPUT * n)(*inputs)
    sent = user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT))
    if sent != n:
        raise DesktopError(
            f"SendInput sent {sent}/{n} (err={ctypes.get_last_error()})",
            name="InputFailed",
        )


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def _mouse(dx: int, dy: int, flags: int, data: int = 0) -> _INPUT:
    inp = _INPUT()
    inp.type = _INPUT_MOUSE
    inp.u.mi = _MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _key_vk(vk: int, *, up: bool) -> _INPUT:
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    flags = _KEYEVENTF_KEYUP if up else 0
    inp.u.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _key_unicode(code_unit: int, *, up: bool) -> _INPUT:
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0)
    inp.u.ki = _KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _abs_norm(logical: int, span: int) -> int:
    """Normalise a span-relative coordinate to the 0..65535 absolute range."""
    if span <= 1:
        return 0
    val = round(logical * 65535 / (span - 1))
    return max(0, min(65535, val))


# ---------------------------------------------------------------------------
# Public primitives
#
# Coordinates are VIRTUAL-DESKTOP logical coords. ``logical_w`` / ``logical_h``
# are the live virtual-desktop SPAN and ``origin_x`` / ``origin_y`` its origin,
# which is NEGATIVE when a monitor sits left of / above the primary — so the
# caller measures both at action time instead of assuming ``(0, 0)`` and a
# single panel. ``MOUSEEVENTF_VIRTUALDESK`` makes the 0..65535 absolute range
# span every monitor, which is what lets a click land on a secondary display.
# With one monitor at origin (0, 0) this reduces exactly to primary-only
# addressing, so single-screen behaviour is unchanged.
# ---------------------------------------------------------------------------

_ABS_POS = (
    _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK
)


def _abs_xy(
    gx: int, gy: int, logical_w: int, logical_h: int, origin_x: int, origin_y: int
) -> tuple[int, int]:
    """Virtual-desktop coord → (ax, ay) in the 0..65535 absolute range."""
    return (
        _abs_norm(gx - origin_x, logical_w),
        _abs_norm(gy - origin_y, logical_h),
    )


def move(
    gx: int,
    gy: int,
    *,
    logical_w: int,
    logical_h: int,
    origin_x: int = 0,
    origin_y: int = 0,
) -> None:
    """Move the pointer to virtual-desktop coordinate ``(gx, gy)``."""
    ax, ay = _abs_xy(gx, gy, logical_w, logical_h, origin_x, origin_y)
    _send([_mouse(ax, ay, _ABS_POS)])


_BUTTON_FLAGS: dict[str, tuple[int, int, int]] = {
    # button -> (down_flag, up_flag, mouse_data)
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, 0),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP, 0),
    "wheel": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP, 0),
    "back": (_MOUSEEVENTF_XDOWN, _MOUSEEVENTF_XUP, _XBUTTON1),
    "forward": (_MOUSEEVENTF_XDOWN, _MOUSEEVENTF_XUP, _XBUTTON2),
}


_CLICK_DWELL_S = 0.03  # brief button-hold so apps register a real click


def click(
    gx: int,
    gy: int,
    button: str,
    *,
    logical_w: int,
    logical_h: int,
    origin_x: int = 0,
    origin_y: int = 0,
) -> None:
    """Move to ``(gx, gy)`` then press+release ``button`` once.

    Both the press AND the release carry the ABSOLUTE target position in the
    same event rather than a bare relative ``(0, 0)`` after a separate
    positioning move, so a button transition is never applied at a stale
    pointer location — one event, one unambiguous position. A short dwell
    separates down from up because a zero-duration synthetic click is
    occasionally dropped or misread (e.g. treated as hover) by strict targets.
    """
    if button not in _BUTTON_FLAGS:
        raise DesktopError(f"invalid button: {button!r}", name="InputFailed")
    ax, ay = _abs_xy(gx, gy, logical_w, logical_h, origin_x, origin_y)
    down, up, data = _BUTTON_FLAGS[button]
    _send([_mouse(ax, ay, _ABS_POS | down, data)])
    time.sleep(_CLICK_DWELL_S)
    _send([_mouse(ax, ay, _ABS_POS | up, data)])


def double_click(
    gx: int,
    gy: int,
    *,
    logical_w: int,
    logical_h: int,
    origin_x: int = 0,
    origin_y: int = 0,
) -> None:
    """Left-button down/up twice at ``(gx, gy)``.

    Every event carries the ABSOLUTE target position (see :func:`click`) so no
    transition is applied at a stale pointer location. A brief dwell within and
    between the two press/release pairs keeps the gesture inside the OS
    double-click window while ensuring neither click is a zero-duration
    (droppable) event.
    """
    ax, ay = _abs_xy(gx, gy, logical_w, logical_h, origin_x, origin_y)
    down, up, _ = _BUTTON_FLAGS["left"]
    for _ in range(2):
        _send([_mouse(ax, ay, _ABS_POS | down)])
        time.sleep(_CLICK_DWELL_S)
        _send([_mouse(ax, ay, _ABS_POS | up)])
        time.sleep(_CLICK_DWELL_S)


# Synthetic-drag timing. Windows (especially the Explorer desktop) recognises a
# press+move+release as a DRAG only when the pointer is positioned ON the target,
# the button is HELD across real elapsed time, and it then crosses the drag
# threshold (SM_CXDRAG, ~4px) while down via a SEQUENCE of small moves rather
# than one teleport. This sequence is VERIFIED to move a real desktop icon: a
# measured run pressed at the icon's true centre and the icon relocated.
#
# NOTE on the "rubber-band selection instead of a drag" symptom: that is what
# Explorer does when the press lands on EMPTY desktop, i.e. a coordinate that
# misses the icon — it is not a timing failure. Verified with LVM_HITTEST: the
# missed coordinate reported no item, the icon's true centre reported the item.
# The dwells below are what makes the held-button gesture unambiguous; they do
# not (and cannot) compensate for a start point that is off-target.
_DRAG_MOVE_SETTLE_S = 0.06   # after positioning, before LEFTDOWN (land on icon)
_DRAG_PRESS_DWELL_S = 0.18   # hold after LEFTDOWN so "grab" registers, not select
_DRAG_NUDGE_PX = 8           # first move just past SM_CXDRAG to begin the drag
_DRAG_NUDGE_DWELL_S = 0.06   # let "drag begin" fire before the long move
_DRAG_STEP_DELAY_S = 0.02    # between interpolated move steps
_DRAG_RELEASE_DWELL_S = 0.12  # settle at target before LEFTUP
_DRAG_STEP_PX = 60           # max pixel distance per interpolated step


def _interpolate(
    a: tuple[int, int], b: tuple[int, int], step_px: int
) -> list[tuple[int, int]]:
    """Points from ``a`` (exclusive) to ``b`` (inclusive), <= ``step_px`` apart.

    A stepped path (rather than a single jump) is what makes Windows register
    a real drag: each intermediate move keeps the button-held gesture "live".
    """
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    dist = max(abs(dx), abs(dy))
    if dist <= step_px:
        return [b]
    n = (dist + step_px - 1) // step_px
    return [
        (a[0] + round(dx * i / n), a[1] + round(dy * i / n))
        for i in range(1, n + 1)
    ]


def drag(
    points: list[tuple[int, int]],
    *,
    logical_w: int,
    logical_h: int,
    origin_x: int = 0,
    origin_y: int = 0,
) -> None:
    """Left-drag: press at the first point, move through the rest, release.

    Uses a held button with dwell + interpolated stepped moves so Windows
    (including strict Explorer desktop icons) registers a genuine DRAG rather
    than a click that leaves the item in place. Verified against a real desktop
    icon: the icon relocates when ``points[0]`` is actually on it.

    ``points[0]`` MUST land on the target. A start point on empty desktop makes
    Explorer rubber-band select instead — no amount of dwell fixes an off-target
    press.

    The final ``LEFTUP`` is always emitted, even if an intermediate move
    fails, so the button never stays stuck down (book 04 §4.4).
    """
    if len(points) < 2:
        raise DesktopError("drag needs >= 2 points", name="InputFailed")
    first = points[0]
    # 1) Position ON the target and let it settle so the press lands exactly on
    #    the icon (a press a few px off starts a rubber-band selection instead).
    move(
        first[0], first[1],
        logical_w=logical_w, logical_h=logical_h,
        origin_x=origin_x, origin_y=origin_y,
    )
    time.sleep(_DRAG_MOVE_SETTLE_S)
    # 2) Press with the button anchored at the ABSOLUTE target position (single
    #    event: ABSOLUTE|VIRTUALDESK|MOVE|LEFTDOWN), not a bare relative (0,0)
    #    down — this removes any ambiguity about where the grab happens, on any
    #    monitor.
    ax, ay = _abs_xy(first[0], first[1], logical_w, logical_h, origin_x, origin_y)
    _send([_mouse(ax, ay, _ABS_POS | _MOUSEEVENTF_LEFTDOWN)])
    time.sleep(_DRAG_PRESS_DWELL_S)
    err: DesktopError | None = None
    prev = first
    try:
        # 3) Tiny nudge just past the drag threshold so Explorer fires
        #    "drag begin" (grab the icon) BEFORE the long move — otherwise a big
        #    first jump reads as a selection sweep.
        second = points[1]
        dx = second[0] - first[0]
        dy = second[1] - first[1]
        if dx or dy:
            norm = math.hypot(dx, dy) or 1.0
            nx = first[0] + round(dx / norm * _DRAG_NUDGE_PX)
            ny = first[1] + round(dy / norm * _DRAG_NUDGE_PX)
            move(
                nx, ny,
                logical_w=logical_w, logical_h=logical_h,
                origin_x=origin_x, origin_y=origin_y,
            )
            time.sleep(_DRAG_NUDGE_DWELL_S)
            prev = (nx, ny)
        # 4) Step to each waypoint in small increments (keeps the drag "live").
        for target in points[1:]:
            for gx, gy in _interpolate(prev, target, _DRAG_STEP_PX):
                move(
                    gx, gy,
                    logical_w=logical_w, logical_h=logical_h,
                    origin_x=origin_x, origin_y=origin_y,
                )
                time.sleep(_DRAG_STEP_DELAY_S)
            prev = target
        time.sleep(_DRAG_RELEASE_DWELL_S)
    except DesktopError as exc:  # keep going to the release
        err = exc
    finally:
        _send([_mouse(0, 0, _MOUSEEVENTF_LEFTUP)])
    if err is not None:
        raise err


def scroll(
    gx: int,
    gy: int,
    steps_x: int,
    steps_y: int,
    *,
    logical_w: int,
    logical_h: int,
    origin_x: int = 0,
    origin_y: int = 0,
) -> None:
    """Move to ``(gx, gy)`` then wheel-scroll by pre-computed steps.

    ``steps_y > 0`` means "content moves DOWN". Win32 ``WHEEL`` positive
    is "away from the user" (content UP), so the vertical sign is negated
    (book 04 §4.5).
    """
    move(
        gx, gy,
        logical_w=logical_w, logical_h=logical_h,
        origin_x=origin_x, origin_y=origin_y,
    )
    events: list[_INPUT] = []
    if steps_y != 0:
        wheel = -steps_y * _WHEEL_DELTA
        events.append(_mouse(0, 0, _MOUSEEVENTF_WHEEL, _to_dword(wheel)))
    if steps_x != 0:
        hwheel = steps_x * _WHEEL_DELTA
        events.append(_mouse(0, 0, _MOUSEEVENTF_HWHEEL, _to_dword(hwheel)))
    _send(events)


def _to_dword(value: int) -> int:
    """Two's-complement 32-bit for signed wheel deltas in a DWORD field."""
    return value & 0xFFFFFFFF


def type_text(text: str) -> None:
    """Type literal ``text`` via Unicode injection.

    Each character is emitted as its UTF-16 code unit(s); astral
    characters (emoji / BMP-external) become a surrogate pair, each unit
    sent as its own down/up (book 04 §4.6).
    """
    events: list[_INPUT] = []
    for code_unit in _utf16_code_units(text):
        events.append(_key_unicode(code_unit, up=False))
        events.append(_key_unicode(code_unit, up=True))
    _send(events)


def _utf16_code_units(text: str) -> list[int]:
    data = text.encode("utf-16-le")
    return [data[i] | (data[i + 1] << 8) for i in range(0, len(data), 2)]


def keypress(chords: list[str]) -> None:
    """Press a chord: press all keys in order, release in REVERSE order.

    Each element may be a ``+``-joined chord (e.g. ``"CTRL+L"``). Release
    always runs for every pressed key even if a release fails; the first
    error is re-raised afterward (book 04 §4.7). A component with no
    reliable VK is injected as a Unicode down/up in place.
    """
    components: list[str] = []
    for element in chords:
        components.extend(part for part in element.split("+"))

    pressed: list[int] = []  # VK codes pressed (for reverse release)
    first_err: DesktopError | None = None
    try:
        for name in components:
            resolved = resolve_key(name)
            if resolved.is_unicode:
                # No VK: inject the character as a self-contained tap.
                for code_unit in _utf16_code_units(resolved.char or ""):
                    _send([
                        _key_unicode(code_unit, up=False),
                        _key_unicode(code_unit, up=True),
                    ])
                continue
            vk = resolved.vk
            assert vk is not None
            _send([_key_vk(vk, up=False)])
            pressed.append(vk)
    except (DesktopError, ValueError) as exc:
        first_err = exc if isinstance(exc, DesktopError) else DesktopError(
            str(exc), name="InputFailed"
        )
    finally:
        for vk in reversed(pressed):
            try:
                _send([_key_vk(vk, up=True)])
            except DesktopError as exc:
                if first_err is None:
                    first_err = exc
    if first_err is not None:
        raise first_err


def press_keys(names: list[str]) -> None:
    """Press (key-down only) each named key in order.

    Used to hold modifier keys around a pointer action; the caller is
    responsible for the matching :func:`release_keys`.
    """
    for name in names:
        resolved = resolve_key(name)
        if resolved.vk is None:
            raise DesktopError(
                f"cannot hold non-VK key: {name!r}", name="InputFailed"
            )
        _send([_key_vk(resolved.vk, up=False)])


def release_keys(names: list[str]) -> None:
    """Release (key-up only) each named key in the given order.

    Every key is attempted even if one release fails; the first error is
    re-raised so a stuck modifier is never silently swallowed.
    """
    first_err: DesktopError | None = None
    for name in names:
        try:
            resolved = resolve_key(name)
            if resolved.vk is None:
                raise DesktopError(
                    f"cannot release non-VK key: {name!r}", name="InputFailed"
                )
            _send([_key_vk(resolved.vk, up=True)])
        except DesktopError as exc:
            if first_err is None:
                first_err = exc
    if first_err is not None:
        raise first_err

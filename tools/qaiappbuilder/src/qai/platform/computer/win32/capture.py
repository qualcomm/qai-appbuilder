# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Primary-display screenshot via Win32 GDI (ctypes only).

Captures the primary monitor with ``BitBlt``, optionally down-scales with
GDI ``StretchBlt`` (zero extra dependencies), and encodes PNG bytes via
GDI+ (``gdiplus.dll``, shipped with Windows — no third-party wheel).

DPI awareness is set once per process (Per-Monitor-V2, fallback
system-DPI-aware) BEFORE any geometry query so coordinates and pixels are
not virtualised (book 04 §1). All GDI handles are released in ``finally``.

This module touches ctypes/Win32 at import-safe module scope only for
symbol setup; every call checks return values and wraps failures in
:class:`~qai.platform.computer.types.DesktopError`.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from ..types import Capabilities, Capture, DesktopError, Display

__all__ = [
    "capture_primary",
    "capture_window",
    "probe_capabilities",
    "set_process_dpi_aware",
]


#: Composite-frame safety ceiling, aligned with the reference implementation's
#: ``MAX_COMPOSITE_PIXELS``. Generous on purpose: a multi-monitor virtual
#: desktop at native resolution is legitimately large, and ``_render_scale``
#: down-scales to fit rather than failing the capture.
_MAX_COMPOSITE_PIXELS = 268_435_456
_SRCCOPY = 0x00CC0020
_BI_RGB = 0
_DIB_RGB_COLORS = 0
_HALFTONE = 4
_DPI_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def _u32() -> "ctypes.WinDLL":
    return ctypes.WinDLL("user32", use_last_error=True)


def _gdi() -> "ctypes.WinDLL":
    return ctypes.WinDLL("gdi32", use_last_error=True)


# ---------------------------------------------------------------------------
# DPI awareness
# ---------------------------------------------------------------------------


def set_process_dpi_aware() -> None:
    """Make the process Per-Monitor-V2 DPI aware (fallback: system aware).

    Idempotent + best-effort: a failure to raise awareness is not fatal
    (a subsequent capture that comes back virtualised is still a valid,
    if scaled, screenshot), but we try the strongest mode first.
    """
    if sys.platform != "win32":
        return
    user32 = _u32()
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(_DPI_PER_MONITOR_AWARE_V2):
            return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware.restype = wintypes.BOOL
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

_SM_CXSCREEN = 0
_SM_CYSCREEN = 1


def _primary_size(user32: "ctypes.WinDLL") -> tuple[int, int]:
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    w = int(user32.GetSystemMetrics(_SM_CXSCREEN))
    h = int(user32.GetSystemMetrics(_SM_CYSCREEN))
    if w <= 0 or h <= 0:
        raise DesktopError(
            f"primary display has no area ({w}x{h})", name="CaptureFailed"
        )
    return w, h


# Virtual-desktop metrics. EVERY geometry value below is read at CAPTURE TIME —
# never cached, never assumed. Panel sizes differ per user, an external monitor
# can be plugged/unplugged mid-session, the arrangement may be left/right or
# above/below, and Duplicate<->Extend can be toggled at any moment. Only a live
# query reflects the truth.
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_SM_CMONITORS = 80

_MONITORINFOF_PRIMARY = 0x1


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


_MONITOR_ENUM_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    ctypes.POINTER(_RECT),
    wintypes.LPARAM,
)


def _virtual_desktop(user32: "ctypes.WinDLL") -> tuple[int, int, int, int]:
    """Live ``(x, y, width, height)`` of the whole virtual desktop.

    The origin can be NEGATIVE (a monitor placed left of / above the primary),
    so callers must offset by it rather than assuming ``(0, 0)``.
    """
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    x = int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
    y = int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
    w = int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
    h = int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    if w <= 0 or h <= 0:
        raise DesktopError(
            f"virtual desktop has no area ({w}x{h})", name="CaptureFailed"
        )
    return x, y, w, h


def _monitor_count(user32: "ctypes.WinDLL") -> int:
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    return max(1, int(user32.GetSystemMetrics(_SM_CMONITORS)))


def _enum_monitors(
    user32: "ctypes.WinDLL",
) -> list[tuple[str, int, int, int, int, bool]]:
    """Enumerate every monitor live: ``(device, x, y, w, h, is_primary)``.

    Coordinates are virtual-desktop coordinates (may be negative). Order is
    whatever Windows reports; the caller sorts so the primary is first.
    """
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(_RECT),
        _MONITOR_ENUM_PROC,
        wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [
        wintypes.HMONITOR,
        ctypes.POINTER(_MONITORINFOEXW),
    ]
    user32.GetMonitorInfoW.restype = wintypes.BOOL

    found: list[tuple[str, int, int, int, int, bool]] = []

    def _cb(hmon, _hdc, _rect, _lparam):  # noqa: ANN001 — ctypes callback
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append((
                str(info.szDevice),
                int(r.left),
                int(r.top),
                int(r.right - r.left),
                int(r.bottom - r.top),
                bool(info.dwFlags & _MONITORINFOF_PRIMARY),
            ))
        return True

    user32.EnumDisplayMonitors(None, None, _MONITOR_ENUM_PROC(_cb), 0)
    # Primary first, then left-to-right / top-to-bottom for a stable order.
    found.sort(key=lambda m: (not m[5], m[1], m[2]))
    return found


def _system_scale(user32: "ctypes.WinDLL") -> float:
    try:
        user32.GetDpiForSystem.restype = wintypes.UINT
        dpi = int(user32.GetDpiForSystem())
        if dpi > 0:
            return dpi / 96.0
    except (AttributeError, OSError):
        pass
    return 1.0


def _render_scale(
    logical_w: int, logical_h: int, max_w: int | None, max_h: int | None
) -> float:
    """Scale factor for the captured frame (never up-scales).

    ``max_w`` / ``max_height`` of :data:`None` (the default) mean capture at the
    display's NATIVE size, so screenshot pixels stay 1:1 with screen pixels and
    the coordinates the model reads map exactly onto what we click. Whatever the
    caller asks for, the frame is additionally clamped to fit
    :data:`_MAX_COMPOSITE_PIXELS` — an enormous desktop degrades to a smaller
    (still usable) frame instead of failing the capture outright.
    """
    scale = 1.0
    if max_w:
        scale = min(scale, max_w / logical_w)
    if max_h:
        scale = min(scale, max_h / logical_h)
    # Safety clamp: keep the frame under the pixel guard by area.
    px = logical_w * scale * logical_h * scale
    if px > _MAX_COMPOSITE_PIXELS:
        scale *= (_MAX_COMPOSITE_PIXELS / px) ** 0.5
    return scale


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def probe_capabilities() -> Capabilities:
    """Probe capture/input capability without taking a full screenshot.

    Success = we can obtain the screen DC and query primary geometry.
    """
    if sys.platform != "win32":
        return Capabilities(
            backend="unavailable",
            display_server=None,
            capture=False,
            input=False,
            capture_permission="unavailable",
            input_permission="unavailable",
            display_count=0,
        )
    set_process_dpi_aware()
    user32 = _u32()
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    hdc = user32.GetDC(None)
    if not hdc:
        return Capabilities(
            backend="unavailable",
            display_server="win32",
            capture=False,
            input=False,
            capture_permission="denied",
            input_permission="denied",
            display_count=1,
        )
    try:
        _primary_size(user32)
    except DesktopError:
        return Capabilities(
            backend="unavailable",
            display_server="win32",
            capture=False,
            input=False,
            capture_permission="denied",
            input_permission="unknown",
            display_count=1,
        )
    finally:
        user32.ReleaseDC(None, hdc)
    return Capabilities(
        backend="win32",
        display_server="win32",
        capture=True,
        input=True,
        capture_permission="granted",
        input_permission="granted",
        # Live count — reflects an external monitor being plugged in or the
        # Duplicate<->Extend toggle without needing a restart.
        display_count=_monitor_count(user32),
    )


def capture_primary(
    *,
    max_width: int | None,
    max_height: int | None,
    display: str = "all",
) -> Capture:
    """Capture + PNG-encode the desktop, spanning every monitor by default.

    ALL geometry is detected at call time (``_enum_monitors`` /
    ``_virtual_desktop``) — nothing is cached or assumed — so plugging in an
    external monitor, changing a resolution, rearranging panels, or toggling
    Duplicate<->Extend is picked up on the very next screenshot without a
    restart. Panel sizes and arrangements differ per user, which is exactly why
    they must be measured rather than configured.

    ``display`` selects what to capture:

    * ``"all"`` (default) — the whole virtual desktop, so a window on ANY
      monitor is visible and clickable. In Extend mode the composite spans
      every panel; in Duplicate mode the panels show the same pixels so the
      composite is effectively the shared image.
    * ``"primary"`` — the primary monitor only (smaller image / fewer tokens).
    * a device name (e.g. ``\\\\.\\DISPLAY1``) — that monitor only.

    Coordinates in the returned :class:`Capture` are pixels of THIS image; the
    session layer maps them back through the reported ``Display`` geometry, and
    the input layer positions against the same virtual-desktop origin, so a
    negative origin (monitor left of / above the primary) stays correct.

    Down-scales with ``StretchBlt`` only when a cap is set or the frame would
    exceed the pixel guard (never up-scales).
    """
    if sys.platform != "win32":
        raise DesktopError(
            "screenshot only available on Windows", name="CaptureFailed"
        )
    set_process_dpi_aware()
    user32 = _u32()
    gdi = _gdi()

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

    # --- live geometry -----------------------------------------------------
    monitors = _enum_monitors(user32)
    if not monitors:
        # Degrade to the primary metrics rather than failing the capture.
        pw, ph = _primary_size(user32)
        monitors = [("primary", 0, 0, pw, ph, True)]
    wanted = (display or "all").strip()
    if wanted.lower() == "primary":
        chosen = [m for m in monitors if m[5]] or monitors[:1]
    elif wanted.lower() == "all":
        chosen = monitors
    else:
        chosen = [m for m in monitors if m[0] == wanted]
        if not chosen:  # unknown id → widest useful default
            chosen = monitors
    if len(chosen) == 1:
        src_x, src_y, src_w, src_h = chosen[0][1], chosen[0][2], chosen[0][3], chosen[0][4]
    else:
        src_x, src_y, src_w, src_h = _virtual_desktop(user32)

    scale = _system_scale(user32)
    render_scale = _render_scale(src_w, src_h, max_width, max_height)
    target_w = max(1, round(src_w * render_scale))
    target_h = max(1, round(src_h * render_scale))
    # ``_render_scale`` already clamps the area under the guard; this is a
    # defensive backstop against a rounding edge (never expected to fire).
    if target_w * target_h > _MAX_COMPOSITE_PIXELS:
        raise DesktopError(
            f"composite exceeds pixel cap ({target_w}x{target_h})",
            name="CaptureFailed",
        )

    for fn, args, res in (
        (gdi.CreateCompatibleDC, [wintypes.HDC], wintypes.HDC),
        (gdi.CreateCompatibleBitmap, [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
        (gdi.SelectObject, [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
        (gdi.DeleteObject, [wintypes.HGDIOBJ], wintypes.BOOL),
        (gdi.DeleteDC, [wintypes.HDC], wintypes.BOOL),
        (gdi.SetStretchBltMode, [wintypes.HDC, ctypes.c_int], ctypes.c_int),
    ):
        fn.argtypes = args
        fn.restype = res
    gdi.StretchBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi.StretchBlt.restype = wintypes.BOOL
    gdi.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
    ]
    gdi.GetDIBits.restype = ctypes.c_int

    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        raise DesktopError("GetDC(None) failed", name="CaptureFailed")
    hdc_mem = None
    hbmp = None
    old_obj = None
    try:
        hdc_mem = gdi.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            raise DesktopError("CreateCompatibleDC failed", name="CaptureFailed")
        hbmp = gdi.CreateCompatibleBitmap(hdc_screen, target_w, target_h)
        if not hbmp:
            raise DesktopError(
                "CreateCompatibleBitmap failed", name="CaptureFailed"
            )
        old_obj = gdi.SelectObject(hdc_mem, hbmp)
        gdi.SetStretchBltMode(hdc_mem, _HALFTONE)
        # Source origin is the virtual-desktop offset (can be negative when a
        # monitor sits left of / above the primary) — never assumed to be 0,0.
        ok = gdi.StretchBlt(
            hdc_mem, 0, 0, target_w, target_h,
            hdc_screen, src_x, src_y, src_w, src_h, _SRCCOPY,
        )
        if not ok:
            raise DesktopError(
                f"StretchBlt failed (err={ctypes.get_last_error()})",
                name="CaptureFailed",
            )
        rgba = _read_pixels(gdi, hdc_mem, hbmp, target_w, target_h)
    finally:
        if old_obj:
            gdi.SelectObject(hdc_mem, old_obj)
        if hbmp:
            gdi.DeleteObject(hbmp)
        if hdc_mem:
            gdi.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)

    png = _encode_png(rgba, target_w, target_h)
    # One Display per captured monitor. ``x/y`` are virtual-desktop logical
    # coords; ``pixel_*`` locate that panel inside THIS composite image so the
    # session layer can map an image pixel back to a screen coordinate.
    displays: list[Display] = []
    for dev, mx, my, mw, mh, prim in chosen:
        displays.append(
            Display(
                id=dev,
                name=("Primary" if prim else dev),
                x=mx,
                y=my,
                width=mw,
                height=mh,
                scale=scale,
                pixel_x=round((mx - src_x) * render_scale),
                pixel_y=round((my - src_y) * render_scale),
                pixel_width=max(1, round(mw * render_scale)),
                pixel_height=max(1, round(mh * render_scale)),
                is_primary=prim,
            )
        )
    return Capture(
        data=png,
        width=target_w,
        height=target_h,
        displays=tuple(displays),
        backend="win32",
        display_server="win32",
        capture_permission="granted",
        input_permission="granted",
    )

# ---------------------------------------------------------------------------
# Window-scoped capture
# ---------------------------------------------------------------------------

#: ``PrintWindow`` flag that renders the window's full content, including the
#: parts covered by other windows and (unlike ``BitBlt`` on the screen DC) the
#: DirectComposition surfaces modern apps draw into.
_PW_RENDERFULLCONTENT = 0x00000002


def capture_window(
    hwnd: int,
    *,
    max_width: int | None = None,
    max_height: int | None = None,
) -> Capture:
    """Capture ONE window's pixels, even when it is partially covered.

    Why this exists alongside :func:`capture_primary`: a full-desktop frame
    forces the model to locate the application it cares about inside the whole
    virtual desktop, and every coordinate it then derives is invalidated the
    moment the window moves or another window covers it. A window-scoped frame
    contains only the target, so the image stays valid regardless of z-order.

    ``PrintWindow`` asks the window to render itself, which is what makes an
    occluded window capturable — a screen ``BitBlt`` would copy whatever is on
    top instead. The returned :class:`Capture` reports a single
    :class:`Display` whose logical rect is the WINDOW's screen rect, so the
    session layer maps image pixels back to real screen coordinates using
    exactly the same arithmetic as a desktop frame.
    """
    if sys.platform != "win32":
        raise DesktopError(
            "window capture requires Windows", name="Unavailable"
        )
    set_process_dpi_aware()
    user32 = _u32()
    gdi = _gdi()

    if not hwnd or not user32.IsWindow(wintypes.HWND(int(hwnd))):
        raise DesktopError(f"not a window: 0x{int(hwnd):X}", name="CaptureFailed")

    rect = _RECT()
    if not user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
        raise DesktopError(
            f"GetWindowRect failed for 0x{int(hwnd):X}", name="CaptureFailed"
        )
    win_x, win_y = int(rect.left), int(rect.top)
    src_w = int(rect.right) - win_x
    src_h = int(rect.bottom) - win_y
    if src_w <= 0 or src_h <= 0:
        raise DesktopError(
            f"window 0x{int(hwnd):X} has no area ({src_w}x{src_h}); "
            "it is probably minimised",
            name="CaptureFailed",
        )

    scale = _system_scale(user32)
    render_scale = _render_scale(src_w, src_h, max_width, max_height)
    target_w = max(1, round(src_w * render_scale))
    target_h = max(1, round(src_h * render_scale))

    # Win32 handles are pointer-sized. ctypes surfaces them as plain Python
    # ints, and a 64-bit handle is a value far larger than the default ``c_int``
    # a bare foreign call assumes for its arguments — passing one through
    # raises ``OverflowError: int too long to convert``. Every handle is
    # therefore wrapped in ``c_void_p`` at the call site, which is the only
    # form that round-trips a full 64-bit handle regardless of its value.
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        raise DesktopError("GetDC(NULL) failed", name="CaptureFailed")

    gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleBitmap.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int
    ]
    gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi.SelectObject.restype = ctypes.c_void_p
    gdi.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi.DeleteDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.PrintWindow.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT
    ]
    user32.PrintWindow.restype = wintypes.BOOL

    hdc_mem = None
    hbmp = None
    old_obj = None
    try:
        hdc_mem = gdi.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            raise DesktopError("CreateCompatibleDC failed", name="CaptureFailed")
        # Render at native size first: PrintWindow cannot scale, so any
        # downscale happens afterwards via StretchBlt.
        hbmp = gdi.CreateCompatibleBitmap(hdc_screen, src_w, src_h)
        if not hbmp:
            raise DesktopError(
                "CreateCompatibleBitmap failed", name="CaptureFailed"
            )
        old_obj = gdi.SelectObject(hdc_mem, hbmp)
        if not user32.PrintWindow(
            ctypes.c_void_p(int(hwnd)), hdc_mem, _PW_RENDERFULLCONTENT
        ):
            raise DesktopError(
                f"PrintWindow failed for 0x{int(hwnd):X} "
                f"(error {ctypes.get_last_error()})",
                name="CaptureFailed",
            )
        if (target_w, target_h) == (src_w, src_h):
            rgba = _read_pixels(gdi, hdc_mem, hbmp, src_w, src_h)
        else:
            rgba = _downscale(
                gdi, hdc_screen, hdc_mem, src_w, src_h, target_w, target_h
            )
    finally:
        if old_obj:
            gdi.SelectObject(hdc_mem, old_obj)
        if hbmp:
            gdi.DeleteObject(hbmp)
        if hdc_mem:
            gdi.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)

    png = _encode_png(rgba, target_w, target_h)
    # A window frame reports exactly one panel: the window itself. Its logical
    # rect is the window's SCREEN rect, so image-pixel -> screen-coordinate
    # mapping needs no special case in the session layer.
    display = Display(
        id=f"window:0x{int(hwnd):X}",
        name=_window_title(user32, int(hwnd)) or f"window 0x{int(hwnd):X}",
        x=win_x,
        y=win_y,
        width=src_w,
        height=src_h,
        scale=scale,
        pixel_x=0,
        pixel_y=0,
        pixel_width=target_w,
        pixel_height=target_h,
        is_primary=False,
    )
    return Capture(
        data=png,
        width=target_w,
        height=target_h,
        displays=(display,),
        backend="win32",
        display_server="win32",
        capture_permission="granted",
        input_permission="granted",
    )


def _window_title(user32: "ctypes.WinDLL", hwnd: int) -> str:
    handle = ctypes.c_void_p(hwnd)
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    length = int(user32.GetWindowTextLengthW(handle))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int
    ]
    user32.GetWindowTextW(handle, buf, length + 1)
    return buf.value


def _downscale(
    gdi: "ctypes.WinDLL",
    hdc_screen: "ctypes.c_void_p",
    hdc_src: "ctypes.c_void_p",
    src_w: int,
    src_h: int,
    dst_w: int,
    dst_h: int,
) -> bytes:
    """StretchBlt ``hdc_src`` into a smaller bitmap and read its pixels.

    Handles arrive already wrapped as ``c_void_p`` (see the note in
    :func:`capture_window`); ``StretchBlt`` and ``SetStretchBltMode`` need the
    same treatment because a bare call would marshal a 64-bit DC as ``c_int``.
    """
    gdi.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdi.StretchBlt.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD,
    ]
    hdc_dst = None
    hbmp_dst = None
    old_obj = None
    try:
        hdc_dst = gdi.CreateCompatibleDC(hdc_screen)
        hbmp_dst = gdi.CreateCompatibleBitmap(hdc_screen, dst_w, dst_h)
        if not hdc_dst or not hbmp_dst:
            raise DesktopError(
                "failed to allocate downscale bitmap", name="CaptureFailed"
            )
        old_obj = gdi.SelectObject(hdc_dst, hbmp_dst)
        gdi.SetStretchBltMode(hdc_dst, _HALFTONE)
        if not gdi.StretchBlt(
            hdc_dst, 0, 0, dst_w, dst_h,
            hdc_src, 0, 0, src_w, src_h,
            _SRCCOPY,
        ):
            raise DesktopError("StretchBlt failed", name="CaptureFailed")
        return _read_pixels(gdi, hdc_dst, hbmp_dst, dst_w, dst_h)
    finally:
        if old_obj:
            gdi.SelectObject(hdc_dst, old_obj)
        if hbmp_dst:
            gdi.DeleteObject(hbmp_dst)
        if hdc_dst:
            gdi.DeleteDC(hdc_dst)

def _read_pixels(
    gdi: "ctypes.WinDLL",
    hdc_mem: "ctypes.c_void_p",
    hbmp: "ctypes.c_void_p",
    w: int,
    h: int,
) -> bytes:
    """Read 32-bit top-down BGRA pixels and swap to RGBA."""
    info = _BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h  # negative => top-down rows
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = _BI_RGB
    buf = (ctypes.c_char * (w * h * 4))()
    # Pointer-sized handles must be declared; see the note in capture_window.
    gdi.GetDIBits.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
    ]
    scanned = gdi.GetDIBits(
        hdc_mem, hbmp, 0, h, buf, ctypes.byref(info), _DIB_RGB_COLORS
    )
    if scanned != h:
        raise DesktopError(
            f"GetDIBits returned {scanned}/{h} rows", name="CaptureFailed"
        )
    raw = bytes(buf)
    out = bytearray(len(raw))
    # BGRA -> RGBA, force alpha 255 (screenshots are opaque).
    out[0::4] = raw[2::4]
    out[1::4] = raw[1::4]
    out[2::4] = raw[0::4]
    out[3::4] = b"\xff" * (w * h)
    return bytes(out)


# ---------------------------------------------------------------------------
# PNG encode via GDI+
# ---------------------------------------------------------------------------


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


_PNG_CLSID_PARTS = (0x557CF406, 0x1A04, 0x11D3, 0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _png_clsid() -> _GUID:
    g = _GUID()
    g.Data1 = _PNG_CLSID_PARTS[0]
    g.Data2 = _PNG_CLSID_PARTS[1]
    g.Data3 = _PNG_CLSID_PARTS[2]
    for i in range(8):
        g.Data4[i] = _PNG_CLSID_PARTS[3 + i]
    return g


def _encode_png(rgba: bytes, w: int, h: int) -> bytes:
    """Encode RGBA bytes to PNG via GDI+ (in-memory IStream)."""
    gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)

    token = ctypes.c_void_p()
    startup = _GdiplusStartupInput()
    startup.GdiplusVersion = 1
    gdiplus.GdiplusStartup.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(_GdiplusStartupInput),
        ctypes.c_void_p,
    ]
    gdiplus.GdiplusStartup.restype = ctypes.c_int
    status = gdiplus.GdiplusStartup(
        ctypes.byref(token), ctypes.byref(startup), None
    )
    if status != 0:
        raise DesktopError(
            f"GdiplusStartup failed ({status})", name="CaptureFailed"
        )
    stream = ctypes.c_void_p()
    bitmap = ctypes.c_void_p()
    try:
        # GDI+ wants BGRA (PixelFormat32bppARGB=0x26200A) with the source
        # in native byte order; convert RGBA back to BGRA for the buffer.
        bgra = bytearray(len(rgba))
        bgra[0::4] = rgba[2::4]
        bgra[1::4] = rgba[1::4]
        bgra[2::4] = rgba[0::4]
        bgra[3::4] = rgba[3::4]
        buf = (ctypes.c_char * len(bgra)).from_buffer(bgra)

        gdiplus.GdipCreateBitmapFromScan0.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        gdiplus.GdipCreateBitmapFromScan0.restype = ctypes.c_int
        stride = w * 4
        pixel_format = 0x0026200A  # PixelFormat32bppARGB
        st = gdiplus.GdipCreateBitmapFromScan0(
            w, h, stride, pixel_format, buf, ctypes.byref(bitmap)
        )
        if st != 0:
            raise DesktopError(
                f"GdipCreateBitmapFromScan0 failed ({st})", name="CaptureFailed"
            )
        ole32.CreateStreamOnHGlobal.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, ctypes.POINTER(ctypes.c_void_p)
        ]
        ole32.CreateStreamOnHGlobal.restype = ctypes.c_int
        hr = ole32.CreateStreamOnHGlobal(None, True, ctypes.byref(stream))
        if hr != 0:
            raise DesktopError(
                f"CreateStreamOnHGlobal failed ({hr})", name="CaptureFailed"
            )
        clsid = _png_clsid()
        gdiplus.GdipSaveImageToStream.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(_GUID), ctypes.c_void_p,
        ]
        gdiplus.GdipSaveImageToStream.restype = ctypes.c_int
        st = gdiplus.GdipSaveImageToStream(
            bitmap, stream, ctypes.byref(clsid), None
        )
        if st != 0:
            raise DesktopError(
                f"GdipSaveImageToStream failed ({st})", name="CaptureFailed"
            )
        return _read_istream(ole32, stream)
    finally:
        if bitmap:
            gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
            gdiplus.GdipDisposeImage(bitmap)
        if stream:
            # IStream::Release via the vtable.
            _release_com(stream)
        gdiplus.GdiplusShutdown.argtypes = [ctypes.c_void_p]
        gdiplus.GdiplusShutdown(token)


def _read_istream(ole32: "ctypes.WinDLL", stream: ctypes.c_void_p) -> bytes:
    """Pull all bytes out of an IStream created on an HGLOBAL."""
    ole32.GetHGlobalFromStream.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    ]
    ole32.GetHGlobalFromStream.restype = ctypes.c_int
    hglobal = ctypes.c_void_p()
    hr = ole32.GetHGlobalFromStream(stream, ctypes.byref(hglobal))
    if hr != 0:
        raise DesktopError(
            f"GetHGlobalFromStream failed ({hr})", name="CaptureFailed"
        )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    size = int(kernel32.GlobalSize(hglobal))
    ptr = kernel32.GlobalLock(hglobal)
    if not ptr or size <= 0:
        raise DesktopError("empty PNG stream", name="CaptureFailed")
    try:
        return ctypes.string_at(ptr, size)
    finally:
        kernel32.GlobalUnlock(hglobal)


def _release_com(iface: ctypes.c_void_p) -> None:
    """Call ``IUnknown::Release`` (vtable slot 2) on a COM interface."""
    vtable = ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))[0]
    release_ptr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[2]
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(release_ptr)
    release(iface)

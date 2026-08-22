# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Desktop-icon geometry, read from the shell instead of guessed from pixels.

Why this exists
---------------
A model driving the ``computer`` tool can only ESTIMATE where a small target sits
by looking at a screenshot. A desktop icon's grab area is roughly 114x76 px on a
2560x1440 panel, so an estimate that is off by a few dozen pixels lands on empty
desktop — and Explorer then rubber-band SELECTS instead of dragging, which looks
like "drag is broken" when the gesture itself was fine.

Windows already knows the exact rectangles. This module asks it (``LVM_GETITEMRECT``
on the desktop's ``SysListView32``) so the model can address an icon by NAME and
get a coordinate that is right by construction. Same idea as using an accessibility
tree instead of eyeballing a picture.

Coordinate space
----------------
Rectangles are returned in the SAME space the input layer drives: DPI-aware
physical screen pixels, which is also the space of a native-resolution
screenshot. The process is made DPI aware before any geometry query (shared with
:mod:`.capture`), so ``GetWindowRect`` on the listview and the item rects agree
with what ``move``/``click``/``drag`` expect — no scaling is applied or needed.

Failure policy
--------------
Every failure degrades to an empty list rather than raising: icon lookup is an
assist, and a shell that does not expose a classic desktop listview (or a
different shell entirely) must not break the tool.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

__all__ = ["list_desktop_icons"]

_LVM_FIRST = 0x1000
_LVM_GETITEMCOUNT = _LVM_FIRST + 4
_LVM_GETITEMRECT = _LVM_FIRST + 14
_LVM_GETITEMTEXTW = _LVM_FIRST + 115
#: ``LVIR_ICON`` — the icon glyph's box, i.e. the reliable grab target. The
#: label box (``LVIR_LABEL``) is clickable too but narrower and easier to miss.
_LVIR_ICON = 1
_LVIF_TEXT = 0x0001

_PROCESS_VM_OPERATION = 0x0008
_PROCESS_VM_READ = 0x0010
_PROCESS_VM_WRITE = 0x0020
_MEM_COMMIT = 0x1000
_MEM_RESERVE = 0x2000
_MEM_RELEASE = 0x8000
_PAGE_READWRITE = 0x04
#: Scratch space in the shell process: one RECT/LVITEM plus a text buffer.
_REMOTE_BYTES = 4096
_TEXT_OFFSET = 1024
_TEXT_CHARS = 260


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _LVITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", wintypes.UINT),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", wintypes.UINT),
        ("stateMask", wintypes.UINT),
        ("pszText", ctypes.c_void_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", wintypes.LPARAM),
        ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int),
        ("cColumns", wintypes.UINT),
        ("puColumns", ctypes.c_void_p),
        ("piColFmt", ctypes.c_void_p),
        ("iGroup", ctypes.c_int),
    ]


def _find_desktop_listview(user32: ctypes.WinDLL) -> int:
    """Locate the desktop icon list view.

    Normally ``Progman > SHELLDLL_DefView > SysListView32``. After a wallpaper
    slideshow transition the shell re-parents ``SHELLDLL_DefView`` under one of
    several ``WorkerW`` windows, so fall back to scanning those — otherwise icon
    lookup would silently stop working depending on wallpaper settings.
    """
    progman = user32.FindWindowW("Progman", None)
    def_view = (
        user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
        if progman
        else 0
    )
    if not def_view:
        worker = 0
        while True:
            worker = user32.FindWindowExW(0, worker, "WorkerW", None)
            if not worker:
                break
            found = user32.FindWindowExW(worker, None, "SHELLDLL_DefView", None)
            if found:
                def_view = found
                break
    if not def_view:
        return 0
    return user32.FindWindowExW(def_view, None, "SysListView32", None)


def list_desktop_icons() -> list[dict[str, Any]]:
    """Return every desktop icon as ``{name, x, y, width, height, center_x, center_y}``.

    Coordinates are DPI-aware physical screen pixels — the same space a
    native-resolution screenshot and the input primitives use — so
    ``center_x``/``center_y`` can be passed straight to ``click`` or as a
    ``drag`` start point.

    Returns an empty list on any failure (non-Windows, no classic desktop,
    cross-process read denied); callers treat it as "unavailable", never fatal.
    """
    if sys.platform != "win32":
        return []
    # Reuse the capture module's awareness call so geometry here and pixels
    # there are in one space; it is idempotent and best-effort.
    from . import capture as _capture

    _capture.set_process_dpi_aware()

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    listview = _find_desktop_listview(user32)
    if not listview:
        return []

    lv_rect = wintypes.RECT()
    if not user32.GetWindowRect(listview, ctypes.byref(lv_rect)):
        return []

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(listview, ctypes.byref(pid))
    # The list view lives in explorer.exe, so its item rects/text must be read
    # through that process's address space.
    handle = kernel32.OpenProcess(
        _PROCESS_VM_OPERATION | _PROCESS_VM_READ | _PROCESS_VM_WRITE,
        False,
        pid.value,
    )
    if not handle:
        return []

    remote = kernel32.VirtualAllocEx(
        handle, None, _REMOTE_BYTES, _MEM_COMMIT | _MEM_RESERVE, _PAGE_READWRITE
    )
    if not remote:
        kernel32.CloseHandle(handle)
        return []

    icons: list[dict[str, Any]] = []
    written = ctypes.c_size_t()
    try:
        count = int(user32.SendMessageW(listview, _LVM_GETITEMCOUNT, 0, 0))
        for index in range(count):
            text_remote = remote + _TEXT_OFFSET
            item = _LVITEMW()
            item.mask = _LVIF_TEXT
            item.iItem = index
            item.iSubItem = 0
            item.pszText = text_remote
            item.cchTextMax = _TEXT_CHARS
            if not kernel32.WriteProcessMemory(
                handle, remote, ctypes.byref(item), ctypes.sizeof(item),
                ctypes.byref(written),
            ):
                continue
            user32.SendMessageW(listview, _LVM_GETITEMTEXTW, index, remote)
            name_buf = ctypes.create_unicode_buffer(_TEXT_CHARS)
            kernel32.ReadProcessMemory(
                handle, text_remote, name_buf, _TEXT_CHARS * 2,
                ctypes.byref(written),
            )

            # LVM_GETITEMRECT takes the wanted part in rect.left and writes the
            # result back over the same struct, in listview CLIENT coords.
            probe = _RECT(_LVIR_ICON, 0, 0, 0)
            if not kernel32.WriteProcessMemory(
                handle, remote, ctypes.byref(probe), ctypes.sizeof(probe),
                ctypes.byref(written),
            ):
                continue
            if not user32.SendMessageW(listview, _LVM_GETITEMRECT, index, remote):
                continue
            got = _RECT()
            if not kernel32.ReadProcessMemory(
                handle, remote, ctypes.byref(got), ctypes.sizeof(got),
                ctypes.byref(written),
            ):
                continue

            left = int(got.left) + int(lv_rect.left)
            top = int(got.top) + int(lv_rect.top)
            width = int(got.right - got.left)
            height = int(got.bottom - got.top)
            if width <= 0 or height <= 0:
                continue
            icons.append({
                "name": name_buf.value,
                "x": left,
                "y": top,
                "width": width,
                "height": height,
                "center_x": left + width // 2,
                "center_y": top + height // 2,
            })
    finally:
        kernel32.VirtualFreeEx(handle, remote, 0, _MEM_RELEASE)
        kernel32.CloseHandle(handle)
    return icons

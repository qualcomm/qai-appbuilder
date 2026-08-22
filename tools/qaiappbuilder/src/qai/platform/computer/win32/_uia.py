# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Minimal ctypes COM plumbing for UI Automation (no third-party deps).

Why hand-rolled COM instead of ``comtypes``/``pywin32``: UI Automation is the
only reliable way to read control names, roles and rectangles out of modern
apps (WinUI / Electron / Chromium expose nothing useful through the classic
window-message APIs), but adding a COM wrapper package would put a
Windows-only runtime dependency in the core install. ``ctypes`` ships with
CPython, so the binding below keeps the dependency budget at zero while
staying inside the platform-specific ``win32`` package.

Threading is the subtle part. UI Automation is apartment-threaded COM: the
proxy is bound to the thread that created it, and calling ``CoInitializeEx``
on an existing thread permanently changes that thread's apartment. The desktop
worker already owns a thread dedicated to screen capture (GDI handles are
thread-affine too), so initialising COM there would entangle two unrelated
affinities and can deadlock. Therefore this module owns a PRIVATE daemon
thread: it initialises COM once, creates the automation root, and executes
every request handed to it through a queue. Callers see a plain synchronous
API and the capture thread is never touched.
"""

from __future__ import annotations

import contextlib
import ctypes
import queue
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any, TypeVar

__all__ = [
    "AX_UNAVAILABLE",
    "ComError",
    "Element",
    "call_in_apartment",
    "element_from_handle",
    "focused_element",
    "root_element",
    "shutdown",
]

_T = TypeVar("_T")

#: Message used when the platform cannot provide UI Automation at all.
AX_UNAVAILABLE = "UI Automation is unavailable on this platform"


class ComError(Exception):
    """A COM call returned a failure ``HRESULT``."""

    def __init__(self, message: str, *, hresult: int = 0) -> None:
        super().__init__(message)
        self.hresult = hresult


# ---------------------------------------------------------------------------
# Library handles
# ---------------------------------------------------------------------------

_S_OK = 0
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1

#: ``CUIAutomation`` coclass and the ``IUIAutomation`` interface it implements.
_CLSID_CUIAUTOMATION = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
_IID_IUIAUTOMATION = "{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


def _libs() -> tuple[ctypes.WinDLL, ctypes.WinDLL] | None:
    if sys.platform != "win32":
        return None
    try:
        return (
            ctypes.WinDLL("ole32", use_last_error=True),
            ctypes.WinDLL("oleaut32", use_last_error=True),
        )
    except OSError:
        return None


# ---------------------------------------------------------------------------
# vtable dispatch
#
# A COM interface pointer points at a pointer to its vtable, an array of
# function pointers. The first three slots are always ``IUnknown``
# (QueryInterface / AddRef / Release), so interface methods start at slot 3 in
# declaration order. Prototypes are cached because building a ``WINFUNCTYPE``
# is comparatively expensive and the tree walk performs thousands of calls.
# ---------------------------------------------------------------------------

_SLOT_RELEASE = 2

_proto_cache: dict[tuple[Any, ...], Any] = {}


def _prototype(argtypes: tuple[Any, ...]) -> Any:
    proto = _proto_cache.get(argtypes)
    if proto is None:
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
        _proto_cache[argtypes] = proto
    return proto


def _argtype_of(arg: Any) -> Any:
    """The ``_argtypes_``-legal ctypes type describing ``arg``.

    ``ctypes.byref(x)`` yields a ``CArgObject``, which is accepted as a call
    ARGUMENT but is not a valid ``_argtypes_`` ENTRY (it has no
    ``from_param``). Out-params are always pointer-sized, so describe them as
    ``c_void_p``; concrete simple types (``c_int``, ``c_void_p``) pass through
    so the marshaller still checks them.
    """
    kind = type(arg)
    return kind if hasattr(kind, "from_param") else ctypes.c_void_p


def _vcall(ptr: ctypes.c_void_p, slot: int, *args: Any) -> int:
    """Invoke vtable ``slot`` on ``ptr``; returns the raw ``HRESULT``."""
    vtable = ctypes.cast(
        ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents
    fn = _prototype(tuple(_argtype_of(a) for a in args))(vtable[slot])
    return int(fn(ptr, *args))


def _check(hr: int, what: str) -> None:
    if hr != _S_OK:
        raise ComError(f"{what} failed (0x{hr & 0xFFFFFFFF:08X})", hresult=hr)


# ---------------------------------------------------------------------------
# Apartment thread
# ---------------------------------------------------------------------------


class _Apartment:
    """Owns the COM apartment and the automation root on a private thread."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[tuple[Callable[[], Any], queue.Queue[Any]]] = (
            queue.Queue()
        )
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._automation: ctypes.c_void_p | None = None
        self._init_error: str | None = None
        self._ready = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def _ensure(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._init_error = None
            self._thread = threading.Thread(
                target=self._serve, name="uia-apartment", daemon=True
            )
            self._thread.start()
        self._ready.wait(timeout=20.0)

    def _serve(self) -> None:
        libs = _libs()
        if libs is None:
            self._init_error = AX_UNAVAILABLE
            self._ready.set()
            return
        ole32, _oleaut = libs
        try:
            ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
            clsid = _guid(ole32, _CLSID_CUIAUTOMATION)
            iid = _guid(ole32, _IID_IUIAUTOMATION)
            ptr = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid),
                None,
                _CLSCTX_INPROC_SERVER,
                ctypes.byref(iid),
                ctypes.byref(ptr),
            )
            if hr != _S_OK or not ptr:
                self._init_error = (
                    f"CoCreateInstance(CUIAutomation) failed "
                    f"(0x{hr & 0xFFFFFFFF:08X})"
                )
                self._ready.set()
                return
            self._automation = ptr
        except Exception as exc:
            self._init_error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return
        self._ready.set()

        while True:
            job, reply = self._jobs.get()
            if job is _STOP_JOB:
                reply.put((True, None))
                return
            try:
                reply.put((True, job()))
            except Exception as exc:
                reply.put((False, exc))

    # -- dispatch -----------------------------------------------------------

    def run(self, job: Callable[[], _T]) -> _T:
        self._ensure()
        if self._init_error is not None:
            raise ComError(self._init_error)
        reply: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._jobs.put((job, reply))
        ok, value = reply.get()
        if not ok:
            raise value
        return value  # type: ignore[return-value]

    @property
    def automation(self) -> ctypes.c_void_p:
        if self._automation is None:
            raise ComError(self._init_error or AX_UNAVAILABLE)
        return self._automation

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is None or not thread.is_alive():
            return
        reply: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._jobs.put((_STOP_JOB, reply))
        # Shutdown is best effort: if the worker is wedged we still drop our
        # references and let the daemon thread die with the process.
        with contextlib.suppress(queue.Empty):
            reply.get(timeout=5.0)
        thread.join(timeout=5.0)
        with self._lock:
            self._thread = None
            self._automation = None


#: Sentinel enqueued by :meth:`_Apartment.stop`; the serve loop identity-compares
#: against it to distinguish "shut down" from a real job.
_STOP_JOB: Callable[[], None] = lambda: None  # noqa: E731


def _guid(ole32: ctypes.WinDLL, text: str) -> _GUID:
    out = _GUID()
    hr = ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(out))
    _check(int(hr), f"CLSIDFromString({text})")
    return out


_apartment = _Apartment()


def call_in_apartment(job: Callable[[], _T]) -> _T:
    """Run ``job`` on the COM apartment thread and return its result."""
    return _apartment.run(job)


def shutdown() -> None:
    """Stop the apartment thread (idempotent; used by tests and teardown)."""
    _apartment.stop()


# ---------------------------------------------------------------------------
# BSTR helpers
# ---------------------------------------------------------------------------


def _bstr_to_str(raw: ctypes.c_void_p) -> str:
    """Consume a ``BSTR`` out-param: copy to ``str``, then free it."""
    if not raw:
        return ""
    try:
        return ctypes.cast(raw, ctypes.c_wchar_p).value or ""
    finally:
        libs = _libs()
        if libs is not None:
            libs[1].SysFreeString(raw)


# ---------------------------------------------------------------------------
# IUIAutomationElement wrapper
#
# Slot numbers follow ``UIAutomationClient.h`` declaration order. Only the
# members this package needs are bound; the property getters are used in
# preference to ``GetCurrentPropertyValue`` because they avoid VARIANT
# marshalling entirely.
# ---------------------------------------------------------------------------

# IUIAutomation
_UIA_ELEMENT_FROM_HANDLE = 6
_UIA_GET_FOCUSED_ELEMENT = 8
_UIA_GET_ROOT_ELEMENT = 5
_UIA_CREATE_TREE_WALKER = 13
_UIA_GET_CONTROL_VIEW_WALKER = 14
_UIA_GET_RAW_VIEW_WALKER = 16

# IUIAutomationElement
_EL_SET_FOCUS = 3
_EL_GET_RUNTIME_ID = 4
_EL_GET_CURRENT_PATTERN = 16
_EL_PROCESS_ID = 20
_EL_CONTROL_TYPE = 21
_EL_NAME = 23
_EL_HAS_KEYBOARD_FOCUS = 26
_EL_IS_KEYBOARD_FOCUSABLE = 27
_EL_IS_ENABLED = 28
_EL_AUTOMATION_ID = 29
_EL_CLASS_NAME = 30
_EL_IS_CONTROL_ELEMENT = 33
_EL_IS_PASSWORD = 35
_EL_NATIVE_WINDOW_HANDLE = 36
_EL_IS_OFFSCREEN = 38
_EL_BOUNDING_RECTANGLE = 43

# IUIAutomationTreeWalker
_WALK_FIRST_CHILD = 4
_WALK_NEXT_SIBLING = 6


class Element:
    """An owned ``IUIAutomationElement`` pointer.

    Every accessor must run on the apartment thread; callers reach them
    through :func:`call_in_apartment`, so the methods here assume they are
    already on it and do not re-marshal.
    """

    __slots__ = ("_ptr",)

    def __init__(self, ptr: ctypes.c_void_p) -> None:
        self._ptr = ptr

    # -- plumbing -----------------------------------------------------------

    @property
    def ptr(self) -> ctypes.c_void_p:
        return self._ptr

    def release(self) -> None:
        if self._ptr:
            _vcall(self._ptr, _SLOT_RELEASE)
            self._ptr = ctypes.c_void_p()

    def _str_prop(self, slot: int) -> str:
        out = ctypes.c_void_p()
        if _vcall(self._ptr, slot, ctypes.byref(out)) != _S_OK:
            return ""
        return _bstr_to_str(out)

    def _int_prop(self, slot: int) -> int:
        out = ctypes.c_int()
        if _vcall(self._ptr, slot, ctypes.byref(out)) != _S_OK:
            return 0
        return int(out.value)

    def _bool_prop(self, slot: int) -> bool:
        return self._int_prop(slot) != 0

    # -- properties ---------------------------------------------------------

    @property
    def name(self) -> str:
        return self._str_prop(_EL_NAME)

    @property
    def automation_id(self) -> str:
        return self._str_prop(_EL_AUTOMATION_ID)

    @property
    def class_name(self) -> str:
        return self._str_prop(_EL_CLASS_NAME)

    @property
    def control_type(self) -> int:
        return self._int_prop(_EL_CONTROL_TYPE)

    @property
    def process_id(self) -> int:
        return self._int_prop(_EL_PROCESS_ID)

    @property
    def is_enabled(self) -> bool:
        return self._bool_prop(_EL_IS_ENABLED)

    @property
    def is_offscreen(self) -> bool:
        return self._bool_prop(_EL_IS_OFFSCREEN)

    @property
    def is_password(self) -> bool:
        return self._bool_prop(_EL_IS_PASSWORD)

    @property
    def is_control_element(self) -> bool:
        return self._bool_prop(_EL_IS_CONTROL_ELEMENT)

    @property
    def is_keyboard_focusable(self) -> bool:
        return self._bool_prop(_EL_IS_KEYBOARD_FOCUSABLE)

    @property
    def has_keyboard_focus(self) -> bool:
        return self._bool_prop(_EL_HAS_KEYBOARD_FOCUS)

    @property
    def native_window_handle(self) -> int:
        out = ctypes.c_void_p()
        if _vcall(self._ptr, _EL_NATIVE_WINDOW_HANDLE, ctypes.byref(out)) != _S_OK:
            return 0
        return int(out.value or 0)

    @property
    def rect(self) -> tuple[int, int, int, int]:
        """``(left, top, right, bottom)`` in physical screen pixels.

        The worker process is per-monitor DPI aware, so these are true device
        pixels and share the coordinate space used for pointer input.
        """
        out = wintypes.RECT()
        if _vcall(self._ptr, _EL_BOUNDING_RECTANGLE, ctypes.byref(out)) != _S_OK:
            return (0, 0, 0, 0)
        return (int(out.left), int(out.top), int(out.right), int(out.bottom))

    @property
    def runtime_id(self) -> tuple[int, ...]:
        """Stable-per-session identity of this element (``[]`` if refused)."""
        out = ctypes.c_void_p()
        if _vcall(self._ptr, _EL_GET_RUNTIME_ID, ctypes.byref(out)) != _S_OK:
            return ()
        return _safearray_ints(out)

    # -- actions ------------------------------------------------------------

    def set_focus(self) -> None:
        _check(_vcall(self._ptr, _EL_SET_FOCUS), "IUIAutomationElement::SetFocus")

    def pattern(self, pattern_id: int) -> ctypes.c_void_p | None:
        """Fetch a control pattern, or ``None`` when unsupported."""
        out = ctypes.c_void_p()
        hr = _vcall(
            self._ptr,
            _EL_GET_CURRENT_PATTERN,
            ctypes.c_int(pattern_id),
            ctypes.byref(out),
        )
        if hr != _S_OK or not out:
            return None
        return out

    # -- traversal ----------------------------------------------------------

    def first_child(self, walker: ctypes.c_void_p) -> Element | None:
        return _walk(walker, _WALK_FIRST_CHILD, self._ptr)

    def next_sibling(self, walker: ctypes.c_void_p) -> Element | None:
        return _walk(walker, _WALK_NEXT_SIBLING, self._ptr)


def _walk(
    walker: ctypes.c_void_p, slot: int, element: ctypes.c_void_p
) -> Element | None:
    out = ctypes.c_void_p()
    hr = _vcall(walker, slot, element, ctypes.byref(out))
    if hr != _S_OK or not out:
        return None
    return Element(out)


def _safearray_ints(psa: ctypes.c_void_p) -> tuple[int, ...]:
    """Read a ``SAFEARRAY`` of ``int`` (used only by ``GetRuntimeId``)."""
    libs = _libs()
    if libs is None or not psa:
        return ()
    oleaut = libs[1]
    lower = ctypes.c_long()
    upper = ctypes.c_long()
    try:
        if oleaut.SafeArrayGetLBound(psa, 1, ctypes.byref(lower)) != _S_OK:
            return ()
        if oleaut.SafeArrayGetUBound(psa, 1, ctypes.byref(upper)) != _S_OK:
            return ()
        values: list[int] = []
        item = ctypes.c_int()
        for index in range(int(lower.value), int(upper.value) + 1):
            idx = ctypes.c_long(index)
            if oleaut.SafeArrayGetElement(
                psa, ctypes.byref(idx), ctypes.byref(item)
            ) != _S_OK:
                break
            values.append(int(item.value))
        return tuple(values)
    finally:
        oleaut.SafeArrayDestroy(psa)


# ---------------------------------------------------------------------------
# Roots and walkers (each must be called on the apartment thread)
# ---------------------------------------------------------------------------


def control_view_walker() -> ctypes.c_void_p:
    """The control-view walker: skips pure-layout nodes, keeps controls."""
    out = ctypes.c_void_p()
    _check(
        _vcall(
            _apartment.automation,
            _UIA_GET_CONTROL_VIEW_WALKER,
            ctypes.byref(out),
        ),
        "IUIAutomation::get_ControlViewWalker",
    )
    return out


def root_element() -> Element:
    """The desktop root element."""
    out = ctypes.c_void_p()
    _check(
        _vcall(_apartment.automation, _UIA_GET_ROOT_ELEMENT, ctypes.byref(out)),
        "IUIAutomation::GetRootElement",
    )
    return Element(out)


def element_from_handle(hwnd: int) -> Element | None:
    """The element for a window handle, or ``None`` if it has none."""
    out = ctypes.c_void_p()
    hr = _vcall(
        _apartment.automation,
        _UIA_ELEMENT_FROM_HANDLE,
        ctypes.c_void_p(int(hwnd)),
        ctypes.byref(out),
    )
    if hr != _S_OK or not out:
        return None
    return Element(out)


def focused_element() -> Element | None:
    """The element with keyboard focus, or ``None``."""
    out = ctypes.c_void_p()
    hr = _vcall(
        _apartment.automation, _UIA_GET_FOCUSED_ELEMENT, ctypes.byref(out)
    )
    if hr != _S_OK or not out:
        return None
    return Element(out)


# ---------------------------------------------------------------------------
# Control patterns
# ---------------------------------------------------------------------------

PATTERN_INVOKE = 10000
PATTERN_VALUE = 10002
PATTERN_EXPAND_COLLAPSE = 10005
PATTERN_SELECTION_ITEM = 10010
PATTERN_TOGGLE = 10015

#: First method slot of each pattern interface (all start right after IUnknown).
_PATTERN_INVOKE_SLOT = 3
_PATTERN_VALUE_SET_SLOT = 3
_PATTERN_VALUE_GET_SLOT = 4
_PATTERN_TOGGLE_SLOT = 3
_PATTERN_EXPAND_SLOT = 3
_PATTERN_COLLAPSE_SLOT = 4
_PATTERN_SELECT_SLOT = 3


def invoke_pattern(ptr: ctypes.c_void_p) -> None:
    _check(_vcall(ptr, _PATTERN_INVOKE_SLOT), "IUIAutomationInvokePattern::Invoke")


def toggle_pattern(ptr: ctypes.c_void_p) -> None:
    _check(_vcall(ptr, _PATTERN_TOGGLE_SLOT), "IUIAutomationTogglePattern::Toggle")


def select_pattern(ptr: ctypes.c_void_p) -> None:
    _check(
        _vcall(ptr, _PATTERN_SELECT_SLOT),
        "IUIAutomationSelectionItemPattern::Select",
    )


def expand_pattern(ptr: ctypes.c_void_p, *, expand: bool) -> None:
    slot = _PATTERN_EXPAND_SLOT if expand else _PATTERN_COLLAPSE_SLOT
    _check(_vcall(ptr, slot), "IUIAutomationExpandCollapsePattern")


def value_pattern_set(ptr: ctypes.c_void_p, text: str) -> None:
    libs = _libs()
    if libs is None:
        raise ComError(AX_UNAVAILABLE)
    oleaut = libs[1]
    oleaut.SysAllocStringLen.restype = ctypes.c_void_p
    bstr = ctypes.c_void_p(
        oleaut.SysAllocStringLen(ctypes.c_wchar_p(text), len(text))
    )
    if not bstr:
        raise ComError("SysAllocStringLen returned NULL")
    try:
        _check(
            _vcall(ptr, _PATTERN_VALUE_SET_SLOT, bstr),
            "IUIAutomationValuePattern::SetValue",
        )
    finally:
        oleaut.SysFreeString(bstr)


def value_pattern_get(ptr: ctypes.c_void_p) -> str:
    out = ctypes.c_void_p()
    if _vcall(ptr, _PATTERN_VALUE_GET_SLOT, ctypes.byref(out)) != _S_OK:
        return ""
    return _bstr_to_str(out)


def release(ptr: ctypes.c_void_p) -> None:
    """Release an arbitrary interface pointer (patterns, walkers)."""
    if ptr:
        _vcall(ptr, _SLOT_RELEASE)

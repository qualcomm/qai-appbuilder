# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Accessibility snapshot: an indented control tree with clickable refs.

Why this exists. A screenshot forces the model to ESTIMATE a click point from
pixels, and small targets (a menu item, a toolbar button) are only a few tens
of pixels tall, so an estimate that is off by a little lands on the wrong
control or on nothing. UI Automation reports every control's name, role and
exact rectangle, so this module turns a window into a compact text tree where
each interactive node carries a stable ``[ref=eN]`` handle. The model then acts
on ``ref="e7"`` and the coordinate comes from the platform, not from guesswork.

Design notes:

* **Physical pixels.** With the process per-monitor DPI aware, UIA bounding
  rectangles are true device pixels in the same virtual-desktop space used for
  pointer input — verified by cross-checking ``GetWindowRect`` against
  ``BoundingRectangle`` for the same window. No DPI conversion is applied
  anywhere, which is what keeps ref-driven clicks exact.
* **Interactive-only by default.** A raw control tree is mostly containers and
  static text. Those are dropped unless they carry a name that gives a nearby
  control meaning, keeping the payload small enough to send every turn.
* **Bounded walk.** Node and depth ceilings bound both the traversal cost and
  the token cost; hitting a ceiling is reported in the output so the model
  knows the tree was truncated rather than empty.
* **Generation-scoped refs.** Refs are only valid for the snapshot that
  produced them. Each snapshot bumps a generation counter, so a ref from an
  earlier snapshot is rejected instead of silently resolving to whatever
  element now occupies that slot.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
import threading
from dataclasses import dataclass, replace
from typing import Any

from . import _uia

__all__ = [
    "MAX_DEPTH",
    "MAX_NODES",
    "AxNode",
    "AxSnapshot",
    "activate_ref",
    "focus_ref",
    "ref_centre",
    "resolve_ref",
    "set_ref_value",
    "snapshot",
]

#: Ceilings on the emitted tree. Chosen to bound the token cost of sending a
#: snapshot on every turn while still covering a full application window; a
#: truncated walk is reported so the model can narrow its request instead of
#: assuming it saw everything.
MAX_NODES = 800
MAX_DEPTH = 24

#: Control types worth showing even when they are not interactive, because
#: their text is what gives an adjacent control meaning (a label next to a
#: field, a heading above a list).
_TEXT_TYPES = frozenset({50020, 50041, 50045})  # Text, Header, HeaderItem

#: Control types that always accept input. Anything outside this set is kept
#: only when UIA reports it focusable or it supports an action pattern.
_INTERACTIVE_TYPES = frozenset(
    {
        50000,  # Button
        50001,  # Calendar
        50002,  # CheckBox
        50003,  # ComboBox
        50004,  # Edit
        50005,  # Hyperlink
        50007,  # ListItem
        50008,  # List
        50009,  # Menu
        50011,  # MenuItem
        50010,  # MenuBar
        50012,  # ProgressBar
        50013,  # RadioButton
        50014,  # ScrollBar
        50015,  # Slider
        50016,  # Spinner
        50018,  # Tab
        50019,  # TabItem
        50021,  # ToolBar
        50023,  # Tree
        50024,  # TreeItem
        50025,  # Custom
        50026,  # Group
        50029,  # Table
        50031,  # Document
        50034,  # SplitButton
        50035,  # WindowControl
        50039,  # DataItem
    }
)

#: Human-readable role names for the control types we emit. Falls back to the
#: numeric id, which still lets a model reason about repeated structures.
_ROLE_NAMES: dict[int, str] = {
    50000: "button",
    50001: "calendar",
    50002: "checkbox",
    50003: "combobox",
    50004: "edit",
    50005: "link",
    50006: "image",
    50007: "listitem",
    50008: "list",
    50009: "menu",
    50010: "menubar",
    50011: "menuitem",
    50012: "progressbar",
    50013: "radio",
    50014: "scrollbar",
    50015: "slider",
    50016: "spinner",
    50017: "statusbar",
    50018: "tab",
    50019: "tabitem",
    50020: "text",
    50021: "toolbar",
    50022: "tooltip",
    50023: "tree",
    50024: "treeitem",
    50025: "custom",
    50026: "group",
    50027: "thumb",
    50028: "datagrid",
    50029: "table",
    50030: "dataitem",
    50031: "document",
    50032: "window",
    50033: "pane",
    50034: "splitbutton",
    50035: "window",
    50036: "titlebar",
    50037: "separator",
    50038: "semanticzoom",
    50039: "dataitem",
    50040: "appbar",
    50041: "header",
    50045: "headeritem",
}

#: Patterns that make a node actionable, in the order ``activate_ref`` tries
#: them: an explicit Invoke wins, then selection, then toggle, then expand.
_ACTION_PATTERNS = (
    _uia.PATTERN_INVOKE,
    _uia.PATTERN_SELECTION_ITEM,
    _uia.PATTERN_TOGGLE,
    _uia.PATTERN_EXPAND_COLLAPSE,
)

#: Name text longer than this is truncated; long names are usually a whole
#: document body or a concatenated label and add tokens without adding meaning.
_MAX_NAME_CHARS = 120


@dataclass(frozen=True, slots=True)
class AxNode:
    """One node of an accessibility snapshot."""

    ref: str | None
    role: str
    name: str
    x: int
    y: int
    width: int
    height: int
    depth: int
    enabled: bool
    focused: bool
    value: str | None

    @property
    def centre(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "depth": self.depth,
        }
        if self.ref is not None:
            out["ref"] = self.ref
        if not self.enabled:
            out["enabled"] = False
        if self.focused:
            out["focused"] = True
        if self.value is not None:
            out["value"] = self.value
        return out


@dataclass(frozen=True, slots=True)
class AxSnapshot:
    """A bounded accessibility tree plus the refs it defined."""

    nodes: tuple[AxNode, ...]
    generation: int
    truncated: bool
    root_title: str
    limit: int = MAX_NODES

    def render(self) -> str:
        """The indented text tree handed to the model.

        Nodes that carry neither a ref nor text are dropped here rather than
        during the walk: they still had to be traversed to reach their
        children, but an anonymous container is a layout artefact the model
        cannot act on or reason about, and emitting one line per pane buries
        the controls that matter.
        """
        lines: list[str] = []
        for node in self.nodes:
            if node.ref is None and not node.name:
                continue
            indent = "  " * node.depth
            parts = [f"{indent}{node.role}"]
            if node.name:
                parts.append(f' "{node.name}"')
            if node.value:
                parts.append(f" value={node.value!r}")
            cx, cy = node.centre
            parts.append(f" ({cx},{cy})")
            if node.ref is not None:
                parts.append(f" [ref={node.ref}]")
            if not node.enabled:
                parts.append(" disabled")
            if node.focused:
                parts.append(" focused")
            lines.append("".join(parts))
        if self.truncated:
            # Report the limit that ACTUALLY applied, which may be a caller's
            # narrower max_nodes rather than the module default.
            lines.append(
                f"... tree truncated at {self.limit} nodes; "
                "narrow the request to a specific window or raise max_nodes"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ref registry
# ---------------------------------------------------------------------------


class _Registry:
    """Maps ``eN`` handles to live elements for the CURRENT generation only.

    A ref names a UI element that may be destroyed, moved or replaced the
    moment the UI changes, so keeping refs alive across snapshots would let a
    stale ``e3`` resolve to an unrelated control. Each snapshot therefore
    starts a new generation and releases the previous one's elements.

    Refs are stored generation-QUALIFIED (``g2:e7``) while the model only ever
    sees the short ``e7``. Without the qualifier a stale ref would silently
    resolve: every snapshot restarts numbering at ``e1``, so an ``e1`` from two
    snapshots ago would match the current ``e1`` — a different control — and
    the click would land on the wrong thing with no error. Comparing the
    qualifier turns that into an explicit failure telling the model to
    re-snapshot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._elements: dict[str, _uia.Element] = {}

    def begin(self) -> int:
        """Release the previous generation and start a new one."""
        with self._lock:
            stale = list(self._elements.values())
            self._elements = {}
            self._generation += 1
            generation = self._generation
        if stale:
            def _drop() -> None:
                for element in stale:
                    element.release()

            with contextlib.suppress(_uia.ComError):
                # Teardown is best effort: the elements die with the apartment.
                _uia.call_in_apartment(_drop)
        return generation

    def put(self, generation: int, ref: str, element: _uia.Element) -> None:
        with self._lock:
            self._elements[f"g{generation}:{ref}"] = element

    def get(self, ref: str) -> _uia.Element:
        """Resolve a model-supplied ``eN`` against the current generation."""
        with self._lock:
            generation = self._generation
            element = self._elements.get(f"g{generation}:{ref}")
            known = bool(self._elements)
        if element is None:
            hint = (
                "take a new ui_snapshot: refs expire when the tree is re-read"
                if known
                else "no snapshot has been taken in this session yet"
            )
            raise _uia.ComError(f"unknown or expired ref {ref!r}; {hint}")
        return element

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation


_registry = _Registry()


def _role_of(control_type: int) -> str:
    return _ROLE_NAMES.get(control_type, f"type{control_type}")


def _clean_name(raw: str) -> str:
    text = " ".join(raw.split())
    if len(text) > _MAX_NAME_CHARS:
        return text[: _MAX_NAME_CHARS - 1] + "\u2026"
    return text


#: Control types that are real widgets: a ref on one is always useful because
#: the model can act on it even when UIA reports no name.
_WIDGET_TYPES = _INTERACTIVE_TYPES - {50025, 50026, 50031, 50035}

#: Generic containers (Group / Custom / Document / WindowControl), plus the
#: layout roles below. Structurally interesting but only worth a ref when
#: something identifies them; otherwise a large app contributes dozens of
#: indistinguishable ``pane [ref=eN]`` rows that cost tokens and tell the model
#: nothing it can act on.
_CONTAINER_TYPES = (_INTERACTIVE_TYPES - _WIDGET_TYPES) | {
    50033,  # Pane
    50037,  # Separator
}


def _is_interesting(
    element: _uia.Element, control_type: int, name: str
) -> tuple[bool, bool, bool]:
    """``(keep, interactive, needs_probe)`` for one element.

    Interactive nodes get a ref; named text nodes are kept for context but get
    no ref, since clicking a label does nothing useful.

    ``needs_probe`` reports whether the caller must still ask UIA for an action
    pattern. Each pattern query is a cross-process COM round trip, so they are
    skipped whenever the control type or focusability already settles the
    answer — that is the difference between a snapshot costing hundreds of
    milliseconds and costing seconds on a large window.
    """
    if element.is_offscreen:
        return (False, False, False)
    # Containers are checked FIRST, before focusability: UIA reports many
    # layout panes as keyboard-focusable, so testing focus earlier would hand a
    # ref to every anonymous pane — exactly the noise this split avoids.
    if control_type in _CONTAINER_TYPES:
        return (True, bool(name), False)
    if control_type in _WIDGET_TYPES or element.is_keyboard_focusable:
        # Already known to be actionable; no pattern probe needed. Disabled
        # controls are kept too, so the model stops retrying a dead button.
        return (True, True, False)
    if control_type in _TEXT_TYPES and name:
        return (True, False, False)
    # Unknown type with no obvious affordance: only a pattern probe can tell
    # whether it is a real control (custom-drawn widgets land here).
    return (False, False, True)


def _probe_action(element: _uia.Element) -> bool:
    """Whether the element exposes any action pattern (pattern is released)."""
    for pattern_id in _ACTION_PATTERNS:
        ptr = element.pattern(pattern_id)
        if ptr is not None:
            _uia.release(ptr)
            return True
    return False


#: Control types whose text content is worth reading. Probing the Value pattern
#: on every node doubles the COM traffic, and only editable/selectable controls
#: carry a value a model can act on.
_VALUE_TYPES = frozenset({50003, 50004, 50005, 50015, 50016, 50031, 50039})


def _read_value(element: _uia.Element, control_type: int) -> str | None:
    """The Value pattern's text, redacted for password fields."""
    if control_type not in _VALUE_TYPES:
        return None
    ptr = element.pattern(_uia.PATTERN_VALUE)
    if ptr is None:
        return None
    try:
        if element.is_password:
            return "\u2022\u2022\u2022"
        text = _uia.value_pattern_get(ptr)
    finally:
        _uia.release(ptr)
    if not text:
        return None
    return _clean_name(text)


def _classify(
    element: _uia.Element, shown_depth: int, *, interactive_only: bool
) -> tuple[bool, bool, AxNode | None]:
    """Decide an element's fate and build its node.

    Returns ``(keep, interactive, node)``. ``node`` carries no ref yet — the
    caller assigns one only for elements it actually emits, so ref numbering has
    no gaps. Splitting this out of the walk keeps the traversal loop about
    stack management and this function about per-element policy.
    """
    control_type = element.control_type
    name = _clean_name(element.name)
    keep, interactive, needs_probe = _is_interesting(element, control_type, name)
    if needs_probe and _probe_action(element):
        keep = interactive = True
    if not interactive_only and not keep:
        keep = True
    if not keep:
        return (False, False, None)

    left, top, right, bottom = element.rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        # Zero-area controls cannot be clicked and carry no layout meaning, but
        # their children may still be real, so the caller must keep descending.
        return (False, False, None)

    return (
        True,
        interactive,
        AxNode(
            ref=None,
            role=_role_of(control_type),
            name=name,
            x=left,
            y=top,
            width=width,
            height=height,
            depth=shown_depth,
            enabled=element.is_enabled,
            focused=element.has_keyboard_focus,
            value=_read_value(element, control_type),
        ),
    )


def _is_minimized(hwnd: int) -> bool:
    """Whether a window is iconic (minimized).

    Cheap guard used before walking a tree: ``IsIconic`` is the authoritative
    answer, and a minimized window's UIA rectangles are all measured from its
    off-screen park position, so they must not be handed out as targets.
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        return bool(user32.IsIconic(ctypes.c_void_p(int(hwnd))))
    except (AttributeError, OSError):  # pragma: no cover — guard only
        return False


def snapshot(
    *,
    hwnd: int | None = None,
    max_nodes: int = MAX_NODES,
    max_depth: int = MAX_DEPTH,
    interactive_only: bool = True,
) -> AxSnapshot:
    """Walk a window's control tree (or the whole desktop) into text + refs.

    ``hwnd`` scopes the walk to one window, which is both far cheaper and far
    more useful than the desktop root: a window's tree is the thing the model
    is working in. Passing ``None`` walks from the desktop, which is bounded to
    top-level windows plus their immediate structure by the node ceiling.
    """
    generation = _registry.begin()

    if hwnd is not None and _is_minimized(hwnd):
        # A minimized window is parked off-screen at roughly (-32000, -32000),
        # and UIA happily reports every descendant relative to that parked
        # origin. The rects are structurally valid but useless as click targets,
        # and emitting them invites the caller to act on coordinates that cannot
        # be hit. Refuse with an instruction instead: observed in a real session
        # where a snapshot of a minimized Notepad returned rects like
        # (-30709, -31279) and the caller spent several rounds working around it.
        raise _uia.ComError(
            f"window 0x{hwnd:X} is minimized, so its controls sit off-screen "
            "and cannot be clicked. Restore the window first (click its taskbar "
            "button), then take the snapshot again."
        )

    def _walk() -> tuple[list[AxNode], bool, str]:
        walker = _uia.control_view_walker()
        root: _uia.Element | None = None
        try:
            if hwnd is not None:
                root = _uia.element_from_handle(hwnd)
                if root is None:
                    raise _uia.ComError(f"window 0x{hwnd:X} exposes no UI tree")
            else:
                root = _uia.root_element()
            title = _clean_name(root.name)
            nodes: list[AxNode] = []
            counter = 0
            truncated = False

            # Depth-first pre-order over an explicit stack. Each entry carries
            # the real tree depth (bounds the walk) and the DISPLAY depth used
            # for indentation: filtered-out containers must not leave gaps in
            # the rendered tree, so a dropped node passes its own display depth
            # to its children instead of incrementing it. Elements on the stack
            # are owned by the stack until they are registered or released.
            stack: list[tuple[_uia.Element, int, int]] = []
            first = root.first_child(walker)
            if first is not None:
                stack.append((first, 0, 0))

            while stack:
                element, depth, shown_depth = stack.pop()
                if len(nodes) >= max_nodes:
                    truncated = True
                    element.release()
                    for pending, _d, _s in stack:
                        pending.release()
                    stack.clear()
                    break

                sibling = element.next_sibling(walker)
                if sibling is not None:
                    stack.append((sibling, depth, shown_depth))

                keep, interactive, node = _classify(
                    element, shown_depth, interactive_only=interactive_only
                )

                # Descend BEFORE releasing: a dropped container still holds the
                # children we need, and it passes its own display depth down so
                # filtered levels leave no gap in the rendered tree.
                child = (
                    element.first_child(walker) if depth + 1 < max_depth else None
                )
                if child is not None:
                    stack.append(
                        (child, depth + 1, shown_depth + 1 if keep else shown_depth)
                    )

                if not keep or node is None:
                    element.release()
                    continue

                if interactive:
                    counter += 1
                    node = replace(node, ref=f"e{counter}")
                nodes.append(node)
                if node.ref is not None:
                    _registry.put(generation, node.ref, element)  # registry owns it
                else:
                    element.release()

            return nodes, truncated, title
        finally:
            if root is not None:
                root.release()
            _uia.release(walker)

    nodes, truncated, title = _uia.call_in_apartment(_walk)
    return AxSnapshot(
        nodes=tuple(nodes),
        generation=generation,
        truncated=truncated,
        root_title=title,
        limit=max_nodes,
    )


# ---------------------------------------------------------------------------
# Ref actions
# ---------------------------------------------------------------------------


def resolve_ref(ref: str) -> tuple[int, int, int, int]:
    """The current ``(left, top, right, bottom)`` of a ref'd element.

    Read live rather than from the snapshot: between snapshot and action the
    control may have scrolled or moved, and the fresh rect is what makes a
    ref-driven click land where the control actually is now.
    """
    element = _registry.get(ref)
    return _uia.call_in_apartment(lambda: element.rect)


def ref_centre(ref: str) -> tuple[int, int]:
    """The live centre point of a ref'd element."""
    left, top, right, bottom = resolve_ref(ref)
    if right <= left or bottom <= top:
        raise _uia.ComError(f"ref {ref!r} has an empty rectangle")
    return ((left + right) // 2, (top + bottom) // 2)


def activate_ref(ref: str) -> str:
    """Invoke a ref'd element through its own action pattern.

    Preferred over a synthetic click when available: it needs no pointer
    movement, cannot be intercepted by an overlapping window, and works on a
    control that is scrolled out of view.
    """
    element = _registry.get(ref)

    def _run() -> str:
        for pattern_id, label in (
            (_uia.PATTERN_INVOKE, "invoke"),
            (_uia.PATTERN_SELECTION_ITEM, "select"),
            (_uia.PATTERN_TOGGLE, "toggle"),
            (_uia.PATTERN_EXPAND_COLLAPSE, "expand"),
        ):
            ptr = element.pattern(pattern_id)
            if ptr is None:
                continue
            try:
                if pattern_id == _uia.PATTERN_INVOKE:
                    _uia.invoke_pattern(ptr)
                elif pattern_id == _uia.PATTERN_SELECTION_ITEM:
                    _uia.select_pattern(ptr)
                elif pattern_id == _uia.PATTERN_TOGGLE:
                    _uia.toggle_pattern(ptr)
                else:
                    _uia.expand_pattern(ptr, expand=True)
            finally:
                _uia.release(ptr)
            return label
        raise _uia.ComError(
            f"ref {ref!r} exposes no action pattern; click its centre instead"
        )

    return _uia.call_in_apartment(_run)


def set_ref_value(ref: str, text: str) -> None:
    """Set a ref'd field's text through the Value pattern.

    Beats synthetic typing for form fields: it replaces the whole value
    atomically, so there is no partial state and no dependence on focus.
    """
    element = _registry.get(ref)

    def _run() -> None:
        ptr = element.pattern(_uia.PATTERN_VALUE)
        if ptr is None:
            raise _uia.ComError(
                f"ref {ref!r} is not a value control; click it and type instead"
            )
        try:
            _uia.value_pattern_set(ptr, text)
        finally:
            _uia.release(ptr)

    _uia.call_in_apartment(_run)


def focus_ref(ref: str) -> None:
    """Give a ref'd element keyboard focus."""
    element = _registry.get(ref)
    _uia.call_in_apartment(element.set_focus)


def ref_window(ref: str) -> int:
    """The top-level window handle owning a ref'd element.

    Needed for background delivery: posted messages address a window, but a
    control is usually a child element whose own ``NativeWindowHandle`` is
    often 0 (WinUI/Chromium draw many controls without individual HWNDs). The
    element's containing window is discovered by hit-testing its centre, which
    works regardless of how the control is implemented.
    """
    element = _registry.get(ref)

    def _read() -> tuple[int, tuple[int, int, int, int]]:
        return element.native_window_handle, element.rect

    handle, (left, top, right, bottom) = _uia.call_in_apartment(_read)
    if handle:
        return int(handle)
    if right <= left or bottom <= top:
        raise _uia.ComError(
            f"ref {ref!r} has no window and no area; take a new ui_snapshot"
        )
    from . import windows_info as _info

    found = _info.window_at((left + right) // 2, (top + bottom) // 2)
    if found is None or not found.get("hwnd"):
        raise _uia.ComError(
            f"cannot determine the window owning ref {ref!r}; pass an explicit "
            "window id for background delivery"
        )
    return int(found["hwnd"])


def ref_value(ref: str) -> str | None:
    """A ref'd element's current value text, or ``None`` when it exposes none.

    Read live through the Value pattern. Its purpose is verification: after
    entering text by any route, this is how the caller confirms the control
    actually holds it, which is the only way to detect an input mechanism the
    target silently ignored.
    """
    element = _registry.get(ref)

    def _read() -> str | None:
        ptr = element.pattern(_uia.PATTERN_VALUE)
        if ptr is None:
            return None
        try:
            return _uia.value_pattern_get(ptr)
        finally:
            _uia.release(ptr)

    return _uia.call_in_apartment(_read)

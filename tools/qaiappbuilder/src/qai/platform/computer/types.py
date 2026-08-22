# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Desktop-control value objects + fail-closed action validation.

This module is the data contract shared across the whole ``computer``
capability: the tool schema, the tool handler, the subprocess supervisor,
and the in-process desktop session all speak these frozen dataclasses.

Everything here is pure data + validation — no Win32, no subprocess, no
I/O — so it is safe to import from any layer and cheap to unit-test.

Design notes
------------
* One :class:`Action` dataclass carries every field for the 9 action
  types (tagged by ``type``); :meth:`Action.parse` enforces a strict
  per-type field whitelist + value-domain check and raises
  :class:`DesktopError` on the first violation (fail-closed).
* Coordinates cross the subprocess boundary as ``i32`` (non-negative);
  an out-of-range value fails at parse time rather than being silently
  truncated.
* :class:`Capture` / :class:`Display` / :class:`Capabilities` are the
  read-side results returned by a screenshot / batch execution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ACTION_TYPES",
    "BUTTONS",
    "DELIVERY_MODES",
    "MODIFIER_ALIASES",
    "MODIFIER_BITS",
    "Action",
    "Capabilities",
    "Capture",
    "DesktopError",
    "Display",
    "Point",
    "SessionOptions",
    "scroll_steps",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DesktopError(Exception):
    """Raised for any desktop-control failure.

    Carries an optional machine-readable ``name`` so the subprocess
    protocol can round-trip a stable error kind (e.g. ``"InvalidAction"``,
    ``"CaptureFailed"``, ``"LayoutChanged"``) across the process boundary.
    """

    def __init__(self, message: str, *, name: str = "DesktopError") -> None:
        super().__init__(message)
        self.name = name
        self.message = message


# ---------------------------------------------------------------------------
# Constants (mirrored by the tool schema + keymap)
# ---------------------------------------------------------------------------

#: The action types exposed to the model. ``ui_snapshot`` reads the target's
#: accessibility tree instead of taking a picture, which is what lets the model
#: address a control by ``ref`` rather than by an estimated pixel.
ACTION_TYPES: frozenset[str] = frozenset(
    {
        "screenshot",
        "click",
        "double_click",
        "move",
        "drag",
        "scroll",
        "type",
        "keypress",
        "wait",
        "ui_snapshot",
    }
)

#: How an input action reaches the target.
#:
#: ``foreground`` synthesises real system input: it moves the physical cursor
#: and requires the target to be frontmost, so it works with every application
#: but disturbs the user and lands in the wrong place if focus changed since
#: the screenshot. ``background`` posts messages straight to a window's queue,
#: which leaves the cursor and focus untouched and is immune to z-order — at
#: the cost of being silently ignored by programs that read the raw input queue
#: (some games, DirectInput clients). ``foreground`` stays the default because
#: universal correctness matters more than politeness.
DELIVERY_MODES: frozenset[str] = frozenset({"foreground", "background"})

#: Pointer buttons. ``wheel`` is the middle button; ``back``/``forward``
#: are the mouse side buttons (XBUTTON1 / XBUTTON2).
BUTTONS: frozenset[str] = frozenset(
    {"left", "right", "wheel", "back", "forward"}
)

#: Modifier-key aliases accepted in a pointer action's ``keys`` list,
#: normalised (upper-cased) → canonical modifier name. Only these are
#: valid as held modifiers; anything else is rejected.
MODIFIER_ALIASES: dict[str, str] = {
    "CTRL": "CTRL",
    "CONTROL": "CTRL",
    "SHIFT": "SHIFT",
    "ALT": "ALT",
    "OPTION": "ALT",
    "META": "META",
    "CMD": "META",
    "COMMAND": "META",
    "SUPER": "META",
    "WINDOWS": "META",
}

#: Bit masks for modifier de-duplication (book 01 §1.3): a repeated
#: modifier in one ``keys`` list is a validation failure.
MODIFIER_BITS: dict[str, int] = {
    "CTRL": 1,
    "SHIFT": 2,
    "ALT": 4,
    "META": 8,
}

_I32_MAX: int = 2_147_483_647
_I32_MIN: int = -2_147_483_648


# ---------------------------------------------------------------------------
# Scroll conversion (book 01 §1.4 / book 04 §4.5)
# ---------------------------------------------------------------------------


def scroll_steps(delta: int) -> int:
    """Convert a pixel scroll delta to wheel steps.

    ``0 -> 0``; otherwise ``sign(delta) * ceil(|delta| / 100)`` so
    ``1..100 -> 1``, ``101..200 -> 2``, ``-250 -> -3``.
    """
    if delta == 0:
        return 0
    sign = 1 if delta > 0 else -1
    return sign * math.ceil(abs(delta) / 100)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Point:
    """A screenshot-pixel coordinate (non-negative ``i32``)."""

    x: int
    y: int


# ---------------------------------------------------------------------------
# Action (tagged union over ``type``)
# ---------------------------------------------------------------------------

# Per-type field whitelist. ``type`` is always allowed; the sets below
# name the ADDITIONAL keys a given action may carry. Any key outside the
# union of {"type"} and the whitelist is rejected (book 02 §2.1).
#
# Three cross-cutting options are optional on the action types where they make
# sense, and are what let the model act precisely instead of estimating:
#
# * ``ref`` — an ``eN`` handle from a ``ui_snapshot``. Supplying it means the
#   platform resolves the target's live rectangle, so ``x``/``y`` become
#   unnecessary (and are rejected together with it, since two sources of truth
#   for one target can disagree).
# * ``window`` — a window id from a ``ui_snapshot``. Coordinates are then
#   relative to that window's client-area origin instead of the virtual
#   desktop, so they stay valid when the window moves.
# * ``delivery`` — ``foreground`` (default; drives the real cursor) or
#   ``background`` (posts messages to the target window, leaving the user's
#   pointer and focus untouched). ``background`` requires a target window,
#   which either ``window`` or ``ref`` supplies.
_POINTER_OPTIONS: frozenset[str] = frozenset({"ref", "window", "delivery"})

_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "screenshot": frozenset(),
    "wait": frozenset(),
    # ``button`` is OPTIONAL and defaults to "left": a bare
    # ``{"type":"click","x":..,"y":..}`` is the obvious way to express a left
    # click, and rejecting it (the old behaviour) made every such call fail with
    # ``InvalidAction`` — the model then retried blindly instead of clicking.
    #
    # ``x``/``y`` are required only when no ``ref`` is given; the check moved
    # into ``parse`` because it depends on another field's presence.
    "click": frozenset(),
    "double_click": frozenset(),
    "move": frozenset(),
    "drag": frozenset({"path"}),
    "scroll": frozenset({"scroll_x", "scroll_y"}),
    "type": frozenset({"text"}),
    "keypress": frozenset({"keys"}),
    "ui_snapshot": frozenset(),
}

_OPTIONAL_FIELDS: dict[str, frozenset[str]] = {
    "screenshot": frozenset({"window"}),
    "wait": frozenset(),
    "click": frozenset({"keys", "button", "x", "y"}) | _POINTER_OPTIONS,
    "double_click": frozenset({"keys", "x", "y"}) | _POINTER_OPTIONS,
    "move": frozenset({"keys", "x", "y"}) | _POINTER_OPTIONS,
    "drag": frozenset({"keys", "window", "delivery"}),
    "scroll": frozenset({"keys", "x", "y"}) | _POINTER_OPTIONS,
    "type": frozenset({"ref", "window", "delivery"}),
    "keypress": frozenset({"window", "delivery"}),
    "ui_snapshot": frozenset({"window", "max_nodes", "interactive_only"}),
}


def _require_i32_nonneg(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DesktopError(
            f"{field} must be an integer", name="InvalidAction"
        )
    if value < 0 or value > _I32_MAX:
        raise DesktopError(
            f"{field} out of range 0..{_I32_MAX}: {value}",
            name="InvalidAction",
        )
    return value


def _require_i32(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DesktopError(
            f"{field} must be an integer", name="InvalidAction"
        )
    if value < _I32_MIN or value > _I32_MAX:
        raise DesktopError(
            f"{field} out of i32 range: {value}", name="InvalidAction"
        )
    return value


def _normalize_modifiers(keys: Any) -> tuple[str, ...]:
    """Validate pointer-action modifier keys: only CTRL/SHIFT/ALT/META
    aliases, no duplicates (bitmask dedup)."""
    if keys is None:
        return ()
    if not isinstance(keys, (list, tuple)):
        raise DesktopError("keys must be a list", name="InvalidAction")
    seen = 0
    out: list[str] = []
    for raw in keys:
        if not isinstance(raw, str) or not raw.strip():
            raise DesktopError(
                "modifier key must be a non-empty string",
                name="InvalidAction",
            )
        canonical = MODIFIER_ALIASES.get(raw.strip().upper())
        if canonical is None:
            raise DesktopError(
                f"not a modifier key: {raw!r}", name="InvalidAction"
            )
        bit = MODIFIER_BITS[canonical]
        if seen & bit:
            raise DesktopError(
                f"duplicate modifier: {canonical}", name="InvalidAction"
            )
        seen |= bit
        out.append(canonical)
    return tuple(out)


#: Ceiling on ``ui_snapshot.max_nodes``. A caller may narrow the tree to save
#: tokens but not widen it past what the AX layer will emit.
_MAX_SNAPSHOT_NODES = 800


def _parse_ref(raw: Any) -> str | None:
    """Validate a ``ref``: the ``eN`` form handed out by ``ui_snapshot``."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise DesktopError("ref must be a non-empty string", name="InvalidAction")
    ref = raw.strip()
    if not (ref.startswith("e") and ref[1:].isdigit()):
        raise DesktopError(
            f"malformed ref {raw!r}: expected the eN form from ui_snapshot",
            name="InvalidAction",
        )
    return ref


def _parse_window(raw: Any) -> int | None:
    """Validate a ``window`` id (a native window handle as an integer)."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise DesktopError(
            "window must be an integer window id from ui_snapshot",
            name="InvalidAction",
        )
    if raw <= 0:
        raise DesktopError(
            f"window id out of range: {raw}", name="InvalidAction"
        )
    return raw


def _parse_delivery(raw: Any) -> str:
    if raw is None:
        return "foreground"
    if not isinstance(raw, str) or raw not in DELIVERY_MODES:
        raise DesktopError(
            f"invalid delivery {raw!r}: expected one of "
            f"{sorted(DELIVERY_MODES)}",
            name="InvalidAction",
        )
    return raw


@dataclass(frozen=True, slots=True)
class Action:
    """A single desktop action (one of :data:`ACTION_TYPES`)."""

    type: str
    x: int | None = None
    y: int | None = None
    button: str | None = None
    path: tuple[Point, ...] | None = None
    keys: tuple[str, ...] | None = None
    scroll_x: int | None = None
    scroll_y: int | None = None
    text: str | None = None
    ref: str | None = None
    window: int | None = None
    delivery: str = "foreground"
    max_nodes: int | None = None
    interactive_only: bool = True

    @classmethod
    def parse(cls, raw: Any) -> "Action":
        """Parse + validate one LLM-shaped action dict (fail-closed).

        Raises :class:`DesktopError` (``name="InvalidAction"``) on any
        unknown type, missing required field, extra field, or
        out-of-domain value.
        """
        if not isinstance(raw, dict):
            raise DesktopError("action must be an object", name="InvalidAction")
        atype = raw.get("type")
        if not isinstance(atype, str) or atype not in ACTION_TYPES:
            raise DesktopError(
                f"unknown action type: {atype!r}", name="InvalidAction"
            )

        allowed = {"type"} | _REQUIRED_FIELDS[atype] | _OPTIONAL_FIELDS[atype]
        extra = set(raw.keys()) - allowed
        if extra:
            raise DesktopError(
                f"unexpected field(s) for {atype}: {sorted(extra)}",
                name="InvalidAction",
            )
        for field in _REQUIRED_FIELDS[atype]:
            if raw.get(field) is None:
                raise DesktopError(
                    f"{atype} requires {field}", name="InvalidAction"
                )

        ref = _parse_ref(raw.get("ref"))
        window = _parse_window(raw.get("window"))
        delivery = _parse_delivery(raw.get("delivery"))

        # A ref and explicit coordinates are two answers to "where?", and if
        # they disagree one of them is silently ignored. Refuse instead.
        has_xy = raw.get("x") is not None or raw.get("y") is not None
        if ref is not None and has_xy:
            raise DesktopError(
                f"{atype} takes either ref or x/y, not both: a ref already "
                "resolves to the control's live position",
                name="InvalidAction",
            )
        # Coordinates are required for a pointer action with no ref. Reported
        # here (rather than via _REQUIRED_FIELDS) because it is conditional.
        needs_xy = atype in ("click", "double_click", "move", "scroll")
        if needs_xy and ref is None:
            if raw.get("x") is None or raw.get("y") is None:
                raise DesktopError(
                    f"{atype} requires x and y (or a ref from ui_snapshot)",
                    name="InvalidAction",
                )
        # Background delivery posts to a specific window's message queue, so
        # without a target there is nowhere to post.
        if delivery == "background" and window is None and ref is None:
            raise DesktopError(
                "delivery='background' needs a window or ref naming the "
                "target; take a ui_snapshot to obtain one",
                name="InvalidAction",
            )

        x: int | None = None
        y: int | None = None
        if raw.get("x") is not None or raw.get("y") is not None:
            # Window-relative coordinates may be negative only in the sense of
            # being outside the client area, which is not a useful target, so
            # both spaces keep the non-negative rule.
            x = _require_i32_nonneg(raw.get("x"), "x")
            y = _require_i32_nonneg(raw.get("y"), "y")

        button: str | None = None
        if atype == "click":
            # Omitted → "left" (the overwhelmingly common intent). An explicit
            # value is still validated against BUTTONS.
            button = raw.get("button") or "left"
            if button not in BUTTONS:
                raise DesktopError(
                    f"invalid button: {button!r}", name="InvalidAction"
                )

        path: tuple[Point, ...] | None = None
        if atype == "drag":
            path = _parse_path(raw["path"])

        scroll_x: int | None = None
        scroll_y: int | None = None
        if atype == "scroll":
            scroll_x = _require_i32(raw["scroll_x"], "scroll_x")
            scroll_y = _require_i32(raw["scroll_y"], "scroll_y")

        text: str | None = None
        if atype == "type":
            text = raw["text"]
            if not isinstance(text, str):
                raise DesktopError("text must be a string", name="InvalidAction")

        keys: tuple[str, ...] | None = None
        if atype == "keypress":
            keys = _parse_chord_keys(raw["keys"])
        elif "keys" in _OPTIONAL_FIELDS[atype]:
            keys = _normalize_modifiers(raw.get("keys"))

        max_nodes: int | None = None
        interactive_only = True
        if atype == "ui_snapshot":
            if raw.get("max_nodes") is not None:
                max_nodes = _require_i32_nonneg(raw["max_nodes"], "max_nodes")
                if max_nodes < 1 or max_nodes > _MAX_SNAPSHOT_NODES:
                    raise DesktopError(
                        f"max_nodes must be 1..{_MAX_SNAPSHOT_NODES}",
                        name="InvalidAction",
                    )
            flag = raw.get("interactive_only")
            if flag is not None:
                if not isinstance(flag, bool):
                    raise DesktopError(
                        "interactive_only must be a boolean",
                        name="InvalidAction",
                    )
                interactive_only = flag

        return cls(
            type=atype,
            x=x,
            y=y,
            button=button,
            path=path,
            keys=keys,
            scroll_x=scroll_x,
            scroll_y=scroll_y,
            text=text,
            ref=ref,
            window=window,
            delivery=delivery,
            max_nodes=max_nodes,
            interactive_only=interactive_only,
        )

    def is_input(self) -> bool:
        """``True`` for any action that produces mouse/keyboard input.

        ``screenshot``, ``wait`` and ``ui_snapshot`` only READ the desktop;
        everything else is an input action requiring approval (book 01 §1.7).
        """
        return self.type not in ("screenshot", "wait", "ui_snapshot")


def _parse_path(raw: Any) -> tuple[Point, ...]:
    if not isinstance(raw, (list, tuple)):
        raise DesktopError("path must be a list", name="InvalidAction")
    if len(raw) < 2:
        raise DesktopError("path requires >= 2 points", name="InvalidAction")
    points: list[Point] = []
    for item in raw:
        if not isinstance(item, dict):
            raise DesktopError("path point must be an object", name="InvalidAction")
        extra = set(item.keys()) - {"x", "y"}
        if extra:
            raise DesktopError(
                f"unexpected path point field(s): {sorted(extra)}",
                name="InvalidAction",
            )
        if item.get("x") is None or item.get("y") is None:
            raise DesktopError("path point requires x and y", name="InvalidAction")
        points.append(
            Point(
                x=_require_i32_nonneg(item["x"], "path.x"),
                y=_require_i32_nonneg(item["y"], "path.y"),
            )
        )
    return tuple(points)


#: Windows-key aliases (all map to VK_LWIN in the keymap). A chord combining
#: any of these with certain letters opens a DISRUPTIVE full-screen system
#: overlay that hijacks the desktop and breaks the automation flow — e.g.
#: ``WIN+SHIFT+S`` (Snip & Sketch screen-annotation overlay), ``WIN+W``
#: (Widgets / Ink workspace), ``WIN+G`` (Game Bar), ``WIN+H`` (voice typing),
#: ``WIN+;`` / ``WIN+.`` (emoji panel). Purely PREVENTIVE: the computer tool has
#: no legitimate need to open these, and an overlay the model cannot dismiss
#: would strand the automation. (This guard was added while investigating stray
#: on-screen annotation overlays; those were never traced to this tool, so treat
#: it as defence-in-depth rather than a fix for a known trigger.)
_WIN_KEY_ALIASES: frozenset[str] = frozenset(
    {"WIN", "WINDOWS", "META", "SUPER", "CMD", "COMMAND"}
)
#: Second key that, combined with the Windows key, opens a disruptive overlay.
_WIN_OVERLAY_KEYS: frozenset[str] = frozenset(
    {"S", "W", "G", "H", "A", "N", "Q", ";", ".", "K"}
)


def _reject_disruptive_win_chord(element: str) -> None:
    """Reject a ``WIN+<overlay-key>`` chord that opens a system overlay.

    Fail-closed: raises :class:`DesktopError` (``name="InvalidAction"``) with an
    actionable message so the model retries via the intended in-app action
    (e.g. click the app's own button) rather than a global OS shortcut.
    """
    comps = {c.strip().upper() for c in element.split("+") if c.strip()}
    if not (comps & _WIN_KEY_ALIASES):
        return
    overlay = comps & _WIN_OVERLAY_KEYS
    if overlay:
        raise DesktopError(
            f"refused Windows-key shortcut {element!r}: it opens a disruptive "
            "system overlay (screen annotation / widgets / game bar / emoji) "
            "that hijacks the desktop. Use the target app's own on-screen "
            "controls instead of a global Win+ shortcut.",
            name="InvalidAction",
        )


def _parse_chord_keys(raw: Any) -> tuple[str, ...]:
    """Validate ``keypress.keys``: non-empty list, each element a
    non-empty ``+``-joined chord whose components are all non-empty."""
    if not isinstance(raw, (list, tuple)) or len(raw) == 0:
        raise DesktopError(
            "keypress requires a non-empty keys list", name="InvalidAction"
        )
    out: list[str] = []
    for element in raw:
        if not isinstance(element, str) or not element.strip():
            raise DesktopError(
                "keypress key must be a non-empty string", name="InvalidAction"
            )
        for component in element.split("+"):
            if not component.strip():
                raise DesktopError(
                    f"empty key component in {element!r}", name="InvalidAction"
                )
        _reject_disruptive_win_chord(element)
        out.append(element)
    return tuple(out)


# ---------------------------------------------------------------------------
# Capture / capabilities / options
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Display:
    """Geometry of one monitor inside the composited screenshot.

    One of these per captured monitor. ``x`` / ``y`` / ``width`` / ``height`` are
    the panel's rect in VIRTUAL-DESKTOP logical coordinates (``x`` / ``y`` may be
    negative when a monitor sits left of / above the primary); ``pixel_*`` locate
    the same panel inside the composite image. Every value is measured when the
    screenshot is taken, so a resolution change, a monitor being plugged in, or a
    Duplicate<->Extend toggle is reflected on the next frame.
    """

    id: str
    name: str
    x: int
    y: int
    width: int
    height: int
    scale: float
    pixel_x: int
    pixel_y: int
    pixel_width: int
    pixel_height: int
    is_primary: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "scale": self.scale,
            "pixel_x": self.pixel_x,
            "pixel_y": self.pixel_y,
            "pixel_width": self.pixel_width,
            "pixel_height": self.pixel_height,
            "is_primary": self.is_primary,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Display":
        return cls(
            id=str(raw["id"]),
            name=str(raw["name"]),
            x=int(raw["x"]),
            y=int(raw["y"]),
            width=int(raw["width"]),
            height=int(raw["height"]),
            scale=float(raw["scale"]),
            pixel_x=int(raw["pixel_x"]),
            pixel_y=int(raw["pixel_y"]),
            pixel_width=int(raw["pixel_width"]),
            pixel_height=int(raw["pixel_height"]),
            is_primary=bool(raw["is_primary"]),
        )


@dataclass(frozen=True, slots=True)
class Capture:
    """The result of a screenshot / batch execution.

    ``ax_text`` carries the rendered accessibility tree produced by any
    ``ui_snapshot`` action in the batch. It rides along with the trailing frame
    rather than being returned separately so one batch still yields one result,
    and it is ``None`` whenever the batch contained no ``ui_snapshot``.
    """

    data: bytes
    width: int
    height: int
    displays: tuple[Display, ...]
    backend: str
    display_server: str | None
    capture_permission: str
    input_permission: str
    ax_text: str | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Probed capabilities of the desktop backend."""

    backend: str
    display_server: str | None
    capture: bool
    input: bool
    capture_permission: str
    input_permission: str
    display_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "display_server": self.display_server,
            "capture": self.capture,
            "input": self.input,
            "capture_permission": self.capture_permission,
            "input_permission": self.input_permission,
            "display_count": self.display_count,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Capabilities":
        return cls(
            backend=str(raw["backend"]),
            display_server=raw.get("display_server"),
            capture=bool(raw["capture"]),
            input=bool(raw["input"]),
            capture_permission=str(raw["capture_permission"]),
            input_permission=str(raw["input_permission"]),
            display_count=int(raw["display_count"]),
        )


@dataclass(frozen=True, slots=True)
class SessionOptions:
    """Desktop-session tunables (mirrors the 5 config keys).

    ``max_width`` / ``max_height`` default to :data:`None` = capture at the
    display's NATIVE resolution. A hard-coded cap cannot be right: every user's
    panel differs and the size changes when an external monitor is plugged in or
    the resolution changes, so a fixed ceiling silently downscales the
    screenshot. That costs coordinate accuracy — the model estimates click
    points from the image, and a downscaled image shrinks small targets (a
    desktop icon's hot spot is only ~40-60 px) while widening the error of every
    estimate. Native capture keeps screenshot pixels 1:1 with screen pixels, so
    the coordinate the model reads IS the coordinate we click. The capture layer
    still auto-downscales as a LAST RESORT if a frame would exceed its pixel
    guard, and a user may still pin an explicit ceiling to save tokens.
    """

    backend: str = "auto"
    display: str = "all"
    max_width: int | None = None
    max_height: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "display": self.display,
            "max_width": self.max_width,
            "max_height": self.max_height,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SessionOptions":
        return cls(
            backend=str(raw.get("backend", "auto")),
            display=str(raw.get("display", "all")),
            max_width=raw.get("max_width"),
            max_height=raw.get("max_height"),
        )

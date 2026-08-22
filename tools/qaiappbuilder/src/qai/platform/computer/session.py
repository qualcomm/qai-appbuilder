# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-worker desktop session: capture + coordinate mapping + scheduling.

Runs inside the worker subprocess on the dedicated Win32 thread. Owns the
policy that the Win32 primitives do not: coordinate mapping (composite
pixel → logical screen), pixel→step scroll conversion, batch scheduling
with a trailing screenshot, wait-budget enforcement, and layout-change
detection between the batch's reference frame and the current display.

The Win32 primitives are reached through a small :class:`DesktopBackend`
Protocol so tests inject a fake backend and exercise ALL of this policy
without touching a real desktop.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Protocol

from .types import (
    Action,
    Capabilities,
    Capture,
    DesktopError,
    Display,
    SessionOptions,
    scroll_steps,
)

__all__ = ["DesktopBackend", "DesktopSession", "Win32Backend"]

#: Total wall-clock budget for one batch. Aligned with the reference
#: implementation's ``OPERATION_TIMEOUT`` (60s): a batch legitimately combines a
#: screenshot, several input actions with their settle delays, and a trailing
#: screenshot — a tight ceiling aborted such batches mid-way with
#: ``OperationTimeout`` even though nothing was wrong.
_OPERATION_BUDGET_S: float = 60.0
#: How long ONE ``wait`` action sleeps (reference ``WAIT_ACTION_DURATION``).
#: ``wait`` means "give the UI time to settle", so it is a real pause rather
#: than a polling slice; it still consumes the batch budget and is clamped to
#: the remaining time.
_WAIT_ACTION_S: float = 2.0


#: Slack reserved for the non-``wait`` work in a batch (two screenshots plus the
#: input actions), mirroring the reference implementation's 5s reservation.
_WAIT_BUDGET_SLACK_S: float = 5.0

#: Default ceiling for a ``ui_snapshot`` that does not name one. Matches the AX
#: layer's own default so the two cannot drift.
_DEFAULT_SNAPSHOT_NODES: int = 800


def _validate_wait_budget(actions: "list[Action]") -> None:
    """Reject a batch whose ``wait`` actions alone cannot fit the budget.

    Fail-closed BEFORE anything is executed: a batch that provably cannot
    complete would otherwise abort part-way, leaving the desktop half-acted.
    """
    waits = sum(1 for a in actions if a.type == "wait")
    if waits * _WAIT_ACTION_S > _OPERATION_BUDGET_S - _WAIT_BUDGET_SLACK_S:
        raise DesktopError(
            f"batch cannot complete within the "
            f"{_OPERATION_BUDGET_S:.0f}s deadline: {waits} wait action(s)",
            name="InvalidAction",
        )


def _panel_rects(ref: "Capture") -> tuple[tuple[int, int, int, int], ...]:
    """Each captured panel's virtual-desktop rect, in report order."""
    return tuple((d.x, d.y, d.width, d.height) for d in ref.displays)


def _virtual_bounds(ref: "Capture") -> tuple[int, int, int, int]:
    """``(origin_x, origin_y, width, height)`` spanned by a captured frame.

    Derived from the frame's own display list — i.e. from geometry measured when
    that screenshot was taken, never from a cached or assumed layout. The origin
    is negative when a monitor sits left of / above the primary, and the span
    covers every captured panel so a coordinate on a secondary display is
    representable. A frame with no displays degrades to the image dimensions.
    """
    if not ref.displays:
        return 0, 0, ref.width, ref.height
    x0 = min(d.x for d in ref.displays)
    y0 = min(d.y for d in ref.displays)
    x1 = max(d.x + d.width for d in ref.displays)
    y1 = max(d.y + d.height for d in ref.displays)
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


class DesktopBackend(Protocol):
    """The Win32 primitive surface a session drives.

    A real implementation delegates to :mod:`.win32.capture` /
    :mod:`.win32.input`; tests supply an in-memory fake.
    """

    def probe(self) -> Capabilities: ...

    def capture(
        self,
        *,
        max_width: int | None,
        max_height: int | None,
        display: str = "all",
    ) -> Capture: ...

    # ``origin_x`` / ``origin_y`` are the virtual-desktop origin (negative when a
    # monitor sits left of / above the primary); ``logical_w`` / ``logical_h`` the
    # span across every captured monitor. Both are measured per action, so a
    # display change is picked up without restarting.
    def move(
        self, gx: int, gy: int, *, logical_w: int, logical_h: int,
        origin_x: int = 0, origin_y: int = 0,
    ) -> None: ...

    def click(
        self, gx: int, gy: int, button: str, *, logical_w: int, logical_h: int,
        origin_x: int = 0, origin_y: int = 0,
    ) -> None: ...

    def double_click(
        self, gx: int, gy: int, *, logical_w: int, logical_h: int,
        origin_x: int = 0, origin_y: int = 0,
    ) -> None: ...

    def drag(
        self, points: list[tuple[int, int]], *, logical_w: int, logical_h: int,
        origin_x: int = 0, origin_y: int = 0,
    ) -> None: ...

    def scroll(
        self, gx: int, gy: int, steps_x: int, steps_y: int, *,
        logical_w: int, logical_h: int,
        origin_x: int = 0, origin_y: int = 0,
    ) -> None: ...

    def type_text(self, text: str) -> None: ...

    def keypress(self, chords: list[str]) -> None: ...

    def press_modifiers(self, mods: list[str]) -> None: ...

    def release_modifiers(self, mods: list[str]) -> None: ...

    # ---- window-scoped capture -------------------------------------------

    def capture_window(
        self, hwnd: int, *, max_width: int | None, max_height: int | None
    ) -> Capture: ...

    # ---- accessibility ---------------------------------------------------

    def ax_snapshot(
        self,
        *,
        hwnd: int | None,
        max_nodes: int,
        interactive_only: bool,
    ) -> str: ...

    def ax_ref_rect(self, ref: str) -> tuple[int, int, int, int]: ...

    def ax_ref_value(self, ref: str) -> str | None: ...

    def ax_activate(self, ref: str) -> str: ...

    def ax_set_value(self, ref: str, text: str) -> None: ...

    def ax_ref_window(self, ref: str) -> int: ...

    # ---- background delivery ---------------------------------------------
    #
    # Screen coordinates in, message-posting out: the backend converts to the
    # target's client space itself, so the session never has to know which
    # message carries which coordinate convention.

    def click_background(
        self, hwnd: int, gx: int, gy: int, button: str,
        modifiers: list[str] | None = None,
    ) -> None: ...

    def double_click_background(
        self, hwnd: int, gx: int, gy: int, modifiers: list[str] | None = None
    ) -> None: ...

    def scroll_background(
        self, hwnd: int, gx: int, gy: int, steps_x: int, steps_y: int,
        modifiers: list[str] | None = None,
    ) -> None: ...

    def type_text_background(self, hwnd: int, text: str) -> None: ...

    def keypress_background(self, hwnd: int, chords: list[str]) -> None: ...

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]: ...


class DesktopSession:
    """A single desktop session (implements ``DesktopSessionPort``)."""

    __slots__ = ("_backend", "_options", "_caps", "_clock")

    def __init__(
        self,
        options: SessionOptions,
        *,
        backend: DesktopBackend,
        clock: "object" = time.monotonic,
    ) -> None:
        self._backend = backend
        self._options = options
        self._clock = clock  # Callable[[], float]
        self._caps = backend.probe()
        if not self._caps.capture:
            raise DesktopError(
                "capture unavailable in this session", name="Unavailable"
            )

    @property
    def capabilities(self) -> Capabilities:
        return self._caps

    def capture(self) -> Capture:
        return self._backend.capture(
            max_width=self._options.max_width,
            max_height=self._options.max_height,
            display=self._options.display,
        )

    def close(self) -> None:  # no persistent handles held between calls
        return None

    # ------------------------------------------------------------------
    # Batch execution
    # ------------------------------------------------------------------

    def execute(self, actions: list[Action]) -> Capture:
        """Run the batch, then return a fresh trailing screenshot.

        A single reference frame is captured up front; all coordinate actions
        map against it, and a layout change (resolution / pixel dims differ on
        the trailing frame) raises ``LayoutChanged``.

        Coordinates are pixels of the reference IMAGE, which spans every
        captured monitor. The bounds come from that frame — measured at capture
        time — so an external monitor, a resolution change, or a
        Duplicate<->Extend toggle is reflected immediately.

        A batch whose ``wait`` actions alone cannot fit in the budget is
        rejected UP FRONT (reference ``validate_batch_wait_budget``) rather than
        aborted half-executed — leaving the desktop in a partially-acted state
        is worse than refusing a request that provably cannot complete.

        Two things modulate the returned frame. A trailing ``screenshot`` that
        names a ``window`` makes the result that window's pixels instead of the
        desktop's, and any ``ui_snapshot`` in the batch attaches its rendered
        tree as ``ax_text``. Both keep one batch returning exactly one result.
        """
        _validate_wait_budget(actions)
        deadline = self._now() + _OPERATION_BUDGET_S
        ref = self.capture()
        ax_chunks: list[str] = []
        for action in actions:
            if self._now() > deadline:
                raise DesktopError(
                    "batch exceeded operation budget", name="OperationTimeout"
                )
            rendered = self._run_one(action, ref, deadline)
            if rendered is not None:
                ax_chunks.append(rendered)

        # A window-scoped trailing frame is worth far more than a desktop frame
        # when the batch was working inside one window: it cannot be invalidated
        # by another window covering the target.
        window = self._trailing_window(actions)
        if window is not None:
            final = self._backend.capture_window(
                window,
                max_width=self._options.max_width,
                max_height=self._options.max_height,
            )
        else:
            final = self.capture()
            # Layout comparison only means something between two frames of the
            # same kind; a window frame has its own geometry by construction.
            self._check_layout(ref, final)
        if ax_chunks:
            final = replace(final, ax_text="\n\n".join(ax_chunks))
        return final

    @staticmethod
    def _trailing_window(actions: list[Action]) -> int | None:
        """The window named by a trailing ``screenshot``, if any."""
        for action in reversed(actions):
            if action.type == "screenshot":
                return action.window
            if action.type != "wait":
                return None
        return None

    def _run_one(
        self, action: Action, ref: Capture, deadline: float
    ) -> str | None:
        """Execute one action; returns rendered AX text for ``ui_snapshot``."""
        atype = action.type
        if atype == "screenshot":
            return None  # trailing frame covers it; no intermediate capture
        if atype == "wait":
            self._consume_wait(deadline)
            return None
        if atype == "ui_snapshot":
            return self._backend.ax_snapshot(
                hwnd=action.window,
                max_nodes=action.max_nodes or _DEFAULT_SNAPSHOT_NODES,
                interactive_only=action.interactive_only,
            )

        if action.delivery == "background":
            self._run_background(action, ref)
            return None

        # Virtual-desktop span + origin spanned by this frame (origin may be
        # negative when a monitor sits left of / above the primary).
        ox, oy, lw, lh = _virtual_bounds(ref)
        geom = {
            "logical_w": lw,
            "logical_h": lh,
            "origin_x": ox,
            "origin_y": oy,
        }
        if atype == "move":
            gx, gy = self._target(action, ref)
            self._apply_modifiers(
                action.keys, lambda: self._backend.move(gx, gy, **geom)
            )
            return None
        if atype == "click":
            gx, gy = self._target(action, ref)
            self._apply_modifiers(
                action.keys,
                lambda: self._backend.click(
                    gx, gy, action.button or "left", **geom
                ),
            )
            return None
        if atype == "double_click":
            gx, gy = self._target(action, ref)
            self._apply_modifiers(
                action.keys, lambda: self._backend.double_click(gx, gy, **geom)
            )
            return None
        if atype == "drag":
            mapped = [
                self._map_point(p.x, p.y, ref, action.window)
                for p in (action.path or ())
            ]
            self._apply_modifiers(
                action.keys, lambda: self._backend.drag(mapped, **geom)
            )
            return None
        if atype == "scroll":
            gx, gy = self._target(action, ref)
            sx = scroll_steps(action.scroll_x or 0)
            sy = scroll_steps(action.scroll_y or 0)
            self._apply_modifiers(
                action.keys,
                lambda: self._backend.scroll(gx, gy, sx, sy, **geom),
            )
            return None
        if atype == "type":
            if action.ref is not None:
                # Setting the value through the control itself is atomic and
                # focus-independent, so try that first.
                try:
                    self._backend.ax_set_value(action.ref, action.text or "")
                    return None
                except DesktopError:
                    # The control has no settable value, so the text has to be
                    # typed — which only works if the control holds focus. Under
                    # FOREGROUND delivery that means a real click, which does
                    # move the pointer; that is inherent to foreground input,
                    # and the approval line for this action already names the
                    # ref as the target. Background delivery never reaches here
                    # (it posts to the window instead, see _run_background).
                    self._focus_ref_target(action, ref)
            self._backend.type_text(action.text or "")
            return None
        if atype == "keypress":
            self._backend.keypress(list(action.keys or ()))
            return None
        raise DesktopError(f"unhandled action: {atype}", name="InvalidAction")

    # ------------------------------------------------------------------
    # Background delivery
    # ------------------------------------------------------------------

    def _run_background(self, action: Action, ref: Capture) -> None:
        """Post the action to its target window instead of synthesising input.

        Message posting is honoured by classic Win32 controls (verified: text
        posted to an ``EDIT`` control and to Notepad++'s Scintilla view both
        arrived while a different window held the foreground) but is IGNORED
        outright by WinUI/XAML apps, whose controls are not message-processing
        windows — Windows 11's own Notepad accepted neither ``WM_CHAR`` nor
        ``EM_REPLACESEL`` nor ``WM_SETTEXT``. Typing therefore prefers the
        accessibility route and VERIFIES the result, because a silent no-op is
        the one outcome the model cannot detect for itself.
        """
        hwnd = self._resolve_window(action)
        mods = list(action.keys or ())
        atype = action.type
        if atype == "type":
            self._type_background(action, hwnd)
            return
        if atype == "keypress":
            self._backend.keypress_background(hwnd, list(action.keys or ()))
            return
        gx, gy = self._target(action, ref)
        if atype == "click":
            self._backend.click_background(
                hwnd, gx, gy, action.button or "left", mods
            )
            return
        if atype == "double_click":
            self._backend.double_click_background(hwnd, gx, gy, mods)
            return
        if atype == "scroll":
            self._backend.scroll_background(
                hwnd,
                gx,
                gy,
                scroll_steps(action.scroll_x or 0),
                scroll_steps(action.scroll_y or 0),
                mods,
            )
            return
        if atype == "move":
            # A pointer move has no meaning without a real cursor: the whole
            # point of background delivery is leaving the cursor alone. Silently
            # doing nothing would be worse than saying so.
            raise DesktopError(
                "move cannot use delivery='background': it exists to position "
                "the real cursor, which background delivery never touches",
                name="InvalidAction",
            )
        raise DesktopError(
            f"{atype} does not support delivery='background'",
            name="InvalidAction",
        )

    def _type_background(self, action: Action, hwnd: int) -> None:
        """Enter text without touching focus, and prove it arrived.

        Order matters. The accessibility ``ValuePattern`` is tried FIRST when a
        ref is available because it is the only mechanism that works on
        WinUI/XAML controls, replaces the value atomically, and needs no focus.
        Message posting is the fallback for classic controls, which UIA often
        exposes without a settable value.

        If neither route can be confirmed the action FAILS rather than reporting
        success: an undetectable no-op would leave the model believing it had
        typed, and every subsequent step would build on that false premise.
        """
        text = action.text or ""
        if action.ref is not None:
            try:
                self._backend.ax_set_value(action.ref, text)
                return
            except DesktopError:
                pass  # not a value control; try posting characters instead
        self._backend.type_text_background(hwnd, text)
        if not text:
            return
        # Verify via the accessibility value, the only observer that works
        # across both classic and WinUI controls. An unverifiable target (no ref,
        # or a control exposing no value) is accepted: we cannot distinguish
        # "worked but unreadable" from "ignored", and refusing every such case
        # would rule out the classic controls this path exists to serve.
        if action.ref is None:
            return
        try:
            observed = self._backend.ax_ref_value(action.ref)
        except DesktopError:
            return
        if observed is not None and text not in observed:
            raise DesktopError(
                f"background typing did not reach window {hwnd}: the control's "
                f"value is still {observed[:40]!r}. This application ignores "
                "posted input (WinUI/XAML apps commonly do); retry without "
                "delivery='background', or target a ref whose value can be set.",
                name="InputFailed",
            )

    def _resolve_window(self, action: Action) -> int:
        """The window a background action posts to."""
        if action.window is not None:
            return action.window
        if action.ref is not None:
            return self._backend.ax_ref_window(action.ref)
        raise DesktopError(
            "background delivery requires a window or ref", name="InvalidAction"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _map(self, px: int | None, py: int | None, ref: Capture) -> tuple[int, int]:
        """Composite-image pixel → virtual-desktop logical coordinate.

        The frame spans every captured monitor, so a pixel anywhere in the image
        — including on a secondary display — resolves to a real screen
        coordinate. Bounds come from the frame itself (measured at capture
        time); an out-of-image pixel raises ``CoordinateOutOfRange``.
        """
        if px is None or py is None:
            raise DesktopError("missing coordinate", name="InvalidAction")
        tw, th = ref.width, ref.height
        if px < 0 or px >= tw or py < 0 or py >= th:
            raise DesktopError(
                f"coordinate ({px},{py}) outside {tw}x{th}",
                name="CoordinateOutOfRange",
            )
        ox, oy, lw, lh = _virtual_bounds(ref)
        gx = ox + round(px * lw / tw)
        gy = oy + round(py * lh / th)
        return gx, gy

    def _target(self, action: Action, ref: Capture) -> tuple[int, int]:
        """The virtual-desktop point an action acts on.

        Three ways to name a target, in order of precision:

        * ``ref`` — the platform reports the control's LIVE rectangle, so the
          point is exact and stays correct even if the control moved or
          scrolled since the snapshot.
        * ``window`` + ``x``/``y`` — coordinates relative to that window's
          top-left, so they survive the window being moved.
        * ``x``/``y`` alone — pixels of the reference frame, mapped through the
          captured geometry (the original behaviour).
        """
        if action.ref is not None:
            left, top, right, bottom = self._backend.ax_ref_rect(action.ref)
            if right <= left or bottom <= top:
                raise DesktopError(
                    f"ref {action.ref!r} has no clickable area; it may have "
                    "been destroyed — take a new ui_snapshot",
                    name="CoordinateOutOfRange",
                )
            return ((left + right) // 2, (top + bottom) // 2)
        return self._map_point(action.x, action.y, ref, action.window)

    def _map_point(
        self, px: int | None, py: int | None, ref: Capture, window: int | None
    ) -> tuple[int, int]:
        """Map a coordinate in whichever space the action chose."""
        if window is None:
            return self._map(px, py, ref)
        if px is None or py is None:
            raise DesktopError("missing coordinate", name="InvalidAction")
        left, top, right, bottom = self._backend.window_rect(window)
        width, height = right - left, bottom - top
        if px >= width or py >= height:
            raise DesktopError(
                f"window-relative coordinate ({px},{py}) outside the window's "
                f"{width}x{height} area",
                name="CoordinateOutOfRange",
            )
        return (left + px, top + py)

    def _focus_ref_target(self, action: Action, ref: Capture) -> None:
        """Click a ref'd control so subsequent typing lands in it.

        Used when a field does not support having its value set directly: the
        text still has to go somewhere, and the control must hold focus first.
        """
        gx, gy = self._target(action, ref)
        ox, oy, lw, lh = _virtual_bounds(ref)
        self._backend.click(
            gx, gy, "left",
            logical_w=lw, logical_h=lh, origin_x=ox, origin_y=oy,
        )

    def _apply_modifiers(self, keys: tuple[str, ...] | None, action: "object") -> None:
        """Hold modifiers down (in order), run ``action``, release in reverse.

        Modifiers are already validated (CTRL/SHIFT/ALT/META, no dupes).
        The release always runs even if ``action`` raises, so no modifier
        stays stuck down; the first error is re-raised.
        """
        mods = list(keys or ())
        if not mods:
            action()  # type: ignore[operator]
            return
        first_err: DesktopError | None = None
        pressed = False
        try:
            self._backend.press_modifiers(mods)
            pressed = True
            action()  # type: ignore[operator]
        except DesktopError as exc:
            first_err = exc
        finally:
            if pressed:
                try:
                    self._backend.release_modifiers(mods)
                except DesktopError as exc:
                    if first_err is None:
                        first_err = exc
        if first_err is not None:
            raise first_err

    def _consume_wait(self, deadline: float) -> None:
        remaining = deadline - self._now()
        if remaining <= 0:
            raise DesktopError("wait budget exhausted", name="OperationTimeout")
        time.sleep(min(_WAIT_ACTION_S, remaining))

    def _check_layout(self, before: Capture, after: Capture) -> None:
        """Reject a batch whose display layout changed underneath it.

        Compares the FRAME geometry (image dims + each panel's virtual-desktop
        rect), so plugging in a monitor, changing a resolution, or toggling
        Duplicate<->Extend mid-batch is caught instead of silently mapping
        coordinates against a stale layout.
        """
        if (before.width, before.height) != (after.width, after.height):
            raise DesktopError(
                "display layout changed mid-batch", name="LayoutChanged"
            )
        if _panel_rects(before) != _panel_rects(after):
            raise DesktopError(
                "display layout changed mid-batch", name="LayoutChanged"
            )

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Real Win32 backend
# ---------------------------------------------------------------------------


class Win32Backend:
    """The production backend wiring :mod:`.win32.capture`/`.win32.input`."""

    __slots__ = ()

    def probe(self) -> Capabilities:
        from .win32 import capture as _capture

        return _capture.probe_capabilities()

    def capture(
        self,
        *,
        max_width: int | None,
        max_height: int | None,
        display: str = "all",
    ) -> Capture:
        from .win32 import capture as _capture

        return _capture.capture_primary(
            max_width=max_width, max_height=max_height, display=display
        )

    def move(
        self, gx: int, gy: int, *, logical_w: int, logical_h: int,
        origin_x: int = 0, origin_y: int = 0,
    ) -> None:
        from .win32 import input as _input

        _input.move(
            gx, gy, logical_w=logical_w, logical_h=logical_h,
            origin_x=origin_x, origin_y=origin_y,
        )

    def click(
        self, gx: int, gy: int, button: str, *, logical_w: int, logical_h: int,
        origin_x: int = 0, origin_y: int = 0,
    ) -> None:
        from .win32 import input as _input

        _input.click(
            gx, gy, button, logical_w=logical_w, logical_h=logical_h,
            origin_x=origin_x, origin_y=origin_y,
        )

    def double_click(
        self, gx: int, gy: int, *, logical_w: int, logical_h: int,
        origin_x: int = 0, origin_y: int = 0,
    ) -> None:
        from .win32 import input as _input

        _input.double_click(
            gx, gy, logical_w=logical_w, logical_h=logical_h,
            origin_x=origin_x, origin_y=origin_y,
        )

    def drag(
        self, points: list[tuple[int, int]], *, logical_w: int, logical_h: int,
        origin_x: int = 0, origin_y: int = 0,
    ) -> None:
        from .win32 import input as _input

        _input.drag(
            points, logical_w=logical_w, logical_h=logical_h,
            origin_x=origin_x, origin_y=origin_y,
        )

    def scroll(
        self, gx: int, gy: int, steps_x: int, steps_y: int, *,
        logical_w: int, logical_h: int,
        origin_x: int = 0, origin_y: int = 0,
    ) -> None:
        from .win32 import input as _input

        _input.scroll(
            gx, gy, steps_x, steps_y, logical_w=logical_w, logical_h=logical_h,
            origin_x=origin_x, origin_y=origin_y,
        )

    def type_text(self, text: str) -> None:
        from .win32 import input as _input

        _input.type_text(text)

    def keypress(self, chords: list[str]) -> None:
        from .win32 import input as _input

        _input.keypress(chords)

    def press_modifiers(self, mods: list[str]) -> None:
        from .win32 import input as _input

        _input.press_keys(mods)

    def release_modifiers(self, mods: list[str]) -> None:
        from .win32 import input as _input

        # Reverse order: last pressed is released first (book 04 §4.7).
        _input.release_keys(list(reversed(mods)))

    # ------------------------------------------------------------------
    # Window-scoped capture
    # ------------------------------------------------------------------

    def capture_window(
        self, hwnd: int, *, max_width: int | None, max_height: int | None
    ) -> Capture:
        from .win32 import capture as _capture

        return _capture.capture_window(
            hwnd, max_width=max_width, max_height=max_height
        )

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        from .win32 import windows_info as _info

        rect = _info.window_rect(hwnd)
        if rect is None:
            raise DesktopError(
                f"window {hwnd} no longer exists", name="InvalidAction"
            )
        return rect

    # ------------------------------------------------------------------
    # Accessibility
    #
    # UI Automation failures are translated to DesktopError here so the session
    # layer never has to know that COM is involved.
    # ------------------------------------------------------------------

    def ax_snapshot(
        self, *, hwnd: int | None, max_nodes: int, interactive_only: bool
    ) -> str:
        from .win32 import ax as _ax
        from .win32 import windows_info as _info

        target = hwnd
        if target is None:
            # Default to the foreground window: the desktop root's tree is
            # dominated by other applications, and the model is virtually
            # always asking about the window it is working in.
            target = _info.foreground_window()
        try:
            snap = _ax.snapshot(
                hwnd=target,
                max_nodes=max_nodes,
                interactive_only=interactive_only,
            )
        except Exception as exc:  # noqa: BLE001 — COM/UIA failure surface
            raise DesktopError(
                f"accessibility snapshot failed: {exc}", name="Unavailable"
            ) from exc
        header = f"ui_snapshot of {snap.root_title!r}" if snap.root_title else "ui_snapshot"
        body = snap.render()
        if not body:
            return (
                f"{header}: no interactive controls exposed. This window draws "
                "its own UI without accessibility information; use a screenshot "
                "and coordinates instead."
            )
        return f"{header}\n{body}"

    def ax_ref_rect(self, ref: str) -> tuple[int, int, int, int]:
        from .win32 import ax as _ax

        try:
            return _ax.resolve_ref(ref)
        except Exception as exc:  # noqa: BLE001
            raise DesktopError(str(exc), name="CoordinateOutOfRange") from exc

    def ax_ref_value(self, ref: str) -> str | None:
        """A ref'd control's current text, or ``None`` if it exposes none.

        Used to CONFIRM that background typing arrived; ``None`` means the
        control cannot be observed this way, which is not an error.
        """
        from .win32 import ax as _ax

        try:
            return _ax.ref_value(ref)
        except Exception as exc:  # noqa: BLE001
            raise DesktopError(str(exc), name="InvalidAction") from exc

    def ax_activate(self, ref: str) -> str:
        from .win32 import ax as _ax

        try:
            return _ax.activate_ref(ref)
        except Exception as exc:  # noqa: BLE001
            raise DesktopError(str(exc), name="InvalidAction") from exc

    def ax_set_value(self, ref: str, text: str) -> None:
        from .win32 import ax as _ax

        try:
            _ax.set_ref_value(ref, text)
        except Exception as exc:  # noqa: BLE001
            raise DesktopError(str(exc), name="InvalidAction") from exc

    def ax_ref_window(self, ref: str) -> int:
        from .win32 import ax as _ax

        try:
            return _ax.ref_window(ref)
        except Exception as exc:  # noqa: BLE001
            raise DesktopError(str(exc), name="InvalidAction") from exc

    # ------------------------------------------------------------------
    # Background delivery
    # ------------------------------------------------------------------

    def click_background(
        self, hwnd: int, gx: int, gy: int, button: str,
        modifiers: list[str] | None = None,
    ) -> None:
        from .win32 import delivery as _delivery

        self._post(
            lambda: _delivery.click_background(
                hwnd, gx, gy, button, modifiers=modifiers
            )
        )

    def double_click_background(
        self, hwnd: int, gx: int, gy: int, modifiers: list[str] | None = None
    ) -> None:
        from .win32 import delivery as _delivery

        self._post(
            lambda: _delivery.double_click_background(
                hwnd, gx, gy, modifiers=modifiers
            )
        )

    def scroll_background(
        self, hwnd: int, gx: int, gy: int, steps_x: int, steps_y: int,
        modifiers: list[str] | None = None,
    ) -> None:
        from .win32 import delivery as _delivery

        self._post(
            lambda: _delivery.scroll_background(
                hwnd, gx, gy, steps_x, steps_y, modifiers=modifiers
            )
        )

    def type_text_background(self, hwnd: int, text: str) -> None:
        from .win32 import delivery as _delivery

        self._post(lambda: _delivery.type_text_background(hwnd, text))

    def keypress_background(self, hwnd: int, chords: list[str]) -> None:
        from .win32 import delivery as _delivery

        self._post(lambda: _delivery.keypress_background(hwnd, chords))

    @staticmethod
    def _post(call: "object") -> None:
        """Run a delivery call, translating its error type to DesktopError."""
        from .win32.delivery import BackgroundDeliveryError

        try:
            call()  # type: ignore[operator]
        except BackgroundDeliveryError as exc:
            raise DesktopError(str(exc), name="InputFailed") from exc

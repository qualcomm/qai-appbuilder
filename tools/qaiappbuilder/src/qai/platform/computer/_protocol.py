# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Wire protocol for the desktop-control worker subprocess.

Newline-delimited JSON (NDJSON) frames, matching the project's existing
runner IPC convention: one JSON object per ``\\n``-terminated UTF-8 line.
Screenshot PNG bytes travel base64-encoded inside the ``result`` frame
(book 03 §3.3 option A).

Direction / frame kinds (book 03 §3):

Inbound  (supervisor -> worker):  ping | init | execute | close
Outbound (worker -> supervisor):  pong | ready | result | error | closed

This module is pure (de)serialization — no I/O, no subprocess — so both
sides share one codec and it is unit-testable in isolation.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from .types import Action, Capabilities, Capture, Display, Point

__all__ = [
    "capture_from_wire",
    "capture_to_wire",
    "decode_frame",
    "encode_frame",
    "make_close",
    "make_closed",
    "make_error",
    "make_execute",
    "make_init",
    "make_ping",
    "make_pong",
    "make_ready",
    "make_result",
]


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded protocol frame: a ``type`` tag + its payload dict."""

    type: str
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Line codec
# ---------------------------------------------------------------------------


def encode_frame(frame: dict[str, Any]) -> bytes:
    """Serialize one frame dict to a ``\\n``-terminated UTF-8 line."""
    return (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")


def decode_frame(line: str) -> Frame:
    """Parse one JSON line into a :class:`Frame`.

    Raises:
        ValueError: if the line is not a JSON object with a string
            ``type`` field.
    """
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("frame must be a JSON object")
    ftype = obj.get("type")
    if not isinstance(ftype, str):
        raise ValueError("frame missing string 'type'")
    return Frame(type=ftype, payload=obj)


# ---------------------------------------------------------------------------
# Inbound frame builders
# ---------------------------------------------------------------------------


def make_ping(frame_id: int) -> dict[str, Any]:
    return {"type": "ping", "id": frame_id}


def make_init(options: dict[str, Any]) -> dict[str, Any]:
    return {"type": "init", "options": options}


def make_execute(frame_id: int, actions: list[Action]) -> dict[str, Any]:
    return {
        "type": "execute",
        "id": frame_id,
        "actions": [_action_to_wire(a) for a in actions],
    }


def make_close() -> dict[str, Any]:
    return {"type": "close"}


# ---------------------------------------------------------------------------
# Outbound frame builders
# ---------------------------------------------------------------------------


def make_pong(frame_id: int) -> dict[str, Any]:
    return {"type": "pong", "id": frame_id}


def make_ready(caps: Capabilities) -> dict[str, Any]:
    return {"type": "ready", "capabilities": caps.to_dict()}


def make_result(
    frame_id: int, capture: Capture, caps: Capabilities
) -> dict[str, Any]:
    return {
        "type": "result",
        "id": frame_id,
        "capture": capture_to_wire(capture),
        "capabilities": caps.to_dict(),
    }


def make_error(
    *, frame_id: int | None, name: str, message: str
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "error",
        "error": {"name": name, "message": message},
    }
    if frame_id is not None:
        frame["id"] = frame_id
    return frame


def make_closed() -> dict[str, Any]:
    return {"type": "closed"}


# ---------------------------------------------------------------------------
# Action (de)serialization
# ---------------------------------------------------------------------------


def _action_to_wire(action: Action) -> dict[str, Any]:
    raw: dict[str, Any] = {"type": action.type}
    if action.x is not None:
        raw["x"] = action.x
    if action.y is not None:
        raw["y"] = action.y
    if action.button is not None:
        raw["button"] = action.button
    if action.path is not None:
        raw["path"] = [{"x": p.x, "y": p.y} for p in action.path]
    if action.keys is not None:
        raw["keys"] = list(action.keys)
    if action.scroll_x is not None:
        raw["scroll_x"] = action.scroll_x
    if action.scroll_y is not None:
        raw["scroll_y"] = action.scroll_y
    if action.text is not None:
        raw["text"] = action.text
    if action.ref is not None:
        raw["ref"] = action.ref
    if action.window is not None:
        raw["window"] = action.window
    # Only send a non-default delivery: the worker re-parses this dict, and
    # emitting "foreground" for every action would trip the whitelist on the
    # types that do not accept the field.
    if action.delivery != "foreground":
        raw["delivery"] = action.delivery
    if action.max_nodes is not None:
        raw["max_nodes"] = action.max_nodes
    if action.type == "ui_snapshot" and not action.interactive_only:
        raw["interactive_only"] = action.interactive_only
    return raw


def action_from_wire(raw: dict[str, Any]) -> Action:
    """Reconstruct an :class:`Action` from a wire dict.

    Re-runs :meth:`Action.parse` so the worker independently validates
    what it received (defence in depth across the process boundary).
    """
    return Action.parse(raw)


# ---------------------------------------------------------------------------
# Capture (de)serialization (PNG bytes base64 per book 03 §3.3 A)
# ---------------------------------------------------------------------------


def capture_to_wire(capture: Capture) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "data": base64.b64encode(capture.data).decode("ascii"),
        "width": capture.width,
        "height": capture.height,
        "displays": [d.to_dict() for d in capture.displays],
        "backend": capture.backend,
        "display_server": capture.display_server,
        "capture_permission": capture.capture_permission,
        "input_permission": capture.input_permission,
    }
    # Omitted unless a ui_snapshot ran, keeping the frame small in the common
    # case (an accessibility tree is comparable in size to the PNG itself).
    if capture.ax_text is not None:
        raw["ax_text"] = capture.ax_text
    return raw


def capture_from_wire(raw: dict[str, Any]) -> Capture:
    return Capture(
        data=base64.b64decode(raw["data"]),
        width=int(raw["width"]),
        height=int(raw["height"]),
        displays=tuple(Display.from_dict(d) for d in raw["displays"]),
        backend=str(raw["backend"]),
        display_server=raw.get("display_server"),
        capture_permission=str(raw["capture_permission"]),
        input_permission=str(raw["input_permission"]),
        ax_text=raw.get("ax_text"),
    )


# Re-export for symmetry with the action builder (worker imports both).
_ = Point

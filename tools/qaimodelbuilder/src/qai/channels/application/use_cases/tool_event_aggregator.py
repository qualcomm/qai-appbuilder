# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure normalisation of ai_coding stream events for channel delivery.

Architecture cleanup (A-1 step1): these two helpers used to live as
module-private functions in the apps-layer
:mod:`apps.api._ai_coding_channel_bridge`.  They are pure data
transforms — no I/O, no collaborator calls, no cross-context imports —
so they belong in the channels application layer where the tool-progress
aggregation pipeline lives.

* :func:`normalise_event` — coerce a heterogeneous ai_coding stream
  event (dict-shaped or duck-typed object) into a canonical
  ``(kind, payload)`` pair the channel formatter understands.
* :func:`coerce_args` — coerce a tool-args payload into a plain
  ``dict[str, Any]`` for the formatter.
"""

from __future__ import annotations

from typing import Any


def normalise_event(event: Any) -> tuple[str, dict[str, Any]]:
    """Normalise an ai_coding stream event into ``(kind, payload)``.

    Accepts both dict-shaped events (``{"type": "delta", "content": ...}``)
    and object-shaped events (duck-typed ``.type`` / ``.text``).  Maps
    legacy ai_coding event names to the canonical four kinds the
    bridge cares about: ``delta`` / ``tool_start`` / ``tool_end`` /
    ``error``.  Unknown kinds round-trip with their original ``type``
    so callers can ignore them without raising.
    """
    if isinstance(event, dict):
        kind = str(event.get("type", "")).lower()
        payload = {k: v for k, v in event.items() if k != "type"}
        # Map legacy aliases.
        if kind == "delta":
            text = payload.get("content") or payload.get("text") or ""
            payload["text"] = text
        elif kind in ("tool_call", "subagent_tool_start"):
            kind = "tool_start"
        elif kind in ("tool_result", "subagent_tool_end"):
            kind = "tool_end"
        return kind, payload
    # Object-style event
    kind = str(getattr(event, "type", "")).lower()
    payload: dict[str, Any] = {}
    text = getattr(event, "content", None) or getattr(event, "text", None)
    if isinstance(text, str):
        payload["text"] = text
    name = getattr(event, "name", None) or getattr(
        event, "tool_name", None
    )
    if isinstance(name, str):
        payload["name"] = name
    args = getattr(event, "args", None) or getattr(
        event, "arguments", None
    )
    if args is not None:
        payload["args"] = args
    success = getattr(event, "success", None)
    if success is not None:
        payload["success"] = bool(success)
    if kind in ("tool_call", "subagent_tool_start"):
        kind = "tool_start"
    elif kind in ("tool_result", "subagent_tool_end"):
        kind = "tool_end"
    return kind, payload


def coerce_args(value: Any) -> dict[str, Any]:
    """Coerce a tool-args payload into a plain dict for the formatter."""
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}

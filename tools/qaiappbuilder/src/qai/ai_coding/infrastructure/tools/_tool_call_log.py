# ---------------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------------
"""One log line per tool call, on the standard ``qai.ai_coding.tools`` logger.

Why this exists
---------------
When a tool misbehaves, the facts that locate the fault are: WHICH tool ran,
with WHAT arguments, and what came back. A chat transcript does not carry them —
the UI renders a summary, not the wire arguments — so reproducing a report means
guessing. Emitting them to the service log puts them where every other
diagnostic in this package already goes.

Level
-----
``INFO`` for a completed call, ``WARNING`` when the tool reported failure or
raised. Nothing here is a second logging channel: it is the same logger, the
same ``%s``-style lazy formatting, and the same handlers configured for the
service.

Never breaks the call it observes
---------------------------------
Every failure mode here (an unencodable argument, a broken ``__repr__``) is
swallowed. A diagnostic that can fail the operation it diagnoses is worse than
no diagnostic.

Secrets
-------
Values under argument names that look secret-bearing are redacted, and every
value is truncated, because ``write``/``edit`` payloads and ``exec`` command
lines pass through here verbatim.
"""

from __future__ import annotations

from typing import Any

from qai.ai_coding.infrastructure.tools.handlers._shared import logger

#: Per-value cap. A ``write`` body can be megabytes; the log line only needs
#: enough to identify what was passed.
_MAX_VALUE_CHARS = 300

#: Container summarisation caps — a log line identifies a call, it does not
#: reproduce its payload.
_MAX_ITEMS = 12
_MAX_SEQ_ITEMS = 6

#: Substrings that mark a value as secret-bearing. Matched case-insensitively
#: against the ARGUMENT NAME, so redaction never depends on recognising the
#: value's shape.
_SECRET_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "credential",
    "private_key",
    "authorization",
)

#: Result fields worth logging: the ones that say whether a call did what it
#: claimed, and the counters that catch a tool LYING about its effect.
_RESULT_FIELDS = (
    "error_code",
    "match_count",
    "file_count",
    "files_changed",
    "applied",
    "truncated",
    "incomplete",
    "backend",
)


def _clip(value: Any) -> Any:
    """Render one argument value compactly, never raising."""
    if isinstance(value, str):
        if len(value) > _MAX_VALUE_CHARS:
            return value[:_MAX_VALUE_CHARS] + f"…(+{len(value) - _MAX_VALUE_CHARS})"
        return value
    if isinstance(value, int | float | bool) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _clip(v) for k, v in list(value.items())[:_MAX_ITEMS]}
    if isinstance(value, list | tuple):
        head = [_clip(v) for v in list(value)[:_MAX_SEQ_ITEMS]]
        if len(value) > _MAX_SEQ_ITEMS:
            head.append(f"…(+{len(value) - _MAX_SEQ_ITEMS} more)")
        return head
    try:
        return _clip(repr(value))
    except Exception:  # noqa: BLE001 — an observer never raises
        return "<unrepresentable>"


def _render_args(args: Any) -> str:
    """``k=v`` pairs, secret-bearing names redacted."""
    if not isinstance(args, dict):
        return ""
    parts: list[str] = []
    for key, value in args.items():
        lowered = str(key).lower()
        if any(hint in lowered for hint in _SECRET_KEY_HINTS):
            parts.append(f"{key}=<redacted>")
        else:
            parts.append(f"{key}={_clip(value)!r}")
    return " ".join(parts)


def _render_result(result: Any) -> tuple[str, bool]:
    """Return ``(summary, failed)`` for a tool result."""
    if not isinstance(result, dict):
        return f"<non-dict {type(result).__name__}>", False
    ok = result.get("ok")
    parts = [f"ok={ok}"]
    parts.extend(
        f"{field}={result[field]!r}"
        for field in _RESULT_FIELDS
        if field in result
    )
    message = result.get("message")
    if isinstance(message, str) and message:
        parts.append(f"message={_clip(message)!r}")
    return " ".join(parts), ok is False


def record(
    *,
    tool: str,
    args: dict[str, Any],
    result: Any = None,
    error: BaseException | None = None,
    duration_ms: float | None = None,
) -> None:
    """Log one tool call. Never raises."""
    try:
        took = f" {duration_ms:.0f}ms" if duration_ms is not None else ""
        rendered_args = _render_args(args)
        if error is not None:
            logger.warning(
                "tool %s RAISED%s | %s | %s: %s",
                tool,
                took,
                rendered_args,
                type(error).__name__,
                error,
            )
            return
        summary, failed = _render_result(result)
        emit = logger.warning if failed else logger.info
        emit("tool %s%s | %s | %s", tool, took, rendered_args, summary)
    except Exception:  # noqa: BLE001 — logging NEVER breaks the logged call
        return

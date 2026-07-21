# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Access-denied error/hint message builders for AI tools (V1 parity).

Ported 1:1 from V1's ``backend/security/access_error_helper.py`` (107
lines). These messages are designed to be parsed by the AI model so it
can give the user clear, actionable guidance on what was blocked and how
to fix it (which Settings panel / whitelist to edit).

Pure helpers (no I/O, no framework imports) — they only assemble
strings — so they belong in the shared :mod:`qai.platform` kernel where
both the ``tools`` exec path and the ``security`` file-guard / sandbox
paths can consume them without crossing a context boundary.

This module is **byte-for-byte aligned** with the V1 Chinese UI copy so
the operator-facing guidance the model relays is unchanged; the
encoding is UTF-8 (no BOM) per AGENTS.md §3.10.

Wiring note (U-019 scope)
-------------------------
U-019 only introduces this module + its unit tests. The actual call
sites (exec exit diagnostics / file-guard blocked-access path) are wired
in a separate change to avoid touching files outside this work item's
file domain.
"""

from __future__ import annotations

__all__ = [
    "UI_HINT_FILEGUARD",
    "UI_HINT_SANDBOX_PATHS",
    "UI_HINT_SKILL",
    "build_blocked_error",
]


# UI navigation hints — where the user goes to fix each kind of issue.
# (V1 ``access_error_helper.py:12-21``.)
UI_HINT_FILEGUARD = (
    "User can add the path to the matching allow list under "
    "[Security -> Allow Lists] (read_allow / write_allow / exec_allow_cwd)"
)
UI_HINT_SANDBOX_PATHS = (
    "User can grant the path via the permission dialog "
    "([Allow permanently / Allow for this session])"
)
UI_HINT_SKILL = (
    "If the path is a SKILL resource, declare the permission in that SKILL's skill.policy.json"
)


def build_blocked_error(
    *,
    operation: str,  # "read" | "write" | "exec" | "delete" | etc.
    path: str,
    reason: str,  # Why it was blocked (one-line)
    blocked_by: str,  # "FileGuard" | "User Denial" | "Protected Paths" | "Policy Center"
    ui_hint: str = UI_HINT_FILEGUARD,
    extra: str = "",  # Additional context
) -> str:
    """Build a ``[tool_error]`` message that the AI can parse for guidance.

    Ported from V1 ``access_error_helper.py:24-52``; the assembled
    layout (field order, indentation, trailing guidance line) is
    preserved verbatim so the model parses it identically.
    """
    parts = [
        "[tool_error] Access Blocked",
        "",
        f"  Operation: {operation}",
        f"  Path: {path}",
        f"  Blocked by: {blocked_by}",
        f"  Reason: {reason}",
        "",
        "  Resolution:",
        f"    {ui_hint}",
    ]
    if extra:
        parts.append("")
        parts.append(f"  Additional info: {extra}")
    parts.append("")
    parts.append(
        "  Relay the path and resolution above to the user so they can fix the access restriction."
    )
    return "\n".join(parts)

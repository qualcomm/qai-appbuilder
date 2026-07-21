# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Diagnostic message builder for native FileGuard denial audit entries.

When an ``exec`` / background-process tool spawns a subprocess and the
native ``guard64.dll`` hook blocks one or more file syscalls issued by
that subprocess (or one of its descendants), the LLM sees only a generic
non-zero exit code + a filesystem-flavoured error string (e.g. ``mv:
cannot move ...: Permission denied``). Without context, LLMs will
routinely retry with alternate tools (``Copy-Item`` / ``robocopy`` /
``xcopy``), admin elevation, or path rewrites - none of which change the
outcome because the denial is enforced at the NTFS syscall interceptor,
not at the ACL layer.

This module renders a compact, authoritative diagnostic note that the
exec / background-process handlers prepend to their tool result so the
LLM sees the actual cause of the denial (FileGuard, not ACL) and the
correct remediation path (user must authorise via Settings, not the
model must find a bypass).

The catalog is intentionally scoped narrowly to native FileGuard
denials; other exec-deny reasons are covered by
:mod:`qai.security.domain.exec_deny_reason` and the generic access-
denied hint in :mod:`qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics`.

Pure domain: standard library only, no framework / cross-context
imports.
"""

from __future__ import annotations

from collections.abc import Sequence

from qai.security.domain.entities import AuditEntry

__all__ = [
    "build_native_guard_denial_note",
    "MAX_DETAIL_ROWS",
]


#: Maximum number of individual DENY rows rendered in the detail list.
#: Beyond this the note appends a ``(and N more, truncated)`` marker so
#: the message stays bounded even for runaway processes that trigger
#: hundreds of denials in a short window.
MAX_DETAIL_ROWS = 10


def build_native_guard_denial_note(
    denies: Sequence[AuditEntry],
) -> str:
    """Render a diagnostic note for native FileGuard DENY audit entries.

    Returns
    -------
    str
        A newline-prefixed diagnostic string ready to be concatenated onto
        an existing exec-diagnostics / background-process log tail. Empty
        string when ``denies`` is empty.

    The returned string always begins with ``"\\n\\n"`` so callers can
    unconditionally append it to whatever output they are assembling
    (matches the shape of ``exec_diagnostics._build_access_denied_hint``
    output). If ``denies`` is empty the return value is exactly ``""``,
    which is also safe to append.

    Notes
    -----
    Callers should have already filtered the audit rows to native
    FileGuard denies (i.e. ``subject.kind == "system"`` AND
    ``subject.identifier == "native.file_guard"`` AND
    ``decision == PolicyAction.DENY``). This function does not re-
    validate - it renders whatever it is given. In practice the source is
    :meth:`AuditQueryPort.query_native_denies_by_pid_tree` which enforces
    the identification key at the SQL layer.

    Language
    --------
    English. LLM training corpora are overwhelmingly English so the
    "DO NOT bypass" language has the strongest steering effect on the
    model's next action selection.
    """

    if not denies:
        return ""

    count = len(denies)
    plural = "" if count == 1 else "s"

    lines: list[str] = [
        "",
        "",
        "[FileGuard] This subprocess was blocked by the native FileGuard hook",
        f"during execution. {count} operation{plural} denied:",
    ]

    for entry in denies[:MAX_DETAIL_ROWS]:
        op = (entry.op or "").strip()
        path = entry.resource.identifier
        if op:
            lines.append(f"  - {op}: {path}")
        else:
            lines.append(f"  - (unlabelled): {path}")

    if count > MAX_DETAIL_ROWS:
        extra = count - MAX_DETAIL_ROWS
        lines.append(f"  (and {extra} more, truncated)")

    lines.extend(
        [
            "",
            "This is an ENFORCED security policy denial (guard64.dll intercepted",
            "the NTFS syscall), NOT a filesystem ACL / read-only / share-violation",
            "issue. DO NOT attempt to bypass it - retrying with alternate tools",
            "(Copy-Item, robocopy, xcopy, cmd copy), alternate paths (short 8.3",
            "name, symlinks, UNC prefix), admin elevation, or splitting the command",
            "will all be blocked identically. If access is truly required, ask the",
            "user to authorize the target path(s) in Settings -> Security -> Allow Lists.",
        ]
    )

    return "\n".join(lines)

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Exec-deny reason codes + user-facing guidance catalog (security domain).

When the exec gate denies a command, the user (and the LLM) must be told
*why* and *which Security UI panel* to visit to authorise it. V1
(``backend/tools/_security.py:134-196``) built these messages inline in the
tool layer; V2 previously kept them hard-coded in the apps-layer FileGuard
bridge (``_build_exec_error``), and the PolicyCenter decision carried no
reason at all (a dead ``getattr(result, "reason", "")`` read).

This module is the single source of truth for the exec-deny vocabulary:

* :class:`ExecDenyReason` — the stable reason-code enum (matches the V1
  exec-gate vocabulary: deny-pattern / cwd-outside-allow / binary-untrusted /
  policy-center outcomes).
* :func:`classify_exec_deny_reason` — map a free-text ``reason`` string
  (as produced by the exec broker / PolicyCenter) to a stable
  :class:`ExecDenyReason` code.
* :func:`exec_deny_message` — render the user-facing guidance for a code,
  interpolating the (already-extracted) command / executable display tokens.

Pure domain: standard library only, no framework / cross-context imports.
The apps-layer bridge remains responsible for *extracting* the command's
executable token (a shell-parsing concern) and passing it in; this module
owns only the code vocabulary + message catalog.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "ExecDenyReason",
    "classify_exec_deny_reason",
    "exec_deny_message",
]


class ExecDenyReason(str, Enum):
    """Stable exec-deny reason codes (V1 exec-gate vocabulary parity)."""

    #: Command matched an explicit dangerous-command deny rule
    #: (e.g. ``rm -rf`` / ``del /s`` / dangerous PowerShell).
    MATCHED_DENY_PATTERN = "matched_deny_pattern"
    #: The command's working directory is outside the allowed exec dirs.
    CWD_OUTSIDE_EXEC_ALLOW = "cwd_outside_exec_allow"
    #: Executable is neither trusted nor inside the workspace (dynamic-auth off).
    BINARY_NOT_TRUSTED_DYNAMIC = "binary_not_trusted_or_workspace_dynamic"
    #: Executable is neither trusted nor inside the workspace (static list).
    BINARY_NOT_TRUSTED_STATIC = "binary_not_trusted_or_workspace_static"
    #: PolicyCenter unavailable / evaluation error (fail-closed).
    POLICY_CENTER_UNAVAILABLE = "policy_center_unavailable"
    #: Generic PolicyCenter deny (no granular reason exposed) — the most
    #: actionable default (Allow Lists / Skill Capabilities).
    POLICY_CENTER_DENY = "policy_center_deny"


def classify_exec_deny_reason(reason: str | None) -> ExecDenyReason:
    """Map a free-text ``reason`` code to a stable :class:`ExecDenyReason`.

    The exec broker / PolicyCenter produce free-text reason strings; this
    normalises the known prefixes to a code. Anything unrecognised (including
    an empty / missing reason — the previous dead-read case) maps to
    :attr:`ExecDenyReason.POLICY_CENTER_DENY`, the generic actionable default.
    """
    r = (reason or "").strip()
    if r.startswith(ExecDenyReason.MATCHED_DENY_PATTERN.value):
        return ExecDenyReason.MATCHED_DENY_PATTERN
    if r == ExecDenyReason.CWD_OUTSIDE_EXEC_ALLOW.value:
        return ExecDenyReason.CWD_OUTSIDE_EXEC_ALLOW
    if r == ExecDenyReason.BINARY_NOT_TRUSTED_DYNAMIC.value:
        return ExecDenyReason.BINARY_NOT_TRUSTED_DYNAMIC
    if r == ExecDenyReason.BINARY_NOT_TRUSTED_STATIC.value:
        return ExecDenyReason.BINARY_NOT_TRUSTED_STATIC
    if r == ExecDenyReason.POLICY_CENTER_UNAVAILABLE.value:
        return ExecDenyReason.POLICY_CENTER_UNAVAILABLE
    return ExecDenyReason.POLICY_CENTER_DENY


def exec_deny_message(
    reason: ExecDenyReason | str | None,
    *,
    command_display: str,
    exe_display: str,
) -> str:
    """Render the user-facing exec-deny guidance for ``reason``.

    ``command_display`` is the (already truncated) command string and
    ``exe_display`` the extracted executable basename — both prepared by the
    caller (the apps bridge owns the shell-parsing). The message names the
    exact Security UI panel the operator must visit (V1
    ``_build_exec_error`` parity).
    """
    code = (
        reason
        if isinstance(reason, ExecDenyReason)
        else classify_exec_deny_reason(reason)
    )

    if code is ExecDenyReason.MATCHED_DENY_PATTERN:
        return (
            f"FileGuard denied this command: {command_display}\n\n"
            "Reason: it matches an explicit dangerous-command deny rule "
            "(e.g. rm -rf, del /s, dangerous PowerShell commands) and was "
            "blocked for safety.\n"
            "If truly needed, remove the dangerous arguments and retry, or "
            "ask an administrator to adjust the deny rules."
        )
    if code is ExecDenyReason.CWD_OUTSIDE_EXEC_ALLOW:
        return (
            f"FileGuard denied this command: {command_display}\n\n"
            "Reason: the command's working directory is outside the allowed "
            "exec directories.\n\n"
            "To allow it, add the working directory under "
            "Security → Allow Lists → exec_allow_cwd."
        )
    if code in (
        ExecDenyReason.BINARY_NOT_TRUSTED_DYNAMIC,
        ExecDenyReason.BINARY_NOT_TRUSTED_STATIC,
    ):
        return (
            f"FileGuard denied this command's executable: {exe_display}\n\n"
            "Reason: the executable is neither in the trusted-binary list nor "
            "inside the current workspace; FileGuard does not allow the AI to "
            "run untrusted binaries outside the workspace by default.\n\n"
            "To allow it:\n"
            "  · add the executable under "
            "Security → Skill Capabilities → trusted_binaries; or\n"
            "  · enable Dynamic Authorization under Security → Overview to "
            "authorize this run temporarily."
        )
    # POLICY_CENTER_UNAVAILABLE + POLICY_CENTER_DENY share the generic
    # actionable guidance (V1 default branch).
    return (
        f"FileGuard denied this command: {command_display}\n\n"
        "Reason: the security policy center has not granted execute "
        "permission for this command.\n\n"
        "Grant permission under Security → Allow Lists / Skill Capabilities, "
        "or ask an administrator to adjust the security policy."
    )

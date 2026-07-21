# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""OS hint builder for ai_coding system prompts (PR-095 / S9 H-11).

The legacy ``backend/ai_coding/session_manager.py`` (lines 48-92)
prepended an *OS context* block to every Claude / OpenCode session
system prompt so the agent knew it was running on Windows under
Git Bash (MSYS) and translated paths accordingly (``C:\\foo`` →
``/c/foo``).  Without this hint the agent emits PowerShell / cmd.exe
syntax and Windows-style paths into shell tools, both of which fail
inside the MSYS bash that the tool registry uses.

This module restores parity with two pure-Python helpers:

* :func:`_to_gitbash_path` — drive-letter → MSYS path conversion;
  no-op on POSIX.
* :func:`build_os_hint` — returns the system-prompt addendum string.
  Caller (the spawn-session adapter / CC provider) prepends the
  return value to whatever system prompt it ships upstream.

Audit: ``docs/90-refactor/S9-final-parity-audit.md`` §2.2 H-11.
"""

from __future__ import annotations

import sys

__all__ = ["build_os_hint", "_to_gitbash_path"]


def _to_gitbash_path(path: str) -> str:
    """Translate a Windows path into the MSYS / Git Bash equivalent.

    Examples::

        C:\\Users\\me\\proj  → /c/Users/me/proj
        D:/repos/foo         → /d/repos/foo
        /already/posix       → /already/posix      (unchanged)

    Behaviour on POSIX hosts: returns the input unchanged so callers
    can use the helper unconditionally.

    The translation matches what ``cygpath -u`` produces under MSYS
    so it round-trips cleanly with the tooling that already runs
    inside the agent's Git Bash subshell.
    """
    if not path:
        return path
    if sys.platform != "win32":
        return path
    # Normalise back-slashes to forward-slashes first so the rest of
    # the function only deals with one separator style.
    p = path.replace("\\", "/")
    # Drive-letter prefix? ``X:/...`` or ``X:`` (no trailing slash).
    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        drive = p[0].lower()
        rest = p[2:]
        if rest.startswith("/"):
            return f"/{drive}{rest}"
        return f"/{drive}/{rest}"
    return p


def build_os_hint() -> str:
    """Return the OS-context addendum to prepend to a system prompt.

    Windows hosts get a Git-Bash / MSYS-aware block; POSIX hosts
    receive a generic block.  Both forms are deterministic so unit
    tests can pin them.

    The hint is intentionally short — long context is expensive — but
    explicit enough that the upstream LLM stops emitting PowerShell
    one-liners and ``C:\\Users\\…`` style paths in shell tool calls.
    """
    if sys.platform == "win32":
        return (
            "OS Context:\n"
            "- Platform: Windows\n"
            "- Shell: Git Bash via MSYS (bash, not PowerShell or cmd.exe)\n"
            "- Path translation: Windows drive-letter paths must be "
            "converted to MSYS form before use in shell tools "
            "(e.g. C:\\Users\\foo -> /c/Users/foo, D:/repos -> /d/repos).\n"
            "- Use forward slashes inside shell commands; keep "
            "back-slashes only when reading/writing file content.\n"
            "- Line endings: prefer LF (\\n) for source files; the "
            "tooling normalises CRLF on save when needed.\n"
        )
    # POSIX (Linux / macOS / WSL) — agents already default to bash
    # and forward-slash paths so the hint is short.
    return (
        "OS Context:\n"
        f"- Platform: {sys.platform}\n"
        "- Shell: POSIX bash / sh\n"
        "- Path translation: not required — paths are already POSIX.\n"
        "- Line endings: LF (\\n).\n"
    )

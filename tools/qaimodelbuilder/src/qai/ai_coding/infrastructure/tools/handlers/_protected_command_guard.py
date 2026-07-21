# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Lightweight shell-level pre-check for protected-path writes.

Design rationale
----------------
The primary protection against writes into protected directories (e.g.
``C:\Qualcomm``) is the **native OS-level hook** (``guard64.dll``) which
intercepts ``NtCreateFile`` / ``NtOpenFile`` at the kernel boundary.  That
hook fires for *every* process and *every* program — ``copy.exe``, ``xcopy``,
``robocopy``, custom binaries, Python scripts — regardless of how the write
was initiated.  It does not need to understand command syntax.

This module is a **thin, shell-level pre-spawn check** that catches only the
constructs where the write target is visible in the command string *before*
the process is even spawned:

1. **Shell redirections** (``> path`` / ``>> path`` / ``N> path``): the shell
   itself opens the destination file for writing *before* the command runs, so
   the native hook on the child process would see the write too late (the
   shell's own ``cmd.exe`` / ``pwsh.exe`` process does the open).  Catching
   these here avoids a race.

2. **PowerShell explicit ``-Destination`` / ``-FilePath`` flags**: same
   rationale — the destination is unambiguous in the command string.

Everything else — ``copy``, ``xcopy``, ``move``, ``del``, custom programs,
Python ``shutil``, etc. — is handled exclusively by the native hook and the
``sitecustomize`` child-process audit hook.  Attempting to parse arbitrary
command verbs here leads to false positives (e.g. flagging the *source* of
``copy SDK_dir workspace_dir``) and a never-ending arms race against new
commands.  The native hook is the correct and complete backstop.

Returns ``None`` when no protected write target is detected (allow), else a
deny reason string naming the offending target.
"""

from __future__ import annotations

import re

from qai.platform import protected_paths

__all__ = ["protected_command_sentinel"]

# A path token: quoted ("..."/'...') or a bare absolute Windows / UNC path.
_QUOTED = r'"([^"]+)"|\'([^\']+)\''
_BARE = r"([A-Za-z]:\\[^\s|&<>]+|\\\\[^\s|&<>]+)"
_PATH = rf"(?:{_QUOTED}|{_BARE})"

# Shell redirection: ``> path`` / ``>> path`` / ``N> path``.
# The shell opens the destination *before* the child process runs, so the
# native hook on the child would see it too late.  Catch it here.
_REDIRECT_RE = re.compile(rf"\d*>>?\s*{_PATH}")

# PowerShell explicit destination flag: ``-Destination <path>`` etc.
# The destination is unambiguous in the command string.
_DEST_FLAG_RE = re.compile(
    rf"(?i)-(?:Destination|FilePath|LiteralPath|Path)\s+{_PATH}"
)


def _iter_matches(pattern: re.Pattern[str], text: str):
    for m in pattern.finditer(text):
        # groups: (quoted-dq, quoted-sq, bare)
        for g in m.groups():
            if g:
                yield g
                break


def protected_command_sentinel(command: str) -> str | None:
    """Return a deny reason if *command* contains a shell-level protected write.

    Only shell redirections and explicit PowerShell ``-Destination`` flags are
    checked here.  All other write operations (copy, move, del, custom programs,
    etc.) are handled by the native OS hook (``guard64.dll``) which intercepts
    file-system calls regardless of the program or command used.

    Returns ``None`` when no protected write target is detected (allow).
    """
    if not command or not isinstance(command, str):
        return None

    candidates: list[str] = []

    # 1) Shell redirections — shell opens the file before the child runs.
    candidates.extend(_iter_matches(_REDIRECT_RE, command))

    # 2) PowerShell explicit destination flags — destination is unambiguous.
    candidates.extend(_iter_matches(_DEST_FLAG_RE, command))

    for target in candidates:
        matched = protected_paths.is_write_blocked(target)
        if matched:
            return protected_paths.deny_message(target, matched)
    return None

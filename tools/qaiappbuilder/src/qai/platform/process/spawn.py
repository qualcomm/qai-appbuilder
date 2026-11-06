# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Spawn an ``argv`` without letting Windows mangle a ``cmd /c`` command's quotes.

Problem
-------
The ``exec`` tool turns a cmd.exe command into ``["cmd", "/c", command]`` and hands
that list to ``asyncio.create_subprocess_exec``. On Windows, Python flattens the list
into one command line with ``subprocess.list2cmdline``, which escapes every ``"``
inside an element as ``\"``. cmd.exe does NOT understand ``\"``, so any command that
quotes a path is corrupted::

    dir /b "C:\Program Files\Qualcomm\QUTS"                  -> "syntax is incorrect"
    if exist "C:\Program Files\Qualcomm" (echo yes) else ...  -> prints "no"  (wrong)
    echo "a b"                                                -> prints  \"a b\"

The wrong answers are silent (a false "not installed"), which is the worst failure
mode for an agent that trusts the tool output.

Fix
---
When the argv is exactly ``[cmd, /c, <command>]`` and ``<command>`` contains a double
quote, spawn it with ``create_subprocess_shell(<command>)``: Python builds
``cmd.exe /c "<command>"`` and cmd strips the outer quotes, leaving the command
untouched. Everything else (no quote, a ``.bat`` path, PowerShell, sh, other
programs) still goes through ``create_subprocess_exec`` and behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import ntpath
import sys
from collections.abc import Sequence
from typing import Any

__all__ = ["cmd_raw_command", "create_subprocess_for_argv"]


def cmd_raw_command(argv: Sequence[str]) -> str | None:
    """Return the command string when ``argv`` is a quote-bearing ``cmd /c`` call, else ``None``."""
    if sys.platform != "win32" or len(argv) != 3:
        return None
    program, flag, command = argv
    if ntpath.basename(str(program)).lower() not in ("cmd", "cmd.exe"):
        return None
    if str(flag).lower() != "/c":
        return None
    if not isinstance(command, str) or '"' not in command:
        return None
    return command


async def create_subprocess_for_argv(argv: Sequence[str], **kwargs: Any) -> asyncio.subprocess.Process:
    """``create_subprocess_exec(*argv)``, except a quote-bearing ``cmd /c`` goes through the shell form."""
    raw = cmd_raw_command(argv)
    if raw is not None:
        return await asyncio.create_subprocess_shell(raw, **kwargs)
    return await asyncio.create_subprocess_exec(*argv, **kwargs)

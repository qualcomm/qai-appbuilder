# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Locate a usable native ``claude`` CLI executable for the SDK provider.

V1 parity (``backend/ai_coding/session_manager.py:481-496,1863-1865``)
---------------------------------------------------------------------
The ``claude_agent_sdk`` ``ClaudeSDKClient`` spawns the Claude Code CLI
as a child process and talks the ``--input-format stream-json`` control
protocol over its stdin/stdout pipes.  Two long-standing Windows pitfalls
the legacy ``ClaudeCodeSessionManager`` already documented bite the SDK
path identically:

* **x64 bundled CLI crashes on Windows ARM64** — the SDK ships a bundled
  ``claude.exe`` that is an x64 binary; running it on an ARM64 host
  segfaults with ``exit code: 3221225477`` (``0xC0000005`` access
  violation).  The fix is to point ``cli_path`` at the system-installed
  *native ARM64* ``claude.exe`` (V1 ``session_manager.py:481-496``).
* **``.cmd`` shims break the pipe handshake** — ``shutil.which("claude")``
  on Windows finds the npm ``claude.cmd`` batch shim first;
  ``asyncio.create_subprocess_exec`` cannot establish the stdin/stdout
  pipes through a ``.cmd`` so the SDK ``initialize`` control request times
  out (V1 ``session_manager.py:1863-1865``).  We must resolve the sibling
  ``.exe`` instead.

This module is platform-neutral (AGENTS.md cross-platform constraint): it
uses ``shutil.which`` + ``sys.platform`` branches and never imports a
Windows-only runtime dependency.  On a non-Windows host it simply returns
whatever ``shutil.which("claude")`` finds (no ``.cmd`` rewrite needed).

The locator is intentionally infrastructure-only (no domain / application
import) and pure (no global state) so it is trivially unit-testable.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

__all__ = [
    "locate_claude_cli",
    "normalise_cli_path",
]

# Well-known install locations of the *native* Claude Code CLI on Windows.
# The npm global install drops ``claude.exe`` (native arch) +
# ``claude.cmd`` (shim) under ``%APPDATA%\npm\node_modules\@anthropic-ai\
# claude-code\bin``.  We probe the real ``.exe`` there first so an ARM64
# host gets the ARM64 binary (avoiding the x64 bundled-CLI crash) without
# the operator having to set ``cli_path`` manually.  Each entry is expanded
# against the current environment at call time.
_WINDOWS_NPM_CLAUDE_EXE_CANDIDATES: tuple[str, ...] = (
    r"%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe",
    r"%APPDATA%\npm\claude.exe",
    r"%ProgramFiles%\nodejs\node_modules\@anthropic-ai\claude-code\bin\claude.exe",
)


def normalise_cli_path(cli_path: str | None) -> str | None:
    """Return a pipe-safe absolute CLI path, rewriting ``.cmd`` → ``.exe``.

    V1 parity (``session_manager.py:1863-1865``): a ``.cmd`` shim cannot
    carry the SDK's stdin/stdout control-protocol pipes, so when the
    operator-configured path (or a ``which`` hit) lands on a ``.cmd`` we
    look for the sibling ``.exe`` and prefer it.  Returns the input
    unchanged when it is already an ``.exe`` (or non-Windows), and
    ``None`` when the input is empty / blank so callers can fall back to
    the SDK's own auto-discovery.
    """
    if cli_path is None:
        return None
    candidate = cli_path.strip()
    if not candidate:
        return None
    expanded = os.path.expandvars(candidate)
    p = Path(expanded)
    # Rewrite a ``.cmd`` / ``.bat`` shim to the sibling native ``.exe``.
    if p.suffix.lower() in {".cmd", ".bat"}:
        sibling = p.with_suffix(".exe")
        if sibling.exists():
            return str(sibling)
        # No sibling exe on disk — return the original so the caller can
        # still surface a clear "cli not usable" error rather than us
        # silently swallowing the configured value.
        return str(p)
    return str(p)


def locate_claude_cli(configured: str | None = None) -> str | None:
    """Resolve a usable native ``claude`` CLI path (V1-parity locator).

    Resolution order (high → low):

    1. ``configured`` — the operator's ``cli_path`` config value, with a
       ``.cmd`` → ``.exe`` rewrite applied (:func:`normalise_cli_path`).
       Returned only when it points at a file that exists on disk.
    2. On Windows: the well-known npm native-CLI install locations
       (:data:`_WINDOWS_NPM_CLAUDE_EXE_CANDIDATES`) — these are the real
       ``claude.exe`` (native arch) so an ARM64 host avoids the x64
       bundled-CLI crash.
    3. ``shutil.which("claude")`` — with a ``.cmd`` → ``.exe`` rewrite so
       the pipe handshake works.
    4. ``None`` — let the SDK fall back to its own bundled-CLI discovery
       (the caller surfaces a clear "install Claude Code CLI" error when
       the spawn then fails on an ARM64 host).

    Pure + best-effort: never raises (a probe failure simply falls through
    to the next source).
    """
    # 1) Operator-configured path wins when it resolves to a real file.
    normalised = normalise_cli_path(configured)
    if normalised:
        try:
            if Path(normalised).exists():
                return normalised
        except OSError:
            pass

    # 2) Windows: probe the well-known npm native-CLI install locations.
    if sys.platform == "win32":
        for raw in _WINDOWS_NPM_CLAUDE_EXE_CANDIDATES:
            expanded = os.path.expandvars(raw)
            try:
                if Path(expanded).exists():
                    return expanded
            except OSError:
                continue

    # 3) PATH lookup, rewriting a ``.cmd`` shim to the sibling ``.exe``.
    which_hit = shutil.which("claude")
    if which_hit:
        rewritten = normalise_cli_path(which_hit)
        if rewritten:
            try:
                if Path(rewritten).exists():
                    return rewritten
            except OSError:
                return rewritten

    # 4) Nothing found — let the SDK try its own discovery.
    return None

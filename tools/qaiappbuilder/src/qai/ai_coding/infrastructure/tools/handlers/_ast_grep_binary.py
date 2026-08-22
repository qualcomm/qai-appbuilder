# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Shared ``ast-grep`` executable discovery for the AST tool handlers.

Both AST tools shell out to the SAME binary — ``ast_grep`` (structural search,
:mod:`..handlers.ast_search`) and ``ast_edit`` (structural rewrite) — so the
"where is it, and what do we say when it is missing" decision lives here once.
Two copies of this would drift, and the arch-selection half is exactly the code
that must not.

Resolution order:

1. ``ast-grep`` on ``PATH`` — an operator who installed their own CLI keeps
   control (and gets a newer version than the one we vendor).
2. On Windows only, ``sg`` on ``PATH`` — the CLI's short alias, shipped in the
   same archive. Not consulted on POSIX, where ``sg`` is a long-standing system
   utility (run a command under a different group) and would execute something
   entirely unrelated.
3. ``<repo_root>/vendor/bin/<arch>/ast-grep.exe`` — the copy this repo bundles,
   which is why the tools work on a machine that has never heard of ast-grep.
   Same layout as ``guard64.dll`` (``vendor/bin/{arm64,x64}/``).

Nothing here downloads anything. When no executable is found the caller raises
:class:`ToolError` built from :func:`missing_binary_message` — never a
successful-looking empty result, because "the binary is missing" and "the code
has no such construct" are opposite facts.

Deliberately UNCACHED: every lookup re-reads ``PATH`` and the filesystem. The
binary can appear mid-session (an operator installs it while the daemon runs),
a cached ``None`` would keep the tools dead until a restart, and tests need to
monkeypatch the probe freely. The cost is one ``which`` plus at most two
``is_file`` calls per tool invocation, against a subprocess that follows.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

#: Actionable install guidance, embedded in the missing-binary error so the
#: model can tell the user what to do instead of just "tool unavailable".
INSTALL_HINT = (
    "Normally this repo's bundled copy at vendor/bin/<arch>/ast-grep.exe is "
    "used and no install is needed; if that file is missing, restore it or "
    "install the CLI with one of: `npm i -g @ast-grep/cli`, "
    "`cargo install ast-grep --locked`, `pip install ast-grep-cli`, or grab "
    "the standalone binary from "
    "https://github.com/ast-grep/ast-grep/releases (Windows arm64: "
    "app-aarch64-pc-windows-msvc.zip; Windows x64: "
    "app-x86_64-pc-windows-msvc.zip) and put ast-grep.exe on PATH."
)

#: What to fall back to per tool when the binary is absent. ``grep`` finds the
#: same text (with string/comment false positives); ``edit`` makes the same
#: change (with an exact-text anchor instead of a syntax pattern).
_FALLBACK_ADVICE: dict[str, str] = {
    "ast_grep": (
        "Until then, use the 'grep' tool instead (regex/text search — it also "
        "matches occurrences inside strings and comments, so check each hit)."
    ),
    "ast_edit": (
        "Until then, use the 'edit' tool instead (exact-text replacement — "
        "locate the sites with 'grep' first and change them one by one)."
    ),
}


def pe_machine(path: Path) -> int | None:
    """Return the PE ``Machine`` value of a Windows executable, or ``None``.

    Same reader as ``exec.py``'s PortableGit arch check (COFF machine field at
    ``e_lfanew + 4``): spawning a binary the current process cannot load fails
    with ``0xC000007B`` (STATUS_INVALID_IMAGE_FORMAT) rather than a clean
    error, so the arch is verified from the file header BEFORE the spawn. Any
    read failure returns ``None`` (no arch check applied).

    PE machine constants: ``0x8664`` = x86-64, ``0xAA64`` = ARM64.
    """
    try:
        with path.open("rb") as fh:
            if fh.read(2) != b"MZ":
                return None
            fh.seek(0x3C)
            e_lfanew = int.from_bytes(fh.read(4), "little")
            fh.seek(e_lfanew)
            if fh.read(4) != b"PE\x00\x00":
                return None
            return int.from_bytes(fh.read(2), "little")
    except OSError:
        return None


def current_pe_machine() -> int | None:
    """PE ``Machine`` value matching the architecture of THIS process.

    Keyed off ``PROCESSOR_ARCHITECTURE`` for the same reason
    :func:`qai.security.adapters.native_file_guard.current_guard_arch` is: on
    Windows-on-ARM ``platform.machine()`` reports the HOST (``ARM64``) even for
    an x64 ``python.exe`` running under emulation, and such an interpreter
    should pick the x64 artefact. The env var reflects the PROCESS (``AMD64``
    when emulated), which is what actually determines loadability. ``None``
    means "unrecognised arch" and disables the preference.
    """
    proc = (os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    if not proc:
        proc = (platform.machine() or "").upper()
    if proc in ("ARM64", "AARCH64"):
        return 0xAA64
    if proc in ("AMD64", "X86_64", "EM64T"):
        return 0x8664
    return None


def _repo_root() -> Path:
    """Repo root inferred from this file's location.

    This module sits at ``src/qai/ai_coding/infrastructure/tools/handlers/`` —
    six levels below the root that holds ``vendor/``. Derived structurally (the
    ``qai.platform.protected_paths`` convention) so no absolute path or CWD
    assumption creeps in: the tools' CWD is the user's WORKSPACE, which is
    generally NOT this repo.
    """
    return Path(__file__).resolve().parents[6]


def vendor_candidates() -> list[Path]:
    """Bundled ``ast-grep`` executables, arch-matching ones first.

    ``Setup.bat`` installs only the HOST arch (``vendor/bin/arm64`` OR
    ``vendor/bin/x64``), so normally exactly one exists; both are probed
    because a machine may carry a copy from an earlier dual-arch install. The
    arch-matching one is preferred; a non-matching one is still returned as a
    last resort, because Windows-on-ARM CAN run an x64 image under emulation
    and refusing outright would break a setup that works.
    """
    root = _repo_root() / "vendor" / "bin"
    if sys.platform != "win32":
        # ``pe_machine`` reads a PE header, so on ELF/Mach-O it returns None for
        # EVERY candidate while ``current_pe_machine`` still reports a real
        # arch. The sort key would then be True for all of them and stable sort
        # would just keep directory order — silently preferring ``arm64`` on an
        # x64 host. With no way to check the arch, return them unranked rather
        # than pretend the first one matches.
        return [
            candidate
            for arch in ("arm64", "x64")
            if (candidate := root / arch / "ast-grep").is_file()
        ]
    existing = [
        candidate
        for arch in ("arm64", "x64")
        if (candidate := root / arch / "ast-grep.exe").is_file()
    ]
    want = current_pe_machine()
    if want is None:
        return existing
    return sorted(existing, key=lambda c: pe_machine(c) != want)


def resolve_ast_grep_binary() -> str | None:
    """Return a usable ``ast-grep`` executable path, or ``None`` if none exists.

    See the module docstring for the resolution order. ``None`` is the caller's
    cue to raise :class:`ToolError` with :func:`missing_binary_message` — it
    must NEVER be turned into an empty-but-successful result.
    """
    found = shutil.which("ast-grep")
    if found:
        return found
    if sys.platform == "win32":
        found = shutil.which("sg")
        if found:
            return found
    # Best-ranked candidate only. A loop that returns on its first iteration
    # reads like a fallback chain but is not one, and there is nothing to fall
    # back to: an existing-but-unloadable image fails at spawn time, where the
    # error names the actual problem instead of being masked by a silent retry.
    return next((str(c) for c in vendor_candidates()), None)


def missing_binary_message(tool: str) -> str:
    """Build the "binary absent" error text for ``tool``.

    ``tool`` is the wire tool name (``"ast_grep"`` / ``"ast_edit"``); an
    unknown name still produces the core message, just without tool-specific
    fallback advice.

    Deliberately explicit that NOTHING ran: the dangerous failure mode is a
    model reading "0 matches" / "0 edits" as a fact about the code when in
    reality the search or rewrite never happened.
    """
    advice = _FALLBACK_ADVICE.get(tool, "")
    message = (
        f"{tool}: no 'ast-grep' executable was found — neither on PATH nor at "
        "vendor/bin/<arch>/ — so NO search or rewrite was performed and "
        "NOTHING was changed. This is NOT a 'no matches' / zero-result answer "
        "and says nothing about whether the pattern occurs in the code. "
        f"{INSTALL_HINT}"
    )
    return f"{message} {advice}" if advice else message

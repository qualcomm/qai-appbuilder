# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Protected write-paths — a small, ALWAYS-ON guard against destructive writes.

Why this module exists (incident 2026-06-16):
  The QAIRT SDK's ``qnn-context-binary-generator.exe`` was silently truncated
  to **0 bytes** by a stray write that an AI-agent command issued into the SDK
  install tree (``C:\Qualcomm\AIStack\QAIRT\...``).  The next time the model
  pipeline launched that ARM64 executable, Windows raised
  ``[WinError 193] %1 is not a valid Win32 application`` (and popped the GUI
  "This app can't run on your PC" dialog), breaking on-device model builds.

  The existing protections did NOT stop it:
    * ``FileGuard`` / ``PolicyCenter`` ships **disabled** by default.
    * the subprocess ``sitecustomize`` audit hook is only injected when the OS
      sandbox is enabled — also **off** by default.
    * the ``exec`` tool does **no** path-level inspection of a command's write
      targets (``>`` redirects / ``copy`` / ``del`` / ``Move-Item`` …).
    * there was **no** user-configurable "do not modify these paths" list.

Design (independent of FileGuard, ALWAYS enforced — AGENTS.md 🔴 State-Truth):
  This module is the single source of truth for "paths the agent must never
  write to / delete / truncate".  It is **not** gated by ``file_guard_enabled``
  or any other settings switch: the built-in entries are enforced
  unconditionally so that even with every optional security module off, the
  QAIRT SDK (and other declared paths) cannot be corrupted by the model.

  Two tiers:
    1. **Built-in, non-removable** prefixes (hard-coded below).  Users CANNOT
       disable or remove these.  Currently: ``C:\Qualcomm`` (the Qualcomm
       toolchain install root that contains the QAIRT SDK).
    2. **User-configured** extra prefixes (``set_user_protected_paths``), merged
       on top.  Users may ADD paths; they can never subtract a built-in one.

  Enforcement points (all call :func:`is_write_blocked` / :func:`deny_message`):
    * ``write`` / ``edit`` / ``apply_patch`` tool handlers (in-process writes).
    * ``exec`` tool — extracted write targets of the shell command.
    * Python child processes — the ``sitecustomize`` audit hook reads the
      ``QAI_PROTECTED_PATHS`` env var (seeded from :func:`env_value`).

Path normalization (anti-bypass, V1 parity ``policy.py:166``):
  Comparison is done on ``os.path.normcase(os.path.abspath(path))`` with a
  best-effort Windows 8.3 short-name expansion, and uses a path-component
  boundary (``prefix`` or ``prefix + os.sep``) so ``C:\QualcommEvil`` does NOT
  match the ``C:\Qualcomm`` prefix.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable

__all__ = [
    "BUILTIN_PROTECTED_PREFIXES",
    "deny_message",
    "env_value",
    "is_write_blocked",
    "protected_prefixes",
    "set_user_protected_paths",
]

# ---------------------------------------------------------------------------
# Tier 1: built-in, NON-REMOVABLE protected prefixes.
#
# These are enforced unconditionally.  ``C:\Qualcomm`` covers the entire
# Qualcomm toolchain install root (QAIRT SDK lives under
# ``C:\Qualcomm\AIStack\QAIRT\...``); per the 2026-06-16 incident the user
# chose to lock the whole ``C:\Qualcomm`` tree, not just the QAIRT subtree.
#
# NOTE: stored pre-normalized (normcase) so membership checks are cheap.
# ---------------------------------------------------------------------------
BUILTIN_PROTECTED_PREFIXES: tuple[str, ...] = (
    os.path.normcase(r"C:\Qualcomm"),
)

#: Tier 2 — user-configured extra prefixes (normalized).  Replaced wholesale by
#: :func:`set_user_protected_paths`; merged with the built-ins at query time.
_user_prefixes: tuple[str, ...] = ()


def _strip_extended_prefix(path: str) -> str:
    r"""Strip the Win32 extended-length / device prefix so it cannot be used to
    dodge a plain ``C:\...`` protected prefix.

    ``\\?\C:\Qualcomm\x`` and ``\\.\C:\Qualcomm\x`` both address the same file
    as ``C:\Qualcomm\x`` but would not match a normcase ``c:\qualcomm`` prefix.
    We remove the ``\\?\`` / ``\\.\`` (and the ``\\?\UNC\`` → ``\\``) prefixes
    so the comparison sees the canonical drive path. A genuine UNC path
    (``\\server\share``) is left as-is (it is not under a local drive prefix and
    a protected prefix could itself be a UNC if a user configures one).
    """
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[len("\\\\?\\UNC\\"):]
    if path.startswith("\\\\?\\") or path.startswith("\\\\.\\"):
        return path[4:]
    return path


def _resolve_final_path(abs_path: str) -> str:
    r"""Resolve symlinks / NTFS junctions to the real target path (Windows).

    A junction ``C:\evil -> C:\Qualcomm`` would let ``C:\evil\x`` write into the
    protected tree while dodging the ``C:\Qualcomm`` prefix. ``os.path.realpath``
    on modern CPython (3.8+) uses ``GetFinalPathNameByHandleW`` semantics and
    follows junctions/symlinks for paths that EXIST. For a not-yet-existing
    leaf, we resolve the longest existing ancestor and re-append the tail so a
    junction on an ancestor directory is still caught. Best-effort: any failure
    falls back to the input.
    """
    try:
        real = os.path.realpath(abs_path)
        if real:
            return real
    except (OSError, ValueError):
        pass
    return abs_path


def _normalize(path: str | bytes) -> str | None:
    r"""Return a normcase'd absolute path for comparison, or ``None``.

    Anti-bypass hardening (V1 ``policy._normalize_path`` parity):
      * accepts ``bytes`` paths (decoded) as well as ``str`` / ``os.PathLike``;
      * strips ``\\?\`` / ``\\.\`` extended-length / device prefixes;
      * expands Windows 8.3 short names (``PROGRA~1``);
      * resolves symlinks / NTFS junctions to the real target so a junction
        redirecting into the protected tree cannot dodge the prefix.

    Returns ``None`` for empty / unresolvable input.
    """
    if isinstance(path, bytes):
        try:
            path = path.decode("utf-8", "surrogateescape")
        except Exception:  # noqa: BLE001
            return None
    if not path or not isinstance(path, str):
        return None
    try:
        abs_path = os.path.abspath(path)
    except (OSError, ValueError):
        return None
    if sys.platform == "win32":
        abs_path = _strip_extended_prefix(abs_path)
        # Resolve symlinks / junctions FIRST (so an ancestor junction into the
        # protected tree is caught), then expand any 8.3 short-name segments.
        abs_path = _resolve_final_path(abs_path)
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(1024)
            # GetLongPathNameW expands 8.3 short names; no-ops on a path that
            # does not exist yet (returns 0 → we keep abs_path as-is).
            res = ctypes.windll.kernel32.GetLongPathNameW(  # type: ignore[attr-defined]
                abs_path, buf, 1024
            )
            if res and res < 1024:
                abs_path = buf.value
        except (OSError, AttributeError, ValueError):
            pass
        abs_path = _strip_extended_prefix(abs_path)
    return os.path.normcase(abs_path)


def _under_prefix(norm_path: str, prefix: str) -> bool:
    """True iff ``norm_path`` equals or lies under ``prefix`` (component-safe)."""
    return norm_path == prefix or norm_path.startswith(prefix + os.sep)


def set_user_protected_paths(paths: Iterable[str] | None) -> None:
    """Replace the user-configured extra protected prefixes.

    The built-in prefixes are NOT affected (they can never be removed). Invalid
    / empty entries are dropped. Idempotent.
    """
    global _user_prefixes
    if not paths:
        _user_prefixes = ()
        return
    normalized: list[str] = []
    for p in paths:
        n = _normalize(p)
        if n and n not in normalized:
            normalized.append(n)
    _user_prefixes = tuple(normalized)


def protected_prefixes() -> tuple[str, ...]:
    """Return the effective (built-in + user) normalized protected prefixes."""
    return BUILTIN_PROTECTED_PREFIXES + _user_prefixes


def is_write_blocked(path: str | bytes) -> str | None:
    """Return the matching protected prefix if writing ``path`` is forbidden.

    Accepts ``str`` or ``bytes`` paths. ``None`` means the write is allowed by
    this guard. The returned string is the matched protected prefix, suitable
    for inclusion in an error/audit message.
    """
    norm = _normalize(path)
    if norm is None:
        return None
    for prefix in protected_prefixes():
        if _under_prefix(norm, prefix):
            return prefix
    return None


def deny_message(path: str, matched_prefix: str | None = None) -> str:
    """Build the denial message returned to the model on a blocked write."""
    prefix = matched_prefix or is_write_blocked(path) or "protected directory"
    shown = str(path)[:160]
    return (
        f"Write denied: path is under protected directory `{prefix}`; "
        "create/modify/delete/overwrite forbidden.\n"
        f"  Target: {shown}\n\n"
        "Reason: this holds a third-party toolchain/SDK install (e.g. QAIRT SDK). "
        "Editing any file here can corrupt the shared toolchain (e.g. truncate an "
        "executable to 0 bytes), causing later model compiles to fail with "
        "`[WinError 193] %1 is not a valid Win32 application`.\n\n"
        "Do:\n"
        "  - Never write here (no `>` redirect, copy/move/del, Out-File, etc.).\n"
        "  - Need an SDK file? Read it from the `data/sdk` backup; do not touch the install dir.\n"
        "This is a hard safety boundary; do not bypass it. If a change here is truly required, "
        "stop and ask the user for authorization."
    )


# ---------------------------------------------------------------------------
# Child-process bridge: serialize the effective prefixes for the subprocess
# ``sitecustomize`` audit hook (which runs in a fresh interpreter and cannot
# import this module's in-process state). The hook reads ``QAI_PROTECTED_PATHS``
# and rebuilds the same prefix check independently of FileGuard.
# ---------------------------------------------------------------------------
def env_value() -> str:
    """Return the ``os.pathsep``-joined effective prefixes for child env.

    Always includes the built-ins, so a child process is protected even when no
    user paths are configured. The hook re-normalizes, so de-normalized or
    normalized forms both work; we emit the stored normalized forms.
    """
    return os.pathsep.join(protected_prefixes())

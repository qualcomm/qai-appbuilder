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
from pathlib import Path

__all__ = [
    "BUILTIN_PROTECTED_PREFIXES",
    "BUILTIN_SELF_PROTECTED_FILES",
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


def _repo_root_from_here() -> Path:
    """Return the repo root inferred from THIS file's location.

    ``protected_paths.py`` lives at ``src/qai/platform/protected_paths.py``
    so the repo root is three ``parent`` hops from this file's parent.
    Mirrors :func:`qai.platform.config.ports._repo_root_from_here`.
    """
    return Path(__file__).resolve().parent.parent.parent.parent


def _resolve_self_protected_files() -> tuple[str, ...]:
    """Locate the FileGuard self-protection surface (L-Nat-1).

    Returns absolute, normcase'd paths to the shipped FileGuard artefacts
    that MUST NOT be overwritten by an in-process write — the guard's own
    DLLs / injector / configuration file. Even when the file does not
    exist on disk yet (a fresh checkout without the vendor bundle), we
    still emit its expected path: the guard exists to prevent a WRITE, and
    a write can create the file too. Prefix-style matching (see
    ``_under_prefix``) collapses to file-only equality when the "prefix"
    is the file's absolute path — no accidental sibling shadowing.
    """
    repo_root = _repo_root_from_here()
    candidates = (
        repo_root / "vendor" / "bin" / "arm64" / "guard64.dll",
        repo_root / "vendor" / "bin" / "x64" / "guard64.dll",
        repo_root / "vendor" / "bin" / "arm64" / "guard-injector.exe",
        repo_root / "vendor" / "bin" / "x64" / "guard-injector.exe",
        repo_root / "factory" / "config" / "file_guard_paths.json",
    )
    return tuple(os.path.normcase(str(p)) for p in candidates)


#: L-Nat-1 (fileguard-audit-2026-07-26) — file-level self-protection.
#: These are the guard's own artefacts (DLLs, injector, config); an
#: in-process write to them must be blocked (they're the trust anchor of
#: the guard itself). Enforced by :func:`is_write_blocked` using exact
#: absolute-path equality (via the ``_under_prefix`` component-safe check
#: below, which collapses to equality when the "prefix" is a file path
#: with no children).
BUILTIN_SELF_PROTECTED_FILES: tuple[str, ...] = _resolve_self_protected_files()

#: Tier 2 — user-configured extra prefixes (normalized).  Replaced wholesale by
#: :func:`set_user_protected_paths`; merged with the built-ins at query time.
_user_prefixes: tuple[str, ...] = ()


def _normalize(path: str | bytes) -> str | None:
    r"""Return a normcase'd absolute path for comparison, or ``None``.

    Group C (M-Py-4): now a thin wrapper around
    :func:`qai.security.adapters.path_normalizer.normalize_windows_path`
    so this side of the compare and the security use case's
    ``check_permission._absolutise`` share the SAME canonical form
    (extended-length strip + realpath + ``GetLongPathNameW`` + normcase).
    A junction / 8.3 / ``\\?\`` dodge cannot skew ONE side any more.

    Preserves the V1 semantics this module's callers rely on:
    ``bytes`` in, ``None`` out on empty / undecodable input.
    """
    if isinstance(path, bytes):
        try:
            decoded = path.decode("utf-8", "surrogateescape")
        except Exception:  # noqa: BLE001
            return None
    elif isinstance(path, str):
        decoded = path
    else:
        return None
    if not decoded:
        return None
    from qai.platform.path_normalize import normalize_windows_path

    result = normalize_windows_path(decoded)
    return result or None


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
    """Return the effective (built-in + self-protected + user) normalized prefixes."""
    return (
        BUILTIN_PROTECTED_PREFIXES + BUILTIN_SELF_PROTECTED_FILES + _user_prefixes
    )


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

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cross-platform absolute-path normalisation for policy comparisons.

This adapter is the new-architecture replacement for the legacy
``backend/security/policy.py`` helpers ``_expand_windows_long_path``
and ``_get_final_path_by_handle`` (see lines 166-246 of the legacy
module). PR-092 (S9 §17.5 #11 / audit C-1 / H-16) consolidates them
into a single :func:`normalize_path` callable that returns a canonical,
absolute :class:`pathlib.Path` suitable for prefix / glob matching by
:class:`qai.security.domain.value_objects.PathPattern` and the
sandbox grant repository.

Behaviour:

* On Windows, expand 8.3 short names via ``GetLongPathNameW`` and
  resolve symlinks / junctions via ``CreateFileW`` +
  ``GetFinalPathNameByHandleW``. Both Win32 calls are guarded — any
  failure (path missing, ACL denied, API error) falls back to
  :meth:`pathlib.Path.resolve` (``strict=False``) and finally to the
  raw input wrapped in :class:`pathlib.Path`.
* On POSIX, dispatch to :func:`os.path.realpath` which follows
  symlinks and produces an absolute path.
* Inputs that are blank, ``None``-equivalent or UNC / device paths
  (``\\\\?\\``, ``\\\\.\\``) are returned unchanged (wrapped in
  :class:`pathlib.Path`); the caller is responsible for default-deny
  on those shapes.

The function is **side-effect-free** beyond the OS path API calls and
is safe to invoke on the audit-hook hot path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["normalize_path"]


def _is_unc_or_device(raw: str) -> bool:
    """Return ``True`` for UNC (``\\\\server\\share``) or device paths."""

    if not raw:
        return False
    s = raw.replace("/", "\\")
    if s.startswith("\\\\?\\") or s.startswith("\\\\.\\"):
        return True
    if s.startswith("\\\\") and len(s) > 2 and s[2] not in ("?", "."):
        return True
    return False


def _expand_long_path_win(p: Path) -> Path:
    """Expand 8.3 short names to long form via Win32 ``GetLongPathNameW``.

    Returns the original ``p`` if the API call fails or yields an
    empty buffer.
    """

    try:
        import ctypes
        from ctypes import wintypes

        get_long = ctypes.windll.kernel32.GetLongPathNameW
        get_long.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        get_long.restype = wintypes.DWORD

        buf_len = 1024
        buf = ctypes.create_unicode_buffer(buf_len)
        rv = get_long(str(p), buf, buf_len)
        if rv == 0:
            return p
        if rv >= buf_len:
            buf = ctypes.create_unicode_buffer(rv + 1)
            rv2 = get_long(str(p), buf, rv + 1)
            if rv2 == 0:
                return p
        result = buf.value
        if result:
            return Path(result)
        return p
    except Exception:  # pragma: no cover - hardening
        return p


def _final_path_by_handle_win(p: Path) -> Path | None:
    """Resolve junctions / symlinks via ``GetFinalPathNameByHandleW``.

    Returns ``None`` on any failure so the caller can fall back to the
    pre-resolution path. Uses ``FILE_FLAG_BACKUP_SEMANTICS`` so
    directories open without ``GENERIC_READ``.
    """

    try:
        import ctypes
        from ctypes import wintypes

        FILE_SHARE_READ = 1
        FILE_SHARE_WRITE = 2
        FILE_SHARE_DELETE = 4
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
        FILE_NAME_NORMALIZED = 0x0

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            str(p),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            return None
        try:
            buf_len = 1024
            buf = ctypes.create_unicode_buffer(buf_len)
            rv = kernel32.GetFinalPathNameByHandleW(
                handle, buf, buf_len, FILE_NAME_NORMALIZED
            )
            if rv == 0 or rv >= buf_len:
                return None
            result = buf.value
            if result.startswith("\\\\?\\"):
                result = result[4:]
            if not result:
                return None
            return Path(result)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - hardening
        return None


def normalize_path(p: "str | Path | None") -> Path:
    """Return the canonical absolute :class:`Path` for ``p``.

    Empty / blank input is mapped to :class:`Path` with the empty
    string so callers can compare without a separate ``None`` check.
    UNC and Win32 device paths bypass long-path / handle resolution
    (security policy treats them as untrusted; see legacy
    ``_normalize_path`` lines 592-632).
    """

    if p is None:
        return Path("")
    raw = str(p).strip()
    if not raw:
        return Path("")
    # Strip surrounding quotes (legacy users often paste C:\... in quotes).
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1].strip()
        if not raw:
            return Path("")

    if _is_unc_or_device(raw):
        # Untrusted shape — return as-is so callers can default-deny.
        return Path(raw)

    if sys.platform == "win32":
        try:
            base = Path(raw)
            try:
                resolved = base.resolve(strict=False)
            except (OSError, RuntimeError):
                try:
                    resolved = base.absolute()
                except OSError:
                    return base
            resolved = _expand_long_path_win(resolved)
            handle_resolved = _final_path_by_handle_win(resolved)
            if handle_resolved is not None:
                resolved = handle_resolved
            return resolved
        except Exception:
            return Path(raw)

    # POSIX: realpath is the canonical resolver (follows symlinks).
    try:
        return Path(os.path.realpath(raw))
    except Exception:
        try:
            return Path(raw).resolve()
        except Exception:
            return Path(raw)

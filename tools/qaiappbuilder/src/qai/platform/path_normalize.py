# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Shared cross-platform path normalisation for the security compare surface.

Group C (FileGuard audit M-Py-4 / L-Py-4 / L-Py-6) consolidates the three
independently-written absolute-path normalisers that previously drifted:

* :mod:`qai.security.application.use_cases.check_permission` — ``_absolutise``
  (a plain ``Path(...).resolve()``); relative agent-supplied paths never
  reached the same shape as the pre-resolved ``resolve_global_allow_paths``
  prefixes.
* :mod:`qai.platform.protected_paths` — ``_normalize`` (its own bespoke
  ``\\?\`` strip + ``realpath`` + ``GetLongPathNameW`` pipeline); a junction
  or 8.3 short-name request could then compare unequal to a ``check_permission``
  compare of the same file.
* :mod:`qai.ai_coding.infrastructure.tools.handlers._shared` —
  ``is_under_tool_result_store_root`` compared a stored-root-``resolve()``
  against a raw-``resolve()`` query so a mixed-case / extended-length /
  short-name query missed the trusted store.

Every one of those callsites now routes through :func:`normalize_windows_path`
here so equivalent input shapes reach a SINGLE canonical form.  The helper
lives in the ``qai.platform`` shared kernel (not ``qai.security.adapters``)
because ``qai.platform.protected_paths`` is one of the three consumers, and
the ``qai.platform`` layer must not depend on ``qai.security`` (context
independence contract — ``.importlinter`` contract 3).

The helper is **side-effect-free** beyond OS path API calls and safe on the
audit-hook hot path.  It never raises: any resolution fault degrades to
:func:`os.path.normcase` of the raw input so a downstream compare still runs
on SOMETHING rather than crashing the security check.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["normalize_windows_path"]


def _strip_win_extended_prefix(raw: str) -> str:
    r"""Strip ``\\?\`` / ``\\.\`` / ``\\?\UNC\`` prefixes.

    A crafted ``\\?\C:\Qualcomm\x`` addresses the SAME file as
    ``C:\Qualcomm\x`` but would not match a plain-drive ``c:\qualcomm``
    protected prefix (V1 ``policy._normalize_path`` parity).  A genuine
    UNC path (``\\server\share``) is left alone.
    """
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[len("\\\\?\\UNC\\") :]
    if raw.startswith("\\\\?\\") or raw.startswith("\\\\.\\"):
        return raw[4:]
    return raw


def _expand_long_path_win(path: str) -> str:
    """Expand 8.3 short-names via Win32 ``GetLongPathNameW`` (best-effort).

    Returns the input unchanged if the API call fails or yields nothing —
    a not-yet-existing leaf produces rv==0 which we swallow.
    """
    try:
        import ctypes

        buf_len = 1024
        buf = ctypes.create_unicode_buffer(buf_len)
        rv = ctypes.windll.kernel32.GetLongPathNameW(  # type: ignore[attr-defined]
            path, buf, buf_len
        )
        if rv == 0:
            return path
        if rv >= buf_len:
            buf = ctypes.create_unicode_buffer(rv + 1)
            rv2 = ctypes.windll.kernel32.GetLongPathNameW(  # type: ignore[attr-defined]
                path, buf, rv + 1
            )
            if rv2 == 0:
                return path
        result = buf.value
        return result or path
    except (OSError, AttributeError, ValueError):  # pragma: no cover — defensive
        return path


def _resolve_final_win(path: str) -> str:
    """Resolve symlinks / NTFS junctions via ``os.path.realpath``.

    ``realpath`` uses ``GetFinalPathNameByHandleW`` semantics on CPython
    3.8+ and follows junctions/symlinks for paths that EXIST.  A
    not-yet-existing leaf falls back to the input.
    """
    try:
        real = os.path.realpath(path)
        return real or path
    except (OSError, ValueError):  # pragma: no cover — defensive
        return path


def normalize_windows_path(p: "str | bytes | os.PathLike[str] | None") -> str:
    r"""Return a single canonical string form of ``p`` for policy compares.

    * ``bytes`` input is decoded ``utf-8 / surrogateescape`` (same envelope
      :func:`os.fsdecode` uses).
    * ``os.PathLike`` / :class:`pathlib.Path` inputs are accepted verbatim.
    * ``None`` / blank input maps to ``""`` so callers can compare without a
      separate ``is None`` branch.
    * On Windows: extended-length / device prefixes (``\\?\``, ``\\.\``,
      ``\\?\UNC\``) are stripped, ``realpath`` resolves symlinks / junctions,
      and ``GetLongPathNameW`` expands 8.3 short-names.
    * The final string is case-folded via :func:`os.path.normcase` — a
      Windows compare is therefore case-insensitive; on POSIX ``normcase``
      is a no-op.

    Never raises: a resolution fault degrades to ``normcase(raw)`` so a
    downstream compare still runs on SOMETHING.
    """
    # ---- input normalisation ----------------------------------------------
    if p is None:
        return ""
    if isinstance(p, bytes):
        try:
            raw = p.decode("utf-8", "surrogateescape")
        except Exception:  # noqa: BLE001 — undecodable bytes → empty
            return ""
    elif isinstance(p, os.PathLike):
        raw = os.fspath(p)
    elif isinstance(p, str):
        raw = p
    else:
        return ""
    raw = raw.strip()
    if not raw:
        return ""
    # Strip surrounding quotes (users paste ``"C:\..."`` on the CLI).
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1].strip()
        if not raw:
            return ""

    # ---- absolute + junction resolution -----------------------------------
    try:
        abs_path = os.path.abspath(raw)
    except (OSError, ValueError):
        return os.path.normcase(raw)

    if sys.platform == "win32":
        abs_path = _strip_win_extended_prefix(abs_path)
        # Resolve symlinks / junctions FIRST (so an ancestor junction into a
        # protected tree is caught), then expand any 8.3 short-name segments.
        abs_path = _resolve_final_win(abs_path)
        abs_path = _expand_long_path_win(abs_path)
        # A ``\\?\`` prefix can reappear after ``GetFinalPathNameByHandleW`` —
        # strip once more so the final form is drive-relative.
        abs_path = _strip_win_extended_prefix(abs_path)
    else:
        # POSIX: realpath follows symlinks + normalises separators.
        try:
            abs_path = os.path.realpath(abs_path) or abs_path
        except (OSError, ValueError):  # pragma: no cover — defensive
            pass

    return os.path.normcase(abs_path)

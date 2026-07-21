# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Path / filename syntactic validation.

These helpers cover the *lexical* layer only -- they make sure a
relative path or filename is well-formed and cannot escape an
implicit base directory through ``..`` segments, absolute paths,
Windows drive letters, NUL bytes, etc.

ACL-style checks ("is this user allowed to read this path?") are NOT
performed here; that belongs to the security path domain (plan §7).
This module is the syntactic gatekeeper that runs *before* an ACL
check, so the ACL layer never has to look at hostile input.

The helpers are platform-portable: a path that would be accepted on
Linux but is reserved on Windows (e.g. ``CON``) is still rejected, so
artifacts validated on a Linux dev box stay safe when copied to a
Windows deployment.
"""

from __future__ import annotations

import re
from typing import Final

from ._errors import raise_validation

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------
_CODE_TRAVERSAL: Final[str] = "validation.path.traversal"
_CODE_BAD_FILENAME: Final[str] = "validation.path.bad_filename"

# Windows reserved device names.  Comparison is case-insensitive AND
# stem-only ("CON.txt" is just as forbidden as "CON" on Windows).
_WIN_RESERVED_BASENAMES: Final[frozenset[str]] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Drive-letter prefix at the start of the string, e.g. ``C:`` or ``c:\\``.
_DRIVE_LETTER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:")

# Characters forbidden in a filename on either Linux or Windows.
# We deliberately *include* both ``/`` and ``\`` since a "filename" is
# meant to be a single path component.
_FORBIDDEN_FILENAME_CHARS: Final[frozenset[str]] = frozenset(
    {"/", "\\", "\x00", ":", "*", "?", '"', "<", ">", "|"}
)


# ---------------------------------------------------------------------------
# Public path assertions
# ---------------------------------------------------------------------------
def assert_no_path_traversal(rel_path: str, *, name: str) -> str:
    """Reject paths that could escape an implicit base directory.

    A path is rejected if any of the following hold:

    * it is empty,
    * it contains a NUL byte (``\\x00``),
    * it starts with a forward slash (POSIX absolute path),
    * it starts with a backslash (Windows root-relative path),
    * it starts with a drive letter (``C:`` / ``c:\\``),
    * any of its segments equals ``..``,
    * any segment is empty (``a//b``) -- avoids ambiguous join semantics.

    We deliberately do NOT call :func:`os.path.normpath` because its
    behaviour differs between POSIX and Windows; lexical checks give us
    a deterministic, cross-platform contract.
    """

    if rel_path == "":
        raise_validation(_CODE_TRAVERSAL, f"{name} must not be empty")

    if "\x00" in rel_path:
        raise_validation(
            _CODE_TRAVERSAL,
            f"{name} must not contain NUL byte",
        )

    if rel_path.startswith("/"):
        raise_validation(
            _CODE_TRAVERSAL,
            f"{name} must not be an absolute POSIX path: {rel_path!r}",
        )

    if rel_path.startswith("\\"):
        raise_validation(
            _CODE_TRAVERSAL,
            f"{name} must not start with a backslash: {rel_path!r}",
        )

    if _DRIVE_LETTER_RE.match(rel_path) is not None:
        raise_validation(
            _CODE_TRAVERSAL,
            f"{name} must not contain a Windows drive letter: {rel_path!r}",
        )

    # Split on BOTH separators so we catch traversal regardless of the
    # platform that produced the string.
    segments = re.split(r"[\\/]", rel_path)
    for segment in segments:
        if segment == "..":
            raise_validation(
                _CODE_TRAVERSAL,
                f"{name} contains parent-directory segment '..': {rel_path!r}",
            )
        if segment == "":
            raise_validation(
                _CODE_TRAVERSAL,
                (
                    f"{name} contains an empty segment "
                    f"(e.g. ``a//b``): {rel_path!r}"
                ),
            )

    return rel_path


def assert_safe_filename(name: str, *, max_length: int = 255) -> str:
    """Reject filenames that are unsafe on Linux *or* Windows.

    A filename is rejected if any of these hold:

    * empty / longer than ``max_length`` bytes (most filesystems cap at 255),
    * contains a path separator (``/`` or ``\\``) -- callers wanting a
      relative path should use :func:`assert_no_path_traversal` instead,
    * contains a NUL byte or any C0 control character (0x00..0x1F),
    * contains any of the Windows-forbidden characters
      ``: * ? " < > |``,
    * starts or ends with a space, or ends with a dot
      (Windows trims those, leading to ambiguous round-trips),
    * its stem (case-folded) matches a reserved Windows device name
      such as ``CON``, ``PRN``, ``AUX``, ``NUL``, ``COM1``..``COM9``,
      ``LPT1``..``LPT9``.

    The function is purely syntactic; it does not touch the filesystem.
    """

    if max_length <= 0:
        raise ValueError(f"max_length must be > 0, got {max_length}")

    if name == "":
        raise_validation(_CODE_BAD_FILENAME, "filename must not be empty")

    if len(name) > max_length:
        raise_validation(
            _CODE_BAD_FILENAME,
            f"filename length {len(name)} exceeds maximum {max_length}",
        )

    # Characters that are unsafe on *some* platform.
    for ch in name:
        if ch in _FORBIDDEN_FILENAME_CHARS:
            raise_validation(
                _CODE_BAD_FILENAME,
                f"filename contains forbidden character {ch!r}: {name!r}",
            )
        if ord(ch) < 0x20:
            raise_validation(
                _CODE_BAD_FILENAME,
                (
                    f"filename contains control character "
                    f"\\x{ord(ch):02x}: {name!r}"
                ),
            )

    # Trailing dot / leading or trailing space confuse Windows.
    if name.endswith("."):
        raise_validation(
            _CODE_BAD_FILENAME,
            f"filename must not end with '.': {name!r}",
        )
    if name != name.strip():
        raise_validation(
            _CODE_BAD_FILENAME,
            f"filename must not have leading or trailing whitespace: {name!r}",
        )

    # Reserved Windows device names ("con", "Con.txt", ...).  Compare
    # against the part *before* the first dot, upper-cased.
    stem = name.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED_BASENAMES:
        raise_validation(
            _CODE_BAD_FILENAME,
            f"filename uses reserved Windows device name: {name!r}",
        )

    return name


__all__ = [
    "assert_no_path_traversal",
    "assert_safe_filename",
]

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""``apply_patch`` text format parser.

Ported from ``backend/tools/_patch_parser.py`` (PR-101 / L1 lane).
Pure parsing — no IO, no security checks, no event publishing.

See module docstring on ``backend/tools/_patch_parser.py`` for the
supported syntax.  This file is a 1:1 copy with the legacy
``backend.exceptions`` import dropped (we keep ``PatchParseError`` as a
local subclass of ``ValueError``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

__all__ = [
    "FileOp",
    "Hunk",
    "OpKind",
    "Patch",
    "PatchParseError",
    "parse_patch",
]


_BEGIN_MARKER = "*** Begin Patch"
_END_MARKER = "*** End Patch"
_ADD_PREFIX = "*** Add File: "
_UPDATE_PREFIX = "*** Update File: "
_DELETE_PREFIX = "*** Delete File: "
_MOVE_PREFIX = "*** Move to: "
_HUNK_HEADER = "@@"


class OpKind(str, Enum):
    """Single file operation kind."""

    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"


@dataclass
class Hunk:
    """One contiguous edit inside an Update FileOp."""

    anchor: Optional[str] = None
    old_lines: List[str] = field(default_factory=list)
    new_lines: List[str] = field(default_factory=list)
    raw_lines: List[str] = field(default_factory=list)


@dataclass
class FileOp:
    """One file-level operation in a patch."""

    kind: OpKind
    path: str
    new_content: Optional[str] = None
    hunks: List[Hunk] = field(default_factory=list)
    header_line_no: int = 0
    #: For an UPDATE op, an optional destination path parsed from a
    #: ``*** Move to: <path>`` line placed immediately after the
    #: ``*** Update File:`` header. When set, the update hunks are applied to
    #: the source ``path`` content and the result is written to ``move_path``
    #: while the source ``path`` is removed (a rename/move). ``None`` for a
    #: plain in-place update and for ADD / DELETE.
    move_path: Optional[str] = None


@dataclass
class Patch:
    """A complete parsed patch."""

    ops: List[FileOp] = field(default_factory=list)


class PatchParseError(ValueError):
    """Raised when a patch text fails to parse.

    The message is already formatted for the model / user.
    """


# ---------------------------------------------------------------------------
# Parser entry
# ---------------------------------------------------------------------------


def parse_patch(text: str) -> Patch:
    """Parse an ``apply_patch`` text into a :class:`Patch` IR.

    Performs only structural validation — no IO, no path checks.
    """

    if not text or not text.strip():
        raise PatchParseError("patch text is empty")

    lines = text.splitlines()

    begin_idx = _find_line(lines, _BEGIN_MARKER)
    if begin_idx is None:
        raise PatchParseError(
            f"begin marker {_BEGIN_MARKER!r} not found. patch must start with this line."
        )

    end_idx = _find_line(lines, _END_MARKER, start=begin_idx + 1)
    if end_idx is None:
        raise PatchParseError(
            f"end marker {_END_MARKER!r} not found. patch must end with this line."
        )

    body = lines[begin_idx + 1 : end_idx]
    patch = Patch()

    i = 0
    n = len(body)
    while i < n:
        line = body[i]
        if line.strip() == "":
            i += 1
            continue

        if line.startswith(_ADD_PREFIX):
            path = line[len(_ADD_PREFIX) :].strip()
            if not path:
                raise PatchParseError(
                    f"line {begin_idx + i + 2}: Add File is missing a path"
                )
            content_lines, consumed = _collect_add_body(body, i + 1)
            patch.ops.append(
                FileOp(
                    kind=OpKind.ADD,
                    path=path,
                    new_content="\n".join(content_lines),
                    header_line_no=begin_idx + i + 2,
                )
            )
            i += 1 + consumed

        elif line.startswith(_UPDATE_PREFIX):
            path = line[len(_UPDATE_PREFIX) :].strip()
            if not path:
                raise PatchParseError(
                    f"line {begin_idx + i + 2}: Update File is missing a path"
                )
            # Optional rename/move directive: a ``*** Move to: <path>`` line
            # placed IMMEDIATELY after the Update header renames the file — the
            # hunks apply to the source content and the result is written to
            # the new path (source removed). Consume that line here (before
            # ``_collect_update_hunks``) so the hunk collector never sees it.
            body_offset = i + 1
            move_path: Optional[str] = None
            if body_offset < n and body[body_offset].startswith(_MOVE_PREFIX):
                move_path = body[body_offset][len(_MOVE_PREFIX) :].strip()
                if not move_path:
                    raise PatchParseError(
                        f"line {begin_idx + body_offset + 2}: "
                        f"Move to is missing a target path"
                    )
                body_offset += 1
            hunks, consumed = _collect_update_hunks(
                body, body_offset, header_offset=begin_idx
            )
            if not hunks:
                raise PatchParseError(
                    f"Update File {path!r} has no hunks (must contain at least one change)"
                )
            patch.ops.append(
                FileOp(
                    kind=OpKind.UPDATE,
                    path=path,
                    hunks=hunks,
                    header_line_no=begin_idx + i + 2,
                    move_path=move_path,
                )
            )
            i = body_offset + consumed

        elif line.startswith(_DELETE_PREFIX):
            path = line[len(_DELETE_PREFIX) :].strip()
            if not path:
                raise PatchParseError(
                    f"line {begin_idx + i + 2}: Delete File is missing a path"
                )
            patch.ops.append(
                FileOp(
                    kind=OpKind.DELETE,
                    path=path,
                    header_line_no=begin_idx + i + 2,
                )
            )
            i += 1

        else:
            raise PatchParseError(
                f"line {begin_idx + i + 2}: unrecognized directive {line!r}. "
                f"expected it to start with {_ADD_PREFIX.strip()!r}, {_UPDATE_PREFIX.strip()!r} "
                f"or {_DELETE_PREFIX.strip()!r}."
            )

    if not patch.ops:
        raise PatchParseError("patch contains no file operations (Add/Update/Delete)")

    seen: dict[str, OpKind] = {}
    for op in patch.ops:
        if op.path in seen:
            raise PatchParseError(
                f"path {op.path!r} is operated on multiple times in the same patch"
                f" ({seen[op.path].value} and {op.kind.value})"
            )
        seen[op.path] = op.kind
        if op.move_path is not None:
            # A move CREATES the target path: it must differ from the source
            # and must not collide with any other op's touched path (source or
            # target). Register it in the same ``seen`` map so a later op that
            # touches the move target — or a second move to the same target —
            # is rejected before any IO.
            if op.move_path == op.path:
                raise PatchParseError(
                    f"Move to target path {op.move_path!r} is the same as the source path"
                    f" (a rename target must be a different path)"
                )
            if op.move_path in seen:
                raise PatchParseError(
                    f"Move to target path {op.move_path!r} conflicts with another"
                    f" operation in the same patch ({seen[op.move_path].value})"
                )
            seen[op.move_path] = op.kind

    return patch


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_line(lines: List[str], target: str, start: int = 0) -> Optional[int]:
    for idx in range(start, len(lines)):
        if lines[idx].rstrip() == target:
            return idx
    return None


def _is_section_header(line: str) -> bool:
    return (
        line.startswith(_ADD_PREFIX)
        or line.startswith(_UPDATE_PREFIX)
        or line.startswith(_DELETE_PREFIX)
    )


def _collect_add_body(body: List[str], start: int) -> tuple[List[str], int]:
    out: List[str] = []
    j = start
    while j < len(body):
        line = body[j]
        if _is_section_header(line):
            break
        if line.startswith("+"):
            out.append(line[1:])
            j += 1
            continue
        if line.strip() == "":
            out.append("")
            j += 1
            continue
        raise PatchParseError(
            f"Add File section contains a line not starting with '+': {line!r}"
            f" (new lines must be marked with '+')"
        )
    return out, j - start


def _collect_update_hunks(
    body: List[str], start: int, *, header_offset: int
) -> tuple[List[Hunk], int]:
    hunks: List[Hunk] = []
    cur: Optional[Hunk] = None
    j = start

    def _flush_current() -> None:
        nonlocal cur
        if cur is not None and (cur.old_lines or cur.new_lines):
            hunks.append(cur)
        cur = None

    while j < len(body):
        line = body[j]
        if _is_section_header(line):
            break

        if line.startswith(_HUNK_HEADER):
            _flush_current()
            anchor = line[len(_HUNK_HEADER) :].strip() or None
            cur = Hunk(anchor=anchor)
            j += 1
            continue

        if line == "":
            if cur is None:
                cur = Hunk()
            cur.old_lines.append("")
            cur.new_lines.append("")
            cur.raw_lines.append("")
            j += 1
            continue

        first = line[0]
        rest = line[1:]
        if cur is None:
            cur = Hunk()

        if first == "+":
            cur.new_lines.append(rest)
        elif first == "-":
            cur.old_lines.append(rest)
        elif first == " ":
            cur.old_lines.append(rest)
            cur.new_lines.append(rest)
        else:
            raise PatchParseError(
                f"line {header_offset + j + 2}: hunk line must start with '+', '-' or ' ', "
                f"got {line!r}"
            )
        cur.raw_lines.append(line)
        j += 1

    _flush_current()
    return hunks, j - start

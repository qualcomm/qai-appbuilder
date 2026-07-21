# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""``apply_patch`` applier.

Ported from ``backend/tools/_patch_applier.py`` (PR-101 / L1 lane).
Pure computation — disk IO is the caller's responsibility (the
:func:`tool_apply_patch` handler in :mod:`patch_tool` opens its files
inside a thread pool via :func:`asyncio.to_thread`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from qai.ai_coding.infrastructure.tools._patch_parser import (
    FileOp,
    Hunk,
    OpKind,
    Patch,
)

__all__ = [
    "FilePlan",
    "PatchApplyError",
    "PlanResult",
    "plan_patch",
]


class PatchApplyError(ValueError):
    """Raised when a patch cannot be applied (hunk not found, etc.)."""


#: Per-hunk match classification returned by :func:`_locate_hunk` /
#: :func:`_locate_anchor`. The numeric values are ordered by risk so a plan
#: can keep the WORST one seen:
#:   * ``MATCH_EXACT``       — the hunk matched byte-for-byte at the expected
#:     cursor. Zero risk.
#:   * ``MATCH_INPLACE_FUZZY`` — matched at the expected cursor only after a
#:     whitespace-tolerant compare (rstrip / strip). LOW risk: same location,
#:     only trailing / surrounding whitespace differs (the common "off by a
#:     space" drift). Applied without friction.
#:   * ``MATCH_RELOCATED``   — matched ONLY after re-scanning from index 0 (the
#:     hunk was NOT at the expected cursor and landed at a DIFFERENT position).
#:     HIGH risk of modifying the wrong region: line-count conservation cannot
#:     tell a correct relocation from a wrong one. Gated as a hard error.
MATCH_EXACT = 0
MATCH_INPLACE_FUZZY = 1
MATCH_RELOCATED = 2


@dataclass
class FilePlan:
    path: str
    kind: OpKind
    new_content: Optional[str] = None
    old_content: Optional[str] = None
    fuzzy: bool = False
    applied_hunks: int = 0
    #: For an UPDATE, the exact line-count delta the hunks imply
    #: (``sum(len(new_lines) - len(old_lines))`` over every hunk). ``None``
    #: for ADD / DELETE (no original→delta relation). The commit step uses it
    #: as a pre-write CONSERVATION sanity check: the planned ``new_content``
    #: line count MUST equal the original line count plus this delta, so a
    #: planner bug that drops lines is caught before any disk write.
    expected_line_delta: Optional[int] = None
    #: For an UPDATE that is also a rename/move, the destination path the
    #: ``new_content`` should be written to (the source ``path`` is removed by
    #: the commit step). ``None`` for a plain in-place update and for
    #: ADD / DELETE. The move is atomic + trash-backed + rollbackable in the
    #: commit layer, exactly like every other op.
    move_to: Optional[str] = None


@dataclass
class PlanResult:
    plans: List[FilePlan] = field(default_factory=list)
    any_fuzzy: bool = False


def plan_patch(
    patch: Patch,
    *,
    file_reader: Callable[[str], str],
    file_exists: Callable[[str], bool],
) -> PlanResult:
    result = PlanResult()
    for op in patch.ops:
        if op.kind is OpKind.ADD:
            plan = _plan_add(op, file_exists=file_exists)
        elif op.kind is OpKind.UPDATE:
            plan = _plan_update(
                op, file_reader=file_reader, file_exists=file_exists
            )
        elif op.kind is OpKind.DELETE:
            plan = _plan_delete(
                op, file_reader=file_reader, file_exists=file_exists
            )
        else:  # pragma: no cover — parser already enumerates all kinds
            raise PatchApplyError(f"unknown FileOp kind: {op.kind!r}")
        if plan.fuzzy:
            result.any_fuzzy = True
        result.plans.append(plan)
    return result


def _plan_add(op: FileOp, *, file_exists: Callable[[str], bool]) -> FilePlan:
    if file_exists(op.path):
        raise PatchApplyError(
            f"Add File failed: {op.path!r} already exists. "
            f"To overwrite use Update File, or Delete File first then Add."
        )
    content = op.new_content or ""
    if content and not content.endswith("\n"):
        content += "\n"
    return FilePlan(
        path=op.path,
        kind=OpKind.ADD,
        new_content=content,
        old_content=None,
    )


def _plan_delete(
    op: FileOp,
    *,
    file_reader: Callable[[str], str],
    file_exists: Callable[[str], bool],
) -> FilePlan:
    if not file_exists(op.path):
        raise PatchApplyError(f"Delete File failed: {op.path!r} does not exist")
    try:
        old_content = file_reader(op.path)
    except PatchApplyError:
        raise
    except Exception as e:  # noqa: BLE001 — surface as PatchApplyError
        raise PatchApplyError(
            f"Delete File failed: error reading {op.path!r}: {e}"
        ) from e
    return FilePlan(
        path=op.path,
        kind=OpKind.DELETE,
        new_content=None,
        old_content=old_content,
    )


def _plan_update(
    op: FileOp,
    *,
    file_reader: Callable[[str], str],
    file_exists: Callable[[str], bool],
) -> FilePlan:
    if not file_exists(op.path):
        raise PatchApplyError(
            f"Update File failed: {op.path!r} does not exist. "
            f"To create a new file use Add File."
        )
    # A rename/move (``*** Move to:``) writes the result to a NEW path and
    # removes the source. For safety we refuse to overwrite an existing target
    # (mirrors ``_plan_add``): a move must never silently clobber another file.
    if op.move_path is not None and file_exists(op.move_path):
        raise PatchApplyError(
            f"Move to failed: target path {op.move_path!r} already exists; "
            f"Move cannot overwrite an existing file. To overwrite, Delete File "
            f"the target first, or use Update File to modify the target directly."
        )
    try:
        original = file_reader(op.path)
    except PatchApplyError:
        raise
    except Exception as e:  # noqa: BLE001
        raise PatchApplyError(
            f"Update File failed: error reading {op.path!r}: {e}"
        ) from e

    lines, had_trailing_nl = _split_keep_trailing(original)
    original_line_count = len(lines)
    # Exact line-count delta implied by the hunks: every hunk replaces
    # ``len(old_lines)`` lines with ``len(new_lines)`` (a pure insert has
    # ``old_lines == []``), so the net change is the sum of the per-hunk
    # deltas. Computed from the PARSED hunks (independent of where they land)
    # so the commit step can verify the planned content conserved the lines.
    expected_line_delta = sum(
        len(h.new_lines) - len(h.old_lines) for h in op.hunks
    )
    fuzzy_used = False
    applied = 0
    cursor = 0
    for h_idx, hunk in enumerate(op.hunks):
        if not hunk.old_lines and hunk.new_lines:
            if hunk.anchor:
                pos, match_kind = _locate_anchor(
                    lines, hunk.anchor, start=cursor
                )
                if pos is None:
                    raise PatchApplyError(
                        _hunk_not_found_msg(
                            op.path, h_idx, hunk, original_lines=lines
                        )
                    )
                if match_kind == MATCH_RELOCATED:
                    raise PatchApplyError(
                        _relocated_msg(op.path, h_idx, hunk)
                    )
                lines[pos:pos] = hunk.new_lines
                cursor = pos + len(hunk.new_lines)
                if match_kind != MATCH_EXACT:
                    fuzzy_used = True
            else:
                raise PatchApplyError(
                    f"Update File {op.path!r} hunk #{h_idx + 1}: a pure-insert hunk "
                    f"must provide an @@ anchor (otherwise the insert position cannot be determined)"
                )
        else:
            pos, length, match_kind = _locate_hunk(
                lines, hunk.old_lines, start=cursor
            )
            if pos is None:
                raise PatchApplyError(
                    _hunk_not_found_msg(
                        op.path, h_idx, hunk, original_lines=lines
                    )
                )
            if match_kind == MATCH_RELOCATED:
                raise PatchApplyError(
                    _relocated_msg(op.path, h_idx, hunk)
                )
            lines[pos : pos + length] = hunk.new_lines
            cursor = pos + len(hunk.new_lines)
            if match_kind != MATCH_EXACT:
                fuzzy_used = True
        applied += 1

    new_content = "\n".join(lines)
    if had_trailing_nl:
        new_content += "\n"

    # Pre-write CONSERVATION sanity check (exact arithmetic, no fuzzy
    # threshold): the resulting line count must equal the original plus the
    # delta the hunks imply. A planner bug that silently dropped/duplicated
    # lines (the 2162→26 failure class) is caught HERE, before any disk IO.
    new_line_count = len(lines)
    expected_line_count = original_line_count + expected_line_delta
    if new_line_count != expected_line_count:
        raise PatchApplyError(
            f"Update File {op.path!r}: line count after applying hunks does not match "
            f"the patch estimate (got {new_line_count} lines, expected "
            f"{expected_line_count} lines from hunk add/remove: original "
            f"{original_line_count} lines, net change "
            f"{expected_line_delta:+d}). The patch may have landed at the wrong "
            f"place or been truncated; refused to write to disk."
        )

    return FilePlan(
        path=op.path,
        kind=OpKind.UPDATE,
        new_content=new_content,
        old_content=original,
        fuzzy=fuzzy_used,
        applied_hunks=applied,
        expected_line_delta=expected_line_delta,
        move_to=op.move_path,
    )


# ---------------------------------------------------------------------------
# Line matching
# ---------------------------------------------------------------------------


def _split_keep_trailing(text: str) -> Tuple[List[str], bool]:
    if text == "":
        return [], False
    had_nl = text.endswith("\n")
    return text.splitlines(), had_nl


def _locate_hunk(
    haystack: List[str], needle: List[str], *, start: int
) -> Tuple[Optional[int], int, int]:
    """Find ``needle`` in ``haystack`` at/after ``start``.

    Returns ``(pos, length, match_kind)`` where ``match_kind`` is one of
    :data:`MATCH_EXACT` / :data:`MATCH_INPLACE_FUZZY` / :data:`MATCH_RELOCATED`
    (see their docstrings). The ladder, in increasing risk:

    #. EXACT at/after the cursor → ``MATCH_EXACT``.
    #. rstrip-tolerant, then strip-tolerant, at/after the cursor →
       ``MATCH_INPLACE_FUZZY`` (LOW risk: same place, whitespace drift only).
    #. ONLY if nothing matched at/after the cursor do we re-scan from index 0
       (``start > 0``). A match found there is a RELOCATION — it lands at a
       DIFFERENT position than intended → ``MATCH_RELOCATED`` (HIGH risk; the
       caller turns this into a hard error). The from-0 re-scan itself uses the
       exact→rstrip→strip ladder, but the OUTCOME is classified RELOCATED
       regardless because the position moved.
    """
    if not needle:
        return start, 0, MATCH_EXACT
    n = len(needle)
    H = len(haystack)
    for i in range(start, H - n + 1):
        if haystack[i : i + n] == needle:
            return i, n, MATCH_EXACT
    needle_rstrip = [s.rstrip() for s in needle]
    for i in range(start, H - n + 1):
        if [s.rstrip() for s in haystack[i : i + n]] == needle_rstrip:
            return i, n, MATCH_INPLACE_FUZZY
    needle_strip = [s.strip() for s in needle]
    for i in range(start, H - n + 1):
        if [s.strip() for s in haystack[i : i + n]] == needle_strip:
            return i, n, MATCH_INPLACE_FUZZY
    if start > 0:
        # Nothing at/after the cursor — re-scan the WHOLE file from 0. Any hit
        # is at a different location than the patch implied, so it is a
        # RELOCATION (the dangerous case): line-count conservation cannot tell
        # a correct relocation from a wrong one, so we never silently apply it.
        sub_pos, sub_len, _sub_kind = _locate_hunk(haystack, needle, start=0)
        if sub_pos is not None:
            return sub_pos, sub_len, MATCH_RELOCATED
    return None, 0, MATCH_EXACT


def _locate_anchor(
    haystack: List[str], anchor: str, *, start: int
) -> Tuple[Optional[int], int]:
    """Find the insertion point after the line containing ``anchor``.

    Returns ``(pos, match_kind)``. A hit at/after the cursor is
    :data:`MATCH_EXACT`; a hit found ONLY by re-scanning from index 0 is
    :data:`MATCH_RELOCATED` (the anchor was not where the patch implied — the
    caller gates this as a hard error). ``pos`` is ``None`` when no anchor line
    is found anywhere.
    """
    target = anchor.strip()
    if not target:
        return None, MATCH_EXACT
    H = len(haystack)
    for i in range(start, H):
        if target in haystack[i]:
            return i + 1, MATCH_EXACT
    if start > 0:
        for i in range(0, H):
            if target in haystack[i]:
                return i + 1, MATCH_RELOCATED
    return None, MATCH_EXACT


def _relocated_msg(path: str, h_idx: int, hunk: Hunk) -> str:
    """Hard-error message for a hunk that only matched after relocating.

    Mirrors how ``_edit_match`` REFUSES an ambiguous match instead of guessing:
    apply_patch will not silently apply a hunk at a position other than the one
    its context implied, because the planner's line-count conservation cannot
    distinguish a correct relocation from a wrong one.
    """
    if hunk.anchor:
        ctx = f"@@ {hunk.anchor}"
    else:
        ctx = "\n".join(hunk.old_lines[:8])
        if len(hunk.old_lines) > 8:
            ctx += f"\n... ({len(hunk.old_lines)} lines total)"
    return (
        f"Update File {path!r} hunk #{h_idx + 1}: the expected context was not found "
        f"at the expected position; it only matches elsewhere in the file "
        f"(relocation). To avoid applying the change at an unintended position, "
        f"refused to write to disk.\n"
        f"  expected context:\n"
        f"    {ctx}\n"
        f"  Please re-read the file, confirm the code snippet to modify and its "
        f"surrounding context, then regenerate the patch with the correct context."
    )


def _hunk_not_found_msg(
    path: str, h_idx: int, hunk: Hunk, *, original_lines: List[str]
) -> str:
    expected = "\n".join(hunk.old_lines[:8])
    if len(hunk.old_lines) > 8:
        expected += f"\n... ({len(hunk.old_lines)} lines total)"
    sample = "\n".join(
        f"{i + 1:>4}: {l}" for i, l in enumerate(original_lines[:6])
    )
    return (
        f"Update File {path!r} hunk #{h_idx + 1}: the expected context was not found in the file.\n"
        f"  expected (old_lines):\n"
        f"    {expected}\n"
        f"  file head reference:\n"
        f"{sample}\n"
        f"  Please re-read the file, confirm the code snippet to modify, then regenerate the patch."
    )

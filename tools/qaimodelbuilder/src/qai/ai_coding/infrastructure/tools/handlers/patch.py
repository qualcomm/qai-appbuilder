# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Atomic multi-file patch tool handler (``apply_patch``)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qai.ai_coding.application.ports import FileGuardPort
from qai.ai_coding.infrastructure.tools._patch_applier import (
    FilePlan,
    PatchApplyError,
    plan_patch,
)
from qai.ai_coding.infrastructure.tools._safe_commit import (
    atomic_write_bytes,
    backup_to_trash,
    verify_after_write,
)
from qai.ai_coding.infrastructure.tools._patch_parser import (
    OpKind,
    PatchParseError,
    parse_patch,
)
from qai.ai_coding.infrastructure.tools.errors import ToolError, ToolGuardDenied
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    _ok,
    get_workspace_base,
    logger,
    resolve_under_workspace,
)
from qai.platform import protected_paths
from qai.platform.scheduling.path_locks import PathLockManager


@dataclass
class _UndoEntry:
    """One reversible commit step recorded for atomic rollback.

    A single entry describes how to undo exactly one committed file operation
    so :func:`_rollback` can restore the pre-patch state precisely — never
    guessing, never leaving the target of a failed move half-applied.

    * Plain ADD / UPDATE / DELETE (``move_to is None``): the operation touched
      a single ``path``. Rollback restores ``path`` to ``prev_content`` when
      ``prev_existed`` is True, else removes ``path`` (it was newly created).
    * MOVE (``move_to`` set): the operation wrote the new content to
      ``move_to`` and removed the source ``path``. On rollback we must return
      to "source in place, target absent": restore ``path`` to
      ``prev_content`` (it always existed for an update) AND remove the
      newly-created ``move_to`` if present.
    """

    path: Path
    prev_existed: bool
    prev_content: str | None
    move_to: Path | None = None


async def tool_apply_patch(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
    path_lock: PathLockManager | None = None,
) -> dict[str, Any]:
    patch_text = args.get("patch") or ""
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise ToolError(
            "apply_patch: 'patch' argument is required and must be non-empty"
        )

    try:
        patch = parse_patch(patch_text)
    except PatchParseError as e:
        raise ToolError(f"apply_patch: parse failed: {e}") from e

    # Resolve every touched path against the per-request workspace base so
    # relative patch paths land in the active workspace (not the daemon
    # CWD == repo root). Absolute paths / no-base are returned unchanged.
    # A rename/move target (``move_path``) is a written path too, so it is
    # resolved the same way — otherwise a relative move target would land in
    # the daemon CWD instead of the workspace.
    for op in patch.ops:
        op.path = resolve_under_workspace(op.path)
        if getattr(op, "move_path", None):
            op.move_path = resolve_under_workspace(op.move_path)

    async def _guarded_apply() -> dict[str, Any]:
        # Pre-flight: enforce guard for every touched path BEFORE any IO.
        for op in patch.ops:
            # ALWAYS-ON protected-path guard (independent of FileGuard): a patch
            # touching the Qualcomm / QAIRT SDK tree is rejected before any IO
            # so a single op cannot corrupt the shared toolchain. See
            # ``qai.platform.protected_paths``.
            matched = protected_paths.is_write_blocked(op.path)
            if matched:
                raise ToolGuardDenied(
                    message=protected_paths.deny_message(op.path, matched),
                    error_code="ai_coding.tool.protected_path_write_denied",
                )
            await file_guard.enforce_project_access(
                path=op.path, operation="apply_patch"
            )
            if op.kind in (OpKind.UPDATE, OpKind.DELETE):
                await file_guard.enforce_read(
                    path=op.path, caller="ai_coding.tool.apply_patch"
                )
            # SEC-ENHANCE-AUDITUX-1: route DELETE to enforce_delete so the
            # audit row records op="delete" (+ AceMask.delete bit) — the
            # security decision is identical to a write (both go through the
            # write-allow grant), but the audit trail can now distinguish an
            # in-place UPDATE from a DELETE on the same path. UPDATE / ADD
            # continue to gate on enforce_write.
            if op.kind is OpKind.DELETE:
                await file_guard.enforce_delete(
                    path=op.path, caller="ai_coding.tool.apply_patch"
                )
            elif op.kind in (OpKind.UPDATE, OpKind.ADD):
                await file_guard.enforce_write(
                    path=op.path, caller="ai_coding.tool.apply_patch"
                )
            # A rename/move CREATES a new path (the move target). It must clear
            # the SAME gates as any other written path BEFORE any IO: the
            # always-on protected-path guard, project-access, and write. The
            # source path already passed read+write above (it is an UPDATE).
            move_target = getattr(op, "move_path", None)
            if move_target:
                target_matched = protected_paths.is_write_blocked(move_target)
                if target_matched:
                    raise ToolGuardDenied(
                        message=protected_paths.deny_message(
                            move_target, target_matched
                        ),
                        error_code=(
                            "ai_coding.tool.protected_path_write_denied"
                        ),
                    )
                await file_guard.enforce_project_access(
                    path=move_target, operation="apply_patch"
                )
                await file_guard.enforce_write(
                    path=move_target, caller="ai_coding.tool.apply_patch"
                )

        def _do_apply() -> dict[str, Any]:
            def _read(path_str: str) -> str:
                try:
                    return Path(path_str).read_text(encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    raise PatchApplyError(
                        f"cannot read {path_str!r}: {e}"
                    ) from e

            def _exists(path_str: str) -> bool:
                return Path(path_str).exists()

            try:
                plan = plan_patch(patch, file_reader=_read, file_exists=_exists)
            except PatchApplyError as e:
                raise ToolError(f"apply_patch: apply failed: {e}") from e

            undo: list[_UndoEntry] = []
            try:
                for fp in plan.plans:
                    _commit_one(fp, undo)
            except Exception as e:  # noqa: BLE001
                failed = _rollback(undo)
                if failed:
                    detail = "; ".join(
                        f"{p} (restore from trash: {bp})" for p, bp in failed
                    )
                    raise ToolError(
                        f"apply_patch: write failed and rollback could not "
                        f"restore some files; restore manually from trash: "
                        f"{detail} (original error: {e})"
                    ) from e
                raise ToolError(
                    f"apply_patch: write failed, rolled back: {e}"
                ) from e

            # Summary
            counts = {OpKind.ADD: 0, OpKind.UPDATE: 0, OpKind.DELETE: 0}
            files_summary: list[dict[str, Any]] = []
            for fp in plan.plans:
                counts[fp.kind] = counts.get(fp.kind, 0) + 1
                entry: dict[str, Any] = {
                    "path": fp.path,
                    "kind": fp.kind.value,
                    "fuzzy": fp.fuzzy,
                    "applied_hunks": fp.applied_hunks,
                }
                # Additive: a rename/move surfaces its destination path so the
                # model/UI can see the file was moved (existing keys unchanged).
                if fp.move_to is not None:
                    entry["moved_to"] = fp.move_to
                files_summary.append(entry)
            head = (
                f"apply_patch OK: +{counts[OpKind.ADD]} "
                f"~{counts[OpKind.UPDATE]} "
                f"-{counts[OpKind.DELETE]} ({len(plan.plans)} files)"
            )
            if plan.any_fuzzy:
                head += "; some hunks matched via fuzzy matching"

            return _ok(
                head,
                files=files_summary,
                any_fuzzy=plan.any_fuzzy,
                total_files=len(plan.plans),
            )

        return await asyncio.to_thread(_do_apply)

    # PARALLEL-TOOL-1: a multi-file patch locks ALL its touched paths (sorted
    # canonical order → no deadlock with another concurrent multi-path op);
    # different files still run in parallel. No lock wired → unchanged.
    if path_lock is not None:
        async with path_lock.lock_many([op.path for op in patch.ops]):
            return await _guarded_apply()
    return await _guarded_apply()


def _workspace_root_for_patch(path: Path) -> Path:
    """Best-effort workspace root for laying out the edit-trash tree.

    Prefers the per-request workspace base; falls back to the touched FILE's
    own parent (NOT the daemon CWD). This is passed to
    :func:`backup_to_trash`, where :func:`resolve_trash_root` places the trash
    at ``<workspace>/.edit_trash`` — co-located with the file actually being
    written, requiring no structural marker — matching
    ``read_write._workspace_root_for``. The trash layer degrades gracefully
    (per-user fallback; skip only as a last resort), so an imperfect root never
    breaks a write.
    """
    base = get_workspace_base()
    if base:
        try:
            return Path(base)
        except (ValueError, OSError):  # pragma: no cover — defensive
            pass
    return path.parent


def _commit_one(
    fp: FilePlan,
    undo: list[_UndoEntry],
) -> None:
    p = Path(fp.path)
    workspace_root = _workspace_root_for_patch(p)
    if fp.kind is OpKind.DELETE:
        if not p.exists():
            raise FileNotFoundError(
                f"file no longer exists when preparing delete: {fp.path} "
                f"(possibly modified concurrently)"
            )
        prev = (
            fp.old_content
            if fp.old_content is not None
            else p.read_text(encoding="utf-8")
        )
        # Trash the content we are about to delete so a wrong delete is
        # recoverable independently of git.
        backup_to_trash(
            workspace_root,
            p,
            prev.encode("utf-8"),
            tool="apply_patch",
            meta={"mode": "delete"},
        )
        undo.append(_UndoEntry(path=p, prev_existed=True, prev_content=prev))
        p.unlink()
        return

    # Rename/move branch (UPDATE + ``*** Move to:``). The hunk-applied content
    # is written to a NEW path and the source is removed. It is nested in the
    # SAME atomic + trash + rollback machinery as every other op:
    #   1. read the source's current content (it exists — it is an UPDATE),
    #   2. trash the source's ORIGINAL content (recoverable move-away),
    #   3. atomic_write_bytes the new content to the TARGET + verify,
    #   4. remove the SOURCE.
    # The undo entry records source path + its prev_content + the move target
    # so :func:`_rollback` can restore "source in place, target absent".
    if fp.move_to is not None:
        target = Path(fp.move_to)
        if not p.exists():
            raise FileNotFoundError(
                f"source file no longer exists when preparing move: {fp.path} "
                f"(possibly modified concurrently)"
            )
        if target.exists():
            # Defensive: the planner already refused an existing target, but a
            # concurrent create could have raced in. Never clobber.
            raise FileExistsError(
                f"move target already exists when preparing move: "
                f"{fp.move_to} (possibly created concurrently), aborted"
            )
        source_prev = (
            fp.old_content
            if fp.old_content is not None
            else p.read_text(encoding="utf-8")
        )
        new_text = fp.new_content or ""
        new_bytes = new_text.encode("utf-8")
        # Trash the source's ORIGINAL content before we move it away, so a
        # wrong move is recoverable independently of git.
        backup_to_trash(
            workspace_root,
            p,
            source_prev.encode("utf-8"),
            tool="apply_patch",
            meta={"mode": "move", "move_to": str(target), "new_bytes_blob": new_bytes},
        )
        # Record undo BEFORE any write so a failure at any step below can be
        # rolled back (restore source content + remove target if created).
        undo.append(
            _UndoEntry(
                path=p,
                prev_existed=True,
                prev_content=source_prev,
                move_to=target,
            )
        )
        # Write the new content to the TARGET atomically + verify, THEN remove
        # the source. Ordering matters: if the target write fails, the source
        # is still in place and rollback removes nothing that was created; if
        # the unlink fails, rollback restores the source and removes target.
        atomic_write_bytes(target, new_bytes)
        verify_after_write(target, new_bytes)
        p.unlink()
        return

    prev_existed = p.exists()
    prev_content = (
        p.read_text(encoding="utf-8") if prev_existed else None
    )
    new_text = fp.new_content or ""
    new_bytes = new_text.encode("utf-8")
    # Trash the ORIGINAL content before overwriting (only when it existed).
    if prev_existed and prev_content is not None:
        backup_to_trash(
            workspace_root,
            p,
            prev_content.encode("utf-8"),
            tool="apply_patch",
            meta={"mode": fp.kind.value, "new_bytes_blob": new_bytes},
        )
    undo.append(
        _UndoEntry(
            path=p, prev_existed=prev_existed, prev_content=prev_content
        )
    )
    # Atomic write + read-back verify: the file is either the old content or
    # the complete new content, never a half-written truncation, and the bytes
    # on disk are confirmed to equal the PLANNED content. NOTE: verify only
    # vouches "on-disk bytes == planned bytes"; it does NOT vouch that the
    # planned content is itself correct. The planner's line-count conservation
    # likewise only proves no lines were dropped/duplicated — it does NOT prove
    # each hunk landed at the CORRECT location. That correctness is enforced
    # separately in the planner, which now HARD-ERRORS on a relocated-fuzzy
    # match (a hunk that matched only after re-scanning from a different
    # position) instead of silently applying it — see ``_patch_applier``
    # ``MATCH_RELOCATED`` / ``_relocated_msg``. Low-risk in-place whitespace
    # tolerance (rstrip/strip at the expected cursor) still applies freely.
    atomic_write_bytes(p, new_bytes)
    verify_after_write(p, new_bytes)


def _rollback(
    undo: list[_UndoEntry],
) -> list[tuple[Path, str]]:
    """Undo committed file writes in reverse order.

    Returns a list of ``(path, trash_hint)`` for files whose rollback write
    FAILED — surfaced to the caller (State-Truth-First: never pretend a
    rollback succeeded when it did not). An empty list means every file was
    restored. Each restore uses :func:`atomic_write_bytes` so the rollback
    itself cannot half-write.
    """
    failed: list[tuple[Path, str]] = []
    for entry in reversed(undo):
        path = entry.path
        try:
            if entry.move_to is not None:
                # MOVE undo: return to "source in place, target absent".
                # Remove the newly-created target FIRST (if the target write
                # had already succeeded), then restore the source content.
                if entry.move_to.exists():
                    entry.move_to.unlink()
                atomic_write_bytes(
                    path, (entry.prev_content or "").encode("utf-8")
                )
            elif entry.prev_existed:
                atomic_write_bytes(
                    path, (entry.prev_content or "").encode("utf-8")
                )
            else:
                if path.exists():
                    path.unlink()
        except Exception as e:  # noqa: BLE001 — surface, do not swallow
            logger.error(
                "apply_patch rollback FAILED path=%s err=%s — recover the "
                "original from the workspace's .edit_trash/",
                path,
                e,
            )
            failed.append((path, ".edit_trash/"))
    return failed

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem-backed :class:`ImportPort` — full-capability implementation.

Reads model-definition manifests from the filesystem and converts them
into the dry-run / commit / rollback workflow defined by
:class:`qai.app_builder.application.ports.ImportPort`.

Capabilities
------------

* :meth:`dry_run` reads each candidate path as a JSON file containing
  ``{"id": ..., "title": ..., "taxonomy": [...], ...}``; missing /
  malformed files are surfaced as ``ImportAction.SKIP`` with a
  human-readable ``reason``.  **Manifest validation** ensures required
  fields (id, title) are present and well-formed.  **Dependency check**
  verifies that any declared ``required_catalog_ids`` can be resolved
  by the repository.
* :meth:`commit` persists each non-skip item via the wrapped
  :class:`AppModelRepositoryPort` and records the plan in the
  ``app_builder_import_commit`` table for rollback.  Commit is
  **atomic** (uses a staging commit-id that is finalized only on
  success).  On failure, **rollback-on-failure** logic removes any
  half-written rows.  **Post-import smoke test** verifies each
  committed model can be loaded back from the repository.
  **Progress reporting** fires an optional callback after each item.
* :meth:`rollback` reverts the prior commit by deleting any model
  whose ``ImportAction`` was ``ADD`` and restoring (re-saving) any
  model whose ``ImportAction`` was ``REPLACE`` from a snapshot taken
  at commit time.

The implementation is deliberately self-contained — it depends only
on :class:`AppModelRepositoryPort` and the import-commit table — so a
richer adapter can be substituted via the DI graph without touching
wiring.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError
from qai.platform.ids import IdGenerator, new_ulid
from qai.platform.time import Clock

from qai.app_builder.application.ports import AppModelRepositoryPort
from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.errors import (
    AppModelNotFoundError,
    ImportConflictError,
)
from qai.app_builder.domain.import_plan import (
    CommitId,
    ImportAction,
    ImportPlan,
    ImportPlanItem,
)
from qai.app_builder.domain.taxonomy import (
    Taxonomy,
    manifest_taxonomy_segments,
)
from qai.app_builder.domain.value_objects import AppModelId, InputPreset

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

logger = logging.getLogger("qai.app_builder.importer")

__all__ = ["FileSystemAppImportAdapter"]

# ---------------------------------------------------------------------------
# Progress callback type
# ---------------------------------------------------------------------------

#: Signature for the optional progress callback passed to :meth:`commit`.
#: Called with ``(current_index, total_count, model_id_value)`` after each
#: item is processed.  If the callback is an async function it is awaited.
ProgressCallback = Callable[[int, int, str], None | Awaitable[None]]

#: Signature for the optional "pack installed" callback fired AFTER a Pack's
#: files have been physically copied into ``pack_root/<id>/`` during commit.
#: Called with ``(model_id_value, pack_dir)`` where ``pack_dir`` is the
#: freshly-written ``pack_root/<id>`` directory. The composition layer
#: (``apps/api``) uses it to refresh the runtime manifest provider + runner
#: command registry so the just-imported model is immediately runnable
#: (State-Truth-First: the runtime reflects what is really on disk now,
#: instead of a startup-only snapshot). If the callback is async it is awaited.
PackInstalledCallback = Callable[[str, Path], None | Awaitable[None]]

#: 1 MiB floor for a usable context binary (V1 ``importer.dry_run`` §c
#: ``size < 1_048_576``).
_MIN_WEIGHTS_BYTES = 1 * 1024 * 1024

#: Pack files copied verbatim from ``app_pack/`` into ``pack_root/<id>/`` on
#: commit (V1 ``importer._copy_pack_files`` single-file set + dir set).
#:
#: P4 分层修复：``"weights"`` 已被移出 ``_PACK_COPY_DIRS``。V1 layout 只有一个
#: root，pack 目录下的 ``weights/`` 与 ``models/<id>/`` 指同一份文件；P4 拆双
#: root 后，pack 目录（``user_pack_root``）与权重锚点（``user_weights_root``）
#: 分离，仍把 ``weights/`` 随 pack 目录复制会产生**冗余副本**——真值源是
#: ``user_weights_root`` 下 manifest ``installPath`` 解析出的那份（见
#: ``weights_presence.py:102-128``；``FileSystemWeightsPresence.install_path_present``
#: 只按 installPath 锚定到 weights root 探测，从不读取 ``<pack_dir>/weights/``
#: 里的 ``.bin``）。``_stage_weights`` 现在直接从**源 app_pack** 目录读 ``.bin``
#: 拷到 weights root，不再依赖 pack 目录下的 ``weights/`` 副本。
#:
#: 为保留 ``weights_presence.py:130-147`` 的 legacy "present-but-empty" fallback
#: 语义（``pack_weights_dir_is_present_but_empty`` 检查目录存在且为空），
#: ``_install_pack`` 在拷完 pack 元数据后会显式创建**空的**
#: ``<pack_dir>/weights/`` 目录。
_PACK_COPY_FILES = ("manifest.json", "runner.py", "requirements.txt", "SKILL.md")
_PACK_COPY_DIRS = ("examples", "provenance", "assets")


class FileSystemAppImportAdapter:
    """Full-capability filesystem importer (see module docstring)."""

    __slots__ = (
        "_db",
        "_app_models",
        "_clock",
        "_ids",
        "_pack_root",
        "_repo_root",
        "_user_pack_root",
        "_user_weights_root",
        "_on_pack_installed",
    )

    def __init__(
        self,
        *,
        db: "Database",
        app_models: AppModelRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
        pack_root: Path | None = None,
        repo_root: Path | None = None,
        user_pack_root: Path | None = None,
        user_weights_root: Path | None = None,
        on_pack_installed: PackInstalledCallback | None = None,
    ) -> None:
        self._db = db
        self._app_models = app_models
        self._clock = clock
        self._ids = ids
        # ``pack_root`` (``factory/app_builder/models``) is the built-in Pack
        # anchor the runtime manifest provider + runner registry read from;
        # ``repo_root`` anchors manifest ``installPath`` for built-in weights.
        #
        # ``user_pack_root`` (``<data_dir>/app_builder/user_models``) is the
        # DESTINATION for freshly-imported user Packs (P4 分层方案 C:
        # built-in Packs stay in the factory tree — release-contracted — and
        # only user-imported Packs are copied out to writable data storage).
        # ``user_weights_root`` (``<data_dir>/app_builder/user_model_weights``)
        # anchors the manifest ``installPath`` for user Packs; the manifest
        # relative path (``models/<id>/<bin>``) is preserved, only the anchor
        # differs (State-Truth-First §5 铁律 4).
        #
        # When BOTH ``user_pack_root`` and ``user_weights_root`` are wired,
        # commit routes new imports to the user anchor. When either is
        # ``None`` (lean test container / no data_dir), the importer degrades
        # to the legacy behaviour: commits land under ``pack_root`` /
        # ``repo_root``. This preserves the existing test-fixture patterns
        # (built-in-only) while unlocking the P4 dual-root layout in
        # production.
        self._pack_root = pack_root
        self._repo_root = repo_root
        self._user_pack_root = user_pack_root
        self._user_weights_root = user_weights_root
        self._on_pack_installed = on_pack_installed

    # ------------------------------------------------------------------
    # anchor resolution (dual-root — user preferred when configured)
    # ------------------------------------------------------------------
    def _target_pack_root(self) -> Path | None:
        """Anchor a fresh import writes its Pack directory under.

        Prefers ``user_pack_root`` when configured (P4 default) and falls
        back to the legacy built-in ``pack_root`` so existing tests /
        lean containers continue to work.
        """
        if self._user_pack_root is not None:
            return self._user_pack_root
        return self._pack_root

    def _target_weights_root(self) -> Path | None:
        """Anchor a fresh import stages weights under (paired with
        :meth:`_target_pack_root`).
        """
        if self._user_weights_root is not None:
            return self._user_weights_root
        return self._repo_root

    # ------------------------------------------------------------------
    # dry_run
    # ------------------------------------------------------------------
    async def dry_run(self, candidates: Iterable[str]) -> ImportPlan:
        items: list[ImportPlanItem] = []
        seen_ids: set[str] = set()

        for raw in candidates:
            item = await self._inspect_candidate(raw, seen_ids=seen_ids)
            if item is not None:
                seen_ids.add(item.model_id.value)
                items.append(item)

        return ImportPlan(items=tuple(items))

    async def _inspect_candidate(
        self, source: str, *, seen_ids: set[str]
    ) -> ImportPlanItem | None:
        path = Path(source)

        # ── V1 parity (discover_candidates): a WORKDIR source ────────────
        # The promote card passes the session model workdir (e.g.
        # ``C:\WoS_AI\inception_v3``). V1 discovered the ready Pack by
        # reading ``<workdir>\app_pack\_candidate.json`` (packId / displayName
        # / generatedAt). When the source IS such a workdir, that branch OWNS
        # it: we return its result directly — including ``None`` for an
        # already-seen pack (silent dedupe) — WITHOUT falling through to the
        # opaque-hint branch below, which would otherwise synthesise a bogus
        # ``imported-NNNN`` duplicate row for the same workdir.
        if self._is_workdir_candidate(path):
            return await self._inspect_workdir_candidate(path, seen_ids=seen_ids)

        # ── Existing directory that is NOT a ready workdir candidate ─────
        # ``_is_workdir_candidate`` returned False, meaning either the path
        # is not a directory, OR it is a directory that has no
        # ``app_pack/_candidate.json`` yet (the Pack has not been generated).
        # A real on-disk directory in the latter case MUST NOT fall through
        # to the opaque-hint branch below — that branch synthesises a bogus
        # ``imported-NNNN`` ADD row with no reason, and the promote card
        # then falsely shows "校验通过 — 可以导入" and lets the user commit an
        # empty imported model (bug #1). Returning ``None`` drops the row
        # from the plan entirely, so the frontend ``hasCandidates`` gate
        # flips false and the card renders its FIRST stage (workspace /
        # pick-precision / Generate App Builder Pack) — the correct
        # workflow state for a workdir that has not yet been auto-exported.
        # State-Truth-First: "ready to import" reflects the real files on
        # disk; no ``app_pack`` ⇒ nothing to import yet.
        try:
            path_is_existing_dir = path.is_dir()
        except OSError:
            path_is_existing_dir = False
        if path_is_existing_dir:
            logger.info(
                "app_import.workdir_not_ready: source=%s "
                "(no app_pack/_candidate.json — user must generate Pack first)",
                source,
            )
            return None

        if not path.is_file():
            # Non-existent path: treat the source string as an
            # opaque model-id hint (legacy backwards-compat — the
            # PR-034 fake did the same). When the string is a valid
            # AppModelId we use it directly; else we synthesise a
            # deterministic placeholder so the row still surfaces.
            try:
                fallback_id = AppModelId(value=source)
            except ValueError:
                fallback_id = AppModelId(
                    value=f"imported-{len(seen_ids):04d}"
                )
            if fallback_id.value in seen_ids:
                return None
            try:
                await self._app_models.get(fallback_id)
                action = ImportAction.REPLACE
            except AppModelNotFoundError:
                action = ImportAction.ADD
            return ImportPlanItem(
                model_id=fallback_id,
                action=action,
                source=source,
                reason=None,
            )

        # ── file manifest source ────────────────────────────────────────
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            model_id_raw = str(payload.get("id"))
            mid = AppModelId(value=model_id_raw)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            try:
                fallback_id = AppModelId(value=path.stem)
            except ValueError:
                fallback_id = AppModelId(
                    value=f"imported-{len(seen_ids):04d}"
                )
            return ImportPlanItem(
                model_id=fallback_id,
                action=ImportAction.SKIP,
                source=source,
                reason=f"manifest invalid: {exc}",
            )

        # ── manifest validation ────────────────────────────────────────
        validation_errors = _validate_manifest(payload)
        if validation_errors:
            return ImportPlanItem(
                model_id=mid,
                action=ImportAction.SKIP,
                source=source,
                reason=f"manifest validation failed: {'; '.join(validation_errors)}",
            )

        # ── dependency check ───────────────────────────────────────────
        required_catalog_ids = payload.get("required_catalog_ids", [])
        if required_catalog_ids:
            dep_errors = await self._check_dependencies(required_catalog_ids)
            if dep_errors:
                return ImportPlanItem(
                    model_id=mid,
                    action=ImportAction.SKIP,
                    source=source,
                    reason=f"dependency check failed: {'; '.join(dep_errors)}",
                )

        if mid.value in seen_ids:
            return None  # silent dedupe within a single dry_run

        # Determine ADD vs REPLACE by checking the existing repo.
        try:
            await self._app_models.get(mid)
            action = ImportAction.REPLACE
        except AppModelNotFoundError:
            action = ImportAction.ADD

        # V1 parity (importer.discover_candidates): surface presentation-only
        # metadata so the promote card can show a human-readable title + a
        # generation timestamp instead of the bare model id. Both are optional
        # — fall back to the manifest's mtime for the timestamp when the
        # candidate JSON has no explicit ``generatedAt``.
        display_name = payload.get("displayName") or payload.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = None
        generated_at = (
            payload.get("generatedAt") or payload.get("generated_at")
        )
        if not isinstance(generated_at, str) or not generated_at.strip():
            generated_at = _file_mtime_iso(path)

        return ImportPlanItem(
            model_id=mid,
            action=action,
            source=source,
            reason=None,
            display_name=display_name,
            generated_at=generated_at,
        )

    @staticmethod
    def _is_workdir_candidate(path: Path) -> bool:
        """Return ``True`` iff ``path`` is a model workdir holding a ready Pack.

        A workdir candidate is a directory containing
        ``app_pack/_candidate.json`` (V1 ``discover_candidates`` source). The
        caller uses this to decide whether the workdir branch OWNS the source
        (so an already-seen pack dedupes to ``None`` instead of falling through
        to the opaque-hint branch and synthesising a bogus duplicate row).
        """
        try:
            if not path.is_dir():
                return False
        except OSError:
            return False
        return (path / "app_pack" / "_candidate.json").is_file()

    async def _inspect_workdir_candidate(
        self, path: Path, *, seen_ids: set[str]
    ) -> ImportPlanItem | None:
        """Resolve a ``C:\\WoS_AI\\<model>`` workdir to its ready Pack.

        Reads ``<workdir>\\app_pack\\_candidate.json`` (V1
        ``discover_candidates`` source) and builds a plan item carrying the
        rich ``display_name`` / ``generated_at`` metadata. Returns ``None``
        when ``path`` is not a directory or has no ``_candidate.json`` (so the
        caller falls through to the file-manifest / opaque-hint paths).

        Two V1-parity filters keep the promote card on the correct stage
        (V1 ``importer.discover_candidates`` ``backend/app_builder/importer.py``
        lines 92, 104):

        * **``ready`` filter** — a ``_candidate.json`` whose ``ready`` field is
          not truthy is not yet an importable candidate (the Pack export is
          incomplete). We skip it so the promote card keeps showing the
          *workspace / pick-precision* stage instead of jumping to *commit*.
        * **already-imported filter** — V1 used a disk ``app_pack/.imported_at``
          marker. V2 is DB-backed, so the **single source of truth** for "this
          Pack was already imported" is the registry itself: an
          :class:`AppModelDefinition` with the same id and ``user_imported=True``
          (set by :func:`_materialise_from_source` on commit). When the DB says
          the Pack is already a user-imported model we drop it from the plan —
          the stale ``_candidate.json`` left on disk no longer pins the card to
          the commit stage, so re-opening the workdir returns the user to the
          pick-precision stage (State-Truth-First: trust the DB, not a disk
          side-marker that V2 never writes).
        """
        try:
            if not path.is_dir():
                return None
        except OSError:
            return None
        cand_file = path / "app_pack" / "_candidate.json"
        if not cand_file.is_file():
            return None
        try:
            cand = json.loads(cand_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return None
        # ``ready`` filter (V1 importer.py:104) — an incomplete export is not a
        # candidate. Missing field is treated as "ready" for back-compat with
        # candidate files written before this field existed (V1 only checked
        # truthiness of an explicitly-present flag); an explicit falsey value
        # (``false`` / ``0`` / ``""``) is honoured as "not ready".
        if "ready" in cand and not cand.get("ready"):
            return None
        pack_id = cand.get("packId") or cand.get("pack_id")
        if not isinstance(pack_id, str) or not pack_id.strip():
            return None
        try:
            mid = AppModelId(value=pack_id)
        except ValueError:
            return None
        if mid.value in seen_ids:
            return None

        display_name = cand.get("displayName") or cand.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = None
        generated_at = cand.get("generatedAt") or cand.get("generated_at")
        if not isinstance(generated_at, str) or not generated_at.strip():
            generated_at = _file_mtime_iso(cand_file)
        source_workdir = cand.get("sourceWorkdir")
        if not isinstance(source_workdir, str) or not source_workdir.strip():
            source_workdir = str(path / "app_pack")
        pack_dir = path / "app_pack"

        # ── Real validation (V1 importer.dry_run parity) ───────────────────
        # We surface HARD errors (block import) + CONFLICT notes (target id
        # already exists) so the promote card can render ✗ errors / ⚠ conflicts
        # instead of unconditionally showing "✓ validation passed". State-
        # Truth-First: "ready to import" must reflect the real files on disk,
        # not merely "a candidate row exists".
        errors = _validate_pack_dir(pack_dir, cand)

        conflicts: list[str] = []
        suggested_version: str | None = None
        try:
            existing = await self._app_models.get(mid)
        except AppModelNotFoundError:
            action = ImportAction.ADD
        else:
            # A model with this id already exists in the DB (whether a built-in
            # Pack or a previously user-imported one). It is still an importable
            # candidate — re-importing / upgrading is a REPLACE. We deliberately
            # do NOT drop already-imported packs here: after the user re-runs
            # auto-export to regenerate the Pack (`<workdir>/app_pack`), the
            # freshly-generated candidate MUST resurface so the commit card
            # appears and the model can be re-imported (V1 parity — V1 cleared
            # its `.imported_at` marker on regenerate so the candidate came
            # back). The `ready` filter above is what keeps an *incomplete*
            # export off the card; "already imported" is not a reason to hide a
            # ready, regenerated Pack.
            action = ImportAction.REPLACE
            conflicts.append(
                f"model id {mid.value!r} already exists (version "
                f"{existing.version}); choose a conflict policy"
            )
            suggested_version = _bump_patch(existing.version)

        # Hard errors force the row to SKIP (V1: dry_run.ok == False blocks
        # commit). The card still shows the row, but with ✗ errors and the
        # Import button gated by the frontend.
        if errors:
            action = ImportAction.SKIP

        return ImportPlanItem(
            model_id=mid,
            action=action,
            source=source_workdir,
            reason=("; ".join(errors) if errors else None),
            display_name=display_name,
            generated_at=generated_at,
            errors=tuple(errors),
            conflicts=tuple(conflicts),
            suggested_version=suggested_version,
        )

    # ------------------------------------------------------------------
    # commit — atomic with rollback-on-failure + progress + smoke test
    # ------------------------------------------------------------------
    async def commit(
        self,
        plan: ImportPlan,
        *,
        progress: ProgressCallback | None = None,
    ) -> CommitId:
        """Execute an import plan atomically.

        The commit follows a three-phase protocol inspired by the v1
        ``backend/app_builder/importer.py`` transactional import:

        1. **Record phase** — persist the commit row with the plan and
           snapshots of any models to be replaced (for rollback).  This
           row acts as the "staging" marker; its ``committed_at`` field
           is NULL until phase 3.
        2. **Apply phase** — iterate non-skip items, persisting each new
           model via the repository.  After each item the optional
           *progress* callback fires.  If any item fails, all previously
           written items in this commit are rolled back (ADD → delete,
           REPLACE → restore from snapshot).
        3. **Finalise phase** — set ``committed_at`` on the commit row
           (the "atomic rename" — conceptually the staging → live
           transition).  Run a post-import smoke test on each written
           model to verify it can be loaded back.

        Parameters
        ----------
        plan:
            A validated :class:`ImportPlan` (typically from :meth:`dry_run`).
        progress:
            Optional callback ``(current, total, model_id) -> None | Awaitable``.
            Fired after each item is processed (or skipped).
        """
        # Snapshot current models for items we are about to REPLACE so
        # rollback can restore them. The snapshot lives in plan_json so
        # we don't need a second table.
        commit_id = CommitId(value=new_ulid())

        snapshot: list[dict[str, object]] = []
        for item in plan.items:
            if item.action == ImportAction.SKIP:
                continue
            if item.action == ImportAction.REPLACE:
                try:
                    existing = await self._app_models.get(item.model_id)
                    snapshot.append(_model_to_snapshot(existing))
                except AppModelNotFoundError:
                    pass

        plan_json = json.dumps(
            {
                "items": [
                    {
                        "model_id": it.model_id.value,
                        "action": it.action.value,
                        "source": it.source,
                        "reason": it.reason,
                    }
                    for it in plan.items
                ],
                "snapshot": snapshot,
            }
        )

        # Phase 1: Record the commit row (staging — committed_at=NULL).
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(
                        "INSERT INTO app_builder_import_commit "
                        "(id, created_at, plan_json, "
                        "rolled_back_at, rolled_back_reason) "
                        "VALUES (?, ?, ?, NULL, NULL)",
                        (
                            commit_id.value,
                            self._clock.now().isoformat(),
                            plan_json,
                        ),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.import.commit_failed",
                f"failed to record import commit: {exc}",
                operation="import.commit",
                cause=exc,
            ) from exc

        # Phase 2: Apply non-skip items with rollback-on-failure.
        applied_items: list[ImportPlanItem] = []
        # ``installed_dirs`` tracks the ``pack_root/<id>`` dirs we physically
        # wrote so rollback-on-failure can remove them (and so we can fire the
        # refresh callback once the whole commit succeeds).
        installed_dirs: dict[str, Path] = {}
        total = len(plan.items)
        try:
            for idx, item in enumerate(plan.items):
                if item.action == ImportAction.SKIP:
                    await _fire_progress(progress, idx + 1, total, item.model_id.value)
                    continue
                # ── Physical install (V1 importer._copy_pack_files + weights
                # copy): copy the Pack into ``pack_root/<id>/`` and stage its
                # weights under the manifest ``installPath`` anchor so the
                # runtime manifest provider / runner registry / weights probe
                # actually find the model. Determine the version to persist
                # (conflict_policy="bump" → next patch) and rewrite the staged
                # manifest's version so on-disk + DB agree.
                resolved_version = await self._resolve_commit_version(item)
                pack_dir = self._install_pack(item, version=resolved_version)
                # ── Bug D defence: refuse to commit an un-installable Pack ──
                # If the composition wired a Pack anchor (either legacy
                # ``pack_root`` or the P4 ``user_pack_root`` — see
                # ``_target_pack_root``), we are in the real runtime (not the
                # lean DB-only test container). If the item claims a directory
                # source that does NOT hold a real Pack
                # (``_resolve_app_pack_dir`` returned None → no manifest /
                # _candidate anywhere), fail the commit rather than silently
                # writing a DB row for a model whose files never landed on
                # disk. Without this guard the user ends up with an entry
                # that looks imported but has no runnable weights — exactly
                # the "空的 imported-0000" symptom bug 1 exposed and this
                # guard would keep preventing if any other path routes an
                # unexported workdir here again.
                target_pack_root = self._target_pack_root()
                target_weights_root = self._target_weights_root()
                if (
                    pack_dir is None
                    and target_pack_root is not None
                    and target_weights_root is not None
                    and Path(item.source).is_dir()
                    and _resolve_app_pack_dir(Path(item.source)) is None
                ):
                    raise PersistenceError(
                        "app_builder.import.no_pack_files",
                        (
                            f"cannot import {item.model_id.value!r}: source "
                            f"directory {item.source!r} has no exported Pack "
                            f"(no manifest.json / _candidate.json under it or "
                            f"under its 'app_pack/'). Generate the App Builder "
                            f"Pack first."
                        ),
                        operation="import.commit",
                    )
                if pack_dir is not None:
                    installed_dirs[item.model_id.value] = pack_dir
                new_model = _materialise_from_source(
                    item, version=resolved_version
                )
                await _save(self._app_models, new_model)
                applied_items.append(item)
                await _fire_progress(progress, idx + 1, total, item.model_id.value)
        except Exception as apply_exc:
            # ── Rollback on failure ────────────────────────────────────
            logger.error(
                "Import apply failed at item %s; rolling back %d applied items",
                item.model_id.value if item else "?",
                len(applied_items),
            )
            self._rollback_installed_dirs(installed_dirs)
            await self._rollback_applied(applied_items, snapshot)
            # Mark the commit as rolled back due to failure
            try:
                async with self._db.connection() as conn:
                    await conn.execute(
                        "UPDATE app_builder_import_commit "
                        "SET rolled_back_at = ?, rolled_back_reason = ? "
                        "WHERE id = ?",
                        (
                            self._clock.now().isoformat(),
                            f"apply_failed: {apply_exc}",
                            commit_id.value,
                        ),
                    )
                    await conn.commit()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to mark commit %s as rolled back", commit_id.value
                )
            raise PersistenceError(
                "app_builder.import.apply_failed",
                f"import apply failed and was rolled back: {apply_exc}",
                operation="import.commit",
                cause=apply_exc,
            ) from apply_exc

        # Phase 3: Atomic finalise — post-import smoke test verifies
        # each model can be read back from the repository.
        smoke_failures: list[str] = []
        for item in applied_items:
            try:
                await self._app_models.get(item.model_id)
            except AppModelNotFoundError:
                smoke_failures.append(item.model_id.value)

        if smoke_failures:
            logger.error(
                "Post-import smoke test failed for %d models: %s; "
                "rolling back entire commit",
                len(smoke_failures),
                smoke_failures,
            )
            self._rollback_installed_dirs(installed_dirs)
            await self._rollback_applied(applied_items, snapshot)
            try:
                async with self._db.connection() as conn:
                    await conn.execute(
                        "UPDATE app_builder_import_commit "
                        "SET rolled_back_at = ?, rolled_back_reason = ? "
                        "WHERE id = ?",
                        (
                            self._clock.now().isoformat(),
                            f"smoke_test_failed: {smoke_failures}",
                            commit_id.value,
                        ),
                    )
                    await conn.commit()
            except Exception as rollback_exc:  # noqa: BLE001
                # Best-effort audit write: the rollback-marker UPDATE failed,
                # so the import_commit row may not reflect that we rolled the
                # applied items back.  We still raise the primary smoke-test
                # error below (do NOT swallow it); log a warning so the dirty
                # marker state is at least observable.
                logger.warning(
                    "Failed to mark import commit %s as rolled back after "
                    "smoke-test failure (commit row may be stale): %s",
                    commit_id.value,
                    rollback_exc,
                )
            raise PersistenceError(
                "app_builder.import.smoke_test_failed",
                f"post-import smoke test failed for: {smoke_failures}",
                operation="import.commit",
            )

        logger.info(
            "Import commit %s finalised: %d items applied, %d skipped",
            commit_id.value,
            len(applied_items),
            total - len(applied_items),
        )

        # ── Runtime refresh (V1 importer.refresh_after_import parity) ──────
        # The Pack files are now physically under ``pack_root/<id>/``. Fire the
        # composition-layer callback so the manifest provider + runner command
        # registry pick up the new model immediately (State-Truth-First: the
        # runtime reflects what is really on disk, not a startup-only snapshot).
        # Best-effort: a refresh failure is logged but does not fail the commit
        # — the model is already persisted + installed; a restart would also
        # recover it via the startup scan.
        if self._on_pack_installed is not None:
            for mid_value, pack_dir in installed_dirs.items():
                try:
                    await _fire_pack_installed(
                        self._on_pack_installed, mid_value, pack_dir
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "app_import.refresh_failed: id=%s pack_dir=%s: %s",
                        mid_value,
                        pack_dir,
                        exc,
                    )

        return commit_id

    async def _resolve_commit_version(self, item: ImportPlanItem) -> str:
        """Resolve the version to persist for ``item`` honouring conflict policy.

        V1 parity (``importer.commit`` version handling): the imported version
        comes from the Pack ``manifest.json`` (default ``"1.0.0"``); when the
        target already exists AND ``conflict_policy="bump"`` the patch is
        incremented off the EXISTING DB row's version (so re-imports climb
        1.0.0 → 1.0.1 → 1.0.2 …). For ``replace`` / ``cancel`` / a fresh ADD
        we keep the manifest version verbatim.
        """
        manifest_version = _read_manifest_version(Path(item.source))
        if item.conflict_policy != "bump":
            return manifest_version
        try:
            existing = await self._app_models.get(item.model_id)
        except AppModelNotFoundError:
            return manifest_version
        return _bump_patch(existing.version)

    def _install_pack(
        self, item: ImportPlanItem, *, version: str
    ) -> Path | None:
        """Physically install a Pack so the runtime can run it (V1 parity).

        Copies the ``app_pack/`` tree (``Path(item.source)``) into
        ``<target_pack_root>/<id>/`` (manifest / runner / requirements /
        SKILL / examples / provenance / assets / weights) and stages the
        weights ``.bin`` under the manifest ``installPath`` anchor
        (``<target_weights_root>/models/<id>/<bin>``) so:

        * the manifest provider reads ``<target_pack_root>/<id>/manifest.json``;
        * the runner registry finds ``<target_pack_root>/<id>/runner.py``;
        * the weights-presence probe + runner spawn resolve
          ``installPath`` under the paired ``<target_weights_root>``.

        The target roots come from :meth:`_target_pack_root` /
        :meth:`_target_weights_root`, which prefer the P4 user-import
        anchor (``user_pack_root`` / ``user_weights_root``) when
        configured and fall back to the legacy built-in anchor
        (``pack_root`` / ``repo_root``) so existing tests / lean
        containers keep working.

        The staged manifest's ``version`` is rewritten to ``version`` so the
        on-disk manifest agrees with the DB row. Returns the written
        ``<target_pack_root>/<id>`` dir, or ``None`` when target roots are
        not configured (lean test container — DB-only behaviour preserved)
        or the source is not an ``app_pack`` directory.

        Bug D defence-in-depth: when the source directory has no manifest /
        ``_candidate.json`` (``_resolve_app_pack_dir`` returns ``None``) we
        MUST NOT create even an empty ``<target_pack_root>/<id>/`` — an
        earlier bailout branch used to do that and produced the "空的
        imported-0000 目录" users reported. Returning ``None`` here also
        causes commit to raise so the DB row is not written for an
        un-installable source (see :meth:`commit`).

        V1 reference: ``backend/app_builder/importer.py`` ``_copy_pack_files``
        (:935-957) + weights ``shutil.copy2`` (:567/576) + atomic install.
        """
        target_pack_root = self._target_pack_root()
        target_weights_root = self._target_weights_root()
        if target_pack_root is None or target_weights_root is None:
            return None
        src = _resolve_app_pack_dir(Path(item.source))
        if src is None:
            # Opaque-hint / file-manifest source / workdir with no exported
            # Pack — nothing to physically copy. Do NOT create an empty
            # <target_pack_root>/<id>/ dir here (bug D). Let commit decide
            # whether this is a hard error or an acceptable DB-only import.
            return None

        dest = target_pack_root / item.model_id.value
        # Ensure the target anchor dir exists (user_pack_root under data_dir
        # may not have been created yet — first-ever import). Idempotent.
        target_pack_root.mkdir(parents=True, exist_ok=True)
        # Atomic-ish replace: write into a staging dir then swap, mirroring
        # V1's "staging → rename" so a half-copy never becomes the live Pack.
        staging = target_pack_root / f".staging_{item.model_id.value}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            for fname in _PACK_COPY_FILES:
                s = src / fname
                if s.is_file():
                    shutil.copy2(str(s), str(staging / fname))
            for dname in _PACK_COPY_DIRS:
                s = src / dname
                if s.is_dir():
                    shutil.copytree(
                        str(s), str(staging / dname), dirs_exist_ok=True
                    )
            # P4 分层修复：``weights/`` 不再随 pack 目录复制（真值源在
            # ``user_weights_root``；见 ``_PACK_COPY_DIRS`` 顶部注释）。
            # 但 ``weights_presence.py:130-147`` 的 legacy fallback
            # (``pack_weights_dir_is_present_but_empty``) 会检查
            # ``<pack_dir>/weights/`` 目录是否 present-but-empty，所以这里
            # 显式创建**空目录**保留该 fallback 语义。
            (staging / "weights").mkdir(exist_ok=True)

            # Rewrite the staged manifest's version so disk == DB.
            staged_manifest = staging / "manifest.json"
            if staged_manifest.is_file():
                try:
                    data = json.loads(
                        staged_manifest.read_text(encoding="utf-8")
                    )
                    if isinstance(data, dict):
                        data["version"] = version
                        staged_manifest.write_text(
                            json.dumps(data, indent=2, ensure_ascii=False)
                            + "\n",
                            encoding="utf-8",
                        )
                except (OSError, json.JSONDecodeError, ValueError):
                    pass

            # Swap staging → live. 缺陷 N — restorable REPLACE:
            # Prior version did ``shutil.rmtree(dest, ignore_errors=True)``
            # BEFORE the ``staging.rename(dest)``, so on Windows a rename
            # failure (target file open in another process / AV lock)
            # left the user with dest DELETED but staging unpromoted; the
            # existing DB row still pointed at the vanished pack, and the
            # previously-working model became silently unrunnable. The
            # fix: rename the existing dest to a sibling ``.old_<ts>`` FIRST
            # (an atomic move on the same volume), then rename staging → dest;
            # only after that succeeds do we rmtree the old copy. If the
            # staging → dest rename raises, we restore the old copy so the
            # user's previously-working pack survives.
            old_backup: Path | None = None
            if dest.exists():
                old_backup = target_pack_root / (
                    f".old_{item.model_id.value}_{new_ulid()}"
                )
                try:
                    dest.rename(old_backup)
                except OSError:
                    # Cannot even move the old dir out of the way (extremely
                    # rare — same-volume rename fails). Fall back to the
                    # legacy rmtree; better than aborting the whole commit
                    # since the user asked for REPLACE. If the subsequent
                    # rename also fails they will get a raise + rollback.
                    shutil.rmtree(dest, ignore_errors=True)
                    old_backup = None
            try:
                staging.rename(dest)
            except OSError:
                # Restore the previously-working pack before propagating.
                if old_backup is not None and old_backup.exists():
                    try:
                        old_backup.rename(dest)
                    except OSError:
                        logger.error(
                            "app_import.install_restore_failed: id=%s "
                            "old=%s dest=%s (previously-working pack lost)",
                            item.model_id.value,
                            old_backup,
                            dest,
                        )
                raise
            # New dest is in place — the old backup is now safe to remove.
            if old_backup is not None:
                shutil.rmtree(old_backup, ignore_errors=True)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # Stage weights under the manifest installPath anchor so the runtime
        # weights probe + runner spawn resolve real files. installPath is
        # ``models/<id>/<bin>`` anchored at the target weights root
        # (repo_root for built-in Packs, user_weights_root for user imports).
        # 缺陷 O rollback: ``_stage_weights`` now raises on missing / failed
        # bin copies (State-Truth-First — no假报成功). Since we already
        # promoted ``staging → dest`` above, we must undo that promotion
        # before propagating the raise, so ``commit()``'s outer rollback
        # does not see a half-installed <target_pack_root>/<id>/ dir. The
        # commit loop hasn't yet added this item to ``installed_dirs`` (that
        # happens on the very next line after ``_install_pack`` returns),
        # so its own ``_rollback_installed_dirs`` cannot clean this dir up.
        try:
            self._stage_weights(
                item.model_id.value,
                dest,
                weights_src_dir=src / "weights",
                weights_root=target_weights_root,
            )
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return dest

    def _stage_weights(
        self,
        model_id: str,
        pack_dir: Path,
        *,
        weights_src_dir: Path,
        weights_root: Path,
    ) -> None:
        """Copy each variant's ``.bin`` to its manifest ``installPath`` anchor.

        Reads the just-written ``pack_dir/manifest.json`` for the install
        path(s) (top-level ``assets.installPath`` + each ``variants[].assets.
        installPath``) and copies the corresponding ``.bin`` from
        ``weights_src_dir`` (原始 ``app_pack/weights/``, 见下) to
        ``<weights_root>/<installPath>`` (V1 copied
        weights to ``models/<id>/``; the manifest ``installPath`` is exactly
        that ``models/<id>/<bin>`` so we honour it verbatim). ``weights_root``
        is:

        * ``repo_root`` for built-in Packs (legacy layout);
        * ``user_weights_root`` for user-imported Packs (P4 layout).

        P4 分层修复：源目录 ``weights_src_dir`` 指向**原始 app_pack** 的
        ``weights/``（``_install_pack`` 传入 ``src / "weights"``），而不是
        pack_dir 下的副本——``_PACK_COPY_DIRS`` 已剥离 ``"weights"``，pack_dir
        下不再有可读的 ``.bin`` 副本（只有空目录以保 legacy fallback 语义）。
        真值源统一到 ``weights_root`` 下。

        缺陷 O — hard-fail semantics (State-Truth-First 铁律 3):

        Prior version silently ``logger.warning``-ed on missing source bins
        and on ``OSError`` during ``shutil.copy2`` and returned successfully,
        so ``commit()`` produced HTTP 201 + a DB row for a model whose
        weights had never landed on disk. The user then saw "导入成功" but hit
        ``WEIGHTS_NOT_INSTALLED`` at runtime. We now raise :class:`PersistenceError`
        for either failure mode — the commit's ``except`` clause will roll
        back the DB row and remove the pack_root dir, so the UI reflects
        reality: a failed import is a failed import.

        A dry-run ``_validate_pack_dir`` covers the "missing source bin"
        case at validate time (its ``_missing_variant_weights`` scan lists
        every ``installPath`` whose ``.bin`` is absent). This runtime raise
        is defence-in-depth for races (someone deleted the bin between
        validate and commit) and for the ``OSError`` copy path (disk full /
        permission denied).
        """
        manifest_path = pack_dir / "manifest.json"
        if not manifest_path.is_file():
            return
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        # ``weights_src_dir`` 来自参数（原始 app_pack/weights/），见 docstring。
        install_paths: list[str] = []
        assets = data.get("assets")
        if isinstance(assets, dict):
            ip = assets.get("installPath")
            if isinstance(ip, str) and ip:
                install_paths.append(ip)
        variants = data.get("variants")
        if isinstance(variants, list):
            for v in variants:
                if not isinstance(v, dict):
                    continue
                v_assets = v.get("assets")
                if isinstance(v_assets, dict):
                    ip = v_assets.get("installPath")
                    if isinstance(ip, str) and ip:
                        install_paths.append(ip)
        for ip in dict.fromkeys(install_paths):  # de-dup, preserve order
            rel = Path(ip)
            dest = (
                rel if rel.is_absolute() else (weights_root / rel)
            )
            bin_name = dest.name
            src_bin = weights_src_dir / bin_name
            if not src_bin.is_file():
                raise PersistenceError(
                    "app_builder.import.weights_missing",
                    (
                        f"cannot stage weights for {model_id!r}: "
                        f"{bin_name} not found under {weights_src_dir} "
                        f"(installPath={ip!r})"
                    ),
                    operation="import.commit",
                )
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src_bin), str(dest))
            except OSError as exc:
                raise PersistenceError(
                    "app_builder.import.weights_copy_failed",
                    (
                        f"failed to copy weights for {model_id!r}: "
                        f"{src_bin} -> {dest}: {exc}"
                    ),
                    operation="import.commit",
                    cause=exc,
                ) from exc

    def _rollback_installed_dirs(self, installed_dirs: dict[str, Path]) -> None:
        """Remove ``pack_root/<id>`` dirs written during a failed commit.

        Best-effort cleanup so a failed commit does not leave orphan Pack
        directories that a subsequent startup scan would resurrect. Weights
        copied under ``repo_root/models/<id>/`` are left in place (harmless;
        the DB row that referenced them is rolled back, and a re-import
        overwrites them).
        """
        for mid_value, pack_dir in installed_dirs.items():
            try:
                if pack_dir.is_dir():
                    shutil.rmtree(pack_dir, ignore_errors=True)
            except OSError as exc:
                logger.warning(
                    "app_import.install_rollback_failed: id=%s dir=%s: %s",
                    mid_value,
                    pack_dir,
                    exc,
                )

    async def _rollback_applied(
        self,
        applied_items: list[ImportPlanItem],
        snapshot: list[dict[str, object]],
    ) -> None:
        """Undo previously applied items during a failed commit.

        ADD items are deleted; REPLACE items are restored from snapshot.
        Errors are logged but do not propagate (best-effort cleanup).
        """
        snapshot_map = {
            str(entry["id"]): entry for entry in snapshot
        }
        for item in applied_items:
            try:
                if item.action == ImportAction.ADD:
                    await self._app_models.delete(item.model_id)
                elif item.action == ImportAction.REPLACE:
                    snap = snapshot_map.get(item.model_id.value)
                    if snap is not None:
                        await _save(
                            self._app_models, _model_from_snapshot(snap)
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Rollback-on-failure: could not revert %s: %s",
                    item.model_id.value,
                    exc,
                )

    async def _check_dependencies(
        self, required_catalog_ids: list[object]
    ) -> list[str]:
        """Verify that required catalog model IDs exist in the repo.

        Returns a list of error strings for missing dependencies.
        An empty list means all dependencies are satisfied.
        """
        errors: list[str] = []
        for raw_id in required_catalog_ids:
            dep_id_str = str(raw_id)
            try:
                dep_id = AppModelId(value=dep_id_str)
            except ValueError:
                errors.append(f"invalid dependency id: {dep_id_str!r}")
                continue
            try:
                await self._app_models.get(dep_id)
            except AppModelNotFoundError:
                errors.append(f"missing dependency: {dep_id_str}")
        return errors

    # ------------------------------------------------------------------
    # rollback
    # ------------------------------------------------------------------
    async def rollback(self, commit_id: CommitId) -> None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT plan_json, rolled_back_at "
                    "FROM app_builder_import_commit WHERE id = ?",
                    (commit_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.import.rollback_failed",
                f"failed to load import commit: {exc}",
                operation="import.rollback",
                cause=exc,
            ) from exc
        if row is None:
            raise ImportConflictError(
                message=f"unknown commit_id {commit_id}",
                details={"commit_id": str(commit_id)},
            )
        if row[1] is not None:
            raise ImportConflictError(
                message=f"commit_id {commit_id} already rolled back",
                details={"commit_id": str(commit_id)},
            )

        payload = json.loads(str(row[0]))
        items = payload.get("items", [])
        snapshot = {
            entry["id"]: entry
            for entry in payload.get("snapshot", [])
        }

        # Apply rollback: ADD → delete; REPLACE → restore from snapshot.
        for raw in items:
            action = raw["action"]
            mid = AppModelId(value=raw["model_id"])
            if action == ImportAction.ADD.value:
                try:
                    await self._app_models.delete(mid)
                except AppModelNotFoundError:
                    pass
            elif action == ImportAction.REPLACE.value:
                snap = snapshot.get(mid.value)
                if snap is not None:
                    await _save(
                        self._app_models, _model_from_snapshot(snap)
                    )

        # Mark commit as rolled back.
        try:
            async with self._db.connection() as conn:
                await conn.execute(
                    "UPDATE app_builder_import_commit "
                    "SET rolled_back_at = ?, rolled_back_reason = ? "
                    "WHERE id = ?",
                    (
                        self._clock.now().isoformat(),
                        "user_rollback",
                        commit_id.value,
                    ),
                )
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.import.rollback_finalise_failed",
                f"failed to finalise rollback: {exc}",
                operation="import.rollback",
                cause=exc,
            ) from exc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _materialise_from_source(
    item: ImportPlanItem, *, version: str = "1.0.0"
) -> AppModelDefinition:
    """Build an :class:`AppModelDefinition` from a plan item.

    For PR-045 we read the ``source`` JSON file (if it exists) and
    fall back to a minimal definition matching the model id otherwise.

    V1 parity (workdir candidates): when ``source`` points at an
    ``app_pack`` directory (the dry-run resolved a workdir to its ready
    Pack via ``_inspect_workdir_candidate``), read ``manifest.json`` inside
    that directory for the App Builder model definition (title / taxonomy /
    presets). Falls back to the model_id title when the manifest is absent /
    invalid (commit still succeeds with degraded metadata).

    ``version`` is the resolved semver to persist (manifest version, or a
    bumped patch under ``conflict_policy="bump"`` — see
    :meth:`FileSystemAppImportAdapter._resolve_commit_version`).
    """
    path = Path(item.source)
    if path.is_dir():
        # Resolve the actual ``app_pack`` dir: the workdir candidate's
        # ``source`` is the WORKDIR (``_candidate.json:sourceWorkdir`` =
        # ``str(workspace.workdir)``), so the manifest lives at
        # ``<workdir>/app_pack/manifest.json`` — not ``<workdir>/manifest.json``.
        # ``_resolve_app_pack_dir`` returns whichever of those holds the
        # manifest (preferring ``app_pack/``).
        app_pack = _resolve_app_pack_dir(path)
        manifest_in_dir = (
            (app_pack / "manifest.json") if app_pack is not None else None
        )
        if manifest_in_dir is not None and manifest_in_dir.is_file():
            path = manifest_in_dir
        else:
            # Workdir/app_pack source without a manifest.json: commit still
            # succeeds via the model_id-title fallback below, but title /
            # taxonomy are lost. Log it so "imported model lost its title /
            # taxonomy" is diagnosable rather than silent.
            logger.warning(
                "app_import.materialise_missing_manifest: source=%s id=%s "
                "(falling back to model_id title)",
                item.source,
                item.model_id.value,
            )
    title = item.model_id.value
    # Prefer the candidate's display_name when present (V1 parity).
    if item.display_name:
        title = item.display_name
    taxonomy_segments: tuple[str, ...] = ()
    enabled = True
    pinned = False
    presets: tuple[InputPreset, ...] = ()
    catalog_ids: tuple[str, ...] = ()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            title = str(data.get("title", data.get("displayName", title)))
            # Shared parser handles BOTH the object form emitted by
            # ModelBuilder export (``taxonomy: {group, task, tags}``) and the
            # legacy list form (``taxonomy: [seg, ...]``) + ``category``
            # fallback — single source of truth with the built-in seed
            # (``apps.api.lifespan._manifest_taxonomy_segments``). Iterating a
            # dict here used to yield its KEYS ("group"/"task"/"tags"), storing
            # a bogus path that matched no task id → imported models never
            # surfaced under their taxonomy task.
            taxonomy_segments = manifest_taxonomy_segments(data)
            # ``enabled`` / ``pinned`` are NOT manifest fields — the export
            # manifest never emits them, and the built-in seed leaves them at
            # their defaults too. Defaulting enabled=True / pinned=False is the
            # correct, intended semantics (a freshly-imported model is runnable
            # and unpinned), NOT a missed mapping. ``data.get(..., default)``
            # keeps forward-compat if a future manifest adds them.
            enabled = bool(data.get("enabled", True))
            pinned = bool(data.get("pinned", False))
            # Input presets: the ModelBuilder export manifest (and the built-in
            # Pack manifests) carry these under ``examples`` as
            # ``[{name, inputs, params}]`` (see model_builder
            # ``_manifest_builder.build_manifest_dict`` + factory packs). The
            # DB ``input_presets`` channel stores ``[{name, payload}]``; we map
            # each example into a preset whose payload merges its ``inputs`` +
            # ``params`` so the two channels (DB presets ↔ manifest examples)
            # stay consistent. A legacy ``input_presets`` key, if ever present,
            # is honoured first.
            presets = _presets_from_manifest(data)
            catalog_ids = tuple(
                str(c) for c in data.get("required_catalog_ids", [])
            )
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    return AppModelDefinition(
        id=item.model_id,
        title=title,
        taxonomy=Taxonomy(segments=taxonomy_segments),
        enabled=enabled,
        pinned=pinned,
        input_presets=presets,
        required_catalog_ids=catalog_ids,
        user_imported=True,
        version=version,
    )


def _presets_from_manifest(data: dict) -> tuple[InputPreset, ...]:
    """Map a Pack manifest's preset/example rows to ``InputPreset`` tuples.

    Precedence (first non-empty wins):

    1. ``input_presets`` — legacy DB-shaped ``[{name, payload}]`` (honoured
       verbatim if a manifest ever carries it);
    2. ``examples`` — the real ModelBuilder-export / built-in shape
       ``[{name, inputs, params}]``; mapped to a preset whose ``payload``
       merges ``inputs`` + ``params`` (the runner reads both).

    Duplicate / blank names are de-duplicated (``AppModelDefinition`` forbids
    duplicate preset names) preserving first occurrence.
    """
    rows: list[InputPreset] = []
    seen: set[str] = set()

    def _add(name: str, payload: dict[str, object]) -> None:
        clean = name.strip() if isinstance(name, str) else ""
        if not clean or clean in seen:
            return
        seen.add(clean)
        rows.append(InputPreset(name=clean, payload=payload))

    legacy = data.get("input_presets")
    if isinstance(legacy, list) and legacy:
        for p in legacy:
            if isinstance(p, dict):
                _add(
                    str(p.get("name", "default")),
                    dict(p.get("payload", {})),
                )
        if rows:
            return tuple(rows)

    examples = data.get("examples")
    if isinstance(examples, list):
        for i, ex in enumerate(examples):
            if not isinstance(ex, dict):
                continue
            payload: dict[str, object] = {}
            inputs = ex.get("inputs")
            if isinstance(inputs, dict):
                payload.update(inputs)
            params = ex.get("params")
            if isinstance(params, dict):
                # Namespace params so they don't collide with input keys; the
                # runner request envelope splits inputs vs params anyway.
                payload["params"] = dict(params)
            _add(str(ex.get("name", f"example-{i + 1}")), payload)
    return tuple(rows)


def _model_to_snapshot(model: AppModelDefinition) -> dict[str, object]:
    return {
        "id": model.id.value,
        "title": model.title,
        "taxonomy": list(model.taxonomy.segments),
        "enabled": model.enabled,
        "pinned": model.pinned,
        "input_presets": [
            {"name": p.name, "payload": p.payload}
            for p in model.input_presets
        ],
        "required_catalog_ids": list(model.required_catalog_ids),
        "user_imported": model.user_imported,
        "version": model.version,
    }


def _model_from_snapshot(snap: dict[str, object]) -> AppModelDefinition:
    presets_data = snap.get("input_presets", [])
    if not isinstance(presets_data, list):
        presets_data = []
    catalog_ids = snap.get("required_catalog_ids", [])
    if not isinstance(catalog_ids, list):
        catalog_ids = []
    taxonomy_segments = snap.get("taxonomy", [])
    if not isinstance(taxonomy_segments, list):
        taxonomy_segments = []
    return AppModelDefinition(
        id=AppModelId(value=str(snap["id"])),
        title=str(snap.get("title", str(snap["id"]))),
        taxonomy=Taxonomy(segments=tuple(str(s) for s in taxonomy_segments)),
        enabled=bool(snap.get("enabled", True)),
        pinned=bool(snap.get("pinned", False)),
        input_presets=tuple(
            InputPreset(
                name=str(p.get("name", "default")),
                payload=dict(p.get("payload", {})),
            )
            for p in presets_data
        ),
        required_catalog_ids=tuple(str(c) for c in catalog_ids),
        user_imported=bool(snap.get("user_imported", False)),
        version=str(snap.get("version", "1.0.0")) or "1.0.0",
    )


async def _save(
    repo: AppModelRepositoryPort, model: AppModelDefinition
) -> None:
    """Save via either ``save`` (added by SqliteAppModelRepository) or
    fall back to ``add`` if the port surface has been extended.

    The :class:`AppModelRepositoryPort` Protocol does not currently
    advertise ``save``; we duck-type through to the concrete adapter
    here. Test fakes that want to verify import-side writes should
    expose a similarly-shaped method.
    """
    save_fn = getattr(repo, "save", None)
    if save_fn is None:
        raise PersistenceError(
            "app_builder.import.repo_no_save",
            "AppModelRepositoryPort implementation lacks save()",
            operation="import.commit",
        )
    await save_fn(model)


# ---------------------------------------------------------------------------
# Manifest validation (capability: manifest_validation)
# ---------------------------------------------------------------------------


def _validate_manifest(payload: dict) -> list[str]:
    """Validate a manifest dict has required fields and correct types.

    Returns a list of error strings; empty means valid.  Mirrors the v1
    ``dry_run`` validation that checked for ``modelId``, ``displayName``,
    ``category``, ``version``, ``runtime``, ``runner`` etc.  Adapted for
    the v2 schema where the minimal required fields are ``id`` and
    ``title``.
    """
    errors: list[str] = []

    # Required fields
    model_id = payload.get("id")
    if not model_id:
        errors.append("missing required field 'id'")
    elif not isinstance(model_id, str):
        errors.append("field 'id' must be a string")

    title = payload.get("title")
    if not title:
        errors.append("missing required field 'title'")
    elif not isinstance(title, str):
        errors.append("field 'title' must be a string")

    # Taxonomy, if present, must be a list of strings
    taxonomy = payload.get("taxonomy")
    if taxonomy is not None:
        if not isinstance(taxonomy, list):
            errors.append("field 'taxonomy' must be a list")
        elif not all(isinstance(s, str) for s in taxonomy):
            errors.append("field 'taxonomy' must contain only strings")

    # input_presets, if present, must be a list of dicts with 'name'
    presets = payload.get("input_presets")
    if presets is not None:
        if not isinstance(presets, list):
            errors.append("field 'input_presets' must be a list")
        else:
            for i, p in enumerate(presets):
                if not isinstance(p, dict):
                    errors.append(f"input_presets[{i}] must be a dict")
                elif "name" not in p:
                    errors.append(f"input_presets[{i}] missing 'name'")

    # required_catalog_ids, if present, must be a list of strings
    deps = payload.get("required_catalog_ids")
    if deps is not None:
        if not isinstance(deps, list):
            errors.append("field 'required_catalog_ids' must be a list")
        elif not all(isinstance(d, str) for d in deps):
            errors.append("field 'required_catalog_ids' must contain only strings")

    return errors


# ---------------------------------------------------------------------------
# Progress callback helper
# ---------------------------------------------------------------------------


async def _fire_progress(
    callback: ProgressCallback | None,
    current: int,
    total: int,
    model_id: str,
) -> None:
    """Invoke the optional progress callback, awaiting if it's async."""
    if callback is None:
        return
    import asyncio as _asyncio

    result = callback(current, total, model_id)
    if _asyncio.iscoroutine(result):
        await result


async def _fire_pack_installed(
    callback: PackInstalledCallback,
    model_id: str,
    pack_dir: Path,
) -> None:
    """Invoke the optional pack-installed callback, awaiting if it's async."""
    import asyncio as _asyncio

    result = callback(model_id, pack_dir)
    if _asyncio.iscoroutine(result):
        await result


# ---------------------------------------------------------------------------
# Pack validation + version helpers (V1 importer.dry_run / _bump_patch parity)
# ---------------------------------------------------------------------------


_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _bump_patch(version_str: str) -> str:
    """Increment the patch component of a semver string (V1 ``_bump_patch``).

    ``"1.0.0" -> "1.0.1"``; a non-semver input degrades to ``"1.0.1"``
    (matches ``backend/app_builder/importer.py:_bump_patch``).
    """
    m = _SEMVER_RE.match(version_str or "")
    if not m:
        return "1.0.1"
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{major}.{minor}.{patch + 1}"


def _resolve_app_pack_dir(source: Path) -> Path | None:
    """Resolve the ``app_pack`` directory holding the Pack files for ``source``.

    The workdir candidate's ``source`` is the WORKDIR (the
    ``_candidate.json:sourceWorkdir`` field = ``str(workspace.workdir)``),
    whose Pack files live under ``<workdir>/app_pack/``. But ``source`` may
    already BE the ``app_pack`` dir (older candidate files set
    ``sourceWorkdir`` to the app_pack path). Returns whichever directory
    contains ``manifest.json`` — preferring ``<source>/app_pack`` — or
    ``None`` when ``source`` is not a directory / has no Pack anywhere.

    Strictness rule (State-Truth-First — bug D): we ONLY return a directory
    that carries at least one of ``manifest.json`` / ``_candidate.json``.
    Returning ``source`` or ``nested`` blindly (as an older bailout branch
    did) caused ``_install_pack`` to create an empty ``pack_root/<id>/``
    directory when the source workdir had no exported Pack — the exact
    "空的 imported-0000 目录" symptom users reported. If there is nothing
    to copy, return ``None`` so the caller skips the physical install
    entirely; the DB row would then be an obviously-broken import which
    the caller can also guard against.
    """
    try:
        if not source.is_dir():
            return None
    except OSError:
        return None
    nested = source / "app_pack"
    if (nested / "manifest.json").is_file() or (
        nested / "_candidate.json"
    ).is_file():
        return nested
    if (source / "manifest.json").is_file() or (
        source / "_candidate.json"
    ).is_file():
        return source
    return None


def _read_manifest_version(source: Path) -> str:
    """Read ``manifest.json:version`` from a Pack source, default 1.0.0.

    Accepts either an ``app_pack`` dir, a workdir (resolved to its
    ``app_pack``), or a direct manifest file path.
    """
    if source.is_dir():
        app_pack = _resolve_app_pack_dir(source)
        manifest = (
            (app_pack / "manifest.json")
            if app_pack is not None
            else (source / "manifest.json")
        )
    else:
        manifest = source
    if not manifest.is_file():
        return "1.0.0"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return "1.0.0"
    if isinstance(data, dict):
        v = data.get("version")
        if isinstance(v, str) and v.strip():
            return v
    return "1.0.0"


def _validate_pack_dir(pack_dir: Path, candidate: dict) -> list[str]:
    """Validate a ready ``app_pack/`` for importability (V1 dry_run parity).

    Returns a list of HARD error strings (empty = importable). Mirrors the
    V1 ``importer.dry_run`` blocking checks that are meaningful without the
    QAIRT runtime:

    * manifest.json present, parseable, has required ``modelId``+``displayName``;
    * runner.py present + ``py_compile``-clean (V1 §e);
    * default weights ``.bin`` present + >= 1 MiB (V1 §c). Weights are located
      under ``pack_dir/weights/`` (the export staged them there) or via the
      candidate's ``weightsAbsPath`` (V1 fallback).

    Soft notes (provenance / io_contract) are intentionally NOT promoted to
    hard errors here — V1 surfaced them as warnings, and the QAIRT-dependent
    smoke test cannot run in every environment.
    """
    import py_compile

    errors: list[str] = []

    # ── manifest.json ──────────────────────────────────────────────────
    manifest_path = pack_dir / "manifest.json"
    manifest: dict | None = None
    if not manifest_path.is_file():
        errors.append("manifest.json not found in app_pack")
    else:
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                errors.append("manifest.json root is not a JSON object")
            else:
                manifest = loaded
                for fld in ("modelId", "displayName"):
                    if not manifest.get(fld):
                        errors.append(
                            f"manifest.json missing required field {fld!r}"
                        )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"manifest.json read/parse error: {exc}")

    # ── runner.py present + compiles (V1 §e) ───────────────────────────
    runner_path = pack_dir / "runner.py"
    if not runner_path.is_file():
        errors.append("runner.py not found in app_pack")
    else:
        try:
            py_compile.compile(str(runner_path), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"runner.py compilation failed: {exc}")
        except OSError as exc:
            errors.append(f"runner.py read error: {exc}")

    # ── default weights present + >= 1 MiB (V1 §c) ─────────────────────
    weights_file = _locate_default_weights(pack_dir, candidate, manifest)
    if weights_file is None:
        errors.append("default weights .bin not found in app_pack")
    else:
        try:
            size = weights_file.stat().st_size
        except OSError as exc:
            errors.append(f"weights stat error: {exc}")
        else:
            if size < _MIN_WEIGHTS_BYTES:
                errors.append(
                    f"weights too small ({size} bytes; expected >= "
                    f"{_MIN_WEIGHTS_BYTES})"
                )

    # ── EVERY declared variant's weights must be present (缺陷 O) ───────
    # State-Truth-First: a multi-variant Pack that lists 3 variants but only
    # ships 2 ``.bin`` files under ``app_pack/weights/`` is NOT ready to
    # import. The old dry_run only checked the DEFAULT variant, so the two
    # non-default variants would silently be missing from the copied Pack
    # and the user would see "导入成功" but hit ``WEIGHTS_NOT_INSTALLED`` at
    # runtime for those variants. Surface each missing bin as a hard error
    # so the ✗ line lists them and the Import button is gated.
    if isinstance(manifest, dict):
        weights_dir = pack_dir / "weights"
        for missing in _missing_variant_weights(manifest, weights_dir):
            errors.append(missing)

    return errors


def _missing_variant_weights(manifest: dict, weights_dir: Path) -> list[str]:
    """Enumerate hard-error strings for every declared variant whose weights
    file is absent under ``pack_dir/weights/`` or below the 1 MiB floor.

    Reads both the top-level ``assets.installPath`` (single-variant Packs)
    and each ``variants[i].assets.installPath`` (multi-variant Packs). A
    variant with no ``installPath`` is skipped (nothing to validate). The
    returned strings are safe to concatenate into the ``errors`` list
    returned by :func:`_validate_pack_dir`.
    """
    errors: list[str] = []
    install_paths: list[str] = []
    top_assets = manifest.get("assets")
    if isinstance(top_assets, dict):
        ip = top_assets.get("installPath")
        if isinstance(ip, str) and ip:
            install_paths.append(ip)
    variants = manifest.get("variants")
    if isinstance(variants, list):
        for v in variants:
            if not isinstance(v, dict):
                continue
            v_assets = v.get("assets")
            if isinstance(v_assets, dict):
                ip = v_assets.get("installPath")
                if isinstance(ip, str) and ip:
                    install_paths.append(ip)
    for ip in dict.fromkeys(install_paths):  # dedupe, preserve order
        bin_name = Path(ip).name
        cand = weights_dir / bin_name
        if not cand.is_file():
            errors.append(
                f"variant weights missing: {bin_name} "
                f"(expected under app_pack/weights/ for installPath={ip!r})"
            )
            continue
        try:
            size = cand.stat().st_size
        except OSError as exc:
            errors.append(f"variant weights stat error for {bin_name}: {exc}")
            continue
        if size < _MIN_WEIGHTS_BYTES:
            errors.append(
                f"variant weights too small: {bin_name} ({size} bytes; "
                f"expected >= {_MIN_WEIGHTS_BYTES})"
            )
    return errors


def _locate_default_weights(
    pack_dir: Path, candidate: dict, manifest: dict | None
) -> Path | None:
    """Find the default variant's weights ``.bin`` for size validation.

    Priority (mirrors how the export staged + the importer copies weights):

    1. ``pack_dir/weights/<assets.installPath basename>`` (the export stages
       every variant's bin under ``app_pack/weights/``);
    2. candidate ``weightsAbsPath`` (V1 legacy single-variant fallback —
       absolute path to the source ``.bin``);
    3. any single ``.bin`` under ``pack_dir/weights/``.
    """
    weights_dir = pack_dir / "weights"
    # 1. manifest installPath basename under weights/
    if isinstance(manifest, dict):
        assets = manifest.get("assets")
        if isinstance(assets, dict):
            ip = assets.get("installPath")
            if isinstance(ip, str) and ip:
                cand = weights_dir / Path(ip).name
                if cand.is_file():
                    return cand
    # 2. candidate weightsAbsPath
    wap = candidate.get("weightsAbsPath")
    if isinstance(wap, str) and wap:
        p = Path(wap)
        if p.is_file():
            return p
    # 3. any NPU weight in weights/ (.bin QNN context binary OR .dlc QNN DLC —
    #    the app_pack contract is format-neutral; QNNContext loads either).
    #    Probe .bin first (deterministic), then .dlc.
    if weights_dir.is_dir():
        entries = sorted(weights_dir.iterdir())
        for ext in (".bin", ".dlc"):
            for f in entries:
                try:
                    if f.is_file() and f.suffix.lower() == ext:
                        return f
                except OSError:
                    continue
    return None


# Suppress unused-import warning for `datetime` (kept for typing context).
_ = datetime


def _file_mtime_iso(path: Path) -> str | None:
    """Return ``path``'s mtime as an ISO-8601 UTC string, or ``None``.

    V1 parity: the promote candidate card shows a generation timestamp.
    When a candidate manifest has no explicit ``generatedAt`` field we fall
    back to the file's modification time (mirrors V1's ``discover_candidates``
    using the on-disk mtime). Best-effort — returns ``None`` if the file is
    gone / unreadable.
    """
    from datetime import timezone

    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

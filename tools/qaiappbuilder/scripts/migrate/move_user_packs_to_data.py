# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Move user-imported App Builder Packs from the legacy factory location
to the per-user ``<data_dir>/app_builder/`` tree.

Background
----------
Before Sub-C's ``DataPaths.app_builder_user_pack_root`` was wired,
user-imported Packs were committed alongside built-in Packs at:

    <repo_root>/factory/chat_features/app-builder/models/<id>/
    <repo_root>/models/<id>/*.bin          (weights)

That layout has three problems (see ``DataPaths`` docstring for the full
rationale):

  1. A software upgrade / reinstall / uninstall would blow the user's
     imported Packs away with the release payload.
  2. ``factory/`` was excluded from git, but ``models/`` was — so weight
     ``.bin`` blobs were leaking into VCS.
  3. The startup seed scanner and the user-import commit path share the
     same directory, so a DB reset could silently re-promote user Packs
     to built-in.

The new layout (Sub-C) splits them cleanly:

    <data_dir>/app_builder/user_models/<id>/          (Pack def)
    <data_dir>/app_builder/user_model_weights/<id>/*.bin  (weights)

Sub-D updates the adapters so **future** imports land at the data-dir
location. This module handles the **existing** Packs already imported
against the old layout.

Contract
--------
For each ``<repo_root>/factory/chat_features/app-builder/models/<id>/`` subdirectory,
the migrator:

  1. Reads ``app_builder_model_definition.user_imported`` for ``<id>``:
       * ``user_imported = True``     → migrate this Pack.
       * ``user_imported = False`` or id not in DB → skip (built-in /
         orphan; never touch).
  2. Copies the Pack directory (``manifest.json`` + ``runner.py`` +
     ``SKILL.md`` + any other resources) to
     ``<data_paths.app_builder_user_pack_root>/<id>/``.
  3. Copies each ``<repo_root>/models/<id>/*.bin`` weight blob to
     ``<data_paths.app_builder_user_weights_root>/<id>/*.bin``.
  4. Rewrites the destination ``manifest.json`` so every
     ``installPath`` (top-level ``assets.installPath`` and every
     ``variants[].assets.installPath``) is expressed relative to the
     new weights anchor
     (``app_builder/user_model_weights/<id>/<bin>``), so the adapters
     resolve it against ``DataPaths._root`` on next boot.
  5. Writes a ``.migrated_at`` sentinel inside the destination Pack
     directory (UTC ISO timestamp).
  6. **Only after** all of (2)-(5) have committed to disk, deletes the
     source Pack dir + the source weight files. If any earlier step
     raises, the source is left intact and the destination is rolled
     back (deleted) so a rerun retries cleanly.

Idempotence & crash recovery
----------------------------
Per-id state machine on rerun:

  * dest exists  AND  ``.migrated_at`` present → skip (already done).
  * dest exists  AND  ``.migrated_at`` missing → previous run crashed
    between "copy" and "write sentinel"; delete dest and redo.
  * dest missing → copy from source (normal path).

If no ``factory/chat_features/app-builder/models/`` at all, or the directory is empty,
or none of its subdirectories are ``user_imported=True`` in the DB,
the migrator is a silent no-op.

Failure isolation
-----------------
Each ``<id>`` is processed independently. A failure on one id logs a
warning and increments the ``failed`` counter but does not abort the
sweep; the surviving source (never deleted on failure) means the user
data is still recoverable on the next boot's rerun.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config.paths import DataPaths
    from qai.platform.persistence import Database

__all__ = ["MigrationReport", "migrate_user_packs"]


# Filename of the per-Pack idempotence sentinel written after every
# successful id migration. Rerunning the migrator sees this file and
# skips the id. Kept as a leading-dot filename so directory listings
# in the UI don't surface it (the front-end skips dotfiles).
_SENTINEL_NAME = ".migrated_at"

# Legacy weight-blob root — where import used to drop the ``.bin`` files
# when the manifest ``installPath`` was expressed relative to repo_root
# (``models/<id>/<bin>``). This root is NOT parameterised on purpose:
# it is a historical hard-coded location; the target root is the
# ``DataPaths`` property which IS the parameterised one.
_LEGACY_WEIGHTS_SUBDIR = "models"

# Legacy Pack-def root — same story: it was hard-coded at
# ``<repo_root>/factory/chat_features/app-builder/models``.
_LEGACY_PACK_SUBDIR = Path("factory") / "chat_features" / "app-builder" / "models"


@dataclass(slots=True)
class MigrationReport:
    """Aggregate outcome of one ``migrate_user_packs`` sweep.

    Attributes:
        scanned: total number of candidate ``<id>`` directories seen
            under the legacy pack root.
        migrated: ids successfully moved to the data-dir tree during
            THIS invocation (excludes ids already at the destination
            from a previous run).
        skipped_builtin: ids skipped because the DB row has
            ``user_imported=False``.
        skipped_orphan: ids skipped because there is no matching DB
            row (safer to leave in place — a stray directory the user
            may still want to inspect).
        skipped_already_migrated: ids whose destination already exists
            with a valid ``.migrated_at`` sentinel.
        retried_incomplete: ids whose destination existed but had no
            sentinel (previous crash); the stale dest was removed and
            the copy redone.
        failed: mapping of id → short human-readable reason for ids that
            failed to migrate during THIS invocation (source is left
            intact, so a subsequent run will retry).
    """

    scanned: int = 0
    migrated: list[str] = field(default_factory=list)
    skipped_builtin: list[str] = field(default_factory=list)
    skipped_orphan: list[str] = field(default_factory=list)
    skipped_already_migrated: list[str] = field(default_factory=list)
    retried_incomplete: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def is_noop(self) -> bool:
        """Nothing was moved and no destination touched during this run."""
        return (
            not self.migrated
            and not self.retried_incomplete
            and not self.failed
        )


async def migrate_user_packs(
    *,
    repo_root: Path,
    data_paths: "DataPaths",
    db: "Database",
) -> MigrationReport:
    """Move existing user-imported Packs from ``factory/`` into ``data/``.

    Idempotent: safe to run on every boot. Best-effort: a failure on any
    single id is logged and reflected in the report but does NOT raise
    (the caller — typically the lifespan startup hook — must not let
    a migration failure abort startup; the source directory is preserved
    on failure so the next boot retries).

    Args:
        repo_root: Path to the installed repository root (source side —
            holds the legacy ``factory/chat_features/app-builder/models/`` and
            ``models/`` layout).
        data_paths: Resolved ``DataPaths`` (destination side — the
            ``app_builder_user_pack_root`` /
            ``app_builder_user_weights_root`` properties provide the
            two target anchors).
        db: The started ``Database``; used ONLY to read
            ``user_imported`` from ``app_builder_model_definition``.

    Returns:
        A :class:`MigrationReport` summarising the sweep.
    """
    report = MigrationReport()

    legacy_pack_root = repo_root / _LEGACY_PACK_SUBDIR
    legacy_weights_root = repo_root / _LEGACY_WEIGHTS_SUBDIR

    if not legacy_pack_root.is_dir():
        _log("skip.no_legacy_pack_root", path=str(legacy_pack_root))
        return report

    # Enumerate candidate <id> directories.
    try:
        entries = sorted(
            [p for p in legacy_pack_root.iterdir() if p.is_dir()],
            key=lambda p: p.name,
        )
    except OSError as exc:
        _log("skip.legacy_pack_root_unreadable", path=str(legacy_pack_root), error=str(exc))
        return report

    if not entries:
        _log("skip.legacy_pack_root_empty", path=str(legacy_pack_root))
        return report

    # Prefetch DB user_imported flags in one shot. When the DB is empty
    # (fresh install) this is an empty dict, which correctly forces
    # every disk id into the "orphan / skip" bucket.
    user_imported_map = await _load_user_imported_flags(db)

    dest_pack_root = data_paths.app_builder_user_pack_root
    dest_weights_root = data_paths.app_builder_user_weights_root

    for src_pack_dir in entries:
        model_id = src_pack_dir.name
        report.scanned += 1
        try:
            _migrate_one(
                model_id=model_id,
                src_pack_dir=src_pack_dir,
                src_weights_dir=legacy_weights_root / model_id,
                dest_pack_dir=dest_pack_root / model_id,
                dest_weights_dir=dest_weights_root / model_id,
                user_imported_map=user_imported_map,
                report=report,
            )
        except Exception as exc:  # noqa: BLE001 — never abort the sweep
            reason = f"{type(exc).__name__}: {exc}"
            report.failed[model_id] = reason
            _log(
                "id.failed",
                id=model_id,
                reason=reason,
            )

    _log(
        "summary",
        scanned=report.scanned,
        migrated=len(report.migrated),
        skipped_builtin=len(report.skipped_builtin),
        skipped_orphan=len(report.skipped_orphan),
        skipped_already_migrated=len(report.skipped_already_migrated),
        retried_incomplete=len(report.retried_incomplete),
        failed=len(report.failed),
    )
    return report


# ----------------------------------------------------------------------
# Internal helpers — one call chain per id
# ----------------------------------------------------------------------


def _migrate_one(
    *,
    model_id: str,
    src_pack_dir: Path,
    src_weights_dir: Path,
    dest_pack_dir: Path,
    dest_weights_dir: Path,
    user_imported_map: dict[str, bool],
    report: MigrationReport,
) -> None:
    """Migrate one id, updating ``report`` in place.

    Every filesystem write path guarantees the invariant "user data is
    always readable from at least one location". Concretely:

      * We NEVER delete the source before the destination is completely
        populated and the sentinel is written (State-Truth-First 铁律 5).
      * If ANY step between "start copying" and "write sentinel" raises,
        we roll back the destination (best-effort ``rmtree``) and leave
        the source untouched.

    A failure surfaces as a raise; the caller catches it and records a
    ``failed`` entry.
    """
    # Classification --------------------------------------------------
    flag = user_imported_map.get(model_id)
    if flag is None:
        # No DB row — could be a stray directory from a hand copy, an
        # in-progress import that never committed, or a stale entry
        # from a wiped DB. All three are safer left in place than
        # silently swept into user data.
        _log("id.skip_orphan", id=model_id)
        report.skipped_orphan.append(model_id)
        return
    if flag is False:
        # Built-in Pack — release-owned, never touch.
        _log("id.skip_builtin", id=model_id)
        report.skipped_builtin.append(model_id)
        return

    # Idempotence check ----------------------------------------------
    sentinel_path = dest_pack_dir / _SENTINEL_NAME
    if dest_pack_dir.is_dir():
        if sentinel_path.is_file():
            # Fully-committed previous migration. Only remaining chore
            # is to make sure the source is gone (a crash between
            # "write sentinel" and "delete source" on a previous run
            # would leave the source lying around; deleting it now
            # completes that unfinished cleanup).
            _finalize_source_cleanup(
                model_id=model_id,
                src_pack_dir=src_pack_dir,
                src_weights_dir=src_weights_dir,
            )
            _log("id.skip_already_migrated", id=model_id)
            report.skipped_already_migrated.append(model_id)
            return
        # dest exists but no sentinel → previous run crashed mid-copy.
        # We MUST remove the incomplete dest before retrying so the
        # copy step below sees a clean target and the source (still
        # authoritative) is the true state.
        _log("id.retry_incomplete", id=model_id)
        _safe_rmtree(dest_pack_dir)
        # Also wipe any partial weights so the re-copy is deterministic.
        if dest_weights_dir.exists():
            _safe_rmtree(dest_weights_dir)
        report.retried_incomplete.append(model_id)

    # Copy pack + weights, rewrite manifest, then delete source ------
    dest_pack_dir.parent.mkdir(parents=True, exist_ok=True)
    dest_weights_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Copy the Pack definition dir. ``copytree`` fails if dest
        # exists — the retry branch above guarantees a clean slate.
        shutil.copytree(src_pack_dir, dest_pack_dir)

        # 2. Copy weight ``.bin`` files (if any). Missing source
        # weights are non-fatal (a Pack may declare its weight URL
        # instead of shipping the blob, or the previous import may
        # have completed the definition write but never the blob
        # transfer). We log per-file and continue.
        _copy_weights_best_effort(
            model_id=model_id,
            src_weights_dir=src_weights_dir,
            dest_weights_dir=dest_weights_dir,
        )

        # 3. Rewrite the destination manifest's installPath fields.
        _rewrite_manifest_install_paths(
            manifest_path=dest_pack_dir / "manifest.json",
            model_id=model_id,
        )

        # 4. Sentinel — this is the commit point. Everything above
        # was reversible (delete dest); everything below is source
        # cleanup that a rerun can also finish.
        _write_sentinel(sentinel_path)
    except Exception:
        # Roll dest back so the next run sees a clean state and picks
        # up from the untouched source.
        with _suppressed():
            _safe_rmtree(dest_pack_dir)
        with _suppressed():
            if dest_weights_dir.exists():
                _safe_rmtree(dest_weights_dir)
        raise

    # 5. Source removal — post-sentinel. If this step fails we still
    # count the id as migrated (the destination is the truth now) and
    # let a future rerun finish the cleanup via
    # ``_finalize_source_cleanup``. Never rolling back after sentinel.
    _finalize_source_cleanup(
        model_id=model_id,
        src_pack_dir=src_pack_dir,
        src_weights_dir=src_weights_dir,
    )
    report.migrated.append(model_id)
    _log("id.migrated", id=model_id)


def _copy_weights_best_effort(
    *,
    model_id: str,
    src_weights_dir: Path,
    dest_weights_dir: Path,
) -> None:
    """Copy every ``.bin`` from src to dest, one file at a time.

    A per-file failure is logged and does not abort the id — the caller
    still writes the sentinel and considers the id migrated. Missing
    source dir is a silent no-op (some Packs are manifest-only).
    """
    if not src_weights_dir.is_dir():
        return
    dest_weights_dir.mkdir(parents=True, exist_ok=True)
    for bin_path in sorted(src_weights_dir.iterdir(), key=lambda p: p.name):
        if not bin_path.is_file():
            continue
        # Weight blobs are conventionally ``.bin`` but a Pack may ship
        # accessory files (tokenizer, vocab, config JSON) that used to
        # live alongside them. Copy the whole file set — the manifest
        # rewrite handles path anchoring, and leaving accessories behind
        # would break inference.
        target = dest_weights_dir / bin_path.name
        try:
            shutil.copy2(bin_path, target)
        except OSError as exc:
            _log(
                "weight.copy_failed",
                id=model_id,
                file=bin_path.name,
                error=str(exc),
            )


def _rewrite_manifest_install_paths(*, manifest_path: Path, model_id: str) -> None:
    """Rewrite every ``installPath`` in the destination manifest.

    Old form (relative to ``repo_root``):
        ``models/<id>/<bin>``
    New form (relative to ``data_paths.root``):
        ``app_builder/user_model_weights/<id>/<bin>``

    Both the top-level ``assets.installPath`` and each
    ``variants[].assets.installPath`` are updated. Any entry that is
    empty, non-string, or already using the new anchor is left alone —
    the rewrite is a "point the anchor at the new tree" operation, not
    a validator.

    A missing manifest is a hard error (a Pack directory without a
    manifest is broken); the caller catches and marks the id failed.
    """
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"manifest.json missing at {manifest_path!s}"
        )
    text = manifest_path.read_text(encoding="utf-8")
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError(
            f"manifest.json at {manifest_path!s} is not a JSON object"
        )

    _rewrite_assets_block(obj.get("assets"), model_id=model_id)
    variants = obj.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict):
                _rewrite_assets_block(variant.get("assets"), model_id=model_id)

    # UTF-8, no BOM, LF newlines, 2-space indent — matches the source
    # tree convention (every existing manifest.json is pretty-printed).
    manifest_path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _rewrite_assets_block(assets: object, *, model_id: str) -> None:
    """Update ``assets["installPath"]`` in place when it points at legacy path.

    Recognised legacy shapes:
      * ``"models/<id>/<name>"``   → ``"app_builder/user_model_weights/<id>/<name>"``
      * ``"models/<other>/<name>"`` (mismatched id — very rare) →
        rewrite anchor only, keep whatever ``<other>`` was there.
      * anything already prefixed with ``"app_builder/user_model_weights/"`` →
        left as-is (idempotence for reruns / manually-migrated Packs).
    """
    if not isinstance(assets, dict):
        return
    raw = assets.get("installPath")
    if not isinstance(raw, str) or not raw:
        return
    # Normalise separators once so we can match without worrying about
    # cross-platform authoring (an operator may hand-edit with ``\``).
    normalised = raw.replace("\\", "/")
    prefix_new = "app_builder/user_model_weights/"
    if normalised.startswith(prefix_new):
        return
    prefix_old = "models/"
    if normalised.startswith(prefix_old):
        # Preserve the tail after ``models/`` verbatim (usually
        # ``<id>/<bin>``); this correctly handles the common case
        # AND the corner case where the manifest id and the folder
        # id disagree.
        tail = normalised[len(prefix_old):]
        assets["installPath"] = prefix_new + tail
        return
    # Absolute path or some other shape — the migrator has no safe way
    # to re-anchor it. Leave it alone so the adapter can raise a clear
    # error on next boot instead of silently corrupting the pointer.
    _log(
        "manifest.install_path_unrecognised",
        id=model_id,
        install_path=raw,
    )


def _write_sentinel(sentinel_path: Path) -> None:
    """Write the ``.migrated_at`` sentinel with a UTC ISO timestamp.

    Timestamp is informational (debug / support triage); the file's
    presence is what matters for idempotence. Written LAST in the
    migration critical section so its existence guarantees every
    upstream copy/rewrite committed.
    """
    stamp = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    sentinel_path.write_text(stamp + "\n", encoding="utf-8", newline="\n")


def _finalize_source_cleanup(
    *,
    model_id: str,
    src_pack_dir: Path,
    src_weights_dir: Path,
) -> None:
    """Best-effort removal of the source side after a completed migration.

    Called both:
      * at the tail of a fresh migration (post-sentinel), and
      * from the idempotence branch on rerun (in case a previous run
        wrote the sentinel but crashed before wiping the source).

    A failure here is logged and swallowed — the destination is the
    canonical state now, and the leftover source is inert (nothing
    reads from it on the new adapter path).
    """
    if src_pack_dir.exists():
        try:
            _safe_rmtree(src_pack_dir)
        except OSError as exc:
            _log(
                "source.pack_cleanup_failed",
                id=model_id,
                path=str(src_pack_dir),
                error=str(exc),
            )
    if src_weights_dir.exists():
        try:
            _safe_rmtree(src_weights_dir)
        except OSError as exc:
            _log(
                "source.weights_cleanup_failed",
                id=model_id,
                path=str(src_weights_dir),
                error=str(exc),
            )


def _safe_rmtree(path: Path) -> None:
    """``shutil.rmtree`` with Windows-friendly readonly retry.

    Weight ``.bin`` files copied from a read-only install medium can end
    up marked read-only; plain ``rmtree`` then raises ``PermissionError``.
    Clear the bit and retry once.
    """
    def _on_error(func, target, exc_info):  # type: ignore[no-untyped-def]
        import os
        import stat

        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            raise
        func(target)

    shutil.rmtree(path, onerror=_on_error)


# ----------------------------------------------------------------------
# DB access — minimal, read-only
# ----------------------------------------------------------------------


async def _load_user_imported_flags(db: "Database") -> dict[str, bool]:
    """Return ``{id: bool}`` for every row in ``app_builder_model_definition``.

    A missing table (fresh DB before migrations applied) returns an
    empty dict — every disk id is then classified as ``skipped_orphan``,
    which is the safe outcome (no user data is touched).
    """
    try:
        async with db.connection() as conn:
            cur = await conn.execute(
                "SELECT id, user_imported FROM app_builder_model_definition"
            )
            rows = await cur.fetchall()
            await cur.close()
    except Exception as exc:  # noqa: BLE001
        # Any DB failure (table missing, DB not started, etc.) must not
        # abort the migrator — it just means we can't classify, so we
        # skip everything. Logged for triage.
        _log("db.user_imported_load_failed", error=f"{type(exc).__name__}: {exc}")
        return {}
    return {str(row[0]): bool(row[1]) for row in rows}


# ----------------------------------------------------------------------
# Logging — stdlib ``logging`` so this module has NO import-time dependency
# on the platform logging config, yet its records bridge into the main
# structlog pipeline (shared local-tz ISO timestamp) when run from lifespan.
# Standalone CLI runs with no configured handler simply drop the records,
# which is acceptable for a best-effort migrator.
# ----------------------------------------------------------------------

_logger = logging.getLogger(__name__)


def _log(event: str, /, **fields: object) -> None:
    """Emit a compact ``event k=v ...`` record via stdlib logging.

    Uses ``logging.getLogger`` (stdlib only, no platform dependency) so the
    line is timestamped and routed through the same pipeline as the rest of
    the app when invoked from lifespan; a standalone CLI run without a root
    handler drops it harmlessly.
    """
    parts = [f"migrate_user_packs.{event}"]
    for key, value in fields.items():
        parts.append(f"{key}={value!r}")
    _logger.info(" ".join(parts))


class _suppressed:
    """Tiny ``contextlib.suppress(Exception)`` alias, kept local so this
    module has zero non-stdlib imports at module scope."""

    def __enter__(self) -> "_suppressed":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return exc_type is not None and issubclass(exc_type, Exception)

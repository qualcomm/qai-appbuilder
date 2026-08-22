# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""One-shot data migration: QAI Service host + model-list upgrade.

Background
----------
Two related changes shipped together in the ``bugfix/qai-service-integration``
branch:

  1. The QAI Service deployment moved off ``14.103.220.205:8012``. Both
     editions moved, at different times and onto different addresses — see
     "Edition split" below.
  2. Its exposed model list collapsed from three per-vendor entries
     (``qai-service::anthropic::claude-4-6-sonnet`` /
     ``qai-service::azure::gpt-5.5`` /
     ``qai-service::vertexai::gemini-3.5-flash``) to a single pseudo-model
     ``qai-service::route-1`` that the broker itself routes.

The out-of-tree ``factory/`` seed carries the model-list change, and now also
the current external address. But ``factory/`` is consumed only by
``seed_defaults`` at **first install** (via ``INSERT OR IGNORE``): an existing
user who merely ``git pull``s the new code keeps the three defunct models AND
the old address in their live ``data/db/qai.db``, and the UI still shows them.

The host is a second, independent axis. ``factory/`` seeds the EXTERNAL
address (it ships to external), so:

* on the **external** edition this migrator is what carries an EXISTING
  install from the legacy address onto the current one seeded in ``factory/``;
  a fresh external install is already correct and the ``LIKE`` guards below
  make the host steps a no-op on it.
* on the **internal** edition it additionally applies the intranet override —
  on an upgrade AND on a fresh install, whose seeded row holds the external
  address that the ``LIKE`` guards match on the very first boot.

Either way the override lives on this always-runs path rather than in the
``[cloud_providers.*]`` install-seed path, which is idempotent on "row already
exists" and would always lose to ``seed_defaults``.

This migrator runs on every lifespan startup (best-effort, non-fatal) and
brings the runtime state forward, guarded so an operator who has already
customized the values manually is not clobbered.

Scope (see docs/PLAN in this branch for the full inventory)
-----------------------------------------------------------
1. ``data/db/qai.db`` — the ``kv_user_prefs['model_catalog.provider.qai-service']``
   row is rewritten to the new base_url + single-``route-1`` model list.
2. ``data/db/qai.db`` — three ``model_catalog_entry`` rows are deleted and
   one ``qai-service::route-1`` row is upserted.
3. ``data/db/qai.db`` — any UI-preference row that still names one of the
   deleted model ids is cleared to the empty string (front end falls back
   to default). Covers ``ui.selected_model_id``,
   ``ui.selected_service_model``, and the ``selected_model_id`` embedded
   in ``ui.preferences``.
4. ``data/user_config.toml`` — the two legacy ``base_url`` literals
   (``[forge.qai_service]`` bare origin + ``[forge.cloud_providers.qai-service]``
   ``/v1`` variant) are rewritten via text-level find-and-replace so the
   file's Chinese comments survive verbatim.
5. QAI Service session JWT (``SecretStore["qai.service.session"]["jwt"]``)
   is cleared so the next login exchanges a fresh JWT against the new
   host. Without this the app would carry an ~8 h bearer bound to the old
   pool and every chat request would 401.

Guards
------
- Every SQLite mutation carries an ``AND ... LIKE '%14.103.220.205%'``
  guard so an operator who has already retargeted the pool to a private
  broker (or a fork with a different address) is left alone.
- The TOML rewrite only replaces the exact ``http://14.103.220.205:8012``
  literal (both the bare and ``/v1`` variants). If either variant is
  absent the write is skipped.
- The two host-only steps (TOML rewrite, JWT clear) additionally
  short-circuit when the target host equals the legacy one. That case no longer
  arises on either edition (both moved off the legacy address), but the check
  is kept: it is what makes a fork that pins the target back to the legacy
  address a clean no-op instead of a pointless rewrite + sign-out.
- The JWT clear is further gated on this run having actually changed
  host-bearing state (``kv_updates or toml_rewritten``). The ``_v2`` id bump
  re-runs the migrator on every install, and without that gate a fresh install
  — or one a previous run already carried over — would be signed out of a pool
  whose address it is already on.
- A sentinel ``_qai_data_migrations`` row (id
  ``qai_service_route1_2026_08_v2``) marks the migration complete; the fast
  path on subsequent boots is a single SELECT.

Idempotence
-----------
- Sentinel row is written only after every step succeeds.
- If any step fails mid-way, the sentinel is NOT written and the next
  boot retries. The SQLite LIKE guards ensure the retry is a no-op on
  the already-migrated rows.
- ``clear_service_jwt()`` is itself idempotent (missing JWT → no-op).

Environment-variable interaction
--------------------------------
``QAI_QAI_SERVICE__BASE_URL`` overrides the TOML value at
``load_settings`` time (env > TOML > defaults). The migration still
rewrites the TOML on-disk so a future unset-of-env falls back to a
sensible value — the env override still wins for this process.

Edition split — which host this migrates TO
-------------------------------------------
Both editions moved off ``14.103.220.205:8012``, but onto DIFFERENT addresses,
so the target host is resolved per edition by :func:`_target_host`:

* **external** → :data:`_EXTERNAL_HOST`, a literal in this file. It is the
  public broker address and ships in ``factory/`` anyway, so there is nothing
  to hide.
* **internal** → ``[qai_service] origin`` from ``internal_config.toml`` (a file
  inside the ``qai.platform.edition`` package, physically excluded from
  external artifacts via ``scripts/release/manifest.toml [exclude]``) behind
  the usual ``settings.is_internal`` gate. That address is an intranet host and
  is never a literal in this file.

Consequences:

* On an **internal** build the migration retargets to the intranet host as
  intended.
* On an **external** build the edition package is absent (and the gate is
  False anyway), :func:`_target_host` returns :data:`_EXTERNAL_HOST`, and
  because that address now DIFFERS from ``_OLD_HOST`` the host rewrites are
  live: an existing install has its stored address, its ``user_config.toml``
  and its stale JWT carried over to the current broker. A fresh external
  install is already seeded correct, so the ``LIKE`` guards make those same
  steps a no-op on it. The model-list collapse (three per-vendor entries →
  ``route-1``) applies on both editions — that part of the change is
  edition-independent.

The ``factory/`` seeds therefore carry the EXTERNAL address, which is what
makes them safe to ship. The intranet address exists in exactly one file in
the tree, and that file never leaves an internal build.

check_release.py sensitive-word scan
------------------------------------
Only the EXTERNAL address appears as a literal in this file. The intranet
host is read at runtime from the edition-excluded TOML and never written
here, so this module — which ships under both editions — carries no
internal-network literal for the external release scan to find. The
intranet host is additionally listed in
``scripts.release.check_release.SENSITIVE_KEYWORDS`` as the layer-4
backstop, so a future re-hardcoding of it anywhere fails the release.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config.paths import DataPaths
    from qai.platform.persistence import Database

__all__ = ["MigrationReport", "migrate_qai_service_route1"]


# ---------------------------------------------------------------------
# Constants — the exact legacy → new mapping for this one-shot migration.
# ---------------------------------------------------------------------

#: Sentinel id recorded in ``_qai_data_migrations`` when the migration
#: completes end-to-end. The date suffix marks the release cutover; a
#: future host swap picks a NEW id rather than editing this one.
#:
#: ``_v2`` bump: the EXTERNAL QAI Service address moved off the original
#: ``14.103.220.205:8012`` onto :data:`_EXTERNAL_HOST`. Installs that already
#: ran the original ``qai_service_route1_2026_08`` hold its sentinel, so the
#: fast-path would skip them forever and leave them pinned to the dead legacy
#: address. Bumping the id re-runs the migrator exactly once per install, which
#: is what carries the existing population onto the new host. The old
#: ``_2026_08`` row is left in place (harmless; no longer read).
_MIGRATION_ID = "qai_service_route1_2026_08_v2"

_OLD_HOST = "14.103.220.205:8012"

#: The external-edition QAI Service host. This address MOVED: the broker that
#: used to answer on :data:`_OLD_HOST` now answers here. Kept as a module
#: constant (not a literal at each use site) so the two editions read
#: symmetrically below, and so the legacy → current mapping this migration
#: implements is stated in exactly one place.
_EXTERNAL_HOST = "qai-service.qualcomm.com:8012"


def _target_host() -> str:
    """Return the QAI Service ``host:port`` this edition should point at.

    Both deployments moved off :data:`_OLD_HOST`, onto different addresses: the
    external broker to :data:`_EXTERNAL_HOST` (a literal here — it is the
    public address and ships in ``factory/`` regardless), the internal one to
    an intranet host that is never a literal in this file. The latter is read
    from ``[qai_service] origin`` in ``internal_config.toml`` via
    :func:`qai.platform.edition.loader.get_qai_service_origin`, whose package
    is physically excluded from external artifacts
    (``scripts/release/manifest.toml [exclude]``).

    That indirection is the point, not a style choice. This module ships under
    BOTH editions, so an internal-network literal here would (a) travel into
    the external artifact and (b) have to be added to
    ``check_release.SENSITIVE_KEYWORDS`` — which would then fail the external
    release on this very file. Reading it from the edition-excluded TOML keeps
    the literal out of every shipped source file, exactly as
    :class:`~qai.platform.config.settings.QaiServiceSettings` documents for
    ``base_url``.

    Two independent conditions must both hold before we use the intranet host:

    * ``settings.is_internal`` — the layer-3 runtime gate every other consumer
      of this package uses. Without it a source checkout of the internal tree
      that was *built* as external (or a mispackaged artifact that kept the
      directory) would silently rewrite users onto an unreachable intranet
      host. Note the gate is not redundant with the import guard below: it is
      the check that survives a packaging mistake.
    * a non-empty, parseable ``origin``.

    Falls back to :data:`_EXTERNAL_HOST` otherwise. On an external artifact
    that IS the correct target, so the migration retargets a legacy install
    onto the current public broker; a config fault on an internal tree degrades
    to the same public address rather than to the dead legacy one.
    """
    try:
        from qai.platform.config.settings import load_settings

        if not load_settings().is_internal:
            return _EXTERNAL_HOST
    except Exception:  # noqa: BLE001 — a config fault must not retarget anyone
        return _EXTERNAL_HOST
    try:
        from qai.platform.edition.loader import get_qai_service_origin

        origin = get_qai_service_origin()
    except Exception:  # noqa: BLE001 — external artifact has no edition pkg
        return _EXTERNAL_HOST
    if not origin:
        return _EXTERNAL_HOST
    # Reduce the origin ("http://host:port") to "host:port" so the callers
    # below can compose both the bare and the /v1 form themselves.
    from urllib.parse import urlparse

    try:
        netloc = urlparse(origin).netloc
    except ValueError:
        return _EXTERNAL_HOST
    return netloc or _EXTERNAL_HOST


#: Resolved lazily (see :func:`_new_host`), never at import: ``load_settings``
#: is not safe to call at module-import time from a migrator that lifespan
#: imports inside a try block, and the tests need to vary the edition per case.
_NEW_HOST_CACHE: str | None = None


def _new_host() -> str:
    """Memoised :func:`_target_host`.

    Resolved once per process: the value cannot change while the app runs, and
    the migrator reads it at several points in one invocation.
    """
    global _NEW_HOST_CACHE  # noqa: PLW0603 — module-level memo, single writer
    if _NEW_HOST_CACHE is None:
        _NEW_HOST_CACHE = _target_host()
    return _NEW_HOST_CACHE

#: The three qualified model ids that ``factory/_source/cloud_models.json``
#: previously listed under provider ``qai-service``. All three are
#: removed and replaced by ``qai-service::route-1``.
_OLD_MODEL_IDS: tuple[str, ...] = (
    "qai-service::anthropic::claude-4-6-sonnet",
    "qai-service::azure::gpt-5.5",
    "qai-service::vertexai::gemini-3.5-flash",
)

#: The single ``qai-service`` model post-migration. Shape mirrors what the
#: factory compiler emits for ``kv_user_prefs['model_catalog.provider.qai-service']``
#: — see tools/build/factory_compiler/cloud_models.py.
_NEW_MODEL_ENTRY: dict[str, object] = {
    "api_model_id": "route-1",
    "context_length": 1_000_000,
    "description": "QAI Service 智能路由（自动选择底层模型）",
    "model_id": "qai-service::route-1",
    "name": "Route 1",
    "supports_streaming": True,
}

def _new_kv_value_json() -> str:
    """``kv_user_prefs['model_catalog.provider.qai-service'].value_json``.

    ``sort_keys=True`` gives deterministic bytes; the shape matches the
    hand-authored ``factory/db_staging/kv_user_prefs.jsonl`` row so a migrated
    install and a fresh one converge on identical content.
    """
    return json.dumps(
        {
            "base_url": f"http://{_new_host()}/v1",
            "models": [_NEW_MODEL_ENTRY],
            "pinned": False,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# ---------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------


@dataclass(slots=True)
class MigrationReport:
    """Aggregate outcome of one ``migrate_qai_service_route1`` invocation.

    Attributes:
        already_applied: ``True`` when the sentinel was present on entry
            and no work was attempted. Fast-path indicator.
        kv_updates: rows changed in ``kv_user_prefs`` for the qai-service
            provider row.
        mc_deletes: rows removed from ``model_catalog_entry`` (one per
            legacy model id that was still present).
        mc_upserts: rows inserted into ``model_catalog_entry`` for
            ``qai-service::route-1`` (1 on migrate, 0 when already there).
        ui_clears: UI-preference rows whose ``selected_*`` value pointed
            at a now-deleted id and got cleared.
        toml_rewritten: whether ``data/user_config.toml`` was updated on
            disk (False when no legacy literal was present).
        jwt_cleared: whether ``clear_service_jwt()`` returned normally.
        failed_step: name of the FIRST step that raised, if any. When
            set the sentinel is NOT written and the next boot retries.
    """

    already_applied: bool = False
    kv_updates: int = 0
    mc_deletes: int = 0
    mc_upserts: int = 0
    ui_clears: int = 0
    toml_rewritten: bool = False
    jwt_cleared: bool = False
    failed_step: str | None = None
    applied: bool = False

    def is_noop(self) -> bool:
        """Nothing observable happened during this run."""
        if self.already_applied:
            return True
        return (
            self.kv_updates == 0
            and self.mc_deletes == 0
            and self.mc_upserts == 0
            and self.ui_clears == 0
            and not self.toml_rewritten
            and not self.jwt_cleared
            and self.failed_step is None
        )


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------


async def migrate_qai_service_route1(
    *,
    repo_root: Path,  # noqa: ARG001 — kept for symmetry with sibling migrators
    data_paths: "DataPaths",
    db: "Database",
) -> MigrationReport:
    """Bring an existing install forward to the route-1 QAI Service topology.

    Idempotent: safe to run on every boot. Best-effort: a failure in any
    single step is logged and reflected in the report but does NOT raise
    — the caller (lifespan startup) must not let this migration abort
    startup, and the next boot retries.

    Args:
        repo_root: repository root (unused today; kept in signature so
            the lifespan hook site matches the sibling ``migrate_user_packs``
            call verbatim and future data-migrations that DO need the
            source tree can slot in without a signature change).
        data_paths: resolved ``DataPaths``; ``data_paths.root`` locates
            ``user_config.toml``.
        db: started ``Database`` (the async ``qai.platform.persistence``
            connection pool).

    Returns:
        A :class:`MigrationReport` summarising the outcome.
    """
    report = MigrationReport()

    # 0. Sentinel probe — fast-path when already applied.
    try:
        if await _sentinel_present(db):
            report.already_applied = True
            _log("skip.already_applied", id=_MIGRATION_ID)
            return report
    except Exception as exc:  # noqa: BLE001 — best-effort at boot
        report.failed_step = "sentinel_probe"
        _log("failed.sentinel_probe", error=repr(exc))
        return report

    # 1. SQLite mutations (single transaction, guarded by legacy-host LIKE).
    try:
        await _rewrite_sqlite(db, report)
    except Exception as exc:  # noqa: BLE001
        report.failed_step = "sqlite_rewrite"
        _log("failed.sqlite_rewrite", error=repr(exc))
        return report

    # 2. TOML rewrite (text-level, comment-preserving).
    try:
        _rewrite_user_config_toml(
            data_paths.root / "user_config.toml", report
        )
    except Exception as exc:  # noqa: BLE001
        report.failed_step = "toml_rewrite"
        _log("failed.toml_rewrite", error=repr(exc))
        # SQLite already committed; the sentinel is intentionally NOT
        # written so the next boot retries the TOML step. SQLite side
        # is a no-op via LIKE guards on retry.
        return report

    # 3. Clear stale JWT so login refreshes against the new host — but ONLY
    # when this run actually changed the host-bearing state. The ``_v2`` id
    # bump re-runs this migrator on EVERY install, including fresh ones already
    # seeded with the current address and ones a previous run already carried
    # over. Those runs changed nothing here, so clearing would needlessly sign
    # the user out of a pool whose address they are already on. ``_clear_jwt``
    # keeps its own host==old short-circuit; this is the outer "did we retarget
    # anything" gate.
    if report.kv_updates or report.toml_rewritten:
        try:
            _clear_jwt(report)
        except Exception as exc:  # noqa: BLE001
            report.failed_step = "jwt_clear"
            _log("failed.jwt_clear", error=repr(exc))
            # JWT clear is best-effort — the user simply re-logs in and gets
            # a fresh JWT from the new host that way. Do NOT block sentinel.
            # Fall through to sentinel write.

    # 4. Record sentinel — from now on this migration is a fast-path no-op.
    try:
        await _write_sentinel(db)
        report.applied = True
    except Exception as exc:  # noqa: BLE001
        report.failed_step = "sentinel_write"
        _log("failed.sentinel_write", error=repr(exc))
        return report

    _log(
        "applied",
        id=_MIGRATION_ID,
        kv_updates=report.kv_updates,
        mc_deletes=report.mc_deletes,
        mc_upserts=report.mc_upserts,
        ui_clears=report.ui_clears,
        toml_rewritten=report.toml_rewritten,
        jwt_cleared=report.jwt_cleared,
    )
    return report


# ---------------------------------------------------------------------
# Sentinel helpers
# ---------------------------------------------------------------------


async def _sentinel_present(db: "Database") -> bool:
    """Return True when this migration has already been recorded."""
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM _qai_data_migrations WHERE id = ?",
            (_MIGRATION_ID,),
        )
        row = await cur.fetchone()
        await cur.close()
    return row is not None


async def _write_sentinel(db: "Database") -> None:
    async with db.connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                "INSERT OR IGNORE INTO _qai_data_migrations (id, applied_at) "
                "VALUES (?, ?)",
                (_MIGRATION_ID, _now_iso()),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


# ---------------------------------------------------------------------
# Step 1 — SQLite mutations
# ---------------------------------------------------------------------


async def _rewrite_sqlite(db: "Database", report: MigrationReport) -> None:
    """Rewrite the qai-service kv row + model_catalog_entry rows + UI prefs.

    All mutations run inside one transaction. LIKE guards make each of
    them a no-op when the value is not the known legacy default, so a
    partial-completion retry is safe.
    """
    now = _now_iso()
    legacy_host_like = f"%{_OLD_HOST}%"

    async with db.connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            # 1a. Rewrite kv_user_prefs row — only if it still points at
            # the legacy host. A user who already retargeted the pool
            # (LIKE mismatches) is left untouched.
            cur = await conn.execute(
                "UPDATE kv_user_prefs "
                "   SET value_json = ?, updated_at = ? "
                " WHERE key = 'model_catalog.provider.qai-service' "
                "   AND value_json LIKE ?",
                (_new_kv_value_json(), now, legacy_host_like),
            )
            report.kv_updates = cur.rowcount if cur.rowcount is not None else 0
            await cur.close()

            # 1b. Delete legacy model_catalog_entry rows — only those whose
            # id is one of the three legacies AND whose source_url still
            # names the legacy host. A user-forked row survives.
            placeholders = ",".join(["?"] * len(_OLD_MODEL_IDS))
            cur = await conn.execute(
                f"DELETE FROM model_catalog_entry "
                f" WHERE id IN ({placeholders}) "
                f"   AND source_url LIKE ?",
                (*_OLD_MODEL_IDS, legacy_host_like),
            )
            report.mc_deletes = cur.rowcount if cur.rowcount is not None else 0
            await cur.close()

            # 1c. Upsert the route-1 row. A REAL upsert, not INSERT OR IGNORE:
            # every install that already ran the original migration HAS this
            # row, with the legacy host in source_url. INSERT OR IGNORE would
            # keep that stale copy forever (the row exists, so the insert is
            # ignored) and the catalog entry would keep naming a dead address.
            # ON CONFLICT DO UPDATE refreshes source_url — but only while it
            # still names the legacy host, so an operator who repointed route-1
            # at a private broker is left untouched (same intent as the LIKE
            # guards above).
            cur = await conn.execute(
                "INSERT INTO model_catalog_entry "
                "(id, name, provider, source_url, description, "
                " taxonomy_tags_json, current_version_id, created_at, updated_at) "
                "VALUES (?, ?, 'generic_cloud', ?, ?, '[]', NULL, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "    source_url = excluded.source_url, "
                "    updated_at = excluded.updated_at "
                " WHERE model_catalog_entry.source_url LIKE ?",
                (
                    "qai-service::route-1",
                    "Route 1",
                    f"http://{_new_host()}/v1",
                    "QAI Service 智能路由（自动选择底层模型）",
                    now,
                    now,
                    legacy_host_like,
                ),
            )
            report.mc_upserts = cur.rowcount if cur.rowcount is not None else 0
            await cur.close()

            # 1d. Clear standalone UI-selection rows that reference a
            # deleted id. Value stored as a JSON-encoded string, e.g.
            # ``"qai-service::azure::gpt-5.5"``. Compare against each of
            # the JSON literals for the legacy ids.
            #
            # The placeholder count is DERIVED (mirrors 1b above) rather
            # than hardcoded: a hardcoded ``IN (?, ?, ?)`` would silently
            # couple this statement to ``len(_OLD_MODEL_IDS) == 3``, and
            # editing that tuple — which is this migration's whole subject
            # — would raise ``sqlite3.ProgrammingError: Incorrect number
            # of bindings supplied``. That exception is swallowed by the
            # best-effort ``except Exception`` in apps/api/lifespan.py, so
            # the symptom would not be a crash but the migration SILENTLY
            # never applying (BEGIN IMMEDIATE rolls back 1a-1c and the
            # sentinel is never written) — leaving the user on the legacy
            # pool with chat 401s, i.e. exactly the bug this migrator fixes.
            legacy_json_literals = [
                json.dumps(mid) for mid in _OLD_MODEL_IDS
            ]
            ui_placeholders = ",".join(["?"] * len(legacy_json_literals))
            for key in ("ui.selected_model_id", "ui.selected_service_model"):
                cur = await conn.execute(
                    "UPDATE kv_user_prefs "
                    "   SET value_json = '\"\"', updated_at = ? "
                    " WHERE key = ? "
                    f"   AND value_json IN ({ui_placeholders})",
                    (now, key, *legacy_json_literals),
                )
                report.ui_clears += cur.rowcount if cur.rowcount is not None else 0
                await cur.close()

            # 1e. ``ui.preferences`` may embed selected_model_id in a
            # nested doc. Read-modify-write only when a legacy id is
            # embedded; leaves other keys of the doc intact.
            cur = await conn.execute(
                "SELECT value_json FROM kv_user_prefs WHERE key = 'ui.preferences'"
            )
            row = await cur.fetchone()
            await cur.close()
            if row is not None:
                try:
                    doc = json.loads(row[0])
                except (TypeError, ValueError):
                    doc = None
                if isinstance(doc, dict):
                    sel = doc.get("selected_model_id")
                    if isinstance(sel, str) and sel in _OLD_MODEL_IDS:
                        doc["selected_model_id"] = ""
                        cur = await conn.execute(
                            "UPDATE kv_user_prefs SET value_json = ?, updated_at = ? "
                            "WHERE key = 'ui.preferences'",
                            (
                                json.dumps(doc, ensure_ascii=False, sort_keys=True),
                                now,
                            ),
                        )
                        report.ui_clears += (
                            cur.rowcount if cur.rowcount is not None else 0
                        )
                        await cur.close()

            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


# ---------------------------------------------------------------------
# Step 2 — TOML rewrite (text-level, comment-preserving)
# ---------------------------------------------------------------------


def _rewrite_user_config_toml(
    path: Path, report: MigrationReport
) -> None:
    """Replace both legacy ``base_url`` literals in-place.

    Uses text-level find-and-replace so the file's 900+ lines of Chinese
    comments and its layout survive verbatim. Two variants:

    * the ``/v1`` form — the OpenAI-compatible chat endpoint under
      ``[forge.cloud_providers.qai-service]``.
    * the bare origin — under ``[forge.qai_service]`` (JWT exchange base).

    Order matters: the ``/v1`` variant is a superstring of the bare one,
    so we substitute the longer form first to avoid a double-rewrite.

    If neither literal is present the write is skipped entirely (no
    stat/mtime churn) — the user has already customised the value, a previous
    invocation of this migration already ran, or this is a fresh install whose
    ``factory/`` seed already carries the current address.

    On the write path we do a tmp + ``os.replace`` swap so a mid-write
    crash cannot leave a truncated TOML that would corrupt the next
    boot's login.
    """
    new_host = _new_host()
    if new_host == _OLD_HOST:
        # Nothing to retarget (a fork that pinned the target back to the legacy
        # address), and short-circuiting here means we do not even read the
        # file. Neither shipped edition hits this any more — both moved off
        # _OLD_HOST — but a pointless rewrite is still worth avoiding.
        _log("skip.toml_same_host", path=str(path))
        return

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _log("skip.toml_missing", path=str(path))
        return
    except OSError as exc:
        # Aborts the whole migration (raises → sentinel not written → retried
        # next boot), and now that the host actually changed this means the
        # install stays on the dead legacy address until it succeeds. WARNING
        # so that is diagnosable rather than buried in INFO.
        _log_warn("failed.toml_read", path=str(path), error=str(exc))
        raise

    original = text
    text = text.replace(
        f'"http://{_OLD_HOST}/v1"',
        f'"http://{new_host}/v1"',
    )
    text = text.replace(
        f'"http://{_OLD_HOST}"',
        f'"http://{new_host}"',
    )
    if text == original:
        _log("skip.toml_no_legacy_literal", path=str(path))
        return

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    report.toml_rewritten = True
    _log("applied.toml_rewrite", path=str(path))


# ---------------------------------------------------------------------
# Step 3 — JWT clear
# ---------------------------------------------------------------------


def _clear_jwt(report: MigrationReport) -> None:
    """Invalidate the persisted QAI Service JWT.

    The JWT is bound to the pool it was minted at (old host); carrying
    it forward against the new host yields a permanent 401. Clearing is
    idempotent — a missing token is a no-op — so we can call it every
    boot up to the sentinel write.

    Two layers keep this from signing anyone out gratuitously:

    * the caller only invokes it when this run actually rewrote host-bearing
      state (``kv_updates or toml_rewritten``) — the gate that matters now that
      the ``_v2`` id bump re-runs the migrator on every install; and
    * the ``host == _OLD_HOST`` short-circuit below, for a fork that pinned the
      target back to the legacy address.

    In both skipped cases the existing JWT is still valid against the address
    it was minted at, and clearing it would silently sign the user out of the
    pool to fix a problem they never had.
    """
    if _new_host() == _OLD_HOST:
        _log("skip.jwt_same_host")
        return

    # Local import: this module is imported at lifespan-startup time and
    # we don't want to pull in the interfaces layer at package import.
    from interfaces.http.auth.qai_service_token import (  # noqa: PLC0415
        clear_service_jwt,
    )

    clear_service_jwt()
    report.jwt_cleared = True


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------


_logger = logging.getLogger(__name__)


def _fmt(event: str, fields: dict[str, object]) -> str:
    """Compose a compact ``qai_service_route1.<event> k=v ...`` record."""
    parts = [f"qai_service_route1.{event}"]
    for key, value in fields.items():
        parts.append(f"{key}={value!r}")
    return " ".join(parts)


def _log(event: str, /, **fields: object) -> None:
    """Emit an INFO-level ``event k=v ...`` record via stdlib logging.

    Matches the shape used by ``move_user_packs_to_data._log`` so both
    migrators log identically when invoked from lifespan.
    """
    _logger.info(_fmt(event, fields))


def _log_warn(event: str, /, **fields: object) -> None:
    """WARNING-level variant of :func:`_log` for degraded / retryable paths."""
    _logger.warning(_fmt(event, fields))


def _now_iso() -> str:
    """UTC ISO-8601 timestamp for ``applied_at`` / ``updated_at`` columns."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``DeleteAppModelUseCase`` — remove a registered app model definition."""

from __future__ import annotations

import inspect
import logging

from qai.app_builder.application.ports import (
    AppModelRepositoryPort,
    PackFileCleanupPort,
    PackRemovedCallback,
    RunCancellationPort,
    RunRepositoryPort,
    VariantDeleteResult,
)
from qai.app_builder.domain.value_objects import AppModelId
from qai.platform.errors import ForbiddenError

__all__ = ["DeleteAppModelUseCase", "DeleteAppModelResult"]

logger = logging.getLogger(__name__)


class DeleteAppModelResult:
    """Outcome of a delete (full or per-variant).

    * ``mode="full"`` — the whole model was removed (DB row + on-disk pack
      files when ``delete_files`` and a cleanup port are configured).
    * ``mode="partial"`` — only the requested variants were removed; the model
      and its DB row survive (the manifest was rewritten with the survivors).

    ``warnings`` carries non-fatal file-cleanup messages (V1 surfaced these in
    the response ``errors`` array without failing the request).
    """

    __slots__ = ("mode", "deleted_variants", "remaining_variants", "new_default", "warnings")

    def __init__(
        self,
        *,
        mode: str,
        deleted_variants: tuple[str, ...] = (),
        remaining_variants: tuple[str, ...] = (),
        new_default: str | None = None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.mode = mode
        self.deleted_variants = deleted_variants
        self.remaining_variants = remaining_variants
        self.new_default = new_default
        self.warnings = warnings


class DeleteAppModelUseCase:
    """Remove an :class:`AppModelDefinition` from the registry.

    Behaviour (V1 parity — ``backend/app_builder/api_routes.py`` delete branch):

    * Unknown id → :class:`qai.app_builder.domain.errors.AppModelNotFoundError`
      (surfaced as HTTP 404).
    * Built-in model (``user_imported`` falsy) → :class:`ForbiddenError`
      (surfaced as HTTP 403). Only user-imported models may be removed; bundled
      built-ins are protected from deletion.
    * ``variant_ids`` given → per-variant delete: remove only those precision
      variants' weights + manifest entries, keep the model. If removing them
      would empty the pack (``would_be_empty``) we fall back to a full delete
      (V1 ``performDelete`` semantics).
    * Otherwise → full delete: remove the DB row AND (when ``delete_files`` and a
      cleanup port are wired) the on-disk pack dir + staged weights, so a delete
      is symmetric with an import (State-Truth-First — no orphaned ``.bin`` left
      on disk; V1 default was ``deleteFiles=true``).

    Runtime state guarantees (P2 / P3 — §🔴 State-Truth-First 铁律 1/2/3)
    -------------------------------------------------------------------

    * **Before** DB / disk mutation: any pending / running / streaming run for
      this model_id is signalled to cancel through :class:`RunCancellationPort`.
      This releases the NPU cleanly instead of pulling pack files out from under
      a live inference (which crashes the resident worker with a native
      teardown-mid-use fault, cf. §🔴 铁律 1). Cancel failures are logged but
      never abort the delete — the pack still needs to disappear.
    * **After** the DB row + on-disk pack files are gone: the injected
      :attr:`_on_pack_removed` callback is invoked so composition-layer caches
      (manifest provider, runner command registry, resident worker) forget the
      id. Firing the callback *after* mutation is deliberate — a pre-mutation
      clear would race the delete (a concurrent request could re-populate from
      the still-present state before we finish). Best-effort: callback failures
      only log, since the persisted truth is already consistent (see the
      module docstring on :data:`PackRemovedCallback`).

    Partial (per-variant) deletes do NOT fire the callback: the pack is still
    on disk, just with fewer variants, so cache entries remain valid.
    """

    def __init__(
        self,
        *,
        app_models: AppModelRepositoryPort,
        pack_files: PackFileCleanupPort | None = None,
        on_pack_removed: PackRemovedCallback | None = None,
        runs: RunRepositoryPort | None = None,
        run_cancellation: RunCancellationPort | None = None,
    ) -> None:
        self._app_models = app_models
        self._pack_files = pack_files
        # P2 / Sub-A: runtime cache invalidation hook, symmetric to the
        # ``on_pack_installed`` callback threaded through the import
        # adapter. Consumed by ``execute()`` at the tail of the full-delete
        # branch (after DB row + pack files are gone — State-Truth-First:
        # clear caches only after the persisted truth is clean, otherwise
        # a concurrent read repopulates them from stale disk state).
        self._on_pack_removed = on_pack_removed
        # P3 / Sub-B: active-run protection. Both ports are optional so a
        # lean test container that wires neither still gets a working
        # delete flow (the runtime cache invalidation degrades to a no-op
        # for the "runs" dimension). Wiring only one of the two is treated
        # as "not wired" for the P3 path — cancellation without a lookup
        # (or vice-versa) is meaningless.
        self._runs = runs
        self._run_cancellation = run_cancellation

    async def execute(
        self,
        *,
        model_id: AppModelId,
        variant_ids: tuple[str, ...] = (),
        delete_files: bool = True,
    ) -> DeleteAppModelResult:
        # ``get`` raises AppModelNotFoundError (→ 404) for unknown ids,
        # preserving the not-found contract before the protection check.
        model = await self._app_models.get(model_id)
        if not model.user_imported:
            raise ForbiddenError(
                "app_builder.app_model_builtin_protected",
                f"Built-in model {str(model_id)!r} cannot be deleted. "
                "Only user-imported models can be removed.",
            )

        # ── P3: cancel any active runs BEFORE mutating disk / DB ──────────
        # §🔴 State-Truth-First 铁律 1/2: yanking pack files out from under
        # a mid-flight ``op:run`` on the resident worker tears the native
        # Genie/QNN model down mid-use → the sticky worker hard-crashes
        # (0xFFFFFFFF). We first signal every non-terminal run to cancel
        # so the worker releases the NPU cleanly; only then do we proceed
        # to the destructive step. Cancel failures are best-effort — a
        # dead / absent worker is a silent no-op and never blocks the
        # delete (the pack still needs to disappear so the user's UI
        # action succeeds).
        cancel_warnings: tuple[str, ...] = ()
        if self._runs is not None and self._run_cancellation is not None:
            try:
                active_runs = await self._runs.list_active_by_model(model_id)
            except Exception as exc:  # noqa: BLE001 — never block delete
                logger.warning(
                    "app_builder.delete.list_active_runs_failed: id=%s: %s "
                    "(proceeding without cancellation — active runs, if any, "
                    "will surface as failures once their worker context is "
                    "gone)",
                    model_id.value,
                    exc,
                )
                active_runs = ()
            if active_runs:
                cancelled = 0
                for run in active_runs:
                    try:
                        await self._run_cancellation.cancel_run(run.id.value)
                        cancelled += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "app_builder.delete.cancel_active_run_failed: "
                            "run=%s model=%s: %s (proceeding with delete)",
                            run.id.value,
                            model_id.value,
                            exc,
                        )
                if cancelled:
                    cancel_warnings = (
                        f"Cancelled {cancelled} active run"
                        f"{'s' if cancelled != 1 else ''} "
                        f"before deleting model {model_id.value!r}.",
                    )

        # ── per-variant delete ────────────────────────────────────────────
        if variant_ids and self._pack_files is not None:
            result: VariantDeleteResult = self._pack_files.delete_variant_files(
                model_id.value, variant_ids
            )
            if result.mode == "partial":
                # Partial delete: the pack still exists on disk (only some
                # variants are gone). Runtime caches (manifest / runner
                # registry / worker) still hold a valid entry for this
                # model_id, so we deliberately do NOT fire on_pack_removed.
                return DeleteAppModelResult(
                    mode="partial",
                    deleted_variants=result.deleted,
                    remaining_variants=result.remaining,
                    new_default=result.new_default,
                    warnings=cancel_warnings + result.errors,
                )
            if result.mode == "noop":
                # Nothing matched (no variants[] / only unknown ids) — leave the
                # model intact; surface the reason as a warning.
                return DeleteAppModelResult(
                    mode="partial",
                    warnings=cancel_warnings + result.errors,
                )
            # ``would_be_empty`` → fall through to full delete below
            # (the user selected every variant). Preserve the warning trail.
            pre_warnings = result.errors
        else:
            pre_warnings = ()

        # ── full delete: DB row + on-disk files ───────────────────────────
        warnings: tuple[str, ...] = cancel_warnings + pre_warnings
        if delete_files and self._pack_files is not None:
            warnings = warnings + self._pack_files.delete_pack_files(model_id.value)
        await self._app_models.delete(model_id)

        # ── P2: runtime cache invalidation (best-effort) ──────────────────
        # Fired AFTER the DB row + pack files are gone — State-Truth-First:
        # the persisted truth is already clean, so a stale disk read from
        # inside the callback cannot repopulate the caches with a phantom
        # entry. A callback failure only logs; the caches will converge on
        # the next full refresh (§🔴 铁律 1 — the truth-source is
        # DB + disk, caches are downstream views).
        if self._on_pack_removed is not None:
            try:
                result_or_awaitable = self._on_pack_removed(model_id.value)
                if inspect.isawaitable(result_or_awaitable):
                    await result_or_awaitable
            except Exception as exc:  # noqa: BLE001 — never fail the delete
                logger.warning(
                    "app_builder.delete.runtime_cache_evict_failed: "
                    "id=%s: %s (DB + disk deletion already complete; "
                    "caches will converge on next refresh)",
                    model_id.value,
                    exc,
                )

        return DeleteAppModelResult(mode="full", warnings=warnings)

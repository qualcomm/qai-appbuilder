# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Inject ``quality_score`` into the catalog prompt build flow (PR-094 §17.5 #15).

Restores the legacy ``backend/app_builder/telemetry.py:77-168`` pipeline
that aggregated user thumbs-up / thumbs-down ratings into a per-model
``qualityScore ∈ [0, 1]``, then pushed that score into the LLM tool
descriptor / catalog prompt so the planner can de-prioritise low-rated
packs in tool selection.

The S9 audit (§3.3 A-14) flagged this as a missing model-selection signal
in the new clean-architecture stack; without it the LLM sees every Pack
as equally qualified, regressing the "favour packs with good user
reviews" experience the legacy backend offered.

Pure-application use case
-------------------------

This use case does NOT mutate the catalog prompt directly — it returns a
``dict[AppModelId, float]`` that the prompt-building flow
(:class:`qai.app_builder.application.use_cases.skill_and_schema.GeneratePackCatalogUseCase`)
can layer on top of the
per-model SKILL.md fragments. We keep the concerns separate so:

* tests can drive the score injection in isolation;
* the prompt builder stays free to ignore the score (e.g. when the user
  has explicitly enabled "no quality bias" mode, a future preference).

Score derivation
----------------

Feedback is persisted on the **Likert ``1..5``** scale (``app_builder_
feedback.rating``; the UI maps 👎→1 / 👍→5). We fold ratings into a
per-model ``quality_score ∈ [0, 1]`` using the SAME mapping as the
metrics panel — :func:`qai.app_builder.domain.aggregate_ratings`
(``thumbs_up = count >= 4``, ``thumbs_down = count <= 2``,
``quality_score = (avg - 1) / 4``). Reusing that single domain function
keeps the LLM-catalog score and the metrics-panel score consistent and
eliminates the earlier scale drift (a 👎=1 review was previously
mis-read as a thumbs-up by an inherited ``-1/0/+1`` normaliser, so
``quality_score`` was stuck at ``1.0`` and bad reviews never lowered a
pack's selection rank — §🟡🟡 fixed, never carry the defect forward).

Legacy inline ratings (older ``run.inputs`` records that predate the
feedback table, stored as ``-1 / 0 / +1``) are lifted onto the same
Likert scale (``-1→1``, ``0→3``, ``+1→5``) before aggregation so both
sources feed one consistent scale. Runs with no rating signal return
``None``; the prompt builder treats ``None`` as "no opinion".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from qai.app_builder.application.ports import (
    FeedbackRepositoryPort,
    RunRepositoryPort,
)
from qai.app_builder.domain.aggregated_metrics import aggregate_ratings
from qai.app_builder.domain.run import Run, RunStatus
from qai.app_builder.domain.value_objects import AppModelId

__all__ = ["InjectQualityScoreUseCase", "QualitySummary"]


_RATING_KEYS: tuple[str, ...] = ("rating", "user_rating", "feedback_rating")


def _feedback_to_likert(value: int) -> int:
    """Clamp a feedback-table rating onto the Likert ``1..5`` scale.

    The ``app_builder_feedback`` table is validated as ``1..5`` on write
    (👎=1 … 👍=5); this clamps defensively so an out-of-range row never
    skews the aggregate.
    """
    return max(1, min(5, value))


def _inline_thumbs_to_likert(value: int) -> int:
    """Lift a legacy inline ``-1 / 0 / +1`` thumbs rating onto Likert ``1..5``.

    Pre-feedback-table records stored ratings inline in ``run.inputs`` on
    the old thumbs scale: ``-1`` (👎) → ``1``, ``0`` (neutral) → ``3``,
    ``+1`` (👍) → ``5``. Mapping both sources onto one scale lets a single
    :func:`aggregate_ratings` call score them consistently.
    """
    if value < 0:
        return 1
    if value == 0:
        return 3
    return 5


@dataclass(frozen=True, slots=True, kw_only=True)
class QualitySummary:
    """Per-model rating + run aggregate (V1 ``telemetry.get_metrics_summary`` parity).

    Mirrors the shape the legacy
    ``backend/app_builder/skill_resolver.generate_pack_catalog_prompt``
    consumed to render the ``历史评分`` / ``已成功运行`` lines:

    * :attr:`run_count` — number of successful (``COMPLETED``) runs in the
      scoring window.
    * :attr:`rating_count` — number of those runs that carried a thumbs
      rating signal.
    * :attr:`thumbs_up` / :attr:`thumbs_down` — counts of positive /
      negative ratings.
    * :attr:`quality_score` — the same ``[0, 1]`` projection
      :class:`InjectQualityScoreUseCase` derives (``None`` when there is no
      rating signal at all).
    """

    run_count: int = 0
    rating_count: int = 0
    thumbs_up: int = 0
    thumbs_down: int = 0
    quality_score: float | None = None


class InjectQualityScoreUseCase:
    """Compute ``quality_score`` for a set of ``AppModelId`` values.

    Caller passes the model ids it wants scored (typically the result of
    :class:`ListAppModelsUseCase`); the use case loads the most recent
    runs for each via :meth:`RunRepositoryPort.list_by_model` and folds
    their thumbs-up / thumbs-down ratings into a single ``[0, 1]`` score.

    Rating source (gap #7 — keep the LLM Pack catalog in sync with user
    feedback)
    ----------------------------------------------------------------------

    The S9 cutover moved feedback persistence out of the ``run`` row and
    into the append-only ``app_builder_feedback`` table
    (:class:`SubmitFeedbackUseCase`). The legacy flow used to overwrite
    ``app_builder_run.user_rating`` in-place, so reading the rating off
    ``run.inputs`` was enough; with the new schema that inline field is
    never written, which silently severed feedback from the catalog the
    LLM sees (V1 ``api_routes.py:1685`` invalidated a 10s TTL catalog
    cache after every feedback so the next chat saw the new score).

    V2 builds the catalog live on every chat request (no TTL cache to
    invalidate — see ``apps/api/_chat_di.py`` ``_app_builder_pack_catalog
    _provider`` → :class:`GeneratePackCatalogUseCase`), so we only need to
    read the *authoritative* rating from the right place. When a
    :class:`FeedbackRepositoryPort` is wired we batch-fetch the latest
    feedback rating per run (one query per model via
    :meth:`FeedbackRepositoryPort.latest_ratings_for_runs`) and treat it
    as the source of truth, falling back to any inline ``run.inputs``
    rating only for legacy records that predate the feedback table. The
    net effect mirrors V1: submitting feedback is reflected in the LLM
    catalog on the very next request, with no stale cache in between.
    """

    __slots__ = ("_runs", "_feedbacks", "_window")

    def __init__(
        self,
        *,
        runs: RunRepositoryPort,
        feedbacks: FeedbackRepositoryPort | None = None,
        window: int = 100,
    ) -> None:
        if not isinstance(window, int) or window <= 0:
            raise ValueError(f"window must be int > 0, got {window!r}")
        self._runs = runs
        self._feedbacks = feedbacks
        self._window = window

    async def execute(
        self,
        model_ids: Iterable[AppModelId],
    ) -> dict[AppModelId, float]:
        """Return the ``quality_score`` map for ``model_ids``.

        Models with no rating signal are omitted from the result so the
        caller can distinguish "no opinion" from "average opinion".
        """
        out: dict[AppModelId, float] = {}
        for mid in model_ids:
            score = await self._score_one(mid)
            if score is not None:
                out[mid] = score
        return out

    async def execute_one(self, model_id: AppModelId) -> float | None:
        """Single-model accessor — returns the score or ``None``."""
        return await self._score_one(model_id)

    async def summarize(
        self,
        model_ids: Iterable[AppModelId],
    ) -> dict[AppModelId, QualitySummary]:
        """Return per-model :class:`QualitySummary` for ``model_ids``.

        Unlike :meth:`execute` (which omits models with no rating signal),
        this surfaces the full ``run_count`` / thumbs breakdown for every
        requested model so the catalog prompt can render either the
        ``历史评分`` (when rated) or ``已成功运行`` (rated count 0 but runs
        exist) line — mirroring V1 ``generate_pack_catalog_prompt``.
        Models with no completed runs at all are omitted.
        """
        out: dict[AppModelId, QualitySummary] = {}
        for mid in model_ids:
            summary = await self.summarize_one(mid)
            if summary is not None:
                out[mid] = summary
        return out

    async def summarize_one(
        self, model_id: AppModelId
    ) -> QualitySummary | None:
        """Single-model :class:`QualitySummary` or ``None`` when no runs."""
        runs = await self._runs.list_by_model(model_id, limit=self._window)
        if not runs:
            return None
        completed = [r for r in runs if r.status == RunStatus.COMPLETED]
        run_count = len(completed)
        if run_count == 0:
            return None
        ratings = await self._collect_ratings(completed)
        # Reuse the single domain Likert mapping so the catalog score and the
        # metrics panel agree (thumbs_up = >=4, thumbs_down = <=2,
        # quality_score = (avg-1)/4).
        agg = aggregate_ratings(ratings)
        return QualitySummary(
            run_count=run_count,
            rating_count=len(ratings),
            thumbs_up=agg.thumbs_up if agg is not None else 0,
            thumbs_down=agg.thumbs_down if agg is not None else 0,
            quality_score=agg.quality_score if agg is not None else None,
        )

    # ── internals ─────────────────────────────────────────────────────

    async def _score_one(self, model_id: AppModelId) -> float | None:
        runs = await self._runs.list_by_model(model_id, limit=self._window)
        if not runs:
            return None
        completed = [r for r in runs if r.status == RunStatus.COMPLETED]
        if not completed:
            return None
        ratings = await self._collect_ratings(completed)
        agg = aggregate_ratings(ratings)
        return agg.quality_score if agg is not None else None

    async def _collect_ratings(self, completed_runs: list[Run]) -> list[int]:
        """Return the Likert ``1..5`` rating for each rated completed run.

        Resolution order per run (gap #7):

        1. The latest row in the ``app_builder_feedback`` table when a
           :class:`FeedbackRepositoryPort` is wired — the authoritative,
           live signal a user just submitted (already Likert ``1..5``).
        2. The legacy inline ``run.inputs`` rating for records that
           predate the feedback table (V1 in-place ``user_rating``,
           ``-1 / 0 / +1``), lifted onto the same Likert scale.

        Runs with no rating signal in either place are skipped so "no
        opinion" stays distinct from "average opinion".
        """
        feedback_by_run: dict[str, int] = {}
        if self._feedbacks is not None:
            try:
                feedback_by_run = await self._feedbacks.latest_ratings_for_runs(
                    [run.id for run in completed_runs]
                )
            except Exception:  # noqa: BLE001 — feedback must not break scoring
                feedback_by_run = {}
        ratings: list[int] = []
        for run in completed_runs:
            fb = feedback_by_run.get(str(run.id))
            if fb is not None:
                ratings.append(_feedback_to_likert(fb))
                continue
            r = self._extract_rating(run.inputs)
            if r is not None:
                ratings.append(_inline_thumbs_to_likert(r))
        return ratings

    @staticmethod
    def _extract_rating(inputs: dict[str, object]) -> int | None:
        """Return a raw rating from a Run's input bag, or ``None``.

        Run aggregates do not carry a typed ``rating`` field today; the
        legacy backend's history record stored ratings inside the
        per-run inputs / metadata bag, so we look there with a small
        priority list of compatible keys. The raw value (legacy
        ``-1 / 0 / +1`` thumbs OR a Likert ``1..5``) is returned as-is and
        lifted onto the Likert scale by the caller via :func:`_to_likert`.
        """
        for key in _RATING_KEYS:
            if key in inputs:
                v = inputs[key]
                if isinstance(v, bool):
                    continue
                if isinstance(v, int):
                    return v
                if isinstance(v, float):
                    return int(round(v))
        return None

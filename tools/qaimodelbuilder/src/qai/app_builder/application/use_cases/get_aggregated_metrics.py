# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Aggregate a model's historical run metrics (V1 model-level metrics parity).

Restores the per-model aggregation the legacy backend served on
``GET /api/appbuilder/metrics/{model_id}``
(``backend/app_builder/history_store.py:617-747`` +
``backend/app_builder/telemetry.py:77-145``): percentile latency stats
over the last *N* successful runs, plus a folded user-rating summary.

The S-audit flagged this as a real gap — V2 only exposed *per-run*
metrics (:class:`...deferred_routes.GetMetricsForRunUseCase`); the
"Aggregated stats over the last N runs" panel the
``MetricsView`` component renders had no backend to talk to (the
frontend composable surfaced ``error = "pendingBackend"`` on the 404).

Clean-architecture split
-------------------------

* The **maths** lives in the pure domain module
  :mod:`qai.app_builder.domain.aggregated_metrics` (percentile / mean /
  max + the Likert rating fold) — no I/O, satisfies ``domain-purity``.
* This **use case** orchestrates the reads through Ports only
  (:class:`RunRepositoryPort` for the latency sample,
  :class:`FeedbackRepositoryPort` for the rating sample) — it imports no
  adapter / infrastructure module, satisfying ``layered-app_builder``.

Latency sample
--------------

The latency sample is the runner-reported **pure inference latency**
(``Run.inference_latency_ms``, the ``metrics`` NDJSON event ``latencyMs``;
persisted by migration 028). This matches V1, whose aggregate latency
also came from ``metrics_json.latencyMs`` — the same quantity the
per-run history "Inference" column shows — so the single-run and
aggregate views measure the *same thing*. For older runs that predate
migration 028 (no persisted inference latency) we fall back to the
wall-clock duration (``finished_at - started_at``) so the sample is not
silently dropped. We fold every **COMPLETED** run for the model,
mirroring V1's "``status = 'success'``" filter.

Memory sample
-------------

The V2 run aggregate does not capture peak memory, so the memory sample
is empty and the aggregate's ``memory`` is ``None`` (the presenter hides
the memory row). The plumbing is in place so a future runner that
surfaces memory needs no use-case change.

Rating sample
-------------

The new feedback store (:class:`FeedbackRepositoryPort`) records a
Likert ``1..5`` rating per run; we batch-load the latest rating for the
model's runs and fold them via
:func:`qai.app_builder.domain.aggregated_metrics.aggregate_ratings`.
When the feedback repository is not wired (stripped test containers) the
use case degrades to "no ratings" rather than failing.
"""

from __future__ import annotations

from qai.app_builder.application.ports import (
    FeedbackRepositoryPort,
    RunRepositoryPort,
)
from qai.app_builder.domain.aggregated_metrics import (
    AggregatedMetrics,
    aggregate_latencies,
    aggregate_memories,
    aggregate_ratings,
)
from qai.app_builder.domain.run import Run, RunStatus
from qai.app_builder.domain.value_objects import AppModelId

__all__ = ["GetAggregatedMetricsForModelUseCase"]


class GetAggregatedMetricsForModelUseCase:
    """Compute :class:`AggregatedMetrics` for one ``AppModelId``.

    The optional ``variant_id`` filters the sample to a single pack
    variant; ``None`` (or the legacy ``"_default"`` sentinel, normalised
    by the route layer to ``None``) aggregates across all variants — V1
    parity (``history_store.get_aggregated_metrics(model_id, variant_id)``).
    """

    __slots__ = ("_runs", "_feedback", "_window")

    def __init__(
        self,
        *,
        runs: RunRepositoryPort,
        feedback: FeedbackRepositoryPort | None = None,
        window: int = 100,
    ) -> None:
        if not isinstance(window, int) or window <= 0:
            raise ValueError(f"window must be int > 0, got {window!r}")
        self._runs = runs
        self._feedback = feedback
        self._window = window

    async def execute(
        self,
        model_id: AppModelId,
        *,
        variant_id: str | None = None,
        limit: int | None = None,
    ) -> AggregatedMetrics:
        """Return the historical aggregate for ``model_id``.

        ``limit`` caps the number of most-recent runs the sample is built
        from (default = the use case's ``window``; V1 default 100). The
        result is always a well-formed :class:`AggregatedMetrics`; an
        empty history yields ``count == 0`` with ``latency`` / ``memory``
        / ``rating`` all ``None`` (HTTP 200, never 404 — the presenter
        hides the panel when ``count == 0``).
        """
        effective_limit = self._window if limit is None else max(1, int(limit))
        runs = await self._runs.list_by_model(model_id, limit=effective_limit)

        completed = [
            run
            for run in runs
            if run.status == RunStatus.COMPLETED and _matches_variant(run, variant_id)
        ]

        latencies = [
            d for run in completed if (d := _latency_ms(run)) is not None
        ]
        latency = aggregate_latencies(latencies)
        # V2 run aggregate carries no peak-memory metric yet; pass an empty
        # sample so the aggregate's ``memory`` stays ``None`` (row hidden).
        memory = aggregate_memories([])

        ratings = await self._collect_ratings(completed)
        rating = aggregate_ratings(ratings)

        return AggregatedMetrics(
            model_id=str(model_id),
            variant_id=variant_id,
            count=len(completed),
            latency=latency,
            memory=memory,
            rating=rating,
        )

    # ── internals ─────────────────────────────────────────────────────

    async def _collect_ratings(self, runs: list[Run]) -> list[int]:
        """Return the latest Likert rating for each run that has one.

        Uses the batch :meth:`FeedbackRepositoryPort.latest_ratings_for_runs`
        so the whole sample is one query (no N+1 fan-out). Degrades to an
        empty list when the feedback repository is not wired.
        """
        if self._feedback is None or not runs:
            return []
        mapping = await self._feedback.latest_ratings_for_runs(
            [run.id for run in runs]
        )
        return list(mapping.values())


def _latency_ms(run: Run) -> float | None:
    """Per-run latency in milliseconds for the aggregate sample.

    Prefers the runner-reported pure inference latency
    (``Run.inference_latency_ms``, migration 028) so the aggregate matches
    V1 and the per-run history "Inference" column. Falls back to the
    wall-clock duration for older runs that have no persisted inference
    latency, so pre-028 history is not dropped from the sample.
    """
    if run.inference_latency_ms is not None:
        return max(0.0, float(run.inference_latency_ms))
    return _duration_ms(run)


def _duration_ms(run: Run) -> float | None:
    """Wall-clock run duration in milliseconds, or ``None`` when unknown.

    Fallback for runs predating migration 028 (no persisted inference
    latency). Matches the single-run wall-clock derivation in
    :class:`...deferred_routes.GetMetricsForRunUseCase`.
    """
    if run.started_at is None or run.finished_at is None:
        return None
    return max(0.0, (run.finished_at - run.started_at).total_seconds() * 1000.0)


def _matches_variant(run: Run, variant_id: str | None) -> bool:
    """Whether ``run`` belongs to the requested ``variant_id``.

    ``variant_id is None`` matches every run (aggregate across variants —
    V1 parity). When a variant is requested we match the run's
    ``inputs["variant_id"]`` (the per-run bag the runner records); runs
    that carry no variant marker are treated as the default variant and
    therefore excluded from a non-default variant filter.
    """
    if variant_id is None:
        return True
    run_variant = run.inputs.get("variant_id")
    return isinstance(run_variant, str) and run_variant == variant_id

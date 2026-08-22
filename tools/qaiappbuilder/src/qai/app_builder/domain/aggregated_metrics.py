# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure aggregation maths for the model-level metrics summary.

This module restores the per-model historical aggregation the legacy
backend exposed through ``GET /api/appbuilder/metrics/{model_id}``
(V1 ``backend/app_builder/history_store.py:617-747`` +
``backend/app_builder/telemetry.py:77-145``). It is the *pure* core of
that pipeline: percentile / mean / max maths plus the rating fold, with
**no I/O and no framework imports** so the ``layered-app_builder`` and
``domain-purity`` import-linter contracts stay satisfied.

Why a domain module
--------------------

The legacy code computed p50/p90/p99 inside ``HistoryStore`` (which also
owned the SQLite connection + a 30s cache). In the clean-architecture
split those concerns separate cleanly:

* **domain** (this module) — the maths: percentile via nearest-rank,
  mean/max, and the Likert rating fold. Deterministic, trivially unit
  testable, zero dependencies.
* **application** —
  :class:`qai.app_builder.application.use_cases.get_aggregated_metrics.GetAggregatedMetricsForModelUseCase`
  pulls the raw per-run latency sequence (from the run repository) and
  the rating sequence (from the feedback repository) and hands them to
  these pure functions.
* **adapters / infrastructure** — own the SQLite ``ORDER BY ... LIMIT``
  reads.

V1 algorithm parity
-------------------

* ``_percentile`` reproduces the V1 nearest-rank formula verbatim
  (``history_store.py:140-151``): for a pre-sorted ascending list it
  maps ``pct`` onto an index via ``round(pct/100 * (n-1))``. Empty list
  → ``0.0``; single element → that element.
* Latency aggregation yields ``{p50, p90, p99, mean, max}`` (rounded to
  3 decimals to avoid float jitter — V1 ``telemetry._round_dict``).
* Rating fold projects the average onto ``qualityScore``. The V2
  feedback store records a **Likert ``1..5``** rating (see
  :class:`qai.app_builder.domain.feedback.Feedback`), unlike the V1
  thumb-style ``-1/0/+1``. We map:

  * ``thumbsUp``   = count of ratings ``>= 4`` (positive)
  * ``thumbsDown`` = count of ratings ``<= 2`` (negative)
  * ``avg``        = arithmetic mean of the Likert values (``1..5``)
  * ``qualityScore`` = ``(avg - 1) / 4`` clamped to ``[0, 1]`` — the
    Likert-range analogue of the V1 ``(avg + 1) / 2`` thumb mapping, so
    the score keeps its documented ``[0, 1]`` semantics for the UI's
    "Quality: NN%" badge and any downstream LLM-selection bias.

  Empty rating sequence → ``None`` (so the presenter renders "no
  ratings yet" rather than a misleading ``0%``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "LatencyAggregate",
    "MemoryAggregate",
    "RatingAggregate",
    "AggregatedMetrics",
    "percentile",
    "aggregate_latencies",
    "aggregate_memories",
    "aggregate_ratings",
]


# ---------------------------------------------------------------------------
# Result value objects (transport-neutral; the route layer maps to camelCase)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class LatencyAggregate:
    """Percentile + mean/max summary over a latency sample (milliseconds)."""

    p50: float
    p90: float
    p99: float
    mean: float
    max: float


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryAggregate:
    """Mean/max summary over a peak-memory sample (megabytes)."""

    mean: float
    max: float


@dataclass(frozen=True, slots=True, kw_only=True)
class RatingAggregate:
    """User-rating fold (Likert ``1..5`` → thumbs + quality score)."""

    avg: float
    count: int
    thumbs_up: int
    thumbs_down: int
    quality_score: float


@dataclass(frozen=True, slots=True, kw_only=True)
class AggregatedMetrics:
    """Per-(model, variant) historical aggregate.

    ``count`` is the number of successful runs the latency aggregate is
    based on. ``latency`` / ``memory`` / ``rating`` are ``None`` when the
    corresponding sample is empty so the presenter can hide the row
    rather than show misleading zeros.
    """

    model_id: str
    variant_id: str | None
    count: int
    latency: LatencyAggregate | None
    memory: MemoryAggregate | None
    rating: RatingAggregate | None


# ---------------------------------------------------------------------------
# Pure maths
# ---------------------------------------------------------------------------
def percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile over a **pre-sorted ascending** sample.

    Reproduces V1 ``history_store._percentile`` (``:140-151``) exactly so
    historical numbers stay comparable:

    * empty sample → ``0.0``;
    * single element → that element;
    * otherwise map ``pct ∈ [0, 100]`` onto an index via
      ``round(pct / 100 * (n - 1))`` clamped to ``[0, n-1]``.

    The caller MUST pass an ascending-sorted sequence (the SQLite read
    path / :func:`aggregate_latencies` already sort).
    """
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_values[0])
    k = max(0, min(n - 1, int(round(pct / 100.0 * (n - 1)))))
    return float(sorted_values[k])


def _round(value: float, ndigits: int = 3) -> float:
    """Round to ``ndigits`` to tame float display jitter (V1 parity)."""
    return round(float(value), ndigits)


def aggregate_latencies(values: Sequence[float]) -> LatencyAggregate | None:
    """Fold a latency sample (ms) into ``{p50, p90, p99, mean, max}``.

    Non-finite / negative entries are the caller's responsibility to
    filter; this function trusts its input is a clean numeric sample.
    Returns ``None`` for an empty sample.
    """
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    mean = sum(ordered) / len(ordered)
    return LatencyAggregate(
        p50=_round(percentile(ordered, 50)),
        p90=_round(percentile(ordered, 90)),
        p99=_round(percentile(ordered, 99)),
        mean=_round(mean),
        max=_round(ordered[-1]),
    )


def aggregate_memories(values: Sequence[float]) -> MemoryAggregate | None:
    """Fold a peak-memory sample (MB) into ``{mean, max}``.

    Returns ``None`` for an empty sample. The V2 run aggregate does not
    yet capture peak memory, so the application layer typically passes an
    empty sequence here and the presenter hides the memory row; the
    function is provided so a future runner that *does* surface memory
    needs no domain change.
    """
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    mean = sum(ordered) / len(ordered)
    return MemoryAggregate(mean=_round(mean), max=_round(ordered[-1]))


def aggregate_ratings(ratings: Sequence[int]) -> RatingAggregate | None:
    """Fold a Likert ``1..5`` rating sample into a :class:`RatingAggregate`.

    Mapping (see module docstring):

    * ``thumbs_up``   = count of ratings ``>= 4``
    * ``thumbs_down`` = count of ratings ``<= 2``
    * ``avg``         = mean of the Likert values
    * ``quality_score`` = ``(avg - 1) / 4`` clamped to ``[0, 1]``

    Returns ``None`` for an empty sample so the presenter shows "no
    ratings" rather than a misleading ``0%``.
    """
    if not ratings:
        return None
    clean = [int(r) for r in ratings]
    count = len(clean)
    avg = sum(clean) / count
    thumbs_up = sum(1 for r in clean if r >= 4)
    thumbs_down = sum(1 for r in clean if r <= 2)
    quality_score = max(0.0, min(1.0, (avg - 1.0) / 4.0))
    return RatingAggregate(
        avg=_round(avg),
        count=count,
        thumbs_up=thumbs_up,
        thumbs_down=thumbs_down,
        quality_score=_round(quality_score),
    )

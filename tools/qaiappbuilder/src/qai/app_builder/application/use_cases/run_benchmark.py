# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``RunBenchmarkUseCase`` — schedule + execute a model latency benchmark.

Wired by S9 close to back the ``POST /api/app-builder/benchmark`` route
(previously surface-only). Mirrors the legacy
``backend/app_builder/api_routes.py:1825-1947`` SSE harness in domain
shape: same ``warmup`` + ``iterations`` parameters, same per-iteration
latency capture, same p50/p90/p99/mean/std/min/max aggregate.

Architectural choice (decision logged in S9 close report)
---------------------------------------------------------

Two viable options:

1. **Compose** :class:`RunAppUseCase`: re-use the existing run pipeline
   (same artifact store + sticky worker + dep checker) and run it
   ``warmup + iterations`` times, measuring wall-clock per iteration.
2. **Standalone harness** that talks directly to the runner port
   (skips the persistence overhead of writing N intermediate Run
   aggregates).

Option (1) is the chosen approach because:

* it preserves the legacy invariant that benchmark iterations are
  observable as regular Run rows in the audit log + UI history;
* it keeps the benchmark inside the same security / sandbox / dep
  envelope as a normal run (no parallel code path to harden);
* the per-iteration Run row is already the canonical place for the
  latency-relevant timestamps (``started_at`` / ``finished_at``);
* the marginal cost is one ``app_builder_run`` insert per iteration —
  on a modern SQLite/aiosqlite the overhead is sub-millisecond and
  uncorrelated with the model latencies the benchmark measures.

Persistence
-----------

The benchmark itself is persisted as a single
:class:`BenchmarkRecord` row (``app_builder_benchmark``):

* on entry we insert with ``status="scheduled"`` so the route can
  return the benchmark id immediately;
* the harness flips to ``"running"`` before the first iteration, then
  ``"completed"`` (with stats + raw_latencies populated) or ``"failed"``
  (with ``error_message``) on terminal state.

The route layer dispatches the harness as a fire-and-forget
``asyncio.create_task`` so the HTTP response stays cheap; clients poll
``GET /api/app-builder/benchmark/{benchmark_id}`` (also wired in S9
close) for terminal status.
"""

from __future__ import annotations

import logging
import time as _time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from qai.app_builder.application.ports import (
    BenchmarkRecord,
    BenchmarkRepositoryPort,
)
from qai.app_builder.domain.run import RunFrame
from qai.app_builder.domain.value_objects import AppModelId
from qai.platform.errors import NotFoundError
from qai.platform.ids import IdGenerator
from qai.platform.time import Clock

__all__ = [
    "RunBenchmarkUseCase",
    "RunBenchmarkCommand",
    "GetBenchmarkUseCase",
    "compute_benchmark_stats",
]

_log = logging.getLogger("qai.app_builder.benchmark")


@dataclass(frozen=True, slots=True, kw_only=True)
class RunBenchmarkCommand:
    """Inputs for :meth:`RunBenchmarkUseCase.execute`."""

    model_id: AppModelId
    iterations: int = 10
    warmup: int = 0
    inputs: dict[str, object] = field(default_factory=dict)


def compute_benchmark_stats(latencies_ms: list[float]) -> dict[str, float]:
    """Derive p50/p90/p99/min/max/mean/std from per-iteration latencies.

    Mirrors ``backend/app_builder/api_routes.py::_compute_benchmark_stats``
    so the wire output stays byte-compatible with the legacy frontend.
    Empty input → ``{"count": 0}`` (matches legacy behaviour for a
    warmup-only run that produced no measurements).
    """
    if not latencies_ms:
        return {"count": 0}
    import statistics as _stat

    s = sorted(latencies_ms)
    n = len(s)

    def _p(q: float) -> float:
        idx = max(0, min(n - 1, int(round(q * (n - 1)))))
        return round(s[idx], 2)

    return {
        "count": float(n),
        "p50": _p(0.5),
        "p90": _p(0.9),
        "p99": _p(0.99),
        "min": round(min(s), 2),
        "max": round(max(s), 2),
        "mean": round(_stat.mean(s), 2),
        "std": round(_stat.stdev(s) if n > 1 else 0.0, 2),
    }


class RunBenchmarkUseCase:
    """Schedule a benchmark and drive its iterations to completion.

    Composes :class:`RunAppUseCase` so each iteration produces a fully-
    observable Run aggregate. The benchmark row owns the aggregate
    stats; the per-iteration Runs are a side effect (they show up in
    ``GET /runs`` and ``GET /history`` like any user-initiated run).

    The constructor accepts ``run_app_use_case`` as ``Any`` to avoid
    pulling :class:`RunAppUseCase` directly into the type signature
    (which would create an import cycle through ``deferred_routes`` /
    ``run_app`` for some test fixtures).
    """

    __slots__ = (
        "_run_app",
        "_benchmarks",
        "_clock",
        "_ids",
    )

    def __init__(
        self,
        *,
        run_app_use_case: Any,
        benchmarks: BenchmarkRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._run_app = run_app_use_case
        self._benchmarks = benchmarks
        self._clock = clock
        self._ids = ids

    async def schedule(
        self, command: RunBenchmarkCommand
    ) -> BenchmarkRecord:
        """Persist a ``"scheduled"`` row and return it.

        The route layer calls this synchronously before returning the
        202 response, then dispatches :meth:`run_to_completion` as a
        background task so the HTTP request stays cheap.
        """
        record = BenchmarkRecord(
            id=self._ids.new_id(),
            model_id=command.model_id,
            iterations=int(command.iterations),
            warmup=int(command.warmup),
            inputs=dict(command.inputs),
            status="scheduled",
            stats={},
            raw_latencies_ms=(),
            error_message=None,
            created_at=self._clock.now(),
            finished_at=None,
        )
        await self._benchmarks.save(record)
        return record

    async def run_to_completion(self, benchmark_id: str) -> BenchmarkRecord:
        """Pick up a scheduled row and execute every iteration.

        Transitions the row through ``"running"`` → terminal and
        returns the final aggregate. Errors during a single iteration
        terminate the harness early with ``status="failed"`` (matching
        the legacy SSE behaviour where the first ``error`` event ended
        the stream).
        """
        # Load + flip to running.
        record = await self._benchmarks.get(benchmark_id)
        record_running = BenchmarkRecord(
            id=record.id,
            model_id=record.model_id,
            iterations=record.iterations,
            warmup=record.warmup,
            inputs=record.inputs,
            status="running",
            stats={},
            raw_latencies_ms=(),
            error_message=None,
            created_at=record.created_at,
            finished_at=None,
        )
        await self._benchmarks.save(record_running)

        latencies_ms: list[float] = []
        total = record.warmup + record.iterations
        terminal_error: str | None = None

        for i in range(total):
            is_warmup = i < record.warmup
            t_start = _time.time()
            try:
                iterator: AsyncIterator[RunFrame] = await self._run_app.execute(
                    model_id=record.model_id,
                    inputs=record.inputs,
                )
                # Drain the iterator; the use case persists Run rows
                # internally and we only need wall-clock latency here.
                async for _frame in iterator:
                    pass
            except Exception as exc:  # noqa: BLE001 — terminal harness failure
                _log.warning(
                    "benchmark.iteration_failed",
                    extra={
                        "benchmark_id": record.id,
                        "iteration": i + 1,
                        "warmup": is_warmup,
                        "error": str(exc),
                    },
                )
                terminal_error = f"iteration {i + 1} failed: {exc}"
                break
            elapsed_ms = (_time.time() - t_start) * 1000.0
            if not is_warmup:
                latencies_ms.append(elapsed_ms)

        finished_at = self._clock.now()
        if terminal_error is not None:
            terminal = BenchmarkRecord(
                id=record.id,
                model_id=record.model_id,
                iterations=record.iterations,
                warmup=record.warmup,
                inputs=record.inputs,
                status="failed",
                stats={},
                raw_latencies_ms=tuple(latencies_ms),
                error_message=terminal_error,
                created_at=record.created_at,
                finished_at=finished_at,
            )
        else:
            stats = compute_benchmark_stats(latencies_ms)
            terminal = BenchmarkRecord(
                id=record.id,
                model_id=record.model_id,
                iterations=record.iterations,
                warmup=record.warmup,
                inputs=record.inputs,
                status="completed",
                stats=stats,
                raw_latencies_ms=tuple(round(x, 2) for x in latencies_ms),
                error_message=None,
                created_at=record.created_at,
                finished_at=finished_at,
            )
        await self._benchmarks.save(terminal)
        return terminal


class GetBenchmarkUseCase:
    """Read a previously-persisted benchmark row by id.

    Backs the ``GET /api/app-builder/benchmark/{benchmark_id}`` route.
    Raises :class:`qai.platform.errors.NotFoundError` when the row is
    unknown — surfaced by the route as 404.
    """

    __slots__ = ("_benchmarks",)

    def __init__(self, *, benchmarks: BenchmarkRepositoryPort) -> None:
        self._benchmarks = benchmarks

    async def execute(self, benchmark_id: str) -> BenchmarkRecord:
        return await self._benchmarks.get(benchmark_id)


# Suppress unused-import warning when the module is imported for its
# protocols only.
_ = (NotFoundError, datetime)

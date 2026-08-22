# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``CancelRunUseCase`` — cancel a non-terminal run.

The use case first signals the worker to abort the actual inference (so the
NPU is released — V1 ``POST /cancel/{runId}`` terminated the runner), then
looks up the :class:`Run`, transitions it to ``CANCELLED``, persists it and
publishes :class:`RunCancelledEvent`. If the run is already terminal
(``COMPLETED`` / ``FAILED`` / ``CANCELLED``) the domain layer raises
:class:`qai.app_builder.domain.errors.RunAlreadyTerminatedError`, which the
caller surfaces as HTTP 409 (handled in the interfaces layer).
"""

from __future__ import annotations

import logging

from qai.app_builder.application.ports import (
    RunCancellationPort,
    RunRepositoryPort,
)
from qai.app_builder.domain.events import RunCancelledEvent
from qai.app_builder.domain.value_objects import RunId
from qai.platform.events import EventBus
from qai.platform.time import Clock

__all__ = ["CancelRunUseCase"]

logger = logging.getLogger(__name__)


class CancelRunUseCase:
    """Cancel a single :class:`Run`."""

    def __init__(
        self,
        *,
        runs: RunRepositoryPort,
        events: EventBus,
        clock: Clock,
        canceller: RunCancellationPort | None = None,
    ) -> None:
        self._runs = runs
        self._events = events
        self._clock = clock
        self._canceller = canceller

    async def execute(
        self,
        *,
        run_id: RunId,
        reason: str | None = None,
    ) -> None:
        # Validate the run is cancellable BEFORE touching the worker so an
        # already-terminal run still raises RunAlreadyTerminatedError (→ 409)
        # and we don't fire a pointless cancel op.
        run = await self._runs.get(run_id)
        run = run.cancel(now=self._clock.now(), reason=reason)
        # Stop the real inference on the worker so the NPU is freed
        # immediately (V1 parity / State-Truth-First). Best-effort: a dead or
        # absent worker is a silent no-op — we still flip the DB state.
        if self._canceller is not None:
            try:
                await self._canceller.cancel_run(run_id.value)
            except Exception as exc:  # noqa: BLE001 — never block the cancel
                logger.warning(
                    "app_builder.cancel.worker_signal_failed: run=%s: %s",
                    run_id.value,
                    exc,
                )
        await self._runs.save(run)
        await self._events.publish(
            RunCancelledEvent(
                run_id=run.id,
                model_id=run.model_id,
                finished_at=run.finished_at,  # type: ignore[arg-type]
                reason=reason,
            )
        )

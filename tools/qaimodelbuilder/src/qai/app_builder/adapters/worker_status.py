# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Worker-status adapter (PR-301).

* :class:`StickyWorkerStatusAdapter` — PR-301 real adapter that reads
  state from a live :class:`StickyWorkerHost` instance and projects it
  onto the SSOT :class:`WorkerPoolStatus` shape (loaded_models[],
  alive, multimodel, active_model_id, state).

It satisfies the
:class:`qai.app_builder.application.ports.WorkerStatusPort` Protocol.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from qai.app_builder.application.ports import (
    LoadedModelInfo,
    WorkerPoolStatus,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.app_builder.infrastructure.sticky_worker import StickyWorkerHost

__all__ = [
    "StickyWorkerStatusAdapter",
]


class StickyWorkerStatusAdapter:
    """Live adapter wrapping :class:`StickyWorkerHost`.

    Projects the host's runtime state onto the new SSOT
    :class:`WorkerPoolStatus` fields (``alive``, ``state``,
    ``multimodel``, ``active_model_id``, ``loaded_models``) while
    keeping the legacy three numeric fields intact:

    * ``total_workers`` — always ``1`` for the single-process sticky
      worker (legacy parity); a multi-worker pool is intentionally
      outside the supported deployment shape.
    * ``busy_workers`` — ``1`` iff the host is in ``"busy"`` state.
    * ``queued_runs`` — ``0``; the sticky-worker host serialises runs
      on its single asyncio task and does not expose a queue depth,
      so the field is a fixed informational value.
    """

    __slots__ = ("_host",)

    def __init__(self, host: "StickyWorkerHost") -> None:
        if host is None:  # pragma: no cover — defensive
            raise ValueError("host must be a StickyWorkerHost instance")
        self._host = host

    async def status(self) -> WorkerPoolStatus:
        host = self._host
        snapshot = host.loaded_models_snapshot()
        now = time.time()
        loaded = tuple(
            LoadedModelInfo(
                model_id=entry.model_id,
                variant_id=entry.variant_id,
                last_used_at=entry.last_used_at,
                age_seconds=max(0.0, now - entry.last_used_at),
                state=entry.state,
            )
            for entry in snapshot
        )
        alive = host.alive
        state = host.state
        # The cross-field invariant on WorkerPoolStatus requires that
        # ``alive=False`` paired with a non-empty loaded_models tuple is
        # rejected. The host should already clear loaded_models on
        # _mark_dead, but we guard anyway to keep the route layer safe.
        if not alive and loaded:
            loaded = ()
        busy = 1 if state == "busy" else 0
        return WorkerPoolStatus(
            total_workers=1,
            busy_workers=busy,
            queued_runs=0,
            alive=alive,
            state=state,  # type: ignore[arg-type]
            active_model_id=host.active_model_id,
            multimodel=host.multimodel,
            loaded_models=loaded,
        )

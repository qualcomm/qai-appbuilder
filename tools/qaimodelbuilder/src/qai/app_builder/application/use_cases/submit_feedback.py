# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``SubmitFeedbackUseCase`` — persist a user-submitted run rating.

Wired by S9 close to back the ``POST /api/app-builder/feedback`` route
(previously a surface-only acknowledgement). Behaviour mirrors the
legacy ``backend/app_builder/api_routes.py:1646-1691`` flow:

1. Resolve the :class:`Run` (raises :class:`RunNotFoundError` so the
   route returns 404 rather than persist orphaned feedback).
2. Build a :class:`Feedback` aggregate with a fresh ULID, the supplied
   rating + comment + extra payload, and the wall-clock timestamp from
   the injected :class:`Clock`.
3. Persist via :class:`FeedbackRepositoryPort.save`.
4. Return the persisted aggregate so the route layer can surface the
   id (legacy returned just ``{"ok": True}``; the new shape augments
   the response without breaking it — the wire ``accepted`` flag is
   still ``True``, the ``feedback_id`` is informational).

The use case does not touch the ``app_builder_run`` row. The legacy
flow updated ``user_rating`` in-place to feed
:class:`InjectQualityScoreUseCase`; the new flow keeps feedback in its
own table and the quality-score injector reads
``FeedbackRepositoryPort.list_for_run`` instead. That keeps the
write path append-only and lets us preserve rating history.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.app_builder.application.ports import (
    FeedbackRepositoryPort,
    RunRepositoryPort,
)
from qai.app_builder.domain.feedback import Feedback
from qai.app_builder.domain.value_objects import RunId
from qai.platform.ids import IdGenerator
from qai.platform.time import Clock

__all__ = ["SubmitFeedbackUseCase", "SubmitFeedbackCommand"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitFeedbackCommand:
    """Inputs for :meth:`SubmitFeedbackUseCase.execute`.

    Defined as an explicit command VO so the route layer can build it
    from the Pydantic ``FeedbackRequestBody`` without leaking framework
    types into the application layer.
    """

    run_id: RunId
    rating: int
    text: str = ""
    extra: dict[str, object] | None = None


class SubmitFeedbackUseCase:
    """Persist a feedback row for a previously-finished Run.

    Constructor takes the run repository (for the existence check) and
    the feedback repository (for the actual write). The :class:`Clock`
    + :class:`IdGenerator` come from the platform DI so the use case
    stays testable without monkey-patching :mod:`time` or :mod:`uuid`.
    """

    __slots__ = ("_runs", "_feedbacks", "_clock", "_ids")

    def __init__(
        self,
        *,
        runs: RunRepositoryPort,
        feedbacks: FeedbackRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._runs = runs
        self._feedbacks = feedbacks
        self._clock = clock
        self._ids = ids

    async def execute(self, command: SubmitFeedbackCommand) -> Feedback:
        # 1. Existence check (raises RunNotFoundError → route maps to 404).
        await self._runs.get(command.run_id)

        # 2. Build aggregate.
        feedback = Feedback(
            id=self._ids.new_id(),
            run_id=command.run_id,
            rating=command.rating,
            text=command.text,
            extra=dict(command.extra or {}),
            created_at=self._clock.now(),
        )

        # 3. Persist.
        await self._feedbacks.save(feedback)

        # 4. Return for the route layer.
        return feedback

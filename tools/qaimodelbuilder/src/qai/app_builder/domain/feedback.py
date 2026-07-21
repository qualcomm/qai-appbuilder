# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``Feedback`` value object — user-submitted rating + comment for a Run.

Models the legacy ``data/app_builder_audit.jsonl`` ``feedback`` event +
the ``app_builder_run.user_rating`` column read by the LLM Pack catalog
(see :class:`InjectQualityScoreUseCase`). The new schema persists
feedback rows in a dedicated ``app_builder_feedback`` table (migration
011) so:

* multiple ratings per run are preserved chronologically (the operator
  can revise or augment a rating without overwriting history);
* free-form comments and structured ``extra`` payloads round-trip
  losslessly;
* :class:`InjectQualityScoreUseCase` keeps reading the latest row per
  run when computing per-pack quality bias.

Rating range
------------

The HTTP boundary accepts ``rating ∈ [1, 5]`` (Likert-style). The legacy
``-1 / 0 / +1`` thumb-style rating is mapped at the route layer when the
legacy frontend posts to the new endpoint; the domain VO stores the
normalised ``[1, 5]`` value to keep aggregation arithmetic uniform.

The VO has no behaviour beyond input validation; persistence is done
via :class:`FeedbackRepositoryPort` (see
:mod:`qai.app_builder.application.ports`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from qai.app_builder.domain.value_objects import RunId

__all__ = ["Feedback"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Feedback:
    """A single ``app_builder_feedback`` row.

    All datetimes MUST be tz-aware (UTC by convention; the adapter
    preserves whatever zone the producing :class:`Clock` supplied).

    Fields
    ------

    * :attr:`id` — opaque feedback row id (ULID-shaped string assigned
      by the adapter on save).
    * :attr:`run_id` — :class:`RunId` the feedback applies to. Soft
      reference; the FK constraint in migration 011 uses
      ``ON DELETE CASCADE`` so deleting the run also removes its
      feedback rows.
    * :attr:`rating` — Likert-style ``1..5`` integer (1 = worst,
      5 = best). ``None`` is rejected at this layer; the route layer
      maps ``None`` to ``3`` (neutral) before constructing the VO when
      the caller only supplies a comment.
    * :attr:`text` — free-form comment, ``""`` when the user did not
      supply one. Capped at 4000 chars (matches the
      :class:`FeedbackRequestBody` Pydantic ``max_length``).
    * :attr:`extra` — opaque structured payload (``dict[str, object]``)
      preserved as JSON. Default empty.
    * :attr:`created_at` — wall-clock timestamp of submission.
    """

    id: str
    run_id: RunId
    rating: int
    text: str = ""
    extra: dict[str, object] = field(default_factory=dict)
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("Feedback.id must be a non-empty str")
        if not isinstance(self.rating, int) or isinstance(self.rating, bool):
            raise ValueError("Feedback.rating must be int")
        if self.rating < 1 or self.rating > 5:
            raise ValueError(
                f"Feedback.rating must be in [1, 5], got {self.rating}"
            )
        if not isinstance(self.text, str):
            raise ValueError("Feedback.text must be str")
        if len(self.text) > 4000:
            raise ValueError(
                f"Feedback.text must be ≤ 4000 chars, got {len(self.text)}"
            )
        if not isinstance(self.extra, dict):
            raise ValueError("Feedback.extra must be a dict")
        if self.created_at.tzinfo is None:
            raise ValueError("Feedback.created_at must be tz-aware")

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""AdHoc implementation task — a frozen value object (DISC-1 §22.10#1).

DISC-1 step3 turns a ``directed_implement`` intent into a single
implementation-mode speaker turn.  :class:`AdHocImplementationTask` is the small
immutable record describing that one work-item — the task text, who it is
assigned to, where it came from, and the ULID that ties the run's audit log +
(future) per-run budget bookkeeping together.

一期 minimal scope (§22.10#1): the orchestrator constructs ONE of these per
``directed_implement`` turn purely as the log/audit carrier — the run() entry
semantics are carried by ``_run_implementation_path``.  Per-run budget
accounting (max tool calls / file edits / wall-clock) lands in 二期; this record
is the seam those layers will read from.

Layering: ``application/use_cases`` — stdlib only; no ports / domain / adapters
(mirrors the frozen-dataclass style of :class:`ImplementationBudget`).
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.chat.application.use_cases.implementation_plan import FeatureItem

__all__ = [
    "AdHocImplementationTask",
    "PlannedImplementationTask",
    "ImplementationTask",
]


@dataclass(frozen=True, slots=True)
class AdHocImplementationTask:
    """One implementation work-item derived from a directed @mention (§22.10#1).

    * ``task_text`` — the user's instruction (the implement directive verbatim).
    * ``assigned_participant_id`` — the resolved speaker (a participant id, not a
      display-name mention).
    * ``run_id`` — the ULID minted for THIS run (audit log correlation + future
      per-run budget keying).  Caller supplies it (the orchestrator's id
      generator) so the value object stays pure / side-effect-free.
    * ``source`` — provenance marker; ``"directed_mention"`` for the only one-期
      path (an @mention + implement verb).
    * ``context_summary`` — an OPTIONAL short summary of the discussion
      conclusions this task executes against (``None`` one-期).
    """

    task_text: str
    assigned_participant_id: str
    run_id: str
    source: str = "directed_mention"
    context_summary: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedImplementationTask:
    """One planned implementation work-item derived from a :class:`FeatureItem` (§22.10#1).

    二期 path: the orchestrator turns each confirmed :class:`FeatureItem` of an
    :class:`~qai.chat.application.use_cases.implementation_plan.ImplementationPlan`
    into a ``PlannedImplementationTask`` and feeds it through the SAME run(task)
    entry the 一期 :class:`AdHocImplementationTask` uses (§22.10#1:
    ``FeatureItem → PlannedImplementationTask → 同一 run(task) 入口``).  This is
    the small immutable record carrying the item plus the run correlation id.

    * ``item`` — the :class:`FeatureItem` to implement (carries its own id /
      title / acceptance_criteria / assigned_role).
    * ``run_id`` — the ULID minted for THIS run (audit log correlation + per-run
      budget keying).  Caller supplies it so the value object stays pure.
    * ``source`` — provenance marker; ``"planned_item"`` for the 二期 path (a
      confirmed plan item, vs the 一期 ``"directed_mention"`` ad-hoc path).
    * ``context_summary`` — an OPTIONAL short summary of the execution context
      this task runs against (§22.6: completed-item summaries + workspace
      changes).  Filled in 二期-step3; ``None`` until then.

    🔴 二期-step1: defined here as the seam step3's run() entry will consume; it
    is NOT yet constructed on any execute path (zero behaviour change).
    """

    item: FeatureItem
    run_id: str
    source: str = "planned_item"
    context_summary: str | None = None


#: Union of the two implementation-task shapes a future unified run() entry
#: (二期-step3) will accept: the 一期 ad-hoc directed-mention task and the 二期
#: planned-item task.  Defined here so step3 can type its entry against one
#: alias rather than re-declaring the union at the call site.
ImplementationTask = AdHocImplementationTask | PlannedImplementationTask

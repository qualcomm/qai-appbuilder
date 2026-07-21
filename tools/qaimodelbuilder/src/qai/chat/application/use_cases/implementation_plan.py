# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Implementation plan data model — FeatureItem + ImplementationPlan (DISC-1 二期-step1).

DISC-1 二期 turns a converged discussion into a concrete, persistable plan:
a list of :class:`FeatureItem` work-items plus an :class:`ImplementationPlan`
envelope that carries the run-level phase state-machine (§22.8) and bookkeeping.
This module is the **pure data geology** for that feature (§22.3a 二期-step1
scope = backend data model + serialization + meta read/write helpers + phase
transition validation ONLY — NO extractor LLM call, NO execution, NO SSE frame,
NO front-end).

The plan is persisted at ``meta["discussion"]["implementation"]`` (§22.8) — a
backend-managed, §3.1-additive ``meta`` key.  The envelope carries a ``version``
field so a future schema bump can migrate in place.

Control-plane vs data-plane (§22.9): the short string fields here
(``result_summary`` / ``last_error`` / ``description`` / ``title``) are the
**control plane** — bounded summaries only.  Full model output, logs, and
diffs travel through the message system / run audit log, NOT through these
fields.  Hence every free-text field has a hard length cap (below) so ``meta``
cannot balloon.

State-Truth-First (AGENTS.md §🔴): every ``*_from_dict`` deserializer is total
and defensive — a malformed / missing / wrong-typed field degrades to a safe
default (illegal ``status`` → ``"pending"``, illegal ``phase`` → ``"none"``,
non-``str`` → ``""``, non-``list`` → empty, non-``dict`` plan → ``None``) rather
than raising.  Persisted ``meta`` is user-influenceable and may be stale across
schema versions, so deserialization never trusts its shape.

Layering: ``application/use_cases`` — stdlib only; no ports / domain / adapters
/ cross-context imports (mirrors :mod:`discussion_intent` /
:mod:`implementation_task`).  This module does NOT import
``implementation_task`` (the dependency is one-way: ``implementation_task``
imports ``FeatureItem`` from here, never the reverse).

🔴 Zero behaviour change (二期-step1): nothing in this module is wired into the
execute / ``_run_*`` paths.  These structures are only read / written when a
later step (step2 extractor / step3 executor) explicitly calls them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "FeatureItemStatus",
    "ImplementationPhase",
    "FEATURE_ITEM_STATUSES",
    "IMPLEMENTATION_PHASES",
    "FeatureItem",
    "ImplementationPlan",
    "feature_item_to_dict",
    "feature_item_from_dict",
    "plan_to_dict",
    "plan_from_dict",
    "is_valid_phase_transition",
    "ALLOWED_PHASE_TRANSITIONS",
    "IMPLEMENTATION_KEY",
    "read_implementation_plan",
    "write_implementation_plan",
    "topological_item_order",
    # length caps (exported so callers / tests share one口径)
    "MAX_TITLE_LEN",
    "MAX_DESCRIPTION_LEN",
    "MAX_RESULT_SUMMARY_LEN",
    "MAX_LAST_ERROR_LEN",
    "MAX_VERIFY_COMMAND_LEN",
]


# ---------------------------------------------------------------------------
# Vocabulary (stable contracts)
# ---------------------------------------------------------------------------
FeatureItemStatus = Literal[
    "pending",
    "in_progress",
    "done",
    "failed",
    "skipped",
]
#: The runtime tuple form of :data:`FeatureItemStatus`, used for validation /
#: degrade-to-default in :func:`feature_item_from_dict`.
FEATURE_ITEM_STATUSES: tuple[str, ...] = (
    "pending",
    "in_progress",
    "done",
    "failed",
    "skipped",
)

ImplementationPhase = Literal[
    "none",
    "planning",
    "planned",
    "planning_failed",
    "implementing",
    "paused",
    "failed",
    "completed",
]
#: Runtime tuple form of :data:`ImplementationPhase` for validation.
IMPLEMENTATION_PHASES: tuple[str, ...] = (
    "none",
    "planning",
    "planned",
    "planning_failed",
    "implementing",
    "paused",
    "failed",
    "completed",
)


# ---------------------------------------------------------------------------
# Length caps (§22.9 control-plane — bounded summaries only; full output goes
# through the message system / run log, never through these fields)
# ---------------------------------------------------------------------------
MAX_TITLE_LEN = 200
MAX_DESCRIPTION_LEN = 2000
MAX_RESULT_SUMMARY_LEN = 2000
MAX_LAST_ERROR_LEN = 1000
#: DISC-1 三期 step5 / 完成判定 B: a per-item *verification command* the user
#: configures (e.g. ``pytest tests/foo.py``).  After the implementing agent
#: finishes, the orchestrator runs this command through the shared ai_coding
#: exec/sandbox channel and uses its exit code as an OBJECTIVE done/failed
#: judge (more trustworthy than the agent's self-report).  It is a short
#: command string, NOT a place for full scripts — hence the cap.
MAX_VERIFY_COMMAND_LEN = 500


def _truncate(value: str, limit: int) -> str:
    """Hard-cap ``value`` to ``limit`` characters (no ellipsis — bytes-stable)."""
    return value if len(value) <= limit else value[:limit]


def _coerce_str(value: Any) -> str:
    """Total ``str`` coercion: non-``str`` → ``""`` (State-Truth-First)."""
    return value if isinstance(value, str) else ""


def _coerce_opt_str(value: Any, limit: int | None = None) -> str | None:
    """Total optional-``str`` coercion: ``None`` stays ``None``; non-``str`` → ``None``.

    A present ``str`` is truncated to ``limit`` when given.
    """
    if value is None or not isinstance(value, str):
        return None
    return _truncate(value, limit) if limit is not None else value


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    """Total ``tuple[str, ...]`` coercion: non-``list``/``tuple`` → ``()``.

    Each element is coerced to ``str``; non-``str`` elements are dropped (rather
    than stringified) so a malformed list cannot inject ``"None"`` / ``"123"``
    placeholder text into the control plane.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _coerce_int(value: Any, default: int = 0) -> int:
    """Total ``int`` coercion: non-``int`` (incl. ``bool``) → ``default``."""
    # bool is an int subclass — reject it explicitly so True/False never count.
    if isinstance(value, bool):
        return default
    return value if isinstance(value, int) else default


# ---------------------------------------------------------------------------
# FeatureItem (§22.4)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FeatureItem:
    """One discrete feature/work-item in an implementation plan (§22.4).

    Persisted at ``meta["discussion"]["implementation"]["items"]``.  Immutable;
    state transitions produce a NEW item (``dataclasses.replace``) rather than
    mutating in place, matching the frozen-value-object style of the rest of the
    discussion layer.

    Fields:

    * ``id`` — ULID minted by the caller (the orchestrator's id generator) so the
      value object stays pure / side-effect free.
    * ``title`` / ``description`` — short human labels (capped at
      :data:`MAX_TITLE_LEN` / :data:`MAX_DESCRIPTION_LEN`).
    * ``acceptance_criteria`` — bullet list of "done when…" checks.
    * ``suggested_role`` — the participant id the extractor proposes; ``None``
      until extracted.
    * ``assigned_role`` — the participant id pinned after user confirmation;
      ``None`` until confirmed.
    * ``status`` — one of :data:`FEATURE_ITEM_STATUSES`.
    * ``result_summary`` — SHORT control-plane summary of the outcome
      (:data:`MAX_RESULT_SUMMARY_LEN`); full output lives in the message system.
    * ``depends_on`` — item ids this one depends on (二期 stores but does NOT
      execute dependency ordering).
    * ``source_refs`` — discussion message ids / short references this item was
      derived from.
    * ``attempt_count`` — how many run attempts have been made.
    * ``started_at`` / ``finished_at`` — ISO-8601 timestamps (``None`` until set).
    * ``last_error`` — SHORT control-plane error summary (:data:`MAX_LAST_ERROR_LEN`).
    """

    id: str
    title: str = ""
    description: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    suggested_role: str | None = None
    assigned_role: str | None = None
    status: FeatureItemStatus = "pending"
    result_summary: str | None = None
    depends_on: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    attempt_count: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    last_error: str | None = None
    #: DISC-1 三期 step5 / 完成判定 B — optional verification command run through
    #: the shared ai_coding exec/sandbox channel after the agent finishes; a
    #: non-zero exit marks the item ``failed``.  ``None`` ⇒ no command configured
    #: (judgement falls back to the simplified A rule: clean finish + no tool
    #: error).  Tail-appended (§3.1 additive).
    verify_command: str | None = None

    @staticmethod
    def new(
        *,
        id: str,
        title: str = "",
        description: str = "",
        acceptance_criteria: tuple[str, ...] | list[str] = (),
        suggested_role: str | None = None,
        assigned_role: str | None = None,
        status: FeatureItemStatus = "pending",
        result_summary: str | None = None,
        depends_on: tuple[str, ...] | list[str] = (),
        source_refs: tuple[str, ...] | list[str] = (),
        attempt_count: int = 0,
        started_at: str | None = None,
        finished_at: str | None = None,
        last_error: str | None = None,
        verify_command: str | None = None,
    ) -> FeatureItem:
        """Construct a :class:`FeatureItem`, applying length caps + tuple coercion.

        The ``id`` is supplied by the caller (the orchestrator mints the ULID) so
        the factory stays pure.  Free-text fields are truncated to their caps and
        list inputs are normalised to tuples (so callers may pass either).
        """
        return FeatureItem(
            id=id,
            title=_truncate(_coerce_str(title), MAX_TITLE_LEN),
            description=_truncate(_coerce_str(description), MAX_DESCRIPTION_LEN),
            acceptance_criteria=_coerce_str_tuple(acceptance_criteria),
            suggested_role=suggested_role,
            assigned_role=assigned_role,
            status=status if status in FEATURE_ITEM_STATUSES else "pending",
            result_summary=_coerce_opt_str(
                result_summary, MAX_RESULT_SUMMARY_LEN
            ),
            depends_on=_coerce_str_tuple(depends_on),
            source_refs=_coerce_str_tuple(source_refs),
            attempt_count=_coerce_int(attempt_count),
            started_at=started_at,
            finished_at=finished_at,
            last_error=_coerce_opt_str(last_error, MAX_LAST_ERROR_LEN),
            verify_command=_coerce_opt_str(
                verify_command, MAX_VERIFY_COMMAND_LEN
            ),
        )


def feature_item_to_dict(item: FeatureItem) -> dict[str, Any]:
    """Serialise a :class:`FeatureItem` to a JSON-safe ``dict`` (tuples → lists)."""
    return {
        "id": item.id,
        "title": item.title,
        "description": item.description,
        "acceptance_criteria": list(item.acceptance_criteria),
        "suggested_role": item.suggested_role,
        "assigned_role": item.assigned_role,
        "status": item.status,
        "result_summary": item.result_summary,
        "depends_on": list(item.depends_on),
        "source_refs": list(item.source_refs),
        "attempt_count": item.attempt_count,
        "started_at": item.started_at,
        "finished_at": item.finished_at,
        "last_error": item.last_error,
        "verify_command": item.verify_command,
    }


def feature_item_from_dict(raw: Any) -> FeatureItem | None:
    """Deserialise a :class:`FeatureItem` defensively (State-Truth-First).

    A non-``dict`` input → ``None`` (the caller drops it).  Every field degrades
    to a safe default; an item with no usable ``id`` is still constructed with an
    empty id (the caller may filter it) rather than raising.
    """
    if not isinstance(raw, dict):
        return None
    return FeatureItem(
        id=_coerce_str(raw.get("id")),
        title=_truncate(_coerce_str(raw.get("title")), MAX_TITLE_LEN),
        description=_truncate(
            _coerce_str(raw.get("description")), MAX_DESCRIPTION_LEN
        ),
        acceptance_criteria=_coerce_str_tuple(raw.get("acceptance_criteria")),
        suggested_role=_coerce_opt_str(raw.get("suggested_role")),
        assigned_role=_coerce_opt_str(raw.get("assigned_role")),
        status=(
            raw.get("status")
            if raw.get("status") in FEATURE_ITEM_STATUSES
            else "pending"
        ),
        result_summary=_coerce_opt_str(
            raw.get("result_summary"), MAX_RESULT_SUMMARY_LEN
        ),
        depends_on=_coerce_str_tuple(raw.get("depends_on")),
        source_refs=_coerce_str_tuple(raw.get("source_refs")),
        attempt_count=_coerce_int(raw.get("attempt_count")),
        started_at=_coerce_opt_str(raw.get("started_at")),
        finished_at=_coerce_opt_str(raw.get("finished_at")),
        last_error=_coerce_opt_str(raw.get("last_error"), MAX_LAST_ERROR_LEN),
        verify_command=_coerce_opt_str(
            raw.get("verify_command"), MAX_VERIFY_COMMAND_LEN
        ),
    )


# ---------------------------------------------------------------------------
# ImplementationPlan (§22.8 envelope + phase state machine)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ImplementationPlan:
    """The run-level implementation plan envelope (§22.8).

    Persisted at ``meta["discussion"]["implementation"]``.  Carries the ordered
    :class:`FeatureItem` list plus the run-level phase state-machine and
    bookkeeping.  Immutable (frozen) — transitions produce a new plan.

    Fields:

    * ``version`` — schema version (``1`` today); reserved for in-place migration.
    * ``phase`` — one of :data:`IMPLEMENTATION_PHASES`; the run state machine
      (see :data:`ALLOWED_PHASE_TRANSITIONS` / :func:`is_valid_phase_transition`).
    * ``run_id`` — ULID of the active/last run; ``None`` before any run.
    * ``current_item`` — id of the item currently being implemented; ``None``
      when idle.
    * ``items`` — the ordered feature items.
    * ``created_at`` / ``updated_at`` — ISO-8601 timestamps.
    * ``last_error`` — SHORT control-plane error summary (:data:`MAX_LAST_ERROR_LEN`).
    * ``stopped_by_user`` — whether the run was paused/stopped by an explicit
      user action (vs an internal failure).
    * ``paused_at`` — ISO-8601 timestamp of the pause; ``None`` when not paused.
    """

    version: int = 1
    phase: ImplementationPhase = "none"
    run_id: str | None = None
    current_item: str | None = None
    items: tuple[FeatureItem, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    last_error: str | None = None
    stopped_by_user: bool = False
    paused_at: str | None = None


def plan_to_dict(plan: ImplementationPlan) -> dict[str, Any]:
    """Serialise an :class:`ImplementationPlan` to a JSON-safe ``dict``."""
    return {
        "version": plan.version,
        "phase": plan.phase,
        "run_id": plan.run_id,
        "current_item": plan.current_item,
        "items": [feature_item_to_dict(item) for item in plan.items],
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
        "last_error": plan.last_error,
        "stopped_by_user": plan.stopped_by_user,
        "paused_at": plan.paused_at,
    }


def plan_from_dict(raw: Any) -> ImplementationPlan | None:
    """Deserialise an :class:`ImplementationPlan` defensively (State-Truth-First).

    A non-``dict`` input → ``None``.  Items are deserialised one-by-one and any
    that fail (non-``dict``) are dropped (rather than failing the whole plan).
    An illegal ``phase`` degrades to ``"none"``; ``version`` falls back to ``1``.
    """
    if not isinstance(raw, dict):
        return None
    raw_items = raw.get("items")
    items: list[FeatureItem] = []
    if isinstance(raw_items, (list, tuple)):
        for entry in raw_items:
            parsed = feature_item_from_dict(entry)
            if parsed is not None:
                items.append(parsed)
    phase = raw.get("phase")
    return ImplementationPlan(
        version=_coerce_int(raw.get("version"), default=1),
        phase=phase if phase in IMPLEMENTATION_PHASES else "none",
        run_id=_coerce_opt_str(raw.get("run_id")),
        current_item=_coerce_opt_str(raw.get("current_item")),
        items=tuple(items),
        created_at=_coerce_str(raw.get("created_at")),
        updated_at=_coerce_str(raw.get("updated_at")),
        last_error=_coerce_opt_str(raw.get("last_error"), MAX_LAST_ERROR_LEN),
        stopped_by_user=bool(raw.get("stopped_by_user")),
        paused_at=_coerce_opt_str(raw.get("paused_at")),
    )


# ---------------------------------------------------------------------------
# Phase state machine (§22.8 — legal transition graph)
# ---------------------------------------------------------------------------
#: The legal phase-transition graph (§22.8):
#:
#:   none → planning → planned / planning_failed
#:   planned → implementing
#:   implementing → completed / paused / failed
#:   paused → implementing
#:   failed / completed → planning / implementing  (re-extract / retry)
ALLOWED_PHASE_TRANSITIONS: dict[str, frozenset[str]] = {
    "none": frozenset({"planning"}),
    "planning": frozenset({"planned", "planning_failed"}),
    "planned": frozenset({"implementing"}),
    "planning_failed": frozenset({"planning"}),
    "implementing": frozenset({"completed", "paused", "failed"}),
    "paused": frozenset({"implementing"}),
    "failed": frozenset({"planning", "implementing"}),
    "completed": frozenset({"planning", "implementing"}),
}


def is_valid_phase_transition(from_phase: str, to_phase: str) -> bool:
    """Return whether ``from_phase → to_phase`` is a legal transition (§22.8).

    Pure + total: an unknown ``from_phase`` (not in
    :data:`ALLOWED_PHASE_TRANSITIONS`) yields ``False`` rather than raising, so a
    stale persisted phase can never crash the validator (State-Truth-First).
    """
    return to_phase in ALLOWED_PHASE_TRANSITIONS.get(from_phase, frozenset())


# ---------------------------------------------------------------------------
# meta read / write helpers (§22.8 — meta["discussion"]["implementation"])
# ---------------------------------------------------------------------------
#: The ``meta["discussion"]`` sub-key the plan is persisted under (§22.8). A
#: backend-managed, §3.1-additive key (registered in
#: ``participant_management._PRESERVED_DISCUSSION_KEYS``).
IMPLEMENTATION_KEY = "implementation"


def read_implementation_plan(
    discussion: dict[str, Any] | None,
) -> ImplementationPlan | None:
    """Read the plan from a ``meta["discussion"]`` blob (State-Truth-First).

    Returns ``None`` when ``discussion`` is missing / not a ``dict`` / carries no
    (or a malformed) ``implementation`` sub-blob.  Never raises.

    🔴 二期-step1: this helper exists but is NOT called from any execute /
    ``_run_*`` path — only a later step (step2/step3) wires it in.
    """
    if not isinstance(discussion, dict):
        return None
    return plan_from_dict(discussion.get(IMPLEMENTATION_KEY))


def write_implementation_plan(
    discussion: dict[str, Any],
    plan: ImplementationPlan,
) -> dict[str, Any]:
    """Return a NEW ``meta["discussion"]`` blob with the plan written (§3.1 additive).

    Does NOT mutate ``discussion`` in place — returns a shallow copy with the
    ``implementation`` key (re)set, preserving every other discussion key.  The
    ``implementation`` key is appended at the tail conceptually; physical key
    order is irrelevant for JSON ``meta``.

    🔴 二期-step1: not called from any execute path; wired in a later step.
    """
    base = dict(discussion) if isinstance(discussion, dict) else {}
    base[IMPLEMENTATION_KEY] = plan_to_dict(plan)
    return base


def topological_item_order(
    items: tuple[FeatureItem, ...] | list[FeatureItem],
) -> tuple[tuple[FeatureItem, ...], bool]:
    """Order items so every dependency precedes its dependants (DISC-1 三期-step1).

    Returns ``(ordered_items, ok)``:

    * ``ok=True``  — a valid topological order was produced (Kahn's algorithm).
    * ``ok=False`` — the dependency graph has a CYCLE (or is otherwise
      unsatisfiable); the items are returned in their ORIGINAL order so the run
      degrades gracefully instead of dropping items (State-Truth-First: never
      lose work to a bad ``depends_on``).

    Edges only count dependencies that reference a KNOWN item id; a
    ``depends_on`` pointing at a missing id is ignored (it cannot block anything
    that exists). Ties (same in-degree) preserve the original relative order so
    the result is deterministic and, for a dependency-free plan, identical to the
    input — i.e. zero behaviour change when no item declares ``depends_on``.
    """
    seq = list(items)
    if not seq:
        return (), True
    ids = {it.id for it in seq if it.id}
    index = {it.id: i for i, it in enumerate(seq)}
    # Build in-degree + adjacency over KNOWN deps only.
    deps: dict[str, list[str]] = {
        it.id: [d for d in it.depends_on if d in ids and d != it.id]
        for it in seq
        if it.id
    }
    indegree: dict[str, int] = {it.id: 0 for it in seq if it.id}
    dependants: dict[str, list[str]] = {it.id: [] for it in seq if it.id}
    for node, node_deps in deps.items():
        for d in node_deps:
            indegree[node] += 1
            dependants[d].append(node)
    # Kahn: repeatedly take the lowest-original-index node with in-degree 0.
    ready = sorted(
        (n for n, deg in indegree.items() if deg == 0), key=lambda n: index[n]
    )
    ordered_ids: list[str] = []
    while ready:
        node = ready.pop(0)
        ordered_ids.append(node)
        newly_ready: list[str] = []
        for dep in dependants[node]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                newly_ready.append(dep)
        if newly_ready:
            ready = sorted(
                ready + newly_ready, key=lambda n: index[n]
            )
    if len(ordered_ids) != len(indegree):
        # A cycle left some nodes unscheduled → degrade to original order.
        return tuple(seq), False
    by_id = {it.id: it for it in seq if it.id}
    ordered = [by_id[i] for i in ordered_ids]
    # Items with a blank id (should not happen) are appended in original order.
    ordered.extend(it for it in seq if not it.id)
    return tuple(ordered), True

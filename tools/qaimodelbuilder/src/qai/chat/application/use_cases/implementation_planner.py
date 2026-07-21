# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Feature-item extractor + planning orchestration (DISC-1 二期-step2 — §22.4).

After a discussion converges, the user may ask to turn the conclusion into a
concrete, reviewable :class:`ImplementationPlan`.  This module owns the
**planning** half of that flow (NO execution — §22.2#2):

* :class:`FeatureItemExtractorPort` — the application-layer Protocol the
  orchestrator wires a concrete LLM-backed extractor behind (dependency
  inversion; the adapter lives in
  :mod:`qai.chat.adapters.llm_feature_item_extractor`).
* :func:`build_extraction_inputs` — a PURE function that assembles the extractor
  prompt inputs from the conversation + discussion config, honouring the §22.4
  input priority (judge conclusion → recent turns → roster persona → the user's
  first goal → constraints).  No LLM call, no IO.
* :func:`parse_extraction_response` — the PURE **three-layer JSON fallback**
  (§22.4 L1/L2/L3): strict JSON + schema validation (L1) → ``"repair_needed"``
  signal for the caller's one-shot repair (L2) → ``"failed"`` for un-parseable
  garbage (L3).  Reuses :func:`feature_item_from_dict` for tolerant
  field-normalisation.
* :class:`OrchestratePlanningUseCase` — wires the injected extractor + an id
  generator + a clock into a single ``plan(...)`` entry that ALWAYS returns an
  :class:`ImplementationPlan`: a ``phase="planned"`` plan on success, or a
  ``phase="planning_failed"`` plan (carrying the raw reply in ``last_error``)
  when the extractor fails / times out / returns ``None`` (🔴 State-Truth-First:
  the use case NEVER raises, NEVER auto-implements — §22.2#2 — and never blocks
  on a hung extractor).

🔴 Zero behaviour change (二期-step2): nothing here is wired into the execute /
``_run_*`` streaming loop, no route, no SSE frame.  The use case is a standalone
entry a later step (step3) explicitly triggers; today it is only constructed (in
DI, for step3 to consume) and exercised by tests.  The whole capability remains
unreachable while the ``implementation_enabled`` flag is OFF.

``suggested_role`` vs ``assigned_role`` (§22.4#5): the extractor fills ONLY
``suggested_role`` (its proposed participant id).  ``assigned_role`` stays
``None`` until the user confirms the plan in a later UI step — the planner never
pins an assignment on its own.

Layering: ``application/use_cases`` — depends on the platform ``Clock`` /
``IdGenerator``, the chat domain (``Conversation`` / ``Participant``), and the
sibling pure modules (``implementation_plan`` / ``implementation_defaults``).  No
adapters, no other context, so ``layered-chat`` / ``context-isolation`` hold.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace as _dc_replace
from typing import Any, Protocol, runtime_checkable

from qai.chat.application.use_cases.implementation_defaults import (
    MAX_FEATURE_ITEMS,
    MIN_FEATURE_ITEMS,
    resolve_planner_model,
    resolve_planner_timeout_ms,
)
from qai.chat.application.use_cases.implementation_plan import (
    FeatureItem,
    ImplementationPlan,
    MAX_LAST_ERROR_LEN,
    feature_item_from_dict,
)
from qai.chat.domain.content import MessageRole
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.participant import Participant
from qai.platform.ids import IdGenerator
from qai.platform.time import Clock
from qai.platform.logging import get_logger

_log = get_logger(__name__)

__all__ = [
    "FeatureItemExtractorPort",
    "ExtractionInputs",
    "ExtractionStatus",
    "build_extraction_inputs",
    "parse_extraction_response",
    "OrchestratePlanningUseCase",
    "DEFAULT_RECENT_TURNS",
]

#: How many of the MOST RECENT conversation turns the extractor is shown (after
#: the judge conclusion, which is highest priority — §22.4).  Bounded so a long
#: discussion does not balloon the extractor prompt; the conclusion already
#: distils the thread, so the recent turns are supporting context only.
DEFAULT_RECENT_TURNS = 8

#: Hard caps on the free-text fields lifted out of the conversation so a runaway
#: turn cannot blow the extractor prompt (control-plane discipline §22.9).
_MAX_CONCLUSION_CHARS = 4000
_MAX_TURN_CHARS = 800
_MAX_GOAL_CHARS = 1000
_MAX_PERSONA_CHARS = 400

#: The three-layer JSON fallback status (§22.4).  ``"ok"`` → items parsed;
#: ``"repair_needed"`` → strict parse failed, the caller may run ONE repair
#: round; ``"failed"`` → un-parseable garbage, give up (→ ``planning_failed``).
ExtractionStatus = str  # Literal["ok", "repair_needed", "failed"] at call sites


@runtime_checkable
class FeatureItemExtractorPort(Protocol):
    """Abstract feature-item extractor (DISC-1 二期-step2 — §22.4).

    ONE low-temperature LLM turn that reads a converged discussion's conclusion
    (+ supporting context) and emits a structured list of
    :class:`FeatureItem`-shaped work items, packaged as a ``phase="planned"``
    :class:`ImplementationPlan`.

    Contract (mirrors :class:`IntentClassifierPort`'s never-raise discipline):

    * **NEVER raises** — any failure / timeout / ERROR frame / un-parseable
      reply returns ``None`` (the in-band "the orchestrator will package a
      ``planning_failed`` plan" signal) OR a ``phase="planning_failed"`` plan it
      built itself.  The use case wraps the call in ``asyncio.wait_for`` so the
      timeout lives in ONE place.
    * **Side-effect free** w.r.t. the conversation — it only reads, never
      persists, and NEVER triggers execution (§22.2#2: planning ≠ implementing).
    * Fills only ``suggested_role`` on each item; ``assigned_role`` stays
      ``None`` until the user confirms (§22.4#5).
    """

    async def extract(
        self,
        *,
        conclusion_text: str,
        recent_turns: tuple[str, ...],
        roster: tuple[tuple[str, str], ...],
        user_goal: str,
        model_hint: str | None,
        timeout_ms: int,
    ) -> ImplementationPlan | None:
        """Extract a ``planned`` plan from the discussion conclusion, or ``None``.

        Args:
            conclusion_text: the judge summary / convergence conclusion (highest
                priority context — §22.4).
            recent_turns: the most recent discussion turn texts (supporting
                context), most-recent-last.
            roster: ``(participant_id, persona_or_display)`` pairs the extractor
                may propose as ``suggested_role``.
            user_goal: the user's first / driving goal message.
            model_hint: the resolved extraction model id (the orchestrator runs
                the §22.12#4 ladder and passes the result), or ``None`` to let
                the adapter fall through to its own default.
            timeout_ms: advisory budget; the use case ALSO enforces it via
                ``asyncio.wait_for``.

        Returns:
            A ``phase="planned"`` plan on success, a ``phase="planning_failed"``
            plan it built, or ``None`` (the use case packages ``planning_failed``).
        """
        ...


class ExtractionInputs:
    """The pure, assembled inputs for one extraction (output of
    :func:`build_extraction_inputs`).

    A plain value holder (not frozen — it carries only immutable primitives /
    tuples) so the adapter / tests can read named fields rather than juggling a
    dict.  All free-text is already length-capped.
    """

    __slots__ = ("conclusion_text", "recent_turns", "roster", "user_goal")

    def __init__(
        self,
        *,
        conclusion_text: str,
        recent_turns: tuple[str, ...],
        roster: tuple[tuple[str, str], ...],
        user_goal: str,
    ) -> None:
        self.conclusion_text = conclusion_text
        self.recent_turns = recent_turns
        self.roster = roster
        self.user_goal = user_goal

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view (handy for logging / the adapter prompt builder)."""
        return {
            "conclusion_text": self.conclusion_text,
            "recent_turns": list(self.recent_turns),
            "roster": [list(pair) for pair in self.roster],
            "user_goal": self.user_goal,
        }


def _truncate(value: str, limit: int) -> str:
    """Hard-cap ``value`` to ``limit`` characters (bytes-stable, no ellipsis)."""
    return value if len(value) <= limit else value[:limit]


def _message_text(message: Any) -> str:
    """Best-effort, total extraction of a message's text (State-Truth-First).

    Reads ``message.content.text``; a malformed / missing shape degrades to
    ``""`` rather than raising.
    """
    content = getattr(message, "content", None)
    text = getattr(content, "text", None)
    return text if isinstance(text, str) else ""


def build_extraction_inputs(
    *,
    conv: Conversation,
    discussion: dict[str, Any] | None,
    roster: tuple[Participant, ...],
) -> ExtractionInputs:
    """Assemble the extractor inputs from the conversation (§22.4 — PURE).

    Input priority (§22.4):

    1. **conclusion** — the highest-priority context.  Preferred source: an
       explicit judge summary persisted on the discussion config
       (``discussion["judge_summary"]`` / ``["conclusion"]``); else the LAST
       assistant turn (the judge / final speaker summarises the thread).
    2. **recent turns** — the last :data:`DEFAULT_RECENT_TURNS` turn texts as
       supporting context (most-recent-last).
    3. **roster persona** — ``(participant_id, persona_or_display_name)`` pairs
       the extractor may cite as ``suggested_role``.
    4. **user goal** — the user's FIRST message (the driving objective).

    No LLM call, no IO — deterministic from its inputs.
    """
    d = discussion or {}
    messages = list(getattr(conv, "messages", []) or [])

    # ── (1) conclusion: explicit judge summary wins; else last assistant turn ─
    conclusion = ""
    for key in ("judge_summary", "conclusion", "convergence_summary"):
        candidate = d.get(key)
        if isinstance(candidate, str) and candidate.strip():
            conclusion = candidate.strip()
            break
    if not conclusion:
        for message in reversed(messages):
            if getattr(message, "role", None) is MessageRole.ASSISTANT:
                text = _message_text(message).strip()
                if text:
                    conclusion = text
                    break
    conclusion = _truncate(conclusion, _MAX_CONCLUSION_CHARS)

    # ── (2) recent turns (most-recent-last, capped count + per-turn length) ──
    recent: list[str] = []
    for message in messages[-DEFAULT_RECENT_TURNS:]:
        text = _message_text(message).strip()
        if text:
            recent.append(_truncate(text, _MAX_TURN_CHARS))

    # ── (3) roster persona pairs ────────────────────────────────────────────
    roster_pairs: list[tuple[str, str]] = []
    for participant in roster:
        pid = getattr(getattr(participant, "id", None), "value", "")
        if not isinstance(pid, str) or not pid:
            continue
        persona = getattr(participant, "persona", None)
        label = persona if isinstance(persona, str) and persona.strip() else ""
        if not label:
            display = getattr(participant, "display_name", "")
            label = display if isinstance(display, str) else ""
        roster_pairs.append((pid, _truncate(label, _MAX_PERSONA_CHARS)))

    # ── (4) user goal: the FIRST user turn ───────────────────────────────────
    user_goal = ""
    for message in messages:
        if getattr(message, "role", None) is MessageRole.USER:
            text = _message_text(message).strip()
            if text:
                user_goal = _truncate(text, _MAX_GOAL_CHARS)
                break

    return ExtractionInputs(
        conclusion_text=conclusion,
        recent_turns=tuple(recent),
        roster=tuple(roster_pairs),
        user_goal=user_goal,
    )


def parse_extraction_response(
    raw: str,
    *,
    ids: IdGenerator | None = None,
) -> tuple[list[FeatureItem], ExtractionStatus]:
    """Three-layer JSON fallback parse of an extractor reply (§22.4 — PURE).

    Returns ``(items, status)`` where ``status`` is one of:

    * ``"ok"`` — at least one valid :class:`FeatureItem` parsed.  Items missing a
      usable ``title`` are dropped; an absent ``id`` is minted (via ``ids`` when
      supplied, else a deterministic positional fallback so the function stays
      pure when no generator is passed); ``acceptance_criteria`` defaults to
      empty.  More than :data:`MAX_FEATURE_ITEMS` items are TRUNCATED (kept, not
      failed); fewer than :data:`MIN_FEATURE_ITEMS` is still accepted.
    * ``"repair_needed"`` — strict JSON parse succeeded structurally but yielded
      NO usable item, OR the JSON failed to parse though it plausibly contains a
      JSON object/array (a ``{`` / ``[`` is present): the caller may run ONE
      repair round.
    * ``"failed"`` — the reply has no JSON-object shape at all (no ``{`` / ``[``)
      → give up (the caller packages a ``planning_failed`` plan).

    Reuses :func:`feature_item_from_dict` for tolerant per-field normalisation
    (State-Truth-First: a malformed item degrades rather than raising).  NEVER
    raises — a ``json.JSONDecodeError`` is caught and mapped to a status.
    """
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return [], "failed"

    payload = _extract_json_payload(text)
    if payload is None:
        # No parseable JSON. If the text *looks* like it tried to be JSON
        # (has a brace/bracket), it is worth ONE repair; otherwise give up.
        if "{" in text or "[" in text:
            return [], "repair_needed"
        return [], "failed"

    raw_items = _coerce_raw_items(payload)
    items: list[FeatureItem] = []
    for index, entry in enumerate(raw_items):
        parsed = feature_item_from_dict(entry)
        if parsed is None:
            continue
        # Drop items with no usable title (§22.4 schema: title is required).
        if not parsed.title.strip():
            continue
        if not parsed.id:
            parsed = _with_minted_id(parsed, ids=ids, index=index)
        items.append(parsed)
        if len(items) >= MAX_FEATURE_ITEMS:
            break  # truncate the surplus — a usable plan beats a hard failure

    if not items:
        # Structurally-valid JSON but zero usable items → worth one repair.
        return [], "repair_needed"
    # MIN_FEATURE_ITEMS is a soft floor (1); any items >= 1 is accepted.
    _ = MIN_FEATURE_ITEMS  # documents the contract; no hard rejection below it
    return items, "ok"


def _extract_json_payload(text: str) -> Any | None:
    """Parse ``text`` to JSON, tolerating a fenced ```json block / prose wrap.

    Tries a strict ``json.loads`` first; on failure it slices the outermost
    ``{...}`` / ``[...]`` span and retries (handles a model that wraps the JSON
    in prose or a markdown fence).  Returns ``None`` when nothing parses.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # Strip a leading ```json fence if present, then re-try a brace/bracket span.
    candidate = text
    span = _outermost_json_span(candidate)
    if span is not None:
        try:
            return json.loads(span)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _outermost_json_span(text: str) -> str | None:
    """Return the substring from the first ``{``/``[`` to its matching close.

    A cheap, dependency-free slice: finds the first opening brace/bracket and the
    LAST matching closing one.  Good enough to recover a JSON object wrapped in
    prose / a code fence; if no pair is found returns ``None``.
    """
    starts = [pos for pos in (text.find("{"), text.find("[")) if pos != -1]
    if not starts:
        return None
    start = min(starts)
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    end = text.rfind(close_ch)
    if end <= start:
        return None
    return text[start : end + 1]


def _coerce_raw_items(payload: Any) -> list[Any]:
    """Pull the item list out of a parsed payload (total / defensive).

    Accepts ``{"items": [...]}`` (the schema), a bare top-level ``[...]`` list,
    or a single ``{...}`` item dict.  Anything else → ``[]``.
    """
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return items
        # A single item dict (no "items" wrapper) — accept it as one-item list.
        if any(k in payload for k in ("title", "description")):
            return [payload]
        return []
    if isinstance(payload, list):
        return payload
    return []


def _with_minted_id(
    item: FeatureItem,
    *,
    ids: IdGenerator | None,
    index: int,
) -> FeatureItem:
    """Return ``item`` with a freshly-minted id when it had none.

    Uses the injected ``ids`` generator when present (production); falls back to
    a deterministic positional id (``"item-<index>"``) so the parser stays a
    pure function callable without a generator (tests).
    """
    new_id = ids.new_id() if ids is not None else f"item-{index}"
    # ``dataclasses.replace`` copies EVERY field (incl. ``verify_command`` and any
    # field added later), so this never silently drops a field the way a manual
    # field-by-field re-construction does.
    return _dc_replace(item, id=new_id)


class OrchestratePlanningUseCase:
    """Drive ONE feature-item extraction into an :class:`ImplementationPlan`.

    Wires the injected :class:`FeatureItemExtractorPort` + an
    :class:`IdGenerator` (run-id minting) + a :class:`Clock` (timestamps).  The
    single ``plan(...)`` entry:

    * resolves the §22.12#4 extraction model, builds the §22.4 inputs;
    * calls the extractor under an ``asyncio.wait_for`` timeout guard;
    * on success → a ``phase="planned"`` plan (carrying the extractor's items,
      ``run_id`` + ``created_at`` / ``updated_at`` stamped);
    * on failure / timeout / ``None`` → a ``phase="planning_failed"`` plan whose
      ``last_error`` carries a short diagnostic (and the raw reply when the
      extractor surfaced one) — §22.4 L3.

    🔴 NEVER raises, NEVER auto-implements (§22.2#2): it returns a ``planned``
    plan for the user to review and STOPS — it does not call any executor.
    """

    def __init__(
        self,
        *,
        extractor: FeatureItemExtractorPort,
        ids: IdGenerator,
        clock: Clock,
    ) -> None:
        self._extractor = extractor
        self._ids = ids
        self._clock = clock

    async def plan(
        self,
        *,
        conv: Conversation,
        discussion: dict[str, Any] | None,
        roster: tuple[Participant, ...],
        timeout_ms: int | None = None,
    ) -> ImplementationPlan:
        """Extract a ``planned`` plan (or a ``planning_failed`` one — §22.4).

        Always returns a plan; never raises (State-Truth-First).  Does NOT
        persist and does NOT execute anything (§22.2#2).
        """
        run_id = self._ids.new_id()
        now = self._clock.now().isoformat()
        # An explicit caller-supplied timeout wins; otherwise resolve the
        # per-conversation tunable (TODO-2) which itself defaults to the
        # constant when no meta key is set.
        effective_timeout = (
            timeout_ms
            if isinstance(timeout_ms, int) and timeout_ms > 0
            else resolve_planner_timeout_ms(discussion)
        )

        inputs = build_extraction_inputs(
            conv=conv, discussion=discussion, roster=roster
        )
        roster_model = roster[0].model_id if roster else None
        model_hint = resolve_planner_model(
            discussion,
            roster_model=roster_model,
        )

        extracted: ImplementationPlan | None = None
        try:
            extracted = await asyncio.wait_for(
                self._extractor.extract(
                    conclusion_text=inputs.conclusion_text,
                    recent_turns=inputs.recent_turns,
                    roster=inputs.roster,
                    user_goal=inputs.user_goal,
                    model_hint=model_hint,
                    timeout_ms=effective_timeout,
                ),
                timeout=max(effective_timeout, 1) / 1000.0,
            )
        except (TimeoutError, asyncio.TimeoutError):
            _log.warning(
                "chat.discussion.planning.extractor_timeout",
                run_id=run_id,
                timeout_ms=effective_timeout,
            )
            extracted = None
        except Exception as exc:  # noqa: BLE001 — use case must never raise
            _log.warning(
                "chat.discussion.planning.extractor_error",
                run_id=run_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            extracted = None

        if extracted is None or extracted.phase == "planning_failed":
            last_error = (
                extracted.last_error
                if extracted is not None and extracted.last_error
                else "feature-item extraction failed (no usable plan produced)"
            )
            _log.info(
                "chat.discussion.planning.failed",
                run_id=run_id,
            )
            return ImplementationPlan(
                phase="planning_failed",
                run_id=run_id,
                items=(),
                created_at=now,
                updated_at=now,
                last_error=last_error[:MAX_LAST_ERROR_LEN],
            )

        # Success: package the extractor's items into a ``planned`` plan, stamping
        # this use case's run id / timestamps (the extractor only knows items).
        _log.info(
            "chat.discussion.planning.planned",
            run_id=run_id,
            item_count=len(extracted.items),
        )
        return ImplementationPlan(
            phase="planned",
            run_id=run_id,
            current_item=None,
            items=extracted.items,
            created_at=now,
            updated_at=now,
        )

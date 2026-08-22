# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LLM-backed feature-item extractor adapter (DISC-1 二期-step2 — §22.4).

Implements
:class:`~qai.chat.application.use_cases.implementation_planner.FeatureItemExtractorPort`
by running ONE low-temperature LLM turn that reads a converged discussion's
conclusion (+ supporting context) and emits a structured JSON list of feature
items, then parsing that reply through the three-layer JSON fallback
(:func:`~qai.chat.application.use_cases.implementation_planner.parse_extraction_response`)
with ONE repair round (§22.4 L1 → L2 → L3).

It is the adapters-layer concrete the orchestrator wires behind the
application-layer Protocol (dependency inversion) — structurally mirroring
:class:`~qai.chat.adapters.llm_intent_classifier.LlmIntentClassifier`: it reuses
the injected :class:`~qai.chat.application.ports.LLMStreamPort` (the SAME
streaming port the discussion / sub-agent loops use), hands its instruction via
``extra["messages"]`` (system + user), and collects CHUNK frames into a complete
text reply.

Contract compliance (§22.4 + State-Truth-First):

* **NEVER raises** — an ERROR frame, an empty / un-parseable reply, a repair that
  still fails, or any exception returns either a ``phase="planning_failed"`` plan
  (carrying the raw reply in ``last_error``) or ``None`` (the use case packages
  ``planning_failed``).  The use case ALSO wraps the call in ``asyncio.wait_for``
  so the timeout lives in ONE place.
* **Side-effect free** w.r.t. the conversation — read-only, no persistence, and
  it NEVER triggers execution (§22.2#2: planning ≠ implementing).
* Fills only ``suggested_role`` on each item; ``assigned_role`` stays ``None``
  until the user confirms (§22.4#5).

Layering: ``adapters`` — may import ``application`` (ports + use_cases), domain,
and platform.  It imports no other context, so ``layered-chat`` /
``context-isolation`` hold (adapters → application is the legal direction, same
as ``llm_intent_classifier``).
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.chat.application.ports import (
    LLMStreamPort,
    LLMStreamRequest,
)
from qai.chat.application.use_cases.implementation_plan import (
    ImplementationPlan,
    MAX_LAST_ERROR_LEN,
)
from qai.chat.application.use_cases.implementation_planner import (
    FeatureItemExtractorPort,
    parse_extraction_response,
)
from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrameType
from qai.platform.logging import get_logger

_log = get_logger(__name__)

__all__ = ["LlmFeatureItemExtractor"]

# An extraction turn that streams more than this many characters is not a bounded
# plan — guard so a runaway model cannot blow memory.  A 7-item plan with short
# control-plane fields fits comfortably under this cap.
_REPLY_MAX_CHARS = 16_000

_SYSTEM_PROMPT = (
    "You are a FEATURE-ITEM EXTRACTOR for a multi-agent engineering assistant. "
    "A discussion has CONCLUDED. Your ONLY job is to turn the conclusion into a "
    "concrete, implementable plan.\n"
    "\n"
    "Output STRICT JSON, no prose, in EXACTLY this shape:\n"
    '{"items": [\n'
    '  {"title": "<short imperative task>",\n'
    '   "description": "<1-3 sentences of what to do>",\n'
    '   "acceptance_criteria": ["<done-when check>", "..."],\n'
    '   "suggested_role": "<participant_id from the roster, or omit>",\n'
    '   "depends_on": ["<id of an item this depends on>", "..."]}\n'
    "]}\n"
    "\n"
    "Rules:\n"
    "- Produce 3 to 7 items. Only extract REAL, implementable engineering tasks "
    "from the conclusion — never invent work the discussion did not agree on.\n"
    "- 'suggested_role' must be one of the given roster participant ids (or "
    "omitted). NEVER assign work yourself beyond a suggestion.\n"
    "- Each item needs a non-empty 'title'.\n"
    "- Output ONLY the JSON object. No markdown fence, no commentary."
)

#: Hander the model a compact repair instruction when the first reply did not
#: parse (§22.4 L2 — ONE repair round).
_REPAIR_PROMPT = (
    "Your previous reply was not valid JSON in the required shape. "
    "Re-emit ONLY a strict JSON object exactly like "
    '{"items": [{"title": "...", "description": "...", '
    '"acceptance_criteria": ["..."], "suggested_role": "...", '
    '"depends_on": ["..."]}]} — no prose, no markdown fence, 3 to 7 items, '
    "each with a non-empty title."
)


# ``MessageContent`` rejects empty text; the extractor hands its instruction via
# ``extra["messages"]`` so this sentinel never reaches the wire.
_PROMPT_SENTINEL = MessageContent(
    text="(feature-item extractor uses extra['messages'])"
)


@dataclass(slots=True)
class LlmFeatureItemExtractor:
    """Production :class:`FeatureItemExtractorPort` over an :class:`LLMStreamPort`.

    Constructed with the shared streaming LLM port (the chat DI injects the SAME
    instance the discussion loop uses).  ``extract`` resolves the upstream from
    the per-call ``model_hint`` (the planner use case runs the §22.12#4 model
    ladder and passes the result in).
    """

    llm: LLMStreamPort

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
        """Extract a ``planned`` plan, or a ``planning_failed`` plan / ``None``.

        NEVER raises (port contract): any failure / ERROR frame / repair-still-
        failed reply degrades to a ``planning_failed`` plan (raw in
        ``last_error``) or ``None``.  The timeout is enforced by the use case
        (one place), so this method does not wrap itself in ``wait_for``.
        """
        user_prompt = _build_user_prompt(
            conclusion_text=conclusion_text,
            recent_turns=recent_turns,
            roster=roster,
            user_goal=user_goal,
        )
        wire: list[dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        # ── L1: first attempt ────────────────────────────────────────────────
        try:
            raw = await self._ask(wire, model_hint=model_hint)
        except Exception as exc:  # noqa: BLE001 — port must never raise
            _log.warning(
                "chat.discussion.planning.extractor_ask_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None
        if raw is None:
            # ERROR frame on the first turn → give up (no raw to repair from).
            return None

        items, status = parse_extraction_response(raw)
        if status == "ok":
            return _planned(items)
        if status == "failed":
            return _planning_failed(raw)

        # ── L2: ONE repair round (status == "repair_needed") ─────────────────
        repair_wire = list(wire) + [
            {"role": "assistant", "content": raw[:_REPLY_MAX_CHARS]},
            {"role": "user", "content": _REPAIR_PROMPT},
        ]
        try:
            repaired = await self._ask(repair_wire, model_hint=model_hint)
        except Exception as exc:  # noqa: BLE001 — port must never raise
            _log.warning(
                "chat.discussion.planning.extractor_repair_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return _planning_failed(raw)
        if repaired is None:
            return _planning_failed(raw)

        repaired_items, repaired_status = parse_extraction_response(repaired)
        if repaired_status == "ok":
            return _planned(repaired_items)

        # ── L3: repair still failed → planning_failed (raw in last_error) ────
        _log.info("chat.discussion.planning.extractor_repair_failed")
        return _planning_failed(repaired)

    async def _ask(
        self,
        wire: list[dict[str, str]],
        *,
        model_hint: str | None,
    ) -> str | None:
        """Run one extractor LLM turn and return its raw text reply.

        Returns ``None`` on an ERROR frame (the caller decides L1-vs-repair
        handling).  Mirrors
        :meth:`LlmIntentClassifier._ask`'s CHUNK-collect loop.
        """
        request = LLMStreamRequest(
            conversation_id=ConversationId("feature-item-extractor"),
            tab_id=TabId("feature-item-extractor"),
            prompt=_PROMPT_SENTINEL,
            history=(),
            model_hint=model_hint,
            extra={"messages": wire},
        )
        parts: list[str] = []
        size = 0
        async for frame in self.llm.stream(request):
            if frame.frame_type is StreamFrameType.CHUNK:
                text = frame.payload.get("text", "")
                if isinstance(text, str) and text:
                    parts.append(text)
                    size += len(text)
                    if size >= _REPLY_MAX_CHARS:
                        break
            elif frame.frame_type is StreamFrameType.ERROR:
                _log.warning(
                    "chat.discussion.planning.extractor_stream_error",
                    message=frame.payload.get("message", ""),
                )
                return None
            elif frame.frame_type is StreamFrameType.END:
                break
        return "".join(parts)


def _build_user_prompt(
    *,
    conclusion_text: str,
    recent_turns: tuple[str, ...],
    roster: tuple[tuple[str, str], ...],
    user_goal: str,
) -> str:
    lines: list[str] = []
    if user_goal:
        lines.append(f"USER GOAL:\n{user_goal}")
        lines.append("")
    if roster:
        lines.append("ROSTER (participant_id — persona):")
        for pid, persona in roster:
            lines.append(f"- {pid}" + (f" — {persona}" if persona else ""))
        lines.append("")
    lines.append("DISCUSSION CONCLUSION (highest priority):")
    lines.append(conclusion_text or "(no explicit conclusion captured)")
    lines.append("")
    if recent_turns:
        lines.append("RECENT DISCUSSION TURNS (supporting context):")
        for turn in recent_turns:
            lines.append(f"- {turn}")
        lines.append("")
    lines.append(
        "Extract the implementable feature items as the strict JSON object "
        "described in the system message."
    )
    return "\n".join(lines)


def _planned(items: object) -> ImplementationPlan:
    """Wrap parsed items into a minimal ``phase="planned"`` plan.

    The use case re-stamps ``run_id`` / timestamps; this adapter only carries the
    items + the ``planned`` phase so the use case can package the envelope.
    """
    # ``items`` is a list[FeatureItem] from ``parse_extraction_response``.
    return ImplementationPlan(phase="planned", items=tuple(items))  # type: ignore[arg-type]


def _planning_failed(raw: str) -> ImplementationPlan:
    """Build a ``phase="planning_failed"`` plan carrying the raw reply (§22.4 L3)."""
    last_error = (
        f"feature-item extraction produced no usable plan; raw reply: "
        f"{raw.strip()}"
    )
    return ImplementationPlan(
        phase="planning_failed",
        items=(),
        last_error=last_error[:MAX_LAST_ERROR_LEN],
    )

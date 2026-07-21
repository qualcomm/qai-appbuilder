# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LLM-backed grey-zone intent classifier adapter (DISC-2 P2-step1 — §22A.5).

Implements :class:`~qai.chat.application.use_cases.discussion_intent.IntentClassifierPort`
by running ONE low-temperature LLM turn whose only job is to refine an ambiguous
(grey-zone) discussion-intent verdict into one of the five public intents.  It is
the adapters-layer concrete the orchestrator wires behind the application-layer
Protocol (dependency inversion) — mirroring
:class:`~qai.chat.adapters.llm_title_generator.HttpLLMTitleGenerator`'s shape but
reusing the injected :class:`~qai.chat.application.ports.LLMStreamPort` (the same
streaming port the discussion / sub-agent loops use) rather than a raw httpx call.

Contract compliance (§22A.5):

* **NEVER raises** — an ERROR frame, an empty / unparseable reply, or any
  exception returns ``None`` (the in-band "keep the heuristic verdict" signal).
  The orchestrator additionally wraps the call in ``asyncio.wait_for`` so the
  timeout lives in ONE place (the orchestrator); this adapter does NOT add its
  own ``wait_for`` to avoid a confusing double-timeout.
* **Side-effect free** w.r.t. the conversation — it only reads, never persists.
* The instruction is handed down via ``extra["messages"]`` (system + user), the
  same contract :meth:`ManagerAgentSelector._ask_manager` uses, so no
  system-prompt plumbing is needed; ``prompt`` is a never-read sentinel.

Layering: ``adapters`` — may import ``application`` (ports + use_cases), domain,
and platform.  It imports no other context, so ``layered-chat`` /
``context-isolation`` hold (adapters → application is the legal direction, same
as ``llm_title_generator``).
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.chat.application.ports import (
    LLMStreamPort,
    LLMStreamRequest,
)
from qai.chat.application.use_cases.discussion_intent import (
    DiscussionState,
    IntentHeuristicResult,
    IntentResult,
)
from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrameType
from qai.platform.logging import get_logger

_log = get_logger(__name__)

__all__ = ["LlmIntentClassifier"]

# A classifier turn that streams more than this many characters is not a bare
# one-line verdict — guard so a chatty model cannot wedge the parse / blow
# memory.  The expected reply is a single short ``intent|confidence`` line.
_REPLY_MAX_CHARS = 256

#: The five legal public intents (mirrors
#: :data:`discussion_intent.DiscussionIntent`).  An LLM reply naming anything
#: outside this set is rejected (→ ``None`` → keep heuristic).
_LEGAL_INTENTS: frozenset[str] = frozenset(
    {"social", "ack", "follow_up", "directed_follow_up", "deep_task"}
)

#: Neutral confidence assigned when the model omits / garbles the confidence
#: field but names a legal intent.  Mid-band so the orchestrator's gating
#: (``LLM_CONFIDENCE_FLOOR``) still meaningfully applies.
_DEFAULT_CONFIDENCE = 0.7

_SYSTEM_PROMPT = (
    "You are an INTENT CLASSIFIER for a multi-agent discussion assistant. "
    "Classify the user's latest message into EXACTLY ONE of these five intents:\n"
    "- social: a greeting / smalltalk / thanks / closing.\n"
    "- ack: a passive acknowledgement or a bare 'continue / go on' request.\n"
    "- follow_up: a scoped, on-topic question or clarification about prior work.\n"
    "- directed_follow_up: a scoped reply addressed to specific role(s).\n"
    "- deep_task: a NEW substantive request that needs a full multi-role analysis.\n"
    "\n"
    "Rules:\n"
    "- Prefer the NARROWEST intent that fits. Only choose deep_task for a clear, "
    "substantial NEW task — never for a short clarifying question.\n"
    "- Output a SINGLE line, no prose, in the form: intent|confidence\n"
    "  where confidence is a number 0.0-1.0 (e.g. 'follow_up|0.82').\n"
    "- Output ONLY that line."
)


@dataclass(slots=True)
class LlmIntentClassifier:
    """Production :class:`IntentClassifierPort` over an :class:`LLMStreamPort`.

    Constructed with the shared streaming LLM port (the chat DI injects the
    SAME instance used by the discussion / sub-agent loops).  ``classify``
    resolves the upstream from the per-call ``model_hint`` (the orchestrator
    resolves the §22A.5 model priority and passes it in).
    """

    llm: LLMStreamPort

    async def classify(
        self,
        *,
        message: str,
        state: DiscussionState,
        awaiting_user: bool,
        previous_user_text: str | None = None,
        mentions: tuple[str, ...] = (),
        model_hint: str | None = None,
        timeout_ms: int = 2000,
        heuristic: IntentHeuristicResult | None = None,
    ) -> IntentResult | None:
        """Refine the grey-zone verdict, or ``None`` to keep the heuristic one.

        NEVER raises (port contract): any failure / ERROR frame / unparseable
        reply returns ``None``.  The timeout is enforced by the orchestrator
        (one place), so this method does not wrap itself in ``wait_for``.
        """
        try:
            reply = await self._ask(
                message=message,
                state=state,
                awaiting_user=awaiting_user,
                previous_user_text=previous_user_text,
                mentions=mentions,
                model_hint=model_hint,
                heuristic=heuristic,
            )
        except Exception as exc:  # noqa: BLE001 — port must never raise
            _log.warning(
                "chat.discussion.intent.classifier_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None
        if reply is None:
            return None
        result = _parse_reply(reply, mentions=mentions)
        _log.info(
            "chat.discussion.intent.classifier_reply",
            reply_preview=reply[:80],
            parsed=result.intent if result is not None else None,
        )
        return result

    async def _ask(
        self,
        *,
        message: str,
        state: DiscussionState,
        awaiting_user: bool,
        previous_user_text: str | None,
        mentions: tuple[str, ...],
        model_hint: str | None,
        heuristic: IntentHeuristicResult | None,
    ) -> str | None:
        """Run one classifier LLM turn and return its raw text reply.

        Returns ``None`` on an ERROR frame (so ``classify`` keeps the heuristic).
        """
        wire = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_user_prompt(
                    message=message,
                    state=state,
                    awaiting_user=awaiting_user,
                    previous_user_text=previous_user_text,
                    mentions=mentions,
                    heuristic=heuristic,
                ),
            },
        ]
        request = LLMStreamRequest(
            conversation_id=ConversationId("intent-classifier"),
            tab_id=TabId("intent-classifier"),
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
                    "chat.discussion.intent.classifier_stream_error",
                    message=frame.payload.get("message", ""),
                )
                return None
            elif frame.frame_type is StreamFrameType.END:
                break
        return "".join(parts)


# ``MessageContent`` rejects empty text; the classifier hands its instruction
# via ``extra["messages"]`` so this sentinel never reaches the wire.
_PROMPT_SENTINEL = MessageContent(
    text="(intent classifier uses extra['messages'])"
)


def _build_user_prompt(
    *,
    message: str,
    state: DiscussionState,
    awaiting_user: bool,
    previous_user_text: str | None,
    mentions: tuple[str, ...],
    heuristic: IntentHeuristicResult | None,
) -> str:
    lines = [
        f"discussion_state: {state}",
        f"awaiting_user: {awaiting_user}",
    ]
    if mentions:
        lines.append(f"mentions: {', '.join(mentions)}")
    if previous_user_text:
        lines.append(f"previous_user_message: {previous_user_text[:300]}")
    if heuristic is not None:
        lines.append(f"heuristic_guess: {heuristic.intent}")
        if heuristic.ambiguity_reasons:
            lines.append(
                f"ambiguity_reasons: {', '.join(heuristic.ambiguity_reasons)}"
            )
    lines.append("")
    lines.append(f'latest_user_message: "{message[:500]}"')
    lines.append("")
    lines.append("Reply with one line: intent|confidence")
    return "\n".join(lines)


def _parse_reply(
    reply: str,
    *,
    mentions: tuple[str, ...],
) -> IntentResult | None:
    """Map a raw classifier reply to an :class:`IntentResult`, or ``None``.

    Accepts ``intent|confidence`` (preferred) or a bare intent token.  Rejects
    (→ ``None``) when no legal intent can be extracted.
    """
    text = reply.strip()
    if not text:
        return None
    # Take the first non-empty line — guard against a model that adds prose.
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not first_line:
        return None
    parts = first_line.split("|", 1)
    intent_token = parts[0].strip().lower()
    if intent_token not in _LEGAL_INTENTS:
        # Tolerate a token embedded in a short phrase (e.g. "intent: follow_up").
        intent_token = _scan_intent(first_line)
        if intent_token is None:
            return None
    confidence = _DEFAULT_CONFIDENCE
    if len(parts) == 2:
        parsed = _parse_confidence(parts[1])
        if parsed is not None:
            confidence = parsed
    target_roles: tuple[str, ...] = ()
    if intent_token == "directed_follow_up":
        target_roles = tuple(mentions)
    return IntentResult(
        intent=intent_token,  # type: ignore[arg-type]  # validated legal above
        subtype="none",
        confidence=confidence,
        target_roles=target_roles,
        needs_full_discussion=intent_token == "deep_task",
    )


def _scan_intent(line: str) -> str | None:
    """Find a legal intent token anywhere in ``line`` (longest match first)."""
    low = line.lower()
    # Longest tokens first so ``directed_follow_up`` wins over ``follow_up``.
    for token in sorted(_LEGAL_INTENTS, key=len, reverse=True):
        if token in low:
            return token
    return None


def _parse_confidence(raw: str) -> float | None:
    """Parse a 0..1 confidence; clamp into range; ``None`` when not a number."""
    try:
        value = float(raw.strip())
    except (ValueError, TypeError):
        return None
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value

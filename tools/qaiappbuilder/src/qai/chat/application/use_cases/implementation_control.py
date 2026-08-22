# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Implementation Control Router (§22.9) — heuristic, dependency-free classifier.

When an implementation phase is ``implementing`` (DISC-1 二期 step3b), the user's
next message must NOT fall through the ordinary discussion path (DISC-2
:mod:`~qai.chat.application.use_cases.discussion_intent`): while a live
implementation run is in flight, "这个先别做" / "stop" is a *control command over
the run*, not a fresh discussion turn.  This router is Layer 0 for that mode: a
**pure function** that classifies the message into one of the nine
implementation-control intents (§22.9) so step3c can later decide what to do.

Public intents (stable contract — §22.9):
``stop`` | ``pause`` | ``resume`` | ``skip_current`` | ``modify_current_item`` |
``add_constraint`` | ``ask_status`` | ``abort_and_discuss`` | ``general_message``.

Heuristic ladder (priority order, §22.9 safety reasoning) — a running
implementation makes a *false negative on the dangerous controls* the risky
failure mode (the run keeps going when the user said stop), while a *false
positive on the dangerous controls* (mistaking a chat line for "stop") is just
as harmful (it halts work the user did not mean to halt).  So the ladder is:

1. **stop / abort_and_discuss** — the hardest "halt now" signals win first.
   ``abort_and_discuss`` ("先停下来讨论") is recognised before bare ``stop`` so an
   explicit "stop AND go back to discuss" is not flattened to a plain stop.
2. **pause** — a softer "hold, I'll resume" (kept distinct from stop).
3. **resume** — "继续 / go on" (only meaningful while paused; classified here,
   acted on by step3c).
4. **skip_current** — "跳过这个 / next".
5. **modify_current_item** — "改成 X / change it to Y"; ``payload`` carries the
   verbatim message so step3c can extract the new instruction.
6. **add_constraint** — "还要 / 另外 / 记得 / also / make sure"; ``payload`` = the
   verbatim message.
7. **ask_status** — "进度到哪了 / status".
8. **general_message** — conservative fallback (nothing control-bearing matched).

Conservatism (§22.9): an ambiguous, non-obvious control phrase degrades to
``general_message`` (step3c then asks the user to pick an explicit action) rather
than firing a dangerous action like ``stop`` on a guess — but an UNAMBIGUOUS
``stop`` / ``pause`` must still win at high priority.

Layering: ``application/use_cases`` — depends only on the discussion rule pack's
locale-tolerant normalisation helper + stdlib (no分词, no LLM, no ports, no
domain, no adapters), so ``layered-chat`` / ``context-isolation`` hold and the
router is reusable from tests directly.  This router is ORTHOGONAL to the
discussion Intent Router (different mode, different vocabulary): it carries its
own control-only phrase dictionaries so it never pollutes the discussion rules.

ZERO execution wiring (step3b boundary): :func:`classify_control_intent` is NOT
called from any ``execute`` / ``_run_*`` path today.  step3c is what consults it
(only while ``phase == "implementing"``) and dispatches the resulting action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from qai.chat.application.use_cases.discussion_intent_rules import (
    SUPPORTED_LOCALES,
    normalize_message,
)

__all__ = [
    "ImplementationControlIntent",
    "ControlResult",
    "classify_control_intent",
]


# ---------------------------------------------------------------------------
# Public vocabulary (stable contract — §22.9)
# ---------------------------------------------------------------------------
ImplementationControlIntent = Literal[
    "stop",
    "pause",
    "resume",
    "retry",
    "skip_current",
    "modify_current_item",
    "add_constraint",
    "ask_status",
    "abort_and_discuss",
    "general_message",
]


@dataclass(frozen=True, slots=True)
class ControlResult:
    """The control router's verdict for one user message (§22.9 output).

    * ``intent`` — the nine-class implementation-control intent.
    * ``confidence`` — 0..1 heuristic confidence (informational; the
      ``general_message`` fallback carries a deliberately LOW number so step3c
      can tell a confident control hit from a "nothing matched" guess).  Not a
      gate in this step.
    * ``payload`` — the verbatim user message, populated ONLY for
      ``modify_current_item`` / ``add_constraint`` so step3c can extract the new
      instruction / constraint text; empty ``""`` for every other intent.
    """

    intent: ImplementationControlIntent
    confidence: float
    payload: str = ""


# ---------------------------------------------------------------------------
# Control-only phrase dictionaries (§22.9) — kept SEPARATE from the discussion
# rule pack so the two routers stay orthogonal.  Each is a flat
# ``{locale: frozenset[str]}`` table of NORMALISED (casefolded, no trailing
# punctuation) substrings; matched via :func:`_contains_any` (CJK terms are not
# whitespace-delimited).  zh-TW carries the traditional-character variants.
# ---------------------------------------------------------------------------

#: Hard "halt the run now" signals.
STOP_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {"stop", "cancel", "halt", "abort", "don't do it", "dont do it"}
    ),
    "zh-CN": frozenset(
        {"停止", "停下", "别做了", "不要做了", "取消", "中止", "终止", "先别做"}
    ),
    "zh-TW": frozenset(
        {"停止", "停下", "別做了", "不要做了", "取消", "中止", "終止", "先別做"}
    ),
}

#: Softer "hold on, I'll resume" — distinct from stop (the run is parked, not
#: cancelled).
PAUSE_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset({"pause", "hold on", "wait", "hold"}),
    "zh-CN": frozenset({"暂停", "先暂停", "等一下", "稍等", "等等"}),
    "zh-TW": frozenset({"暫停", "先暫停", "等一下", "稍等", "等等"}),
}

#: Resume a paused run / keep going.
RESUME_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset({"resume", "continue", "go on", "go ahead", "carry on"}),
    "zh-CN": frozenset({"继续", "恢复", "接着做", "接着", "go on"}),
    "zh-TW": frozenset({"繼續", "恢復", "接著做", "接著", "go on"}),
}

#: Retry the failed work-item(s) — re-run what failed (三期-step2). Distinct from
#: resume (which continues remaining PENDING items): retry RE-RUNS the FAILED
#: ones by resetting them to pending first.
RETRY_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {"retry", "try again", "redo", "re-run", "rerun", "run it again"}
    ),
    "zh-CN": frozenset({"重试", "重新做", "再试一次", "重跑", "再跑一次", "重新执行"}),
    "zh-TW": frozenset({"重試", "重新做", "再試一次", "重跑", "再跑一次", "重新執行"}),
}

#: Skip the current work-item, move to the next one.
SKIP_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset({"skip", "skip this", "skip current", "next one", "next"}),
    "zh-CN": frozenset({"跳过", "跳过这个", "跳过这一个", "下一个", "略过"}),
    "zh-TW": frozenset({"跳過", "跳過這個", "跳過這一個", "下一個", "略過"}),
}

#: Change the current item — "改成 X / change it to Y / instead".
MODIFY_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {"modify", "change it to", "change to", "instead", "use", "switch to"}
    ),
    "zh-CN": frozenset(
        {"改成", "换成", "改用", "改为", "不要用", "替换成", "改一下"}
    ),
    "zh-TW": frozenset(
        {"改成", "換成", "改用", "改為", "不要用", "替換成", "改一下"}
    ),
}

#: Add a constraint / extra requirement on top of the current work.
ADD_CONSTRAINT_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "also",
            "make sure",
            "don't forget",
            "dont forget",
            "remember to",
            "in addition",
            "additionally",
            "note that",
        }
    ),
    "zh-CN": frozenset(
        {"还要", "另外", "注意", "约束", "记得", "顺便", "记住", "别忘了", "另外还要"}
    ),
    "zh-TW": frozenset(
        {"還要", "另外", "注意", "約束", "記得", "順便", "記住", "別忘了", "另外還要"}
    ),
}

#: Ask for current run status / progress.
ASK_STATUS_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset({"status", "progress", "where are we", "how far", "what's the status"}),
    "zh-CN": frozenset({"进度", "到哪了", "状态", "现在到哪", "做到哪", "进展"}),
    "zh-TW": frozenset({"進度", "到哪了", "狀態", "現在到哪", "做到哪", "進展"}),
}

#: Halt the run AND go back to discussion — distinct from a bare stop (the user
#: wants to re-open the design conversation, not just cancel).
ABORT_AND_DISCUSS_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "let's discuss",
            "lets discuss",
            "back to discussion",
            "let's talk",
            "lets talk",
            "discuss first",
            # "abort the discussion" / "abort and discuss" carry "abort" (a
            # STOP_TERMS member) but the intent is "halt AND re-open design",
            # not a bare cancel. Listed here so the ladder (abort_and_discuss
            # before stop) routes them correctly; safe because they all carry
            # "discuss", so bare "abort" still falls through to a plain stop.
            "abort the discussion",
            "abort and discuss",
            "abort and go back to discussion",
        }
    ),
    "zh-CN": frozenset(
        {"先讨论", "停下来讨论", "回到讨论", "先讨论一下", "停下来讨论一下", "再讨论"}
    ),
    "zh-TW": frozenset(
        {"先討論", "停下來討論", "回到討論", "先討論一下", "停下來討論一下", "再討論"}
    ),
}


# ---------------------------------------------------------------------------
# Matching helper (pure, locale-tolerant) — mirrors rules.contains_any but on
# this module's OWN control tables (kept local to avoid coupling the discussion
# rule pack's signature to control vocabulary).
# ---------------------------------------------------------------------------
def _contains_any(
    normalized: str,
    table: dict[str, frozenset[str]],
    locale: str | None = None,
) -> bool:
    """Return ``True`` when ``normalized`` contains any term in ``table``.

    Scans the resolved locale first, then EVERY other supported locale (a zh
    user may type an English "stop" and vice versa).  Substring match (CJK terms
    are not whitespace-delimited).  ``normalized`` MUST already be
    :func:`~qai.chat.application.use_cases.discussion_intent_rules.normalize_message`-d.
    """
    if not normalized:
        return False
    resolved = locale if (locale in SUPPORTED_LOCALES) else "en"
    ordered = (resolved, *(loc for loc in SUPPORTED_LOCALES if loc != resolved))
    for loc in ordered:
        for term in table.get(loc, frozenset()):
            if term and term in normalized:
                return True
    return False


# ---------------------------------------------------------------------------
# Heuristic classifier (pure) — §22.9
# ---------------------------------------------------------------------------
def classify_control_intent(
    message: str,
    *,
    locale: str | None = None,
) -> ControlResult:
    """Classify ``message`` into an implementation-control intent (§22.9).

    Pure + deterministic: the same inputs always yield the same verdict; no IO,
    no LLM, no分词.  Intended to be consulted (by step3c) ONLY while the
    implementation ``phase == "implementing"`` — it is NOT wired into any
    ``execute`` / ``_run_*`` path in step3b.

    Args:
        message: the user's latest raw message text.
        locale: best-effort UI locale ("en" / "zh-CN" / "zh-TW"); the control
            dictionaries still scan EVERY locale so this is only a priority hint.

    Returns:
        A :class:`ControlResult`.  ``payload`` is the verbatim ``message`` ONLY
        for ``modify_current_item`` / ``add_constraint`` (so step3c can extract
        the new instruction / constraint); empty for everything else.  An
        ambiguous, non-control line degrades to ``general_message`` at LOW
        confidence (§22.9 conservatism — never guess a dangerous ``stop``).
    """
    raw = message if isinstance(message, str) else ""
    normalized = normalize_message(raw)

    # Empty / non-text → nothing to control.
    if not normalized:
        return ControlResult(intent="general_message", confidence=0.2)

    # -- Priority ladder (§22.9): dangerous "halt" controls win first ----------
    # abort_and_discuss before stop: an explicit "停下来讨论" is NOT just a stop.
    if _contains_any(normalized, ABORT_AND_DISCUSS_TERMS, locale):
        return ControlResult(intent="abort_and_discuss", confidence=0.9)
    if _contains_any(normalized, STOP_TERMS, locale):
        return ControlResult(intent="stop", confidence=0.9)
    if _contains_any(normalized, PAUSE_TERMS, locale):
        return ControlResult(intent="pause", confidence=0.85)

    # -- Flow controls ---------------------------------------------------------
    # skip before resume: "跳过这个" must not be diluted by a "继续" elsewhere.
    if _contains_any(normalized, SKIP_TERMS, locale):
        return ControlResult(intent="skip_current", confidence=0.85)
    # retry before resume: "重新做/再试一次" is a re-run of FAILED items, not a
    # plain "继续" of the remaining pending ones.
    if _contains_any(normalized, RETRY_TERMS, locale):
        return ControlResult(intent="retry", confidence=0.8)
    if _contains_any(normalized, RESUME_TERMS, locale):
        return ControlResult(intent="resume", confidence=0.8)

    # -- Item edits (carry payload for step3c instruction extraction) ----------
    if _contains_any(normalized, MODIFY_TERMS, locale):
        return ControlResult(
            intent="modify_current_item", confidence=0.8, payload=raw
        )
    if _contains_any(normalized, ADD_CONSTRAINT_TERMS, locale):
        return ControlResult(
            intent="add_constraint", confidence=0.75, payload=raw
        )

    # -- Status query ----------------------------------------------------------
    if _contains_any(normalized, ASK_STATUS_TERMS, locale):
        return ControlResult(intent="ask_status", confidence=0.8)

    # -- Conservative fallback (§22.9): nothing control-bearing matched --------
    # Degrade to general_message at LOW confidence rather than guess a dangerous
    # action; step3c surfaces an explicit-action prompt when it sees this.
    return ControlResult(intent="general_message", confidence=0.3)

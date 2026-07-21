# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-role stance memory for multi-agent discussions (DISC-2 P3-step1).

A multi-agent discussion (docs/70-multi-agent/multi-agent-conversation-design.md
§22A.6 P3-a + §22A.9#1) suffers from roles that "speak as if for the first time"
every round — restating their framing, losing the thread of their own prior
position. This module owns the LIGHTWEIGHT, LLM-FREE per-role *stance* the
orchestrator threads through a discussion so each speaker can continue its own
previously-stated position instead of re-deriving it.

Design (MVP, no extra LLM call):

* :class:`RoleStance` — a small frozen value object holding ONE role's running
  position: ``current_stance`` (the role's standing position),
  ``last_contribution_summary`` (what it last added), ``unresolved_concern`` (an
  open risk/blocker/question it raised, sticky until superseded), and
  ``updated_at_turn``. **All text fields are HARD-TRUNCATED at construction**
  (the single choke point), so no matter whether a stance is built from live
  extraction or hydrated from a persisted snapshot, the per-field length is
  bounded — the discussion meta blob can never grow unbounded (AGENTS.md §3.8 /
  meta-膨胀 guard).

* :func:`extract_stance` — a pure, deterministic, regex-only extractor that
  distils a role's FINAL turn text into a refreshed :class:`RoleStance`. No LLM,
  no IO. Sentence splitting is conservative (CJK + ASCII terminal punctuation).
  ``unresolved_concern`` is only refreshed when THIS turn actually surfaces a
  risk / blocker / question cue (bilingual cue set carried HERE so this module is
  decoupled from ``discussion_convergence_rules`` internals); otherwise the prior
  concern is carried forward.

* :func:`stance_to_dict` / :func:`stance_from_dict` — plain-``dict``
  (de)serialisation for the boundary ``meta["discussion"]["stance_snapshot"]``
  persistence. ``stance_from_dict`` is State-Truth-First: a malformed / partial
  payload returns ``None`` (never raises), and a valid one is funnelled through
  the same truncating constructor so a tampered oversized snapshot is re-bounded
  on the way back in.

Layering: this module lives in ``application/use_cases`` and depends only on the
standard library. It imports NO adapters / apps / interfaces and — crucially —
does NOT import ``speaker_selection`` (the dependency is one-way:
``speaker_selection`` imports :class:`RoleStance` from here), so it satisfies the
``layered-chat`` / ``context-isolation`` import-linter contracts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CONCERN_MAX_CHARS",
    "CONTRIBUTION_MAX_CHARS",
    "STANCE_MAX_CHARS",
    "RoleStance",
    "extract_stance",
    "stance_from_dict",
    "stance_to_dict",
]

# ---------------------------------------------------------------------------
# Length bounds — the single source of truth for per-field truncation. These
# cap the persisted snapshot so the discussion meta blob stays bounded
# (≤ STANCE_MAX_CHARS + CONTRIBUTION_MAX_CHARS + CONCERN_MAX_CHARS per role).
# ---------------------------------------------------------------------------
STANCE_MAX_CHARS = 240
CONTRIBUTION_MAX_CHARS = 240
CONCERN_MAX_CHARS = 160

# Sentence terminators — CJK + ASCII, plus newline. Conservative: we split on
# these and keep the terminator OUT of the captured fragment (re-joined text is
# the human-readable sentence without trailing punctuation).
_SENTENCE_SPLIT_RE = re.compile(r"[。！？.!?\n]+")

# Bilingual cue set for an UNRESOLVED CONCERN — risk / blocker / open question.
# Carried locally (NOT imported from discussion_convergence_rules) so this module
# owns its own decoupled vocabulary. Matched case-insensitively against the
# lower-cased sentence (CJK cues are matched verbatim — case folding is a no-op).
_CONCERN_CUES: tuple[str, ...] = (
    # English — risk / blocker / open question
    "risk",
    "blocker",
    "blocked",
    "concern",
    "concerned",
    "worry",
    "worried",
    "issue",
    "problem",
    "question",
    "unclear",
    "uncertain",
    "caveat",
    "however",
    "but ",
    "drawback",
    "limitation",
    "?",
    # Chinese — risk / blocker / open question
    "风险",
    "隐患",
    "阻碍",
    "卡点",
    "担忧",
    "担心",
    "顾虑",
    "问题",
    "疑问",
    "不确定",
    "不清楚",
    "但是",
    "然而",
    "局限",
    "缺陷",
    "？",
)


def _truncate(text: str, limit: int) -> str:
    """Strip + hard-truncate ``text`` to ``limit`` chars (the choke point)."""
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip()


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` into trimmed, non-empty sentences (conservative)."""
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def _has_concern_cue(sentence: str) -> bool:
    """Return ``True`` when ``sentence`` carries a risk/blocker/question cue."""
    low = sentence.lower()
    return any(cue in low for cue in _CONCERN_CUES)


@dataclass(frozen=True, slots=True)
class RoleStance:
    """One role's running position in a discussion (all fields length-bounded).

    Constructed via the ordinary dataclass call OR :meth:`of`; either way the
    text fields are funnelled through :func:`_truncate` in ``__post_init__`` so
    the per-field length is ALWAYS bounded — the single choke point that keeps
    both live-extracted and snapshot-hydrated stances within
    :data:`STANCE_MAX_CHARS` / :data:`CONTRIBUTION_MAX_CHARS` /
    :data:`CONCERN_MAX_CHARS`.

    Fields:

    * ``current_stance`` — the role's standing position (its lead sentence(s)).
    * ``last_contribution_summary`` — a short summary of what it last added.
    * ``unresolved_concern`` — an open risk / blocker / question it raised,
      sticky across turns until a later turn surfaces a fresh one.
    * ``updated_at_turn`` — the (1-based) discussion round this was refreshed in.
    """

    current_stance: str = ""
    last_contribution_summary: str = ""
    unresolved_concern: str = ""
    updated_at_turn: int = 0

    def __post_init__(self) -> None:
        # frozen dataclass → mutate via object.__setattr__ at the choke point.
        object.__setattr__(
            self, "current_stance", _truncate(self.current_stance, STANCE_MAX_CHARS)
        )
        object.__setattr__(
            self,
            "last_contribution_summary",
            _truncate(self.last_contribution_summary, CONTRIBUTION_MAX_CHARS),
        )
        object.__setattr__(
            self,
            "unresolved_concern",
            _truncate(self.unresolved_concern, CONCERN_MAX_CHARS),
        )
        # ``updated_at_turn`` is a small int index; coerce defensively (a
        # malformed snapshot value is clamped to a non-negative int).
        raw = self.updated_at_turn
        turn = raw if isinstance(raw, int) and raw >= 0 else 0
        object.__setattr__(self, "updated_at_turn", turn)

    @classmethod
    def of(
        cls,
        *,
        current_stance: str = "",
        last_contribution_summary: str = "",
        unresolved_concern: str = "",
        updated_at_turn: int = 0,
    ) -> RoleStance:
        """Factory mirroring the constructor (truncation still applies)."""
        return cls(
            current_stance=current_stance,
            last_contribution_summary=last_contribution_summary,
            unresolved_concern=unresolved_concern,
            updated_at_turn=updated_at_turn,
        )

    def is_empty(self) -> bool:
        """Return ``True`` when every text field is blank (nothing to inject)."""
        return not (
            self.current_stance
            or self.last_contribution_summary
            or self.unresolved_concern
        )


def extract_stance(
    *,
    text: str,
    round_index: int,
    prev: RoleStance | None = None,
) -> RoleStance:
    """Distil a role's FINAL turn ``text`` into a refreshed :class:`RoleStance`.

    Pure, deterministic, LLM-free (MVP — §22A.6 P3-a). Rules:

    * ``current_stance`` — the role's lead sentence (its standing position this
      turn), truncated to :data:`STANCE_MAX_CHARS`.
    * ``last_contribution_summary`` — the first up-to-two sentences (what it just
      added), truncated to :data:`CONTRIBUTION_MAX_CHARS`.
    * ``unresolved_concern`` — REFRESHED only when THIS turn surfaces a
      risk/blocker/question cue (the first such sentence, truncated to
      :data:`CONCERN_MAX_CHARS`); otherwise the prior concern (if any) is carried
      forward so an open worry stays sticky until superseded.

    A blank / whitespace-only ``text`` yields the prior stance unchanged (or an
    empty stance when there is no prior) — the caller is expected to skip the
    update for empty speaker text, but this is defensive.
    """
    sentences = _split_sentences(text)
    if not sentences:
        # Nothing extractable — keep the prior stance (sticky), else empty.
        if prev is not None:
            return prev
        return RoleStance(updated_at_turn=max(round_index, 0))

    current_stance = sentences[0]
    last_contribution_summary = " ".join(sentences[:2])

    # Unresolved concern: only refresh on a fresh cue this turn; else carry the
    # prior concern forward (sticky), else blank.
    concern = ""
    for sentence in sentences:
        if _has_concern_cue(sentence):
            concern = sentence
            break
    if not concern and prev is not None:
        concern = prev.unresolved_concern

    return RoleStance(
        current_stance=current_stance,
        last_contribution_summary=last_contribution_summary,
        unresolved_concern=concern,
        updated_at_turn=max(round_index, 0),
    )


def stance_to_dict(stance: RoleStance) -> dict[str, object]:
    """Serialise a :class:`RoleStance` to a plain JSON-safe ``dict``."""
    return {
        "current_stance": stance.current_stance,
        "last_contribution_summary": stance.last_contribution_summary,
        "unresolved_concern": stance.unresolved_concern,
        "updated_at_turn": stance.updated_at_turn,
    }


def stance_from_dict(raw: object) -> RoleStance | None:
    """Deserialise one persisted stance dict (State-Truth-First).

    Returns ``None`` for any non-dict / structurally-broken payload (never
    raises). A valid payload is funnelled through the truncating constructor so
    an oversized / tampered snapshot is re-bounded on the way back in.
    """
    if not isinstance(raw, dict):
        return None
    current = raw.get("current_stance")
    contribution = raw.get("last_contribution_summary")
    concern = raw.get("unresolved_concern")
    turn = raw.get("updated_at_turn")
    # Coerce each field defensively; non-str text → empty, non-int turn → 0.
    return RoleStance(
        current_stance=current if isinstance(current, str) else "",
        last_contribution_summary=(
            contribution if isinstance(contribution, str) else ""
        ),
        unresolved_concern=concern if isinstance(concern, str) else "",
        updated_at_turn=turn if isinstance(turn, int) and turn >= 0 else 0,
    )

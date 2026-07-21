# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure helpers for collaboration-mode resolution in the discussion orchestrator.

A **collaboration mode** (design §26 / §27) defines HOW a roster collaborates
(讨论 / 评审 / 辩论 / 实施 / custom).  This module keeps the *resolution* logic —
"which mode is selected, and how does it shape framing + tool advertisement" —
as small pure functions, OUT of the already-large ``orchestrate_discussion.py``
(§3.6 cohesion).  The orchestrator calls these and applies the result; it does
not embed the policy logic.

Design invariants honoured here:

* **State-Truth-First / §26.3 / §26.5** — a mode's tool intersection is the SOLE
  real gate on tool advertisement; framing prose carries no permission.
* **deep_task zero-regression** — when no mode is selected (or the selected mode
  has empty framing, e.g. the built-in 讨论 mode), :func:`resolve_mode_framing`
  returns ``None`` so the orchestrator keeps its existing
  ``_DEFAULT_DISCUSSION_PROMPT`` / per-conversation override behaviour verbatim.
* **§26.4** — privilege selection is the user's explicit ``selected_mode_id``;
  this module only READS it, it never upgrades a mode on its own.

No IO, no ports here — the orchestrator loads the :class:`ModeTemplate` (via its
injected repository) and passes it in.
"""

from __future__ import annotations

from qai.chat.domain.mode_template import ModeTemplate, effective_advertised_tools
from qai.chat.domain.participant import Participant

__all__ = [
    "read_selected_mode_id",
    "resolve_mode_framing",
    "advertised_tool_names",
    "build_hard_constraints_clause",
    "resolve_system_model",
]


def read_selected_mode_id(discussion: dict | None) -> str | None:
    """Return ``meta["discussion"]["selected_mode_id"]`` if set, else ``None``.

    Defensive: any non-str / empty value degrades to ``None`` (no mode), so a
    malformed meta blob never breaks the discussion (zero-regression).
    """
    if not isinstance(discussion, dict):
        return None
    value = discussion.get("selected_mode_id")
    if isinstance(value, str) and value.strip():
        return value
    return None


def resolve_mode_framing(mode: ModeTemplate | None) -> str | None:
    """Return the mode's framing prose to use as the base framing, or ``None``.

    ``None`` when no mode is selected OR the mode's framing is empty (e.g. the
    built-in 讨论 mode keeps framing empty on purpose) — in which case the
    orchestrator falls through to its existing behaviour
    (``_DEFAULT_DISCUSSION_PROMPT`` or the per-conversation ``discussion_prompt``
    override), guaranteeing deep_task zero-regression.
    """
    if mode is None:
        return None
    framing = mode.framing.strip()
    return framing or None


def advertised_tool_names(
    *,
    role_tools: set[str],
    mode: ModeTemplate | None,
    global_excluded: frozenset[str],
) -> set[str]:
    """Compute the tool names advertised to a speaker (design §26.5 V1 subset).

    ``effective = role_tools ∩ mode_policy ∩ global_policy``.  Thin pass-through
    to the domain helper so the orchestrator imports one name; kept here so the
    intersection rule lives next to the other mode-resolution helpers.
    """
    return effective_advertised_tools(
        role_tools=role_tools,
        mode=mode,
        global_excluded=global_excluded,
    )


def build_hard_constraints_clause(mode: ModeTemplate | None) -> str | None:
    """Render the meeting-room soft-constraint clause for a speaker (§26.8).

    Decisions 3 + 4 + 5 + 9: when a mode carries ``hard_constraints``, append a
    SOFT prose instruction to the speaker's persona — NO streaming truncation,
    NO ``asyncio.wait_for`` (decision 4). The clause spells out the char-count
    ALGORITHM (Chinese by character, English by whitespace-delimited word —
    decision 5) so the model does not self-estimate by tokens.

    Returns ``None`` when no mode is selected or NEITHER constraint is enabled
    (nothing to append → zero-regression for unconstrained modes). The two
    constraints are independent (decision 9): char-only, seconds-only, or both.
    """
    if mode is None:
        return None
    hc = mode.hard_constraints
    if hc.is_empty:
        return None
    n = hc.max_chars_per_turn
    s = hc.max_seconds_per_turn
    char_part = (
        f"单次发言不超过 {n} 个字"
        "（中文按字符数计，英文按以空白分词的单词数计；中英混合按各自规则相加）"
        if n is not None
        else None
    )
    sec_part = f"每轮发言不超过 {s} 秒" if s is not None else None
    parts = [p for p in (char_part, sec_part) if p]
    body = "；".join(parts)
    return f"本场会议有发言约束：{body}。请遵守。"


def resolve_system_model(
    mode: ModeTemplate | None,
    roster: list[Participant],
) -> str | None:
    """Resolve the model id for a discussion's *system-level* LLM calls.

    System-level calls are the validator (验收), the manager speaker-selector
    (选发言人) and the social-style policy (社交风格) — work the discussion does
    on its own behalf, NOT a participant speaking.  A discussion is fully
    self-contained: it never depends on an externally supplied ``model_id``
    (e.g. ``query::mb_pro``).  The model source is, in order:

    1. the selected mode's ``flow_policy.system_model_id`` (a dedicated,
       mode-template-level setting the user configures), when set;
    2. otherwise the first roster member's ``model_id`` (every NAMED_AGENT
       participant is now required to carry a non-empty model — see
       ``CreateParticipantUseCase`` / ``UpdateParticipantUseCase``).

    Returns ``None`` only when neither source yields a model (e.g. an empty
    roster in a unit-test wiring); callers degrade gracefully exactly as they
    did when the old ``default_model_id`` fallback returned ``None``.
    """
    if mode is not None and mode.flow_policy.system_model_id:
        return mode.flow_policy.system_model_id
    return roster[0].model_id if roster else None

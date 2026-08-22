# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure token-usage aggregation math (C档 阶段2 — usage收口).

Single source of truth for the "cumulative-prompt provider" usage-finalize
口径 that was previously expressed twice with divergent shapes:

* the main agent — ``StreamChatUseCase._finalize_turn_usage``
  (``application/use_cases/streaming.py``): for an Anthropic/Claude turn it
  overrides the summed ``prompt_tokens`` with the LAST round's value and
  recomputes ``total_tokens``, because Claude RE-SENDS the full conversation
  each round so a per-round ``prompt_tokens`` is already the running wire size
  (summing it across rounds is quadratic — the observed 10M-token bug);
* the sub-agent — ``SubAgentSession.accumulate_usage``
  (``domain/sub_agent_session.py``) which SUMs every key (including
  ``prompt_tokens``) into the persisted cumulative ``usage`` and separately
  tracks a replace-last ``last_prompt_tokens`` for the live badge.

This module hosts the ONE pure rule both layers can share. It lives in the
domain layer so both the domain ``SubAgentSession`` entity and the application
``StreamChatUseCase`` may import it without breaking the ``layered-chat``
import-linter contract (application ⇐ domain is the allowed direction; domain
imports nothing upward).

No I/O, no global state, no time, no random — pure functions only.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "is_cumulative_prompt_family",
    "finalize_cumulative_prompt_usage",
]


def is_cumulative_prompt_family(model_id: str | None) -> bool:
    """True when ``model_id`` is an Anthropic/Claude-family model.

    Anthropic/Claude RE-SEND the entire conversation (plus all prior tool
    results) on every round, so each round's provider ``prompt_tokens`` is
    ALREADY the running wire size — summing it across rounds double-counts
    (quadratic). Such families need the last-round prompt override in
    :func:`finalize_cumulative_prompt_usage`. OpenAI / Azure / Gemini / Vertex
    report the current round only, so their per-round sum is already correct.

    Keyed on the model id (``"claude"`` substring) — the authoritative
    selector (the client-supplied provider field is unvalidated input). This is
    the canonical home for the rule previously duplicated as
    ``streaming._is_anthropic_family`` /
    ``_token_estimate_helpers.is_anthropic_family`` /
    ``agent_tool._is_anthropic_family``.
    """
    return isinstance(model_id, str) and "claude" in model_id.lower()


def finalize_cumulative_prompt_usage(
    summed: dict[str, Any],
    last_round_usage: dict[str, Any] | None,
    *,
    is_cumulative: bool,
) -> dict[str, int]:
    """Return a usage dict with ``prompt_tokens`` corrected for cumulative-prompt
    providers.

    ``summed`` is the round-over-round SUM of every integer usage key (correct
    for ``completion_tokens`` / ``cache_read_tokens`` — independent and
    additive). For a cumulative-prompt family (``is_cumulative=True``, i.e.
    Anthropic/Claude) the summed ``prompt_tokens`` / ``total_tokens`` are wrong
    (quadratic), so they are overridden:

    * ``prompt_tokens`` ← the LAST round's ``prompt_tokens`` (the true final
      wire size);
    * ``total_tokens`` ← ``prompt_tokens(last round) + completion_tokens(SUM)``
      so the dict stays self-consistent.

    Every other key is preserved verbatim from ``summed``.

    Invariants (zero behaviour change where the SUM was already right):

    * **Single-round turn**: ``last_round == summed`` numerically, so the
      override is a no-op (prompt unchanged; total = prompt + completion which
      for one round equals the model's total).
    * **Non-cumulative provider** (``is_cumulative=False``): returns
      ``dict(summed)`` untouched — their per-round ``prompt_tokens`` is the
      current round only, so the SUM口径 is left exactly as before.
    * **No last-round usage captured** (``None`` / non-dict / missing or
      non-int ``prompt_tokens``): falls back to ``dict(summed)`` (cannot correct
      without the keystone figure).

    The SHAPE (field names / types) is unchanged — only the runtime VALUE of
    ``prompt_tokens`` / ``total_tokens`` is corrected from the bogus SUM to the
    true last-round wire size.
    """
    result = dict(summed)
    if not is_cumulative:
        return result
    if not isinstance(last_round_usage, dict):
        return result
    last_prompt = last_round_usage.get("prompt_tokens")
    if not isinstance(last_prompt, int) or isinstance(last_prompt, bool):
        return result
    result["prompt_tokens"] = last_prompt
    completion = result.get("completion_tokens", 0)
    if not isinstance(completion, int) or isinstance(completion, bool):
        completion = 0
    result["total_tokens"] = last_prompt + completion
    return result

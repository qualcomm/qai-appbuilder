# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Adaptive tool-result truncator (PR-401b / S7.5 lane L4).

Migrates the ad-hoc adaptive truncation logic at
``backend/chat_handler.py:820-854`` into the chat bounded context.

The legacy implementation looked at the model's family (``Claude`` /
``Gemini`` / ``GPT-4o`` / ``Doubao`` / unknown) and the running
context-usage ratio to pick a per-tool-call character budget; the
chosen budget was then applied as a head-tail split with a
``"... [N 字符已省略] ..."`` separator.

退化 #11 (subtask 4) — pressure-shrink removed
---------------------------------------------
The legacy "context-usage pressure shrink" (multiply the family budget
by 60% / 30% / 15% as the conversation fills up) is GONE here. In V2 the
caller never had a real ``current_used_tokens`` / per-model context length
to feed it (the streaming loop hard-coded ``current_used_tokens=0`` and the
``context_length_override`` defaulted to 0), so the shrink ladder could
NEVER fire — it was dead code that only created the illusion of adaptivity.
The robust replacement for "a single tool result must not blow the budget"
is the oversized-output STORE (``tool_result_store`` /
``data/tool_results/``): the full body is persisted to disk and the model
``read``s it back on demand, instead of being shrunk in-prompt by a ratio
that was never computed. This adapter therefore keeps ONLY the per-family
base budget head/tail split (the genuine, always-active backstop):

Ordered-slice / persisted-result exemption (recoverability-aware)
-----------------------------------------------------------------
``truncate`` PASSES THROUGH (no split) result classes whose omitted content is
already recoverable upstream (or whose content is whole), so a head+tail split
here would be both useless and destructive:

* ``tool_name in {"read", "list"}`` — these emit an ORDERED slice (file lines /
  directory entries) with their own "continue with offset=N" pagination notice
  and are deliberately NOT routed through the disk store. They return the whole
  slice up to their own line / entry caps, so a result that is large-but-whole
  (e.g. a 68KB SKILL.md, or a 1500-entry directory page) must pass through
  intact — splitting it would drop the middle with no recovery path.
* ``request.already_truncated`` / any text containing ``[full_output_saved]`` —
  the producing tool bounded its own output and/or persisted the full body to
  the store with a ``read(path=...)`` retrieval hint; re-splitting would corrupt
  that footer.

For every OTHER tool the per-family base budget head/tail split still applies:

* **Family budgets** — high-cap models (Claude, Gemini, anthropic,
  ``opus``) get a generous baseline (``high_budget`` = 50 000 chars
  by default — V1's single-result hard ceiling
  ``TOOL_RESULT_HARD_CAP_CHARS``); mid-tier models (``gpt`` / ``doubao`` /
  ``volces``) get ``mid_budget`` (50 000); unknown / small models fall
  back to ``low_budget`` (30 000).
* **Head-tail split** — once the budget is fixed, results above it
  are sliced into ``[:half] + separator + [-half:]`` (with
  ``separator`` carrying the elided character count for
  observability).
* **Family resolver** — a callable that maps a ``model_id`` to a
  family bucket; the default heuristic is a substring match.

The public contract is :class:`ToolResultTruncatorPort` on which the
agentic loop depends; the heuristic stays internal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from qai.chat.application.ports import (
    ToolResultTruncationRequest,
    ToolResultTruncationResult,
    ToolResultTruncatorPort,
)


# Default per-family character budgets.
#
# V1 PARITY CEILING (root cause fix): the legacy single-result hard cap was
# ``TOOL_RESULT_HARD_CAP_CHARS = 50_000`` (``backend/truncation_constants.py``;
# ``model_profiles.compute_tool_result_max`` clamps EVERY family's result to
# ``min(50_000, ...)``).  V1's per-family *bases* were 50K (claude/gemini),
# 25K (doubao/gpt/deepseek/qwen) and 15K (gpt-4-legacy) — all <= 50K.  The
# rewritten adapter had drifted ``high_budget`` to 100_000, i.e. it let a
# SINGLE Claude/Gemini tool result reach 100K chars (~25-30K tokens) — twice
# what V1 ever allowed — which on a small-context model can blow the budget in
# one tool call before the inter-round compressor's 80%-of-budget gate fires.
# ``high_budget`` is therefore pinned back to the V1 ceiling (50_000) so no
# single tool result exceeds what V1 permitted.  ``mid`` (50K) sits exactly at
# the ceiling and ``low`` (30K) below it, both within V1's bound.
DEFAULT_HIGH_BUDGET: int = 50_000
DEFAULT_MID_BUDGET: int = 50_000
DEFAULT_LOW_BUDGET: int = 30_000

#: Hard ceiling for read/list results. These tools use ordered-slice
#: pagination (offset re-reads) so we do NOT head+tail split them, but a
#: single slice that itself exceeds this cap is truncated at the TAIL (keeping
#: the head + the offset-recovery footer) — a 50KB read
#: byte cap so one oversized slice cannot dominate the context window.
READ_LIST_HARD_CAP: int = 50_000


def _default_family_resolver(model_id: str) -> str:
    """Heuristic family lookup based on substring of ``model_id``.

    Returns ``"high"`` / ``"mid"`` / ``"low"``.  Maps:

    * ``"claude"`` / ``"gemini"`` / ``"anthropic"`` / ``"opus"`` → high
    * ``"gpt"`` / ``"doubao"`` / ``"volces"`` → mid
    * everything else → low
    """
    lower = (model_id or "").lower()
    if any(tok in lower for tok in ("claude", "gemini", "anthropic", "opus")):
        return "high"
    if any(tok in lower for tok in ("gpt", "doubao", "volces")):
        return "mid"
    return "low"


@dataclass(slots=True)
class AdaptiveToolResultTruncator(ToolResultTruncatorPort):
    """Default :class:`ToolResultTruncatorPort` implementation.

    All numeric knobs are public attributes so adapters wired in tests
    (or with model_profile-aware overrides) can replace individual
    budgets without subclassing.
    """

    high_budget: int = DEFAULT_HIGH_BUDGET
    mid_budget: int = DEFAULT_MID_BUDGET
    low_budget: int = DEFAULT_LOW_BUDGET
    family_resolver: Callable[[str], str] = field(default=_default_family_resolver)
    """Maps model_id -> ``"high"`` / ``"mid"`` / ``"low"``."""

    def truncate(
        self,
        request: ToolResultTruncationRequest,
    ) -> ToolResultTruncationResult:
        text = request.result_text
        original_length = len(text)

        # Do NOT head+tail SPLIT results that already carry a recoverable
        # pagination / persistence contract. Several signals, any of which means
        # the omitted content is recoverable upstream so a split here is both
        # useless and destructive:
        #
        #   1. ``request.already_truncated`` — the producing tool STRUCTURALLY
        #      reported that it bounded its own output (it set ``truncated`` on
        #      its result dict and/or persisted the full body to the
        #      oversized-output store). This is the robust, tool-agnostic
        #      signal: it covers read / glob / grep / exec / list whose
        #      recovery footers differ (``[truncation note] ... read(path=...)``
        #      vs ``[full_output_saved]`` vs ``offset=N``), instead of matching
        #      one specific footer string.
        #   2. ``tool_name in {"read", "list"}`` — these emit an ORDERED slice
        #      (file lines / directory entries) and recover via ``offset``
        #      re-reads: read returns the whole slice up to its own 2000-line /
        #      50KB cap, list the whole page up to its entry cap. A head+tail
        #      split would drop the MIDDLE of that ordered content with no
        #      recovery path (they are deliberately NOT routed through the
        #      disk store — see ``registry._STORABLE_RESULT_FIELDS``), so a
        #      whole-but-large result (e.g. a 68KB SKILL.md, or a 1500-entry
        #      directory page that fits under the line/entry cap yet exceeds the
        #      char budget here) must pass through intact. This is the primary
        #      guard for the un-truncated-but-large case (``already_truncated``
        #      is False then).
        #   3. Any text containing ``[full_output_saved]`` — the oversized body
        #      was persisted to the store and the text embeds a
        #      ``read(path=...)`` retrieval hint; re-splitting would corrupt
        #      that footer.
        #
        # In all cases the upstream layer already guarantees the omitted
        # content is recoverable (or there is nothing to recover because the
        # result is whole), so the budget split here is not only unnecessary but
        # actively harmful. Pass the text through unchanged.
        # read/list use ordered-slice pagination (offset re-reads); a head+tail
        # split would drop the middle with no recovery path. So we pass them through
        # WHOLE up to a hard cap — but a single slice exceeding the cap is truncated
        # at the TAIL (head + offset footer preserved) so it cannot blow the window.
        #
        # ``skill`` joins them: the skill tool paginates its SKILL.md BY LINE and
        # already self-bounds each page to ~10K chars / 250 lines, appending a
        # ``[skill note] ... offset=N ... call skill again`` continuation footer
        # (the same ordered-slice / offset-recovery contract as ``read``). A
        # head+tail split here would drop the MIDDLE of an already-bounded page
        # and corrupt that footer's line coordinates, so skill results must pass
        # through intact. Because the skill tool self-bounds below every family
        # budget, this branch is effectively a whole pass-through for it.
        if request.tool_name in ("read", "list", "skill"):
            if len(text) <= READ_LIST_HARD_CAP:
                return ToolResultTruncationResult(
                    text=text,
                    truncated=False,
                    original_length=original_length,
                    final_length=original_length,
                    omitted_chars=0,
                )
            kept = text[:READ_LIST_HARD_CAP]
            omitted = original_length - READ_LIST_HARD_CAP
            # This is a rare SECOND-level backstop: read/list/skill already
            # paginate + self-bound each slice (with their own
            # ``showed lines X-Y of total N; offset=Z`` footer), so we only get
            # here when a SINGLE slice itself exceeds the 50KB hard cap. We
            # cannot recover the producing tool's true total-line count from the
            # rendered string, but the kept HEAD's newline count is a faithful
            # lower bound on "how many lines fit", which maps directly to the
            # ``offset`` the model should jump past to read the remainder. Give
            # that actionable coordinate instead of a bare char count.
            kept_lines = kept.count("\n") + 1
            next_offset = kept_lines + 1
            return ToolResultTruncationResult(
                text=(
                    kept
                    + f"\n\n... [{omitted} chars omitted — this single "
                    f"{request.tool_name} slice exceeded the "
                    f"{READ_LIST_HARD_CAP // 1024}KB hard cap after ~"
                    f"{kept_lines} line(s). Call `{request.tool_name}` again "
                    f"with offset={next_offset} (and optionally a smaller "
                    f"`limit`) to read the remaining {omitted} character(s).]"
                ),
                truncated=True,
                original_length=original_length,
                final_length=len(kept),
                omitted_chars=omitted,
            )

        if request.already_truncated or "[full_output_saved]" in text:
            return ToolResultTruncationResult(
                text=text,
                truncated=False,
                original_length=original_length,
                final_length=original_length,
                omitted_chars=0,
            )

        budget = self._resolve_budget(model_id=request.model_id)

        if original_length <= budget:
            return ToolResultTruncationResult(
                text=text,
                truncated=False,
                original_length=original_length,
                final_length=original_length,
                omitted_chars=0,
            )

        half = budget // 2
        omitted = original_length - half * 2
        # Defensive: if `budget < 2` the head-tail split degenerates;
        # truncate to the budget length and emit a tail marker only.
        if half <= 0:
            new_text = f"... [{original_length} chars omitted] ..."
            return ToolResultTruncationResult(
                text=new_text,
                truncated=True,
                original_length=original_length,
                final_length=len(new_text),
                omitted_chars=original_length,
            )
        separator = f"\n\n... [{omitted} chars omitted] ...\n\n"
        new_text = text[:half] + separator + text[-half:]
        return ToolResultTruncationResult(
            text=new_text,
            truncated=True,
            original_length=original_length,
            final_length=len(new_text),
            omitted_chars=omitted,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_budget(self, *, model_id: str) -> int:
        family = self.family_resolver(model_id)
        if family == "high":
            return self.high_budget
        if family == "mid":
            return self.mid_budget
        return self.low_budget


__all__ = [
    "AdaptiveToolResultTruncator",
    "DEFAULT_HIGH_BUDGET",
    "DEFAULT_MID_BUDGET",
    "DEFAULT_LOW_BUDGET",
]

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

Artifact overflow instead of silent middle loss
----------------------------------------------
The head/tail split above is the ONLY branch that used to destroy content with
no way back: ``text[:half] + separator + text[-half:]`` dropped the middle
permanently. Tools that manage their own oversized output (``exec`` / ``grep``)
never had that problem — they persist the full body to the oversized-output
store and leave a ``[full_output_saved] path=... `` + ``read(path=...)`` footer,
so the model can always recover the elided middle.

:attr:`AdaptiveToolResultTruncator.overflow_store` closes that gap for every
OTHER tool: when a result exceeds its family budget the full text is first
offered to the store, and on success the returned text becomes
``head + omit-marker + tail + the SAME footer the self-managing tools emit``.
The model therefore sees ONE recovery contract across both paths (it must not
have to learn a second footer dialect).

The store is an OPTIONAL, injected capability (:class:`ToolResultOverflowStore`,
a narrow structural protocol declared here rather than imported: the concrete
implementation lives in another bounded context, so the composition root
supplies it and the ``context-isolation`` import contract stays intact). When it
is absent — or persistence fails, or the store declines to persist — the
truncator degrades to the byte-for-byte prior behaviour (plain head+tail split).
Tool-result handling must never fail because a disk write did.

The ordered-slice branch above deliberately does NOT overflow to the store, and
that is not merely because its ``offset`` footer is already a recovery path: a
persisted ``read``/``list`` body is itself larger than the store's preview
threshold, so reading it back would just get head+tail cut AGAIN — an
unrecoverable loop. That is exactly why ``read``/``list`` are excluded from
``registry._STORABLE_RESULT_FIELDS`` upstream; routing them through the store
from here would reintroduce the loop through the back door.

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

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from qai.chat.application.ports import (
    ToolResultTruncationRequest,
    ToolResultTruncationResult,
    ToolResultTruncatorPort,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class ToolResultOverflowResult(Protocol):
    """Structural view of an overflow-store outcome.

    Only ``stored_path`` matters here: it is the coordinate the model passes
    back to the ``read`` tool to recover the full body. ``None`` means the
    store declined or failed to persist, in which case this truncator emits no
    recovery footer and falls back to the plain head+tail split.
    """

    @property
    def stored_path(self) -> str | None: ...


@runtime_checkable
class ToolResultOverflowStore(Protocol):
    """Narrow "persist the full body, tell me where" capability.

    Declared HERE rather than imported: the production implementation lives in
    another bounded context (its tool handlers are where oversized outputs are
    produced), and the ``context-isolation`` import contract forbids a direct
    edge between contexts. A structural protocol lets the composition root
    inject that implementation unchanged — it already satisfies this shape — so
    no adapter shim exists to drift.

    ``force=True`` is what this caller always passes: the store's own gate is a
    BYTE threshold, while the decision to overflow here is driven by a
    per-family CHARACTER budget. Those two signals disagree (a result over a
    small character budget can sit well under the byte threshold), so leaving
    the gate to the store would silently discard the middle of exactly the
    results this path exists to rescue.

    Implementations MUST degrade gracefully rather than raise, but this
    truncator treats any exception as "not persisted" regardless: tool-result
    handling may never fail because persistence did.
    """

    def store(
        self,
        output: str,
        *,
        tool_name: str = "",
        context_hint: str = "",
        force: bool = False,
    ) -> ToolResultOverflowResult: ...


#: Recovery advice appended after an overflow footer.
#:
#: Byte-for-byte the advice the self-managing tools emit
#: (``tool_result_store.DEFAULT_TRUNCATION_ADVICE``) so the model sees ONE
#: recovery contract, not two dialects. It is RESTATED rather than imported
#: because that constant lives in another bounded context; a test asserts the
#: two are literally identical, so any drift fails loudly.
TRUNCATION_OVERFLOW_ADVICE: str = (
    "[truncation_advice] If you need the omitted middle, "
    "call read(path=...) on the saved file path above; "
    "otherwise rely on head+tail and continue."
)


def _overflow_footer(stored_path: str) -> str:
    """Render the persisted-body footer in the self-managing tools' format."""
    return (
        "\n---\n"
        f"[full_output_saved] path={stored_path}\n"
        f"{TRUNCATION_OVERFLOW_ADVICE}\n"
    )


#: Overflow outcomes reported by :meth:`AdaptiveToolResultTruncator.truncate`.
#:
#: A truncation either persisted the full body somewhere recoverable, or it
#: did not — and WHY it did not is the difference between "this deployment
#: never wired the store" (a wiring bug, silently fixable) and "the store was
#: there but refused / crashed" (a runtime problem). Collapsing them into one
#: "unstored" label would force a log reader to correlate two records to tell
#: a misconfiguration from a disk failure, so each outcome gets its own name
#: and they all ride on ONE log line.
OVERFLOW_STORED: str = "stored"
OVERFLOW_NOT_INJECTED: str = "not_injected"
OVERFLOW_DECLINED: str = "declined"
OVERFLOW_FAILED: str = "failed"
#: The ordered-slice branch (``read`` / ``list`` / ``skill``) DELIBERATELY does
#: not overflow — persisting those bodies would re-truncate on read-back (see
#: :meth:`AdaptiveToolResultTruncator.truncate`). Reported explicitly so a
#: reader can tell "by design" from "wiring missing".
OVERFLOW_NOT_APPLICABLE: str = "not_applicable"

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

#: Default hard ceiling for ordered-slice results (``read`` / ``list`` /
#: ``skill``). These tools use ordered-slice pagination (offset re-reads) so we
#: do NOT head+tail split them, but a single slice that itself exceeds this
#: ceiling is truncated at the TAIL so one oversized slice cannot dominate the
#: context window.
#:
#: This MUST sit ABOVE the largest slice the producing tools can legitimately
#: return, or it stops being a backstop and becomes a second routine cut that
#: destroys the tool's own truncation notice (which lives at the very END of the
#: text, so a tail cut always eats it). The previous 50_000 was set to "match
#: read's 50KB byte cap" — but read's cap counts BODY BYTES while this counts
#: RENDERED CHARS, and the rendered text additionally carries a ``N\t`` line-num
#: prefix per line plus the notice. An ordinary in-spec ``read`` therefore
#: rendered ~54-69K chars and tripped this ceiling EVERY time, replacing read's
#: accurate "showed lines 800-1351" footer with a fabricated offset.
#:
#: Worst case in-spec render at DEFAULT read caps: body <= ``read_max_bytes``
#: (51200) + one boundary-crossing line (<= ``read_max_line_length`` 2000 + tag)
#: + <= ``read_max_lines`` (2000) line-number prefixes (<= 8 chars each) +
#: notice ~= 69.5K chars. 80_000 clears that with headroom while still bounding
#: a pathological slice.
#:
#: NOTE: read's caps are operator-CONFIGURABLE
#: (``settings.tool_output.read_max_*``), so a deployment that RAISES them can
#: outgrow this default. The value is therefore an injectable instance attribute
#: (``AdaptiveToolResultTruncator.ordered_slice_cap``) and the wiring root
#: raises it from the live thresholds via :func:`ordered_slice_cap_for` — a
#: fixed constant cannot track a configurable producer.
DEFAULT_ORDERED_SLICE_CAP: int = 80_000


def ordered_slice_cap_for(
    *,
    read_max_bytes: int,
    read_max_lines: int,
    read_max_line_length: int,
    floor: int = DEFAULT_ORDERED_SLICE_CAP,
) -> int:
    """Return an ordered-slice ceiling that clears the worst in-spec render.

    Derived from the SAME knobs that bound ``read``, so raising a read cap can
    never push an ordinary in-spec slice over the backstop (which would tail-cut
    it and destroy read's own truncation notice). Terms mirror how ``read``
    renders a code file:

    * ``read_max_bytes``       — the materialised body (bytes ≈ chars worst case);
    * ``read_max_line_length`` — the one line allowed to cross the byte boundary,
      plus its "(line truncated: kept N/M chars)" tag;
    * ``read_max_lines``       — a ``N\\t`` line-number prefix per emitted line;
    * a fixed allowance for the trailing truncation notice.

    Never returns below ``floor`` so a deployment that SHRINKS its read caps
    keeps the default headroom rather than tightening the backstop onto
    ``skill`` / ``list`` output (whose sizes these knobs do not describe).
    """
    _NOTICE_ALLOWANCE = 400
    _LINE_NUM_PREFIX = 8  # up to 7 digits + tab
    _CLIP_TAG = 60  # " ... (line truncated: kept N/M chars)"
    derived = (
        max(0, read_max_bytes)
        + max(0, read_max_line_length)
        + _CLIP_TAG
        + max(0, read_max_lines) * _LINE_NUM_PREFIX
        + _NOTICE_ALLOWANCE
    )
    # Headroom above the worst case so the backstop stays a backstop.
    return max(floor, int(derived * 1.15))


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
    ordered_slice_cap: int = DEFAULT_ORDERED_SLICE_CAP
    """Tail-cut ceiling for ordered-slice tools (``read`` / ``list`` /
    ``skill``). MUST stay above the largest in-spec slice those tools can
    return, else the backstop routinely destroys their own truncation notice —
    derive it with :func:`ordered_slice_cap_for` from the live ``read`` caps."""
    family_resolver: Callable[[str], str] = field(default=_default_family_resolver)
    """Maps model_id -> ``"high"`` / ``"mid"`` / ``"low"``."""
    overflow_store: ToolResultOverflowStore | None = None
    """Optional store that persists an over-budget body so the head+tail split
    can hand the model a ``read(path=...)`` recovery hint instead of dropping
    the middle for good. ``None`` (the default, and the state of every caller
    that does not wire one) keeps the historical pure-truncation behaviour
    byte for byte."""

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
        #
        # This branch deliberately does NOT overflow to
        # :attr:`overflow_store`, and the reason is stronger than "its
        # ``offset`` footer is already a recovery path": a persisted
        # ``read``/``list`` body necessarily exceeds the store's own preview
        # threshold, so reading the saved file back would get head+tail cut
        # AGAIN — an unrecoverable loop where the model can never reach the
        # full text. That is precisely why ``read``/``list`` are kept out of
        # ``registry._STORABLE_RESULT_FIELDS`` upstream; persisting them from
        # here would reintroduce the loop through the back door.
        if request.tool_name in ("read", "list", "skill"):
            cap = self.ordered_slice_cap
            if len(text) <= cap:
                return ToolResultTruncationResult(
                    text=text,
                    truncated=False,
                    original_length=original_length,
                    final_length=original_length,
                    omitted_chars=0,
                )
            kept = text[:cap]
            omitted = original_length - cap
            # This is a rare SECOND-level backstop: read/list/skill already
            # paginate + self-bound each slice (with their own
            # ``showed lines X-Y of total N; offset=Z`` footer), so we only get
            # here when a SINGLE slice itself exceeds the hard cap. We cannot
            # recover the producing tool's true total-line count from the
            # rendered string, but the kept HEAD's newline count tells us how
            # many of the slice's lines survived — which becomes a usable
            # ``offset`` only after being rebased onto where the slice STARTED.
            # ``slice_start_line`` carries that origin: for a slice from
            # ``read(path, offset=800)`` the kept-line count alone pointed the
            # model back to offset ~516, i.e. INTO content it had already seen,
            # so "continuing" the read re-fetched the same window forever. The
            # tail cut also destroys the producing tool's own footer (it lived
            # at the very end of the text), which is why this footer must carry
            # a correct coordinate itself rather than defer to it.
            kept_lines = kept.count("\n") + 1
            next_offset = max(1, request.slice_start_line) + kept_lines
            # Ordered-slice backstop fired. ``overflow=not_applicable`` is a
            # DESIGN statement, not a wiring gap: persisting a read/list/skill
            # body would re-truncate on read-back (see the branch comment
            # above), so this path deliberately never consults the store.
            logger.info(
                "tool_result_truncator: truncated %s - original=%d budget=%d "
                "final=%d omitted=%d overflow=%s stored_path=- "
                "ordered_slice_next_offset=%d",
                request.tool_name or "<unknown tool>",
                original_length,
                cap,
                len(kept),
                omitted,
                OVERFLOW_NOT_APPLICABLE,
                next_offset,
            )
            return ToolResultTruncationResult(
                text=(
                    kept
                    + f"\n\n... [{omitted} chars omitted — this single "
                    f"{request.tool_name} slice exceeded the "
                    f"{cap // 1024}KB hard cap after "
                    f"{kept_lines} line(s), and this cut REPLACED that "
                    f"{request.tool_name}'s own truncation notice. Call "
                    f"`{request.tool_name}` again with offset={next_offset} "
                    f"(and optionally a smaller `limit`) to read the remaining "
                    f"{omitted} character(s).]"
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
            # Degenerate configuration (budget < 2): the whole body is dropped
            # and there is no head/tail to overflow around, so no store is
            # consulted. Logged with the same shape as the normal branch so a
            # misconfigured budget is visible rather than mysterious.
            logger.warning(
                "tool_result_truncator: truncated %s - original=%d budget=%d "
                "final=%d omitted=%d overflow=%s stored_path=- "
                "(degenerate budget, whole body dropped)",
                request.tool_name or "<unknown tool>",
                original_length,
                budget,
                len(new_text),
                original_length,
                OVERFLOW_NOT_APPLICABLE,
            )
            return ToolResultTruncationResult(
                text=new_text,
                truncated=True,
                original_length=original_length,
                final_length=len(new_text),
                omitted_chars=original_length,
            )
        separator = f"\n\n... [{omitted} chars omitted] ...\n\n"
        # Overflow BEFORE discarding: the middle is only lost if persistence is
        # unavailable or fails. On success the footer matches, byte for byte,
        # what the self-managing tools emit, so the model reads one recovery
        # contract everywhere.
        stored_path, overflow_outcome = self._persist_overflow(
            text, tool_name=request.tool_name
        )
        footer = _overflow_footer(stored_path) if stored_path is not None else ""
        new_text = text[:half] + separator + text[-half:] + footer
        # One line carrying everything needed to judge a truncation: which
        # tool, how big the body was, the budget it blew, what survived, how
        # much was cut, and whether the cut middle is recoverable (with the
        # exact reason when it is not).
        logger.info(
            "tool_result_truncator: truncated %s - original=%d budget=%d "
            "final=%d omitted=%d overflow=%s stored_path=%s",
            request.tool_name or "<unknown tool>",
            original_length,
            budget,
            len(new_text),
            omitted,
            overflow_outcome,
            stored_path or "-",
        )
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
    def _persist_overflow(
        self, text: str, *, tool_name: str
    ) -> tuple[str | None, str]:
        """Persist ``text`` in full; return ``(path_or_None, outcome_label)``.

        A ``None`` path is a first-class outcome, not an error: no store was
        wired, the store declined to persist, or persistence failed (disk full /
        permission denied). The caller then emits the historical plain head+tail
        split, so a storage problem can never turn a successful tool call into a
        failed one.

        The second element names WHICH of those three happened
        (:data:`OVERFLOW_NOT_INJECTED` / :data:`OVERFLOW_DECLINED` /
        :data:`OVERFLOW_FAILED`, else :data:`OVERFLOW_STORED`) so the caller
        can report the outcome on a single log line — the fall-back behaviour
        is identical in all three cases, and only the label distinguishes a
        wiring bug from a runtime one.
        """
        store = self.overflow_store
        if store is None:
            # No store wired at all. Logged because a MISSING injection is a
            # silent-failure class of its own: everything still "works", the
            # middle is just gone forever, and nothing in the old code path
            # said so.
            logger.info(
                "tool_result_truncator: no overflow store injected for %s; "
                "omitted middle is unrecoverable (plain head+tail truncation)",
                tool_name or "<unknown tool>",
            )
            return None, OVERFLOW_NOT_INJECTED
        try:
            # ``force``: the store gates on BYTES, we decided on CHARACTERS —
            # see :class:`ToolResultOverflowStore`.
            outcome = store.store(text, tool_name=tool_name, force=True)
            stored_path = outcome.stored_path
        except Exception:  # noqa: BLE001 — persistence is strictly best-effort
            logger.warning(
                "tool_result_truncator: overflow store failed for %s; "
                "falling back to head+tail truncation",
                tool_name or "<unknown tool>",
                exc_info=True,
            )
            return None, OVERFLOW_FAILED
        if not stored_path:
            # Promoted from DEBUG: a store that silently declines produces the
            # exact same user-visible outcome as no store at all, so it must be
            # visible at the level the operator actually collects.
            logger.info(
                "tool_result_truncator: overflow store did not persist %s; "
                "falling back to head+tail truncation",
                tool_name or "<unknown tool>",
            )
            return None, OVERFLOW_DECLINED
        return stored_path, OVERFLOW_STORED

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
    "TRUNCATION_OVERFLOW_ADVICE",
    "OVERFLOW_STORED",
    "OVERFLOW_NOT_INJECTED",
    "OVERFLOW_DECLINED",
    "OVERFLOW_FAILED",
    "OVERFLOW_NOT_APPLICABLE",
    "ToolResultOverflowResult",
    "ToolResultOverflowStore",
]

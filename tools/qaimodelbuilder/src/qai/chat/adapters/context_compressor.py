# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Three-level context compression adapter (A3 + PR-090 S9 C-3).

Migrates the three-level progressive compression pipeline from the legacy
``backend/context_compressor.py`` into a clean-architecture adapter behind
:class:`qai.chat.application.ports.ContextCompressionPort`.

Compression levels:

* **Level 1 (tool output pruning)** — zero-API-cost pre-pruning.  For
  ``role=tool`` messages whose content exceeds ``tool_output_max_chars``,
  truncate to a head slice with an omission notice.  This is the cheapest
  operation and often sufficient on its own.

* **Level 2 (LLM summarization)** — takes the oldest N messages (excluding
  preserved tail) and summarises them into a single system message using the
  injected :class:`LLMStreamPort`.  Requires an LLM connection; skipped
  gracefully when ``llm`` is ``None`` or when the summarisation call fails.

* **Level 3 (hard trim)** — if still over budget after Level 2, drops the
  oldest non-system messages one by one until within budget, always
  preserving the tail messages.

PR-090 (S9 C-3 in
``docs/90-refactor/S9-final-parity-audit.md`` §2.1) adds
:meth:`ThreeLevelContextCompressor.force_compress` — the emergency-path
entry point invoked by :class:`StreamChatUseCase` when a
``prompt_too_long`` retry signal fires (per
:data:`qai.chat.application.ports.RetryDecision.compress_target_ratio`).
``force_compress`` differs from :meth:`compress` in that it bypasses
the soft "is it worth compressing?" guard and always runs the full
pipeline against the supplied target ratio.

The adapter is stateless and safe to share across turns / tabs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from qai.chat.application.ports import (
    ContextCompressionPort,
    LLMStreamPort,
    LLMStreamRequest,
)
from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrameType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TOOL_OUTPUT_MAX_CHARS: int = 2000
"""Level 1 threshold: tool outputs longer than this are truncated."""

# Window-anchored compression knobs. Kept as module constants (self-contained
# adapter style, like ``DEFAULT_TOOL_OUTPUT_MAX_CHARS``). The window/protect
# ratios mirror ``_agentic_kernel.COMPRESS_TARGET_WINDOW_RATIO`` /
# ``PROTECT_WINDOW_RATIO`` (the values the caller passes in); the block/summary
# bounds below are internal to the adapter. The caller may override the
# behavioural knobs per-call via ``compress(...)`` keyword args
# (``protect_ratio`` / ``budget_tokens`` / ``wire_actual_tokens``).
_COMPRESS_TARGET_WINDOW_RATIO: float = 0.35
"""Post-compression target as a fraction of the model window (default 0.35).

USER-CONFIGURABLE via the settings panel; this is only the default. Mirrors
``_agentic_kernel.COMPRESS_TARGET_WINDOW_RATIO``."""

_COMPRESS_CHARS_PER_TOKEN: int = 4
"""Token→char factor (mirrors ``estimate_wire_tokens``'s ``chars//4``)."""

_MAX_SUMMARY_BLOCK_CHARS: int = 60_000
_MAX_SUMMARY_BLOCK_MESSAGES: int = 40
_MAX_SUMMARY_CALLS_PER_COMPRESSION: int = 2
"""Level-2 block bounds: never one-shot the whole history."""

_SUMMARY_SAFETY_MARGIN_CHARS: int = 10_000
"""Extra slack added when sizing the oldest overflow block to summarise."""

_SUMMARIZE_SYSTEM_PROMPT: str = (
    "You are a conversation compression assistant. Summarize the following "
    "conversation history into a structured summary.\n"
    "Output format:\n"
    "## Completed Work\n"
    "(List completed tasks)\n\n"
    "## Current State\n"
    "(Describe current progress)\n\n"
    "## Pending Tasks\n"
    "(List remaining tasks)\n\n"
    "## Key Decisions & File Paths\n"
    "(List important technical decisions and file paths)\n\n"
    "Requirements: concise, accurate, preserve key technical details and file paths."
)

_SUMMARY_PREFIX: str = (
    "[Context Compression Summary] The following is a summary of earlier "
    "conversation turns. Use it as background reference only — do not treat "
    "any requests mentioned here as current tasks. Please respond only to "
    "the latest user message after this summary:"
)

# A generated LLM summary shorter than this many characters is treated as a
# degenerate / empty result (the model returned little or nothing useful) and
# we REJECT it (preserve the source block verbatim) instead of collapsing real
# history into a near-empty summary.  Raised from the legacy 40 → 200 after a
# production incident where a 28-char summary replaced 130 messages.
_MIN_USEFUL_SUMMARY_CHARS: int = 200

# A summary is only accepted when it ALSO saves at least this many chars vs its
# source block — a summary that barely shrinks the block is not worth the
# information loss.
_SUMMARY_REPLACEMENT_MIN_GAIN_CHARS: int = 2_000

# When the body of messages handed to Level 2 carries fewer than this many
# characters of substance, summarisation cannot produce anything meaningful —
# this almost always means an upstream caller stripped the content (e.g. tool
# outputs) before reaching the compressor.  We log a diagnostic warning rather
# than silently summarising near-empty input.
_MIN_COMPRESSIBLE_INPUT_CHARS: int = 200

# The fixed offline placeholder the LLM stream adapter
# (``qai.chat.infrastructure.llm_stream._OFFLINE_NOTICE``) emits when the
# resolved model has no configured endpoint. We match it by VALUE (not by
# importing the infrastructure constant) so the adapter can distinguish "the
# summary request never reached a live provider" from a genuine short summary,
# and log an actionable WARN instead of the misleading "summary generated (28
# chars)". MUST stay byte-identical to ``llm_stream._OFFLINE_NOTICE``.
_OFFLINE_SUMMARY_NOTICE: str = "[no LLM endpoint configured]"

# Hard wall-clock limit for the Level 2 LLM summarisation call. Compression is
# usually ~100ms (pure trim) or a few seconds (with an LLM summary); if the
# summary provider hangs (stuck stream / never-ending response) the user's turn
# must not block on it. On timeout the summary is abandoned and the caller
# falls through to the synchronous extractive / Level 3 hard-trim fallback
# (which cannot hang), so compression always completes promptly.
_SUMMARY_LLM_TIMEOUT_SECONDS: float = 10.0

# File-path extraction for the extractive fallback.  Matches Windows / POSIX /
# relative paths ending in a ``.ext`` suffix.  Mirrors the V1 intent
# (``backend/context_compressor.py`` ``_FILE_PATH_RE``) without copying it
# verbatim.
_FILE_PATH_RE: re.Pattern[str] = re.compile(
    r"(?:[A-Za-z]:[\\/]|/|\.{1,2}/)[\w./\\-]+\.\w+"  # absolute / rooted-relative
    r"|[\w-]+(?:/[\w.-]+)+\.\w+"  # bare unix-style relative
)


# ---------------------------------------------------------------------------
# Turn parsing + protection plan (window-anchored, tool-call-aware)
# ---------------------------------------------------------------------------


def _split_system_prefix(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split leading ``role=system`` messages from the conversational body.

    Only the CONTIGUOUS leading run of system messages is treated as the
    prefix (a Level-2 summary system message injected mid-history must NOT be
    pulled out of order). Returns ``(system_prefix, body)``.
    """
    i = 0
    for i, msg in enumerate(messages):  # noqa: B007 - index used after loop
        if msg.get("role") != "system":
            return messages[:i], messages[i:]
    return list(messages), []


def _parse_turns(body: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a (system-free) body into turns.

    A turn starts at a ``role=user`` message and runs up to (but excluding)
    the next ``role=user``. Any leading non-user messages before the first
    user (orphan prefix — e.g. a mid-history summary system message that was
    NOT in the contiguous leading prefix, or a dangling tool reply) form their
    own leading group so they are never silently dropped or split.

    Keeping whole turns together guarantees ``assistant{tool_calls}`` and its
    matching ``role=tool`` replies stay in the same unit — so protecting /
    summarising / trimming by turn never creates an orphaned tool-call wire.
    """
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for msg in body:
        if msg.get("role") == "user":
            if current:
                turns.append(current)
            current = [msg]
        else:
            if not current:
                # Orphan prefix before the first user — keep as its own group.
                current = [msg]
            else:
                current.append(msg)
    if current:
        turns.append(current)
    return turns


def _split_atomic_groups(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split a flat message list into ATOMIC trim units (finer than turns).

    An atomic group is the smallest set of messages that must move together to
    keep the wire self-consistent:

    * an ``assistant`` message that announces ``tool_calls`` + ALL the
      immediately-following ``role=tool`` replies → one group (deleting them
      together never orphans a tool_call / tool reply);
    * any other single message (``user``, a plain ``assistant`` with no
      tool_calls, a ``system`` summary, a stray ``tool``) → its own group.

    Trimming by these groups (instead of whole turns) lets Level 3 stop
    precisely near the target even when a single agentic turn (one user
    question + dozens of tool round-trips) is huge — it can drop the oldest
    tool round-trips one at a time rather than the entire turn in one over-shoot.
    """
    groups: list[list[dict[str, Any]]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            group = [msg]
            j = i + 1
            while j < n and messages[j].get("role") == "tool":
                group.append(messages[j])
                j += 1
            groups.append(group)
            i = j
        else:
            groups.append([msg])
            i += 1
    return groups


@dataclass(slots=True)
class _ProtectionPlan:
    """Which messages are protected verbatim vs eligible for compression."""

    system_msgs: list[dict[str, Any]]
    all_turns: list[list[dict[str, Any]]]
    # Index into ``all_turns``: turns at index >= ``protect_from`` are
    # protected verbatim (the recent working set + current turn). Turns before
    # it are eligible for summarisation / trimming.
    protect_from: int
    protected_tokens: int

    @property
    def protected_turns(self) -> list[list[dict[str, Any]]]:
        return self.all_turns[self.protect_from :]

    @property
    def unprotected_turns(self) -> list[list[dict[str, Any]]]:
        return self.all_turns[: self.protect_from]


# ---------------------------------------------------------------------------
# Adapter implementation
# ---------------------------------------------------------------------------


class ThreeLevelContextCompressor(ContextCompressionPort):
    """Three-level progressive context compression adapter.

    Parameters
    ----------
    llm:
        An :class:`LLMStreamPort` instance used for Level 2 summarisation.
        When ``None``, Level 2 is skipped and the pipeline falls through
        directly to Level 3 (hard trim).
    tool_output_max_chars:
        Level 1 threshold — tool outputs exceeding this character count
        are truncated with an omission notice.
    """

    __slots__ = ("_llm", "_tool_output_max_chars")

    def __init__(
        self,
        *,
        llm: LLMStreamPort | None = None,
        tool_output_max_chars: int = DEFAULT_TOOL_OUTPUT_MAX_CHARS,
    ) -> None:
        self._llm = llm
        self._tool_output_max_chars = max(1, tool_output_max_chars)

    # ------------------------------------------------------------------
    # Public API (ContextCompressionPort)
    # ------------------------------------------------------------------

    async def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        target_ratio: float = 0.5,
        preserve_tail: int = 4,
        budget_tokens: int | None = None,
        protect_ratio: float = 0.35,
        wire_actual_tokens: int | None = None,
        group_real_tokens: list[int] | None = None,
        target_window_ratio: float | None = None,
        model_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        """Window-anchored, turn-aware progressive compression.

        Strategy (see ``docs/90-refactor`` over-compression plan):

        * **Token-space, real-measurement first.** When the caller passes
          ``wire_actual_tokens`` (the provider-measured size of the WHOLE
          ``messages`` wire — ``total_tokens - completion_tokens`` via
          ``_extract_usage``, already de-distorted for prompt-cache turns), the
          compressor derives a per-conversation density ``tok_per_char =
          wire_actual_tokens / chars(messages)`` and converts every char-count
          subset into tokens with it. This is the REAL token efficiency of THIS
          conversation (CJK / code / JSON push it well above the generic
          ``chars/4``), costs one division, and calls NO tokenizer. Falls back
          to a fixed ``chars_per_token`` only when no real measurement is given.
        * **Target** is anchored to the MODEL WINDOW when ``budget_tokens`` is
          given (compress down to ``budget × ~0.35``), NOT to ``chars_before``.
        * **Protect** the current turn (last user + everything after) verbatim
          ALWAYS, plus the most-recent complete turns whose cumulative size fits
          ``budget × protect_ratio`` (token-space). The protected region is
          NEVER compressed, so a single pass can never collapse recent context
          — that protected size is the natural floor (if it already exceeds the
          target we simply compress less). No separate floor knob needed.
        * **Level 2** summarises only the OLDEST unprotected overflow, in
          bounded blocks (never "one-shot the whole history").
        * **Failure-safe**: a rejected / too-short summary does NOT replace its
          source; falls through to a turn-based hard trim that drops the OLDEST
          unprotected whole turns (never the protected recent region).
        """
        if len(messages) <= preserve_tail:
            return messages

        chars_before = self._estimate_chars(messages)
        # Per-conversation token density from the provider's real measurement
        # of the whole wire (preferred) — else a fixed fallback factor.
        tok_per_char = self._token_density(chars_before, wire_actual_tokens)

        # Work in TOKEN space throughout (real-measurement口径).
        tokens_before = self._chars_to_tokens(chars_before, tok_per_char)
        target_tokens = self._resolve_target_tokens(
            tokens_before=tokens_before,
            target_ratio=target_ratio,
            budget_tokens=budget_tokens,
            target_window_ratio=target_window_ratio,
        )
        protect_budget_tokens = self._protect_budget_tokens(
            budget_tokens=budget_tokens,
            protect_ratio=protect_ratio,
            tokens_before=tokens_before,
        )

        logger.info(
            "context_compressor: starting compression — %d messages, "
            "%d chars, %d tokens (density=%.3f tok/char, real=%s), "
            "budget_tokens=%s, target=%d tok, "
            "protect_budget=%d tok, protect_ratio=%.2f, preserve_tail=%d",
            len(messages),
            chars_before,
            tokens_before,
            tok_per_char,
            wire_actual_tokens,
            budget_tokens,
            target_tokens,
            protect_budget_tokens,
            protect_ratio,
            preserve_tail,
        )

        # ── Level 1: tool output pruning ──
        pruned = await asyncio.to_thread(self._prune_tool_outputs, messages)
        pruned_tokens = self._chars_to_tokens(
            self._estimate_chars(pruned), tok_per_char
        )
        logger.info(
            "context_compressor: Level 1 (tool prune) — %d -> %d tokens",
            tokens_before,
            pruned_tokens,
        )
        if pruned_tokens <= target_tokens:
            logger.info("context_compressor: Level 1 sufficient")
            return pruned

        # ── Build the protection plan (turn-aware, token-space) ──
        plan = self._build_protection_plan(
            pruned,
            preserve_tail=preserve_tail,
            protect_budget_tokens=protect_budget_tokens,
            tok_per_char=tok_per_char,
        )
        logger.info(
            "context_compressor: protection_plan — system=%d, total_turns=%d, "
            "protected_turns=%d, protected_messages=%d, protected_tokens=%d, "
            "protect_budget=%d",
            len(plan.system_msgs),
            len(plan.all_turns),
            len(plan.protected_turns),
            sum(len(t) for t in plan.protected_turns),
            plan.protected_tokens,
            protect_budget_tokens,
        )

        # ── Level 2: bounded, oldest-overflow summarisation ──
        result = await self._level2_progressive(
            pruned,
            plan=plan,
            target_tokens=target_tokens,
            preserve_tail=preserve_tail,
            tok_per_char=tok_per_char,
            model_hint=model_hint,
        )
        result_tokens = self._chars_to_tokens(
            self._estimate_chars(result), tok_per_char
        )
        logger.info(
            "context_compressor: after Level 2 — %d tokens (target=%d)",
            result_tokens,
            target_tokens,
        )
        if result_tokens <= target_tokens:
            logger.info("context_compressor: Level 2 sufficient")
            return self._finalize(result, tokens_before, tok_per_char)

        # ── Level 3: turn-based hard trim (drops oldest unprotected turns) ──
        result = await asyncio.to_thread(
            self._level3_trim_turns,
            result,
            target_tokens=target_tokens,
            preserve_tail=preserve_tail,
            protect_budget_tokens=protect_budget_tokens,
            tok_per_char=tok_per_char,
            group_real_tokens=group_real_tokens,
            input_messages=messages,
        )
        logger.info(
            "context_compressor: Level 3 complete — %d messages, %d tokens",
            len(result),
            self._chars_to_tokens(self._estimate_chars(result), tok_per_char),
        )
        return self._finalize(result, tokens_before, tok_per_char)

    # ------------------------------------------------------------------
    # New: token-density (real-measurement first) + target resolution
    # ------------------------------------------------------------------

    def _token_density(
        self, chars_before: int, wire_actual_tokens: int | None
    ) -> float:
        """tokens-per-char for THIS conversation.

        Preferred: the provider's real measurement of the whole wire
        (``wire_actual_tokens`` = ``total_tokens - completion_tokens``, already
        de-distorted for prompt-cache turns by ``_extract_usage``) divided by
        the wire's char count — capturing the conversation's true token
        efficiency without any tokenizer call. Falls back to a fixed factor
        (``1 / _COMPRESS_CHARS_PER_TOKEN``) only when no real measurement is
        available (e.g. local models that never report usage).
        """
        if (
            wire_actual_tokens is not None
            and wire_actual_tokens > 0
            and chars_before > 0
        ):
            return wire_actual_tokens / chars_before
        return 1.0 / _COMPRESS_CHARS_PER_TOKEN

    @staticmethod
    def _chars_to_tokens(chars: int, tok_per_char: float) -> int:
        return int(chars * tok_per_char)

    def _resolve_target_tokens(
        self,
        *,
        tokens_before: int,
        target_ratio: float,
        budget_tokens: int | None,
        target_window_ratio: float | None = None,
    ) -> int:
        """Token target the pass aims for (window-anchored).

        Window-anchored when ``budget_tokens`` is known: target =
        ``budget × window_ratio`` (≈0.35 of the window by default). The
        ``window_ratio`` is the per-call override ``target_window_ratio`` when
        supplied (the user-configurable "post-compression keep size" slider),
        otherwise the module default :data:`_COMPRESS_TARGET_WINDOW_RATIO`
        (0.35) — so omitting it reproduces the prior behaviour byte-for-byte.
        Legacy ``tokens_before × target_ratio`` otherwise (no window known).
        The protected recent region is never compressed below, so it forms the
        natural floor — no separate floor knob is needed.
        """
        if budget_tokens is not None and budget_tokens > 0:
            window_ratio = (
                max(0.01, min(1.0, float(target_window_ratio)))
                if target_window_ratio is not None
                else _COMPRESS_TARGET_WINDOW_RATIO
            )
            return int(budget_tokens * window_ratio)
        ratio = max(0.01, min(1.0, float(target_ratio)))
        return int(tokens_before * ratio)

    def _protect_budget_tokens(
        self,
        *,
        budget_tokens: int | None,
        protect_ratio: float,
        tokens_before: int,
    ) -> int:
        """Tokens of recent history protected verbatim (window-anchored)."""
        pr = max(0.0, min(1.0, float(protect_ratio)))
        if budget_tokens is not None and budget_tokens > 0:
            return int(budget_tokens * pr)
        return int(tokens_before * pr)

    def _finalize(
        self,
        result: list[dict[str, Any]],
        tokens_before: int,
        tok_per_char: float,
    ) -> list[dict[str, Any]]:
        """Emit a finish-metrics log line and return the result unchanged."""
        tokens_after = self._chars_to_tokens(
            self._estimate_chars(result), tok_per_char
        )
        retain = (tokens_after / tokens_before) if tokens_before > 0 else 1.0
        logger.info(
            "context_compressor: finish — tokens_before=%d tokens_after=%d "
            "retain_ratio=%.3f messages_after=%d",
            tokens_before,
            tokens_after,
            retain,
            len(result),
        )
        return result

    # ------------------------------------------------------------------
    # Emergency-path: force_compress (PR-090 / S9 C-3)
    # ------------------------------------------------------------------

    async def force_compress(
        self,
        target_ratio: float,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        preserve_tail: int = 4,
    ) -> list[dict[str, Any]]:
        """Run the full compression pipeline unconditionally.

        Invoked by :class:`StreamChatUseCase` when the upstream LLM
        returns a ``prompt_too_long`` signal and
        :class:`qai.chat.application.ports.RetryPolicyPort.next_attempt`
        responds with ``compress_target_ratio``.  Unlike
        :meth:`compress`, this method does NOT bail out early when the
        message list is shorter than ``preserve_tail``: a
        prompt-too-long error means the conversation has already been
        rejected by the upstream, so there is nothing to gain from
        skipping compression.

        Parameters
        ----------
        target_ratio:
            Target compression ratio (0 < ratio <= 1.0).  ``0.5`` matches
            the legacy ``force_compress(target_ratio=0.50)`` call site.
        messages:
            The full message list that just failed to send.  May
            include an inlined ``role=system`` prompt.
        system_prompt:
            Optional system-prompt text whose character count should
            be subtracted from the budget calculation.  When the system
            prompt is already present in ``messages`` (the common case)
            pass ``None`` and the prompt is counted via the message
            list directly.
        preserve_tail:
            Number of trailing messages to keep intact.  Defaults to
            ``4`` to match the soft-path :meth:`compress` default.

        Returns
        -------
        list[dict[str, Any]]
            The compressed message list.  May still be over the
            upstream's hard budget if Level 2 summarisation failed and
            Level 3 hit the ``preserve_tail`` floor — the caller is
            expected to surface the original ``prompt_too_long`` error
            in that case rather than retrying again.

        Notes
        -----
        **Differential path (real-token attribution) is NOT used here.**
        ``force_compress`` runs ``_level3_hard_trim`` (line ~1365), not
        ``_level3_trim_turns`` (line ~876). Rationale: this is the
        emergency PROMPT_TOO_LONG retry path — the provider already
        rejected the prompt, so the goal is "must drop content, accuracy
        secondary". Char × density is sufficient and avoids any caller-
        side bookkeeping under error conditions. The soft / proactive path
        (``compress``) IS where ``group_real_tokens`` matters. See
        ``docs/90-refactor/CONTEXT-COMPRESSION.md``.
        """
        ratio = max(0.01, min(1.0, float(target_ratio)))

        total_chars = self._estimate_chars(messages)
        if system_prompt:
            total_chars += len(system_prompt)
        target_chars = int(total_chars * ratio)

        logger.info(
            "context_compressor.force_compress: starting — %d messages, "
            "%d chars (incl. system_prompt=%d), target_ratio=%.2f "
            "(target=%d chars), preserve_tail=%d",
            len(messages),
            total_chars,
            len(system_prompt) if system_prompt else 0,
            ratio,
            target_chars,
            preserve_tail,
        )

        # ── Level 1: tool output pruning (always runs) ──
        # CPU-bound pure list rebuild → offload off the event loop.
        result = await asyncio.to_thread(self._prune_tool_outputs, messages)
        result_chars = self._estimate_chars(result)
        logger.info(
            "context_compressor.force_compress: Level 1 — %d -> %d chars",
            total_chars,
            result_chars,
        )
        if result_chars <= target_chars:
            return result

        # ── Level 2: LLM summarisation (best-effort) ──
        result = await self._level2_summarize(
            result,
            target_chars=target_chars,
            preserve_tail=preserve_tail,
        )
        result_chars = self._estimate_chars(result)
        logger.info(
            "context_compressor.force_compress: after Level 2 — %d chars "
            "(target=%d)",
            result_chars,
            target_chars,
        )
        if result_chars <= target_chars:
            return result

        # ── Level 3: hard trim (always runs to floor) ──
        # CPU-bound pure list rebuild → offload off the event loop.
        result = await asyncio.to_thread(
            self._level3_hard_trim,
            result,
            target_chars=target_chars,
            preserve_tail=preserve_tail,
        )
        logger.info(
            "context_compressor.force_compress: Level 3 complete — "
            "%d messages, %d chars",
            len(result),
            self._estimate_chars(result),
        )
        return result

    # ------------------------------------------------------------------
    # Level 1: Tool output pruning
    # ------------------------------------------------------------------

    def _prune_tool_outputs(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Truncate excessively long tool outputs."""
        result: list[dict[str, Any]] = []
        threshold = self._tool_output_max_chars

        for msg in messages:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > threshold:
                    omitted = len(content) - threshold
                    truncated = (
                        content[:threshold]
                        + f"\n... [{omitted} chars omitted] ..."
                    )
                    result.append({**msg, "content": truncated})
                else:
                    result.append(msg)
            else:
                result.append(msg)
        return result

    # ------------------------------------------------------------------
    # New: protection plan (turn-aware, window-anchored)
    # ------------------------------------------------------------------

    def _build_protection_plan(
        self,
        messages: list[dict[str, Any]],
        *,
        preserve_tail: int,
        protect_budget_tokens: int,
        tok_per_char: float,
    ) -> _ProtectionPlan:
        """Decide which recent turns are protected verbatim.

        Always protects the CURRENT turn (last user message + everything after
        it). Then walks older turns from the tail, accumulating until the
        protected size would exceed ``protect_budget_tokens`` (token-space,
        using the conversation's real density). A ``preserve_tail``
        message-count floor is honoured: enough recent turns are protected to
        cover at least ``preserve_tail`` trailing messages.
        """
        system_msgs, body = _split_system_prefix(messages)
        turns = _parse_turns(body)
        if not turns:
            return _ProtectionPlan(
                system_msgs=system_msgs,
                all_turns=turns,
                protect_from=0,
                protected_tokens=0,
            )

        def _turn_tokens(turn: list[dict[str, Any]]) -> int:
            return self._chars_to_tokens(
                self._estimate_chars(turn), tok_per_char
            )

        # Always protect the LAST turn (the current turn: last user + after).
        protect_from = len(turns) - 1
        protected_tokens = _turn_tokens(turns[protect_from])
        protected_msgs = len(turns[protect_from])

        # Walk older turns from the tail while within the protect budget OR
        # while we still owe ``preserve_tail`` trailing messages.
        idx = protect_from - 1
        while idx >= 0:
            turn_tokens = _turn_tokens(turns[idx])
            need_more_for_tail = protected_msgs < preserve_tail
            within_budget = (
                protected_tokens + turn_tokens <= protect_budget_tokens
            )
            if not (within_budget or need_more_for_tail):
                break
            protected_tokens += turn_tokens
            protected_msgs += len(turns[idx])
            protect_from = idx
            idx -= 1

        return _ProtectionPlan(
            system_msgs=system_msgs,
            all_turns=turns,
            protect_from=protect_from,
            protected_tokens=protected_tokens,
        )

    # ------------------------------------------------------------------
    # New: Level 2 progressive (oldest-overflow bounded summarisation)
    # ------------------------------------------------------------------

    async def _level2_progressive(
        self,
        messages: list[dict[str, Any]],
        *,
        plan: _ProtectionPlan,
        target_tokens: int,
        preserve_tail: int,
        tok_per_char: float,
        model_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        """Summarise ONLY the oldest unprotected overflow, in bounded blocks.

        Never one-shots the whole history: each block is capped at
        ``_MAX_SUMMARY_BLOCK_*`` and at most ``_MAX_SUMMARY_CALLS_PER_COMPRESSION``
        summaries run. A rejected / too-short / low-gain summary does NOT
        replace its source block (failure-safe) — the block is left verbatim
        and we stop summarising (Level 3 then trims if still over). The
        loop-termination target is in TOKEN space (real density); block sizing
        stays char-based (the block caps are char limits by design).
        """
        if self._llm is None:
            logger.info("context_compressor: Level 2 skipped (no LLM)")
            return messages

        unprotected = list(plan.unprotected_turns)
        if not unprotected:
            return messages

        protected_block = plan.protected_turns
        summary_msgs: list[dict[str, Any]] = []
        summary_calls = 0
        oldest_idx = 0  # index into ``unprotected`` of the next turn to eat

        def _current_tokens() -> int:
            remaining = unprotected[oldest_idx:]
            flat_remaining = [m for t in remaining for m in t]
            flat_protected = [m for t in protected_block for m in t]
            chars = self._estimate_chars(
                plan.system_msgs
                + summary_msgs
                + flat_remaining
                + flat_protected
            )
            return self._chars_to_tokens(chars, tok_per_char)

        # Char-space target for sizing the overflow block to summarise.
        target_chars = (
            int(target_tokens / tok_per_char) if tok_per_char > 0 else 0
        )

        while _current_tokens() > target_tokens:
            if summary_calls >= _MAX_SUMMARY_CALLS_PER_COMPRESSION:
                break
            if oldest_idx >= len(unprotected):
                break

            remaining = unprotected[oldest_idx:]
            flat_now = (
                plan.system_msgs
                + summary_msgs
                + [m for t in remaining for m in t]
                + [m for t in protected_block for m in t]
            )
            over = self._estimate_chars(flat_now) - target_chars
            block_target = min(
                max(over, 0) + _SUMMARY_SAFETY_MARGIN_CHARS,
                _MAX_SUMMARY_BLOCK_CHARS,
            )
            # Select oldest turns until the block target / caps are hit.
            block_turns: list[list[dict[str, Any]]] = []
            block_chars = 0
            block_msg_count = 0
            j = oldest_idx
            while j < len(unprotected):
                t_chars = self._estimate_chars(unprotected[j])
                if block_turns and (
                    block_chars + t_chars > _MAX_SUMMARY_BLOCK_CHARS
                    or block_msg_count + len(unprotected[j])
                    > _MAX_SUMMARY_BLOCK_MESSAGES
                ):
                    break
                block_turns.append(unprotected[j])
                block_chars += t_chars
                block_msg_count += len(unprotected[j])
                j += 1
                if block_chars >= block_target:
                    break

            if not block_turns:
                break

            block_msgs = [m for t in block_turns for m in t]
            logger.info(
                "context_compressor: Level 2 block_selected — turns=%d "
                "messages=%d chars=%d",
                len(block_turns),
                block_msg_count,
                block_chars,
            )
            summary_text = await self._generate_summary(
                block_msgs, model_hint=model_hint
            )
            if not self._is_useful_summary(summary_text, block_chars):
                logger.warning(
                    "context_compressor: Level 2 summary rejected "
                    "(len=%s, block_chars=%d) — preserving source verbatim, "
                    "falling through to Level 3",
                    len(summary_text) if summary_text else 0,
                    block_chars,
                )
                break

            summary_msgs.append(
                {
                    "role": "system",
                    "content": f"{_SUMMARY_PREFIX}\n\n{summary_text}",
                }
            )
            summary_calls += 1
            oldest_idx = j  # consumed up to j
            logger.info(
                "context_compressor: Level 2 summary_accepted — "
                "block_chars=%d summary_chars=%d saved=%d",
                block_chars,
                len(summary_text or ""),
                block_chars - len(summary_text or ""),
            )

        # Rebuild: system prefix + accepted summaries + any UN-summarised
        # oldest turns (preserved verbatim) + protected recent turns.
        remaining_unprotected = [
            m for t in unprotected[oldest_idx:] for m in t
        ]
        protected_flat = [m for t in protected_block for m in t]
        return (
            plan.system_msgs
            + summary_msgs
            + remaining_unprotected
            + protected_flat
        )

    def _is_useful_summary(
        self, summary_text: str | None, block_chars: int
    ) -> bool:
        """A summary is usable only if long enough AND it actually saves space."""
        if not summary_text:
            return False
        if len(summary_text) < _MIN_USEFUL_SUMMARY_CHARS:
            return False
        saved = block_chars - len(summary_text)
        return saved >= _SUMMARY_REPLACEMENT_MIN_GAIN_CHARS

    # ------------------------------------------------------------------
    # New: Level 3 turn-based trim (drops oldest unprotected whole turns)
    # ------------------------------------------------------------------

    def _level3_trim_turns(
        self,
        messages: list[dict[str, Any]],
        *,
        target_tokens: int,
        preserve_tail: int,
        protect_budget_tokens: int,
        tok_per_char: float,
        group_real_tokens: list[int] | None = None,
        input_messages: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Drop the OLDEST unprotected content until within target.

        Trims at ATOMIC-GROUP granularity (an ``assistant{tool_calls}`` + its
        ``role=tool`` replies move as one unit) rather than whole turns — so a
        single huge agentic turn (one user question + dozens of tool
        round-trips) is trimmed round-trip-by-round-trip and the result can
        stop PRECISELY near the target instead of over-shooting when an entire
        100K-token turn is dropped at once (the "retain 5.8%" bug).

        When ``group_real_tokens`` is supplied (the "real-token differential"
        path, per ``docs/90-refactor/CONTEXT-COMPRESSION.md``), it is **accepted
        for contract stability (AGENTS.md §3.1) but no longer drives the trim
        decision** — 方案 A (2026-06-28, §11 排名 1-bis) unified Level 3 onto the
        SAME density gauge the rest of ``compress()`` uses. The previous branch
        mixed density ``fixed_tokens`` with cumulative-derived cloud-real
        per-group tokens (measured BEFORE Level 1 prune), so ``running_tokens``
        started massively overestimated and the drop loop over-shot down to the
        protected region (retain 6.6% bug). With density everywhere, the start,
        ``fixed``, ``target`` and per-group sizes are all self-consistent.

        Never drops system, never drops the protected recent region. The
        protected region is the natural floor: trimming stops once only
        protected content remains. Atomic-group removal keeps the tool-call
        wire self-consistent (a tool reply is never separated from its
        announcing assistant).
        """
        system_msgs, body = _split_system_prefix(messages)
        turns = _parse_turns(body)
        if not turns:
            return messages

        plan = self._build_protection_plan(
            messages,
            preserve_tail=preserve_tail,
            protect_budget_tokens=protect_budget_tokens,
            tok_per_char=tok_per_char,
        )
        protected_flat = [m for t in plan.protected_turns for m in t]
        unprotected_flat = [m for t in plan.unprotected_turns for m in t]

        # Fine-grained atomic groups over the UNPROTECTED region (oldest first).
        groups = _split_atomic_groups(unprotected_flat)

        # Real-token attribution: the caller's ``group_real_tokens`` list
        # corresponds to the ORIGINAL wire's atomic groups (before Level 1/2
        # transformations). We need to align it to THIS pass's ``groups``
        # (which span only the unprotected region). The simplest, robust
        # mapping: count how many atomic groups the ORIGINAL wire had vs how
        # many we see now; when they match (the common case — Level 1 only
        # mutates content, Level 2 fell through), prefix-skip by the count of
        # protected + system groups so the remaining slice lines up with our
        # unprotected ``groups``. On mismatch we disable the branch.
        group_tokens, source_dist = self._group_token_contributions(
            current_groups=groups,
            all_messages=messages,
            input_messages=input_messages if input_messages is not None else messages,
            group_real_tokens=group_real_tokens,
            tok_per_char=tok_per_char,
        )

        fixed_chars = self._estimate_chars(system_msgs + protected_flat)
        fixed_tokens = self._chars_to_tokens(fixed_chars, tok_per_char)
        running_tokens = fixed_tokens + sum(group_tokens)

        # Drop oldest atomic groups one at a time until within target (O(N)).
        drop = 0
        while running_tokens > target_tokens and drop < len(group_tokens):
            running_tokens -= group_tokens[drop]
            drop += 1

        if running_tokens > target_tokens:
            logger.info(
                "context_compressor: Level 3 — only protected region remains "
                "(%d tok > target %d); compressing less to keep recent context",
                running_tokens,
                target_tokens,
            )

        logger.info(
            "context_compressor: Level 3 source_distribution — "
            "cloud_or_tokenizer=%d, char_density=%d, total_groups=%d, "
            "group_real_tokens_supplied=%s",
            source_dist["real"],
            source_dist["fallback"],
            len(groups),
            group_real_tokens is not None,
        )

        kept_unprotected = [m for g in groups[drop:] for m in g]
        return system_msgs + kept_unprotected + protected_flat

    def _group_token_contributions(
        self,
        *,
        current_groups: list[list[dict[str, Any]]],
        all_messages: list[dict[str, Any]],
        input_messages: list[dict[str, Any]],
        group_real_tokens: list[int] | None,
        tok_per_char: float,
    ) -> tuple[list[int], dict[str, int]]:
        """Per-group token contribution for the trim pass + source distribution.

        **Unified density gauge (方案 A, 2026-06-28 — see
        ``docs/90-refactor/CONTEXT-COMPRESSION.md`` §11 排名 1-bis).** Every
        atomic group's contribution is ``chars × tok_per_char`` (the same
        density gauge the WHOLE ``compress()`` pipeline uses for
        ``tokens_before`` / ``pruned_tokens`` / ``result_tokens`` / ``target``).
        ``group_real_tokens`` is **deliberately NOT consumed** here.

        Why: the previous real-token branch mixed THREE gauges into one
        subtraction — ``fixed_tokens`` (density), per-group cloud-real tokens
        (cumulative-derived, measured BEFORE Level 1 prune so they still
        carried truncated tool-output bytes), and density fallbacks. The
        ``running_tokens`` start was massively overestimated (~224K), the drop
        loop overshot and trimmed down to only the protected region — the
        retain-6.6% over-shoot bug. The fix (方案 A) keeps Level 3 on the SAME
        gauge as the rest of ``compress()`` so the start, ``fixed``, ``target``
        and per-group sizes are all self-consistent. A pure-density experiment
        stopped exactly at target (retain 0.539).

        ``group_real_tokens`` is still accepted (port/``compress`` signature is
        a locked contract per AGENTS.md §3.1; other potential consumers may
        rely on it) but no longer drives the Level 3 trim decision.

        Returns ``(per_group_tokens, source_distribution_counts)``.
        """
        del all_messages, input_messages, group_real_tokens  # 方案 A: density-only
        tokens_fallback = [
            self._chars_to_tokens(self._estimate_chars(g), tok_per_char)
            for g in current_groups
        ]
        source_dist: dict[str, int] = {
            "real": 0,
            "fallback": len(current_groups),
        }
        return tokens_fallback, source_dist

    # ------------------------------------------------------------------
    # Level 2: LLM summarization
    # ------------------------------------------------------------------

    async def _level2_summarize(
        self,
        messages: list[dict[str, Any]],
        *,
        target_chars: int,
        preserve_tail: int,
    ) -> list[dict[str, Any]]:
        """Summarize the oldest messages via LLM, preserving the tail."""
        if self._llm is None:
            logger.info("context_compressor: Level 2 skipped (no LLM)")
            return messages

        # Split: system messages + body
        system_msgs: list[dict[str, Any]] = []
        body_msgs: list[dict[str, Any]] = []

        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                system_msgs.append(msg)
            else:
                body_msgs = messages[i:]
                break
        else:
            # All system messages — nothing to compress
            return messages

        # Preserve tail
        effective_tail = min(preserve_tail, len(body_msgs))
        if effective_tail >= len(body_msgs):
            # Nothing left to compress
            return messages

        compressible = body_msgs[: len(body_msgs) - effective_tail]
        tail = body_msgs[len(body_msgs) - effective_tail:]

        if not compressible:
            return messages

        # Input validation: if the messages handed to Level 2 carry almost no
        # substance, summarising them is pointless and usually signals that an
        # upstream caller stripped the content (e.g. tool outputs) before it
        # reached the compressor.  Emit a diagnostic warning but keep going —
        # the extractive fallback below still records what little we have
        # (turn counts, file paths) rather than crashing.
        compressible_chars = self._estimate_chars(compressible)
        if compressible_chars < _MIN_COMPRESSIBLE_INPUT_CHARS:
            logger.warning(
                "context_compressor: Level 2 input is unusually small "
                "(%d messages, ~%d chars) — upstream may have stripped "
                "message content before compression",
                len(compressible),
                compressible_chars,
            )

        logger.info(
            "context_compressor: Level 2 — summarizing %d messages "
            "(keeping %d tail messages)",
            len(compressible),
            len(tail),
        )

        summary_text = await self._generate_summary(compressible)
        # Treat empty / degenerate (too-short) LLM output as a failure and fall
        # back to an extractive summary instead of giving up on compression
        # entirely (the V1 ``_fallback_extractive_summary`` behaviour — "have a
        # fallback, don't drop the compression").  V2 implements it as a pure
        # method rather than a module function so the adapter stays self
        # contained and testable.
        if summary_text is None or len(summary_text) < _MIN_USEFUL_SUMMARY_CHARS:
            if summary_text is None:
                logger.warning(
                    "context_compressor: Level 2 LLM summarization "
                    "returned nothing — using extractive fallback"
                )
            else:
                logger.warning(
                    "context_compressor: Level 2 LLM summary too short "
                    "(%d chars < %d) — using extractive fallback",
                    len(summary_text),
                    _MIN_USEFUL_SUMMARY_CHARS,
                )
            # CPU-bound pure text build → offload off the event loop.
            summary_text = await asyncio.to_thread(
                self._fallback_extractive_summary, compressible
            )

        if not summary_text:
            # Even the extractive fallback produced nothing usable (extremely
            # degenerate input).  Fall through to Level 3 hard-trim untouched.
            logger.warning(
                "context_compressor: Level 2 produced no usable summary "
                "(LLM and extractive fallback both empty), "
                "falling through to Level 3"
            )
            return messages

        # Build the summary message
        summary_msg: dict[str, Any] = {
            "role": "system",
            "content": f"{_SUMMARY_PREFIX}\n\n{summary_text}",
        }
        return system_msgs + [summary_msg] + tail

    async def _generate_summary(
        self,
        messages: list[dict[str, Any]],
        *,
        model_hint: str | None = None,
    ) -> str | None:
        """Use the LLM to produce a structured summary of messages.

        ``model_hint`` (optional): the conversation's CURRENT model id, threaded
        down from :meth:`compress`. Passed as ``LLMStreamRequest.model_hint`` so
        the summarisation request resolves to the SAME live provider/endpoint
        the conversation uses. When ``None`` (legacy callers / tests) the
        request falls back to the static default model resolution — which on
        some deployments resolves to NO configured endpoint and returns the
        offline placeholder, defeating Level 2 entirely (see the WARN below).

        Returns the summary text on success, ``None`` on any failure.
        """
        assert self._llm is not None

        # Build conversation text for summarization
        conversation_parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                content = "\n".join(text_parts)
            if not isinstance(content, str):
                content = str(content) if content else ""

            # Truncate per-message for the summarization prompt
            if role == "tool":
                content = content[:500]
            else:
                content = content[:1000]
            conversation_parts.append(f"[{role}]: {content}")

        conversation_text = "\n\n".join(conversation_parts)

        # Build a minimal LLM request for summarization.
        # We construct a prompt that asks for summarization and stream the
        # response, collecting chunk text.
        try:
            from qai.chat.domain.message import Message
            from qai.chat.domain.content import MessageContent as MC

            request = LLMStreamRequest(
                conversation_id=ConversationId("__compressor__"),
                tab_id=TabId("__compressor__"),
                prompt=MC(text=conversation_text),
                history=(),
                model_hint=model_hint,
                extra={
                    "system_prompt": _SUMMARIZE_SYSTEM_PROMPT,
                    "max_tokens": 3000,
                    "temperature": 0.3,
                },
            )

            collected: list[str] = []

            async def _collect() -> bool:
                """Drain the summary stream into ``collected``.

                Returns ``False`` on an ERROR frame (caller maps to ``None``),
                ``True`` on a clean END / exhausted iterator.
                """
                async for frame in self._llm.stream(request):
                    if frame.frame_type is StreamFrameType.CHUNK:
                        text = frame.payload.get("text", "")
                        if isinstance(text, str):
                            collected.append(text)
                    elif frame.frame_type is StreamFrameType.ERROR:
                        logger.warning(
                            "context_compressor: LLM summarization stream "
                            "error: %s",
                            frame.payload.get("message", "unknown"),
                        )
                        return False
                    elif frame.frame_type is StreamFrameType.END:
                        break
                return True

            # Level 2's LLM summary call is the only network-bound step in
            # compression and the ONLY one that can hang (a slow/stuck provider,
            # a model that never streams an END). Bound it hard: on timeout we
            # abandon the summary and return ``None`` so the caller falls
            # through to the extractive / Level 3 hard-trim fallback (which is
            # synchronous + cannot hang), guaranteeing compression always
            # completes promptly instead of blocking the turn indefinitely.
            try:
                ok = await asyncio.wait_for(
                    _collect(), timeout=_SUMMARY_LLM_TIMEOUT_SECONDS
                )
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning(
                    "context_compressor: Level 2 LLM summary timed out after "
                    "%.0fs (model_hint=%s) — abandoning summary, falling "
                    "through to extractive / Level 3 fallback.",
                    _SUMMARY_LLM_TIMEOUT_SECONDS,
                    model_hint,
                )
                return None
            if not ok:
                return None

            summary = "".join(collected).strip()
            if summary:
                # The LLM stream adapter returns a fixed offline placeholder
                # (``_OFFLINE_NOTICE`` in ``llm_stream.py``) when the resolved
                # model has NO configured endpoint — the summarisation request
                # never reached a live provider. Detecting this here turns the
                # otherwise-misleading "summary generated (28 chars)" INFO into
                # an explicit WARN that names the root cause, so an unresolved /
                # offline summary endpoint is diagnosable instead of silently
                # collapsing Level 2 into the Level 3 hard-trim fallback. (We
                # match the sentinel by VALUE rather than importing the
                # ``infrastructure`` constant — keeping the adapter free of a
                # cross-module dependency on the concrete stream adapter.)
                if summary == _OFFLINE_SUMMARY_NOTICE:
                    logger.warning(
                        "context_compressor: Level 2 summary endpoint "
                        "unresolved/offline — model_hint=%s resolved to no LLM "
                        "endpoint (got the offline placeholder, %d chars); the "
                        "summarisation request never reached a provider. "
                        "Falling through to the extractive / Level 3 fallback.",
                        model_hint,
                        len(summary),
                    )
                    return None
                logger.info(
                    "context_compressor: LLM summary generated (%d chars, "
                    "model_hint=%s)",
                    len(summary),
                    model_hint,
                )
                return summary
            return None

        except Exception as exc:
            logger.warning(
                "context_compressor: LLM summarization failed: %s",
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Level 2 fallback: extractive summary (no LLM)
    # ------------------------------------------------------------------

    def _fallback_extractive_summary(
        self,
        messages: list[dict[str, Any]],
    ) -> str:
        """Build a deterministic extractive summary without calling the LLM.

        Used when the Level 2 LLM summarisation returns nothing or a
        degenerate (too-short) result.  Mirrors the V1
        ``_fallback_extractive_summary`` *behaviour* — "always have a
        fallback so a failed summary never silently drops compression" —
        but is implemented as a self-contained adapter method rather than
        a copied module function.

        The extracted summary preserves:

        * the **first** user request (initial task) and, when distinct,
          the **most recent** user request;
        * a **tool-call count** so the model knows work happened even
          though the detail was dropped;
        * the set of **file paths** mentioned anywhere in the compressed
          window (capped) — the highest-signal "key nouns" for a coding
          agent;
        * a compact **turn-count breakdown** by role.

        Returns an empty string only when ``messages`` is empty (the
        caller then falls through to Level 3).
        """
        if not messages:
            return ""

        user_msgs = [m for m in messages if m.get("role") == "user"]
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]

        parts: list[str] = []

        if user_msgs:
            first = self._message_text(user_msgs[0]).strip()
            if first:
                parts.append(f"## Initial Request\n{first[:500]}")

        if len(user_msgs) > 1:
            last = self._message_text(user_msgs[-1]).strip()
            if last:
                parts.append(f"## Most Recent Request\n{last[:500]}")

        if tool_msgs:
            parts.append(
                f"## Tool Activity\n{len(tool_msgs)} tool call(s) executed"
            )

        all_text = " ".join(self._message_text(m) for m in messages)
        file_paths = sorted(set(_FILE_PATH_RE.findall(all_text)))
        if file_paths:
            listed = "\n".join(f"- {p}" for p in file_paths[:30])
            parts.append(f"## Files Involved\n{listed}")

        parts.append(
            "## Conversation Stats\n"
            f"- user messages: {len(user_msgs)}\n"
            f"- assistant messages: {len(assistant_msgs)}\n"
            f"- tool messages: {len(tool_msgs)}\n"
            f"- total messages: {len(messages)}"
        )

        return "\n\n".join(parts)

    @staticmethod
    def _message_text(msg: dict[str, Any]) -> str:
        """Extract plain text from a message's ``content`` (str or blocks)."""
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        return str(content) if content else ""

    # ------------------------------------------------------------------
    # Level 3: Hard trim
    # ------------------------------------------------------------------

    def _level3_hard_trim(
        self,
        messages: list[dict[str, Any]],
        *,
        target_chars: int,
        preserve_tail: int,
    ) -> list[dict[str, Any]]:
        """Drop oldest non-system messages until within budget."""
        result = list(messages)
        max_drops = len(result)  # prevent infinite loop

        for _ in range(max_drops):
            if self._estimate_chars(result) <= target_chars:
                break
            if len(result) <= preserve_tail:
                break

            # Find the first non-system message (from the front) and remove it
            removed = False
            for i, msg in enumerate(result):
                if msg.get("role") != "system":
                    # Don't remove if it's in the tail region
                    if i < len(result) - preserve_tail:
                        result.pop(i)
                        removed = True
                        break
            if not removed:
                break

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_chars(messages: list[dict[str, Any]]) -> int:
        """Rough character-count estimator for a message list."""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            total += len(part.get("text", ""))
                        else:
                            # image or other block — estimate ~100 chars overhead
                            total += 100
            # Add role/structural overhead
            total += 20
        return total


__all__ = [
    "ThreeLevelContextCompressor",
    "DEFAULT_TOOL_OUTPUT_MAX_CHARS",
]

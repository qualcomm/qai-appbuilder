# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Protection-aware layered context compression adapter.

Simplified from the earlier three-level (tool-prune + LLM-summary + hard-trim)
pipeline into a 4-phase, drop-only strategy that keeps the window/protection
plan and the checkpoint-friendly turn model while removing all LLM cost and
best-effort tool-output pruning (both handled elsewhere in the stack).

Phases (each runs only if the previous did not bring the wire under target):

* **Phase 1** — strip tool interactions in unprotected turns (drops
  ``assistant{tool_calls}`` + their ``role=tool`` replies + intermediate
  reasoning ``assistant`` messages; keeps ``user`` + the last no-tool
  ``assistant`` summary of each turn).
* **Phase 2** — drop oldest unprotected turns until under target.
* **Phase 3** — strip tool interactions in protected turns (crosses the
  protection line only when the unprotected region alone cannot fit).
* **Phase 4** — drop oldest protected turns (last resort).

The class name ``ThreeLevelContextCompressor`` is kept for DI / import
stability. The compressor is stateless and safe to share across turns/tabs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from qai.chat.application._token_estimate_helpers import (
    NON_TEXT_PART_BYTE_EQUIVALENT as _COMPRESS_NON_TEXT_PART_BYTES,
)
from qai.chat.application.ports import ContextCompressionPort
from qai.chat.domain.reference_ledger import strip_read_selector

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMPRESS_TARGET_WINDOW_RATIO: float = 0.35
"""Post-compression target as a fraction of the model window (default 0.35).

Kept in sync with the caller-side default in
``_agentic_kernel.COMPRESS_TARGET_WINDOW_RATIO``."""

_COMPRESS_BYTES_PER_TOKEN: int = 4
"""Token->UTF-8-byte factor (mirrors ``estimate_wire_tokens``'s ``bytes//4``)."""

# The ``_COMPRESS_NON_TEXT_PART_BYTES`` constant (4800 bytes ≈ 1200 tokens
# per image, the greatest-common-divisor across OpenAI/Anthropic/Gemini) is
# imported from the shared helper module so every byte-based estimator on the
# compaction path (compressor's :meth:`_estimate_bytes`, kernel
# :func:`estimate_wire_tokens`, ``coarse_byte_estimate``) applies the SAME
# image-cost rule. See :data:`NON_TEXT_PART_BYTE_EQUIVALENT`.

# ---------------------------------------------------------------------------
# Phase 0 — redundant tool-group pruning (drop-only, zero-information-loss)
# ---------------------------------------------------------------------------

_PHASE0_PRUNED_TARGETS_LOG_LIMIT: int = 20
"""Max distinct pruned-target rows one Phase 0 log line may name.

A diagnostic line must stay readable and bounded; the tally is ranked by
drop count first, so the truncated remainder is by construction the least
interesting. Overflow is reported as a ``(+K more)`` suffix, never dropped
silently.
"""

_SUPERSEDABLE_READ_TOOLS: frozenset[str] = frozenset(
    {"read", "list", "glob", "grep"}
)
"""Tools whose result is a SNAPSHOT of state that the newest call re-reads.

Calling one of these twice with the SAME target (see
:func:`_supersede_key`) makes the older result stale by construction — the
filesystem's current content is what the model needs, and the newest call
already carries it. Deliberately EXCLUDES:

* ``web_fetch`` / ``web_search`` — a network result is not "current state";
  fetching one URL twice may be a deliberate before/after comparison.
* every write / execute tool (``write`` / ``edit`` / ``apply_patch`` /
  ``exec`` / ``background_process`` / ``agent`` / ...) — those results are
  an ACTION LOG, not a re-readable snapshot. Dropping one would erase the
  record that the action happened.
"""

_SUPERSEDE_TARGET_ARGS: dict[str, tuple[str, ...]] = {
    "read": ("path",),
    "list": ("path",),
    "glob": ("pattern", "cwd"),
    "grep": ("pattern", "path", "include"),
}
"""Per-tool argument names that identify WHAT a read targeted.

Mirrors the concrete handler signatures (``tool_read`` / ``tool_list`` /
``tool_glob`` / ``tool_grep``). A tool whose entry is missing, or a call
missing every listed argument, never participates in superseding — the
conservative direction (keep the result).
"""

_SUPERSEDE_PAGINATION_ARGS: tuple[str, ...] = ("offset", "limit")
"""Arguments that make two same-target reads carry DIFFERENT content.

These are part of the supersede key, NOT excluded from it: lines 1-100 and
lines 500-600 of one file are disjoint slices, so neither supersedes the
other. Getting this wrong silently deletes content the model still needs,
so both mechanisms treat a differing page as a different target.
"""

_USELESS_RESULT_MAX_BYTES: int = 200
"""Size ceiling (UTF-8 bytes) under which a non-error result counts as noise.

A short, successful, non-read tool result is a bare completion
acknowledgement (``Successfully wrote 42 bytes to x.py``) whose only
information — that the call succeeded — is already implied by the absence of
an error. A LENGTH gate is used rather than matching known phrasings because
each handler writes its own free-text ``message``; enumerating them would
silently miss every new or reworded tool.
"""

_RESULT_ERROR_SENTINELS: tuple[str, ...] = (
    "[tool_error]",
    "[guardrail_blocked]",
    "[cancelled]",
    "[interrupted]",
    "[sub-agent error:",
    "[sub-agent round ",
    "[appbuilder_run error]",
    "[question cancelled]",
)
"""Literal failure markers a tool result may carry — verified against source.

Produced by ``_agentic_kernel.build_tool_reply_blocks`` (``[tool_error]``),
``build_cancelled_tool_message`` (``[cancelled]``),
``_tool_round_executor`` (``[interrupted]``), ``agent_tool`` (``[sub-agent
error:`` / ``[sub-agent round ``) and the App-Builder / question bridges.
A result carrying ANY of these is NEVER pruned: the model must keep seeing
what failed, or it repeats the failing call.
"""

_RESULT_FAILURE_FIELD_MARKERS: tuple[str, ...] = (
    "'ok': False",
    '"ok": False',
    "error_code",
    "'timed_out': True",
    '"timed_out": True',
    "'truncated': True",
    '"truncated": True',
)
"""Stringified-envelope markers of failure / self-managed truncation.

A tool result reaches the wire as ``str(result_dict)`` (see
``streaming.py``'s ``result_text``), so an ``ok=False`` / ``error_code`` /
``timed_out`` envelope survives as these literal substrings. ``truncated``
is included because a self-truncating tool appends a recovery footer the
model needs in order to fetch the rest.
"""


def _normalize_target(raw: Any) -> str:
    """Canonicalise one read-target argument into a comparison key.

    Pure string work — deliberately NO ``Path.resolve()`` / ``os.stat``:
    compaction runs on the hot send path (10-80 ms budget) and the process
    CWD may no longer be the one the call was made from, so touching the
    filesystem would be both slow and wrong.

    Consequence (accepted, conservative): two spellings that name the same
    file through different prefixes (``src/a.py`` vs ``C:/proj/src/a.py``)
    hash to DIFFERENT keys, so neither supersedes the other. Missing a prune
    wastes a few tokens; a wrong prune deletes content the model needs.

    Non-string arguments (numbers, bools, ``None``) are rendered via
    ``repr`` so they still take part in the key without ever colliding with
    a path string.
    """
    if not isinstance(raw, str):
        return "" if raw is None else repr(raw)
    value = strip_read_selector(raw).replace("\\", "/")
    while "//" in value:
        value = value.replace("//", "/")
    if len(value) > 1:
        value = value.rstrip("/")
    return value.casefold()


def _supersede_key(tool_name: str, arguments: dict[str, Any]) -> tuple | None:
    """Identity of "what this read targeted", or ``None`` when unprunable.

    The key is ``(tool_name, normalized targets..., pagination...)``. Two
    calls share a key ONLY when they read the same target through the same
    page, so a paginated re-read of a different slice never supersedes an
    earlier slice.

    ``None`` (never prune) is returned when the tool is not a superseding
    read, or when the call names none of the tool's target arguments — a
    shape we do not understand is a shape we leave alone.
    """
    if tool_name not in _SUPERSEDABLE_READ_TOOLS:
        return None
    target_args = _SUPERSEDE_TARGET_ARGS.get(tool_name)
    if not target_args:
        return None
    targets = tuple(
        _normalize_target(arguments.get(name)) for name in target_args
    )
    if not any(targets):
        return None
    pages = tuple(
        _normalize_target(arguments.get(name))
        for name in _SUPERSEDE_PAGINATION_ARGS
    )
    return (tool_name, targets, pages)


def _parse_tool_call(call: Any) -> tuple[str, dict[str, Any]] | None:
    """Extract ``(tool_name, arguments)`` from one wire ``tool_calls`` entry.

    The wire shape is ``{"id", "type", "function": {"name", "arguments"}}``
    where ``arguments`` is a JSON STRING (``build_assistant_tool_calls_block``
    serialises it with ``ensure_ascii=False``). Returns ``None`` for any
    entry we cannot read confidently — malformed JSON, a non-dict payload, a
    missing name — so the caller keeps the group rather than guessing.
    """
    if not isinstance(call, dict):
        return None
    function = call.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_args = function.get("arguments")
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except (TypeError, ValueError):
            # Unreadable arguments: we cannot know what this call targeted,
            # so the caller must keep the group rather than guess.
            return None
    # A non-dict payload (list / scalar / absent) names no target, which
    # ``_supersede_key`` treats as "not a superseding read".
    return name, raw_args if isinstance(raw_args, dict) else {}


def _index_replies_by_call_id(
    tail: list[dict[str, Any]], *, expected: int
) -> dict[str, dict[str, Any]] | None:
    """Index an atomic group's ``role:tool`` replies by ``tool_call_id``.

    ``None`` means the group does not pair 1:1 with its ``expected`` number
    of calls — a reply count mismatch, a missing/blank ``tool_call_id``, or a
    duplicate id. Any of those is a shape we do not model, and the caller
    keeps the group untouched rather than risk breaking the pairing.
    """
    replies = [m for m in tail if m.get("role") == "tool"]
    if len(replies) != expected:
        return None
    by_id: dict[str, dict[str, Any]] = {}
    for reply in replies:
        tcid = reply.get("tool_call_id")
        if not isinstance(tcid, str) or not tcid or tcid in by_id:
            return None
        by_id[tcid] = reply
    return by_id


def _result_is_error(content: Any) -> bool:
    """True when a ``role:tool`` result reports a failure of any kind.

    RED LINE: an erroring result is never pruned — the model has to see what
    went wrong or it reissues the same failing call. Unreadable content
    (non-string) counts as an error so the conservative branch wins.
    """
    if not isinstance(content, str):
        return True
    stripped = content.lstrip()
    if stripped.startswith(_RESULT_ERROR_SENTINELS):
        return True
    return any(
        marker in content for marker in _RESULT_FAILURE_FIELD_MARKERS
    )


def _result_is_useless(tool_name: str, content: Any) -> bool:
    """True when a result carries no information the model can reason from.

    Two shapes qualify: an empty/whitespace body, and a short SUCCESSFUL
    body from a non-read tool (a bare completion acknowledgement).

    A read-class tool is never judged useless: "this file is empty" / "no
    matches found" is a real, load-bearing answer to what the model asked.
    """
    if _result_is_error(content):
        return False
    if not isinstance(content, str):
        return False
    # The read-class exemption is checked FIRST, ahead of the empty-body
    # rule: an empty read result is not an absence of information, it is the
    # positive fact "this file/directory/search came back empty" — exactly
    # what the model asked. Ordering these the other way round silently ate
    # that answer and made the model re-read the same empty file.
    if tool_name in _SUPERSEDABLE_READ_TOOLS:
        return False
    stripped = content.strip()
    if not stripped:
        return True
    if stripped == "(no output)":
        return True
    return len(stripped.encode("utf-8")) <= _USELESS_RESULT_MAX_BYTES


# ---------------------------------------------------------------------------
# Turn parsing + protection plan (window-anchored, tool-call-aware)
# ---------------------------------------------------------------------------


def _split_system_prefix(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split leading ``role=system`` messages from the conversational body.

    Only the CONTIGUOUS leading run of system messages is treated as the
    prefix. Returns ``(system_prefix, body)``.
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
    user (orphan prefix) form their own leading group so they are never
    silently dropped or split.

    Keeping whole turns together guarantees ``assistant{tool_calls}`` and its
    matching ``role=tool`` replies stay in the same unit.
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

    An atomic group is the smallest set of messages that must move together
    to keep the wire self-consistent:

    * an ``assistant`` message that announces ``tool_calls`` + ALL the
      immediately-following ``role=tool`` replies -> one group;
    * any other single message -> its own group.
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
    # protected verbatim (the recent working set + current turn). Turns
    # before it are eligible for compression.
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
    """Protection-aware layered compaction adapter.

    Historical name kept for DI/import stability; internally implements a
    4-phase drop-based strategy (no LLM summarisation, no tool-output prune):

    * Phase 1 - strip tool interactions in unprotected turns
    * Phase 2 - drop oldest unprotected turns
    * Phase 3 - strip tool interactions in protected turns (only if still
      over target)
    * Phase 4 - drop oldest protected turns (last resort)
    """

    __slots__ = ()

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Public API (ContextCompressionPort)
    # ------------------------------------------------------------------
    async def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        # ``target_ratio`` is a LEGACY FALLBACK used only when the caller does
        # not supply ``budget_tokens`` (i.e. the model context window is
        # unknown). In production the ``CompactionCheckpointEngine`` always
        # passes ``budget_tokens=context_limit`` + ``target_window_ratio``, so
        # ``target_ratio`` is dormant on the live path. Kept for port
        # stability + non-engine callers (unit tests, ad-hoc compaction).
        target_ratio: float = _COMPRESS_TARGET_WINDOW_RATIO,
        preserve_tail: int = 4,
        budget_tokens: int | None = None,
        protect_ratio: float = _COMPRESS_TARGET_WINDOW_RATIO,
        wire_actual_tokens: int | None = None,
        target_window_ratio: float | None = None,
        outcome_sink: dict[str, Any] | None = None,
        # Tail-appended (AGENTS.md §3.1). Default ``False`` keeps EVERY direct
        # caller of this port byte-for-byte on the prior 4-phase behaviour —
        # existing unit tests, ad-hoc compaction, and any replacement engine.
        #
        # NOTE: ``CompactionCheckpointEngine`` defaults its OWN flag to
        # ``True``. The two defaults point in OPPOSITE directions BY DESIGN:
        # the compressor defaults conservative (a bare port call must not
        # change shape), while the engine defaults enabled (Phase 0 is the
        # correct main-agent behaviour, and a future third caller should
        # inherit the optimisation rather than silently regress). Do NOT
        # "harmonise" these two defaults.
        prune_redundant: bool = False,
    ) -> list[dict[str, Any]]:
        # Defensive clamp: the runtime ``CompactionCheckpointEngine`` already
        # enforces ``protect <= target`` before calling us (see
        # ``_compaction_engine.resolve_ratios`` :249-263), but ``compress`` is
        # a public :class:`ContextCompressionPort` — a direct caller (test /
        # replacement engine / future integration) could otherwise pass
        # inconsistent ratios and get "always Phase 3/4" behaviour. Clamp
        # here so the guarantee lives with the algorithm, not just the wiring.
        if (
            target_window_ratio is not None
            and 0.0 < target_window_ratio < protect_ratio
        ):
            logger.warning(
                "context_compressor: caller passed protect_ratio=%.3f > "
                "target_window_ratio=%.3f — clamping protect_ratio down so "
                "the protected region does not exceed the compression target",
                protect_ratio,
                target_window_ratio,
            )
            protect_ratio = target_window_ratio
        if len(messages) <= preserve_tail:
            if outcome_sink is not None:
                outcome_sink["phase_reached"] = 0
            return messages

        # P1-7: per-``compress()`` byte-count cache. All phase-loop measures
        # (``_total_tokens`` / ``_tokens_of`` / turn-token sums inside
        # ``_build_protection_plan`` and ``_drop_oldest_turns``) reuse it so
        # every message's UTF-8 encode happens EXACTLY once, regardless of
        # how many phases walk it. Keyed by ``id(msg)`` — safe because the
        # message dicts are treated as immutable by the compression pass.
        bytes_cache: dict[int, int] = {}
        bytes_before = self._estimate_bytes_cached(messages, bytes_cache)
        tok_per_byte = self._token_density(bytes_before, wire_actual_tokens)
        tokens_before = self._bytes_to_tokens(bytes_before, tok_per_byte)
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
            "context_compressor: starting compression - %d messages, "
            "%d bytes, %d tokens (density=%.3f tok/byte, real=%s), "
            "budget_tokens=%s, target=%d tok, "
            "protect_budget=%d tok, protect_ratio=%.2f, preserve_tail=%d",
            len(messages),
            bytes_before,
            tokens_before,
            tok_per_byte,
            wire_actual_tokens,
            budget_tokens,
            target_tokens,
            protect_budget_tokens,
            protect_ratio,
            preserve_tail,
        )

        plan = self._build_protection_plan(
            messages,
            preserve_tail=preserve_tail,
            protect_budget_tokens=protect_budget_tokens,
            tok_per_byte=tok_per_byte,
            bytes_cache=bytes_cache,
        )
        system_msgs = plan.system_msgs
        unprotected = list(plan.unprotected_turns)
        protected = list(plan.protected_turns)

        logger.info(
            "context_compressor: protection_plan - system=%d, total_turns=%d, "
            "protected_turns=%d, protected_messages=%d, protected_tokens=%d, "
            "protect_budget=%d",
            len(system_msgs),
            len(plan.all_turns),
            len(protected),
            sum(len(t) for t in protected),
            plan.protected_tokens,
            protect_budget_tokens,
        )

        def _tokens_of(turns: list[list[dict[str, Any]]]) -> int:
            flat = [m for t in turns for m in t]
            return self._bytes_to_tokens(
                self._estimate_bytes_cached(flat, bytes_cache), tok_per_byte
            )

        def _assemble() -> list[dict[str, Any]]:
            return (
                system_msgs
                + [m for t in unprotected for m in t]
                + [m for t in protected for m in t]
            )

        def _total_tokens() -> int:
            return self._bytes_to_tokens(
                self._estimate_bytes_cached(_assemble(), bytes_cache),
                tok_per_byte,
            )

        sys_tokens = self._bytes_to_tokens(
            self._estimate_bytes_cached(system_msgs, bytes_cache), tok_per_byte
        )
        # --- Phase 0: prune redundant tool groups (zero-information-loss) ---
        # Runs BEFORE Phase 1 on purpose. Phases 1-4 are LOSSY (they strip
        # whole tool rounds / drop whole turns), while Phase 0 only removes
        # content that is either already superseded by a newer read of the
        # same target or carries no information at all. Doing the free work
        # first can bring the wire under target so no lossy phase ever runs;
        # running it later would be pointless, since Phase 1 has by then
        # already stripped every tool interaction Phase 0 could prune.
        #
        # It shares the pre-phase "already under target" return below instead
        # of owning a second early exit: both mean "no LOSSY phase was
        # needed", which is exactly what ``phase_reached = 0`` records. Not
        # minting a 5th enum value keeps the journal's JSONL schema and
        # ``tools/diag/compaction_diag.py`` untouched; Phase 0's own
        # observability rides on the separate ``pruned_groups`` sink field.
        #
        # Pruning an already-under-target wire would be wasted work, so the
        # cheap size check gates it.
        if prune_redundant and _total_tokens() > target_tokens:
            unprotected = self._prune_redundant_tool_groups(
                unprotected,
                tok_per_byte=tok_per_byte,
                bytes_cache=bytes_cache,
                outcome_sink=outcome_sink,
            )
            _after_phase0 = _total_tokens()
            # ``lossy_phases_skipped`` is Phase 0's whole reason to exist: a
            # True here means the free, information-preserving prune alone
            # brought the wire under target, so Phases 1-4 (which strip tool
            # rounds / drop whole turns) never ran this compaction.
            logger.info(
                "context_compressor: after Phase 0 - %d tokens "
                "(target=%d, under_target=%s, lossy_phases_skipped=%s)",
                _after_phase0,
                target_tokens,
                _after_phase0 <= target_tokens,
                _after_phase0 <= target_tokens,
            )

        if _total_tokens() <= target_tokens:
            if outcome_sink is not None:
                outcome_sink["phase_reached"] = 0
            return self._finalize(
                _assemble(), tokens_before, tok_per_byte,
                bytes_cache=bytes_cache,
            )

        # --- Phase 1: strip tool interactions in unprotected turns ---
        unprotected = self._strip_tool_interactions(unprotected)
        after1 = _total_tokens()
        logger.info(
            "context_compressor: after Phase 1 - %d tokens (target=%d)",
            after1,
            target_tokens,
        )
        if after1 <= target_tokens:
            if outcome_sink is not None:
                outcome_sink["phase_reached"] = 1
            return self._finalize(
                _assemble(), tokens_before, tok_per_byte,
                bytes_cache=bytes_cache,
            )

        # --- Phase 2: drop oldest unprotected turns ---
        protected_tokens = _tokens_of(protected)
        unprotected = self._drop_oldest_turns(
            unprotected,
            target_tokens=target_tokens,
            fixed_tokens=sys_tokens + protected_tokens,
            tok_per_byte=tok_per_byte,
            bytes_cache=bytes_cache,
        )
        after2 = _total_tokens()
        logger.info(
            "context_compressor: after Phase 2 - %d tokens (target=%d)",
            after2,
            target_tokens,
        )
        if after2 <= target_tokens:
            if outcome_sink is not None:
                outcome_sink["phase_reached"] = 2
            return self._finalize(
                _assemble(), tokens_before, tok_per_byte,
                bytes_cache=bytes_cache,
            )

        # --- Phase 3: strip tool interactions in protected turns ---
        logger.warning(
            "context_compressor: entering protection region - stripping "
            "tool interactions in %d protected turns (unprotected region "
            "insufficient; %d tokens over target %d)",
            len(protected),
            after2,
            target_tokens,
        )
        protected = self._strip_tool_interactions(protected)
        after3 = _total_tokens()
        logger.info(
            "context_compressor: after Phase 3 - %d tokens (target=%d)",
            after3,
            target_tokens,
        )
        if after3 <= target_tokens:
            if outcome_sink is not None:
                outcome_sink["phase_reached"] = 3
            return self._finalize(
                _assemble(), tokens_before, tok_per_byte,
                bytes_cache=bytes_cache,
            )

        # --- Phase 4: drop oldest protected turns (last resort) ---
        logger.warning(
            "context_compressor: protection region shrinking - dropping "
            "oldest protected turns (%d tokens still over target %d)",
            after3,
            target_tokens,
        )
        protected = self._drop_oldest_turns(
            protected,
            target_tokens=target_tokens,
            fixed_tokens=sys_tokens,
            tok_per_byte=tok_per_byte,
            bytes_cache=bytes_cache,
        )
        after4 = _total_tokens()
        if after4 > target_tokens:
            logger.warning(
                "context_compressor: Phase 4 exhausted but wire STILL exceeds "
                "target - %d tokens > %d (the current turn alone is over the "
                "post-compression budget). Returning the reduced wire anyway; "
                "the provider may reject it with PROMPT_TOO_LONG. Correlate "
                "with an upstream ``chat.provider_error`` (retry_category="
                "prompt_too_long) on the same conversation.",
                after4,
                target_tokens,
            )
        else:
            logger.info(
                "context_compressor: after Phase 4 - %d tokens (target=%d)",
                after4,
                target_tokens,
            )
        if outcome_sink is not None:
            outcome_sink["phase_reached"] = 4
        return self._finalize(
            _assemble(), tokens_before, tok_per_byte,
            bytes_cache=bytes_cache,
        )

    # ------------------------------------------------------------------
    # Phase algorithms
    # ------------------------------------------------------------------

    def _prune_redundant_tool_groups(
        self,
        turns: list[list[dict[str, Any]]],
        *,
        tok_per_byte: float,
        bytes_cache: dict[int, int],
        outcome_sink: dict[str, Any] | None = None,
    ) -> list[list[dict[str, Any]]]:
        """Phase 0: drop tool groups that are superseded or information-free.

        Two independent mechanisms, both pure drops:

        1. **Superseded reads** — when the same read target (see
           :func:`_supersede_key`) is read several times, only the NEWEST
           result survives; the older snapshots are stale by construction.
        2. **Useless results** — a successful, non-error, information-free
           result (empty body / bare completion acknowledgement) carries
           nothing the model can reason from.

        **Atomicity / orphan safety.** The unit of removal is the ATOMIC
        GROUP produced by :func:`_split_atomic_groups`: one
        ``assistant{tool_calls}`` together with ALL of its paired
        ``role:tool`` replies. Because a group is only ever kept or dropped
        WHOLE, an ``assistant.tool_calls`` id can never lose its reply and a
        ``role:tool`` can never lose its opener — the orphan class that makes
        providers answer 400 is structurally impossible here, so no
        after-the-fact repair pass is needed. The corollary is that a
        parallel round (one assistant, several calls) is pruned only when
        EVERY one of its results is prunable; a single result worth keeping
        keeps the whole group.

        **Known, accepted trade-off.** An older read of a path is pruned even
        when a ``write`` / ``edit`` to that same path sits between the two
        reads. The model needs the file's CURRENT content, which the newest
        read carries; the fact that an edit happened (and what it changed) is
        carried by the edit's own result, which is a write-class tool and
        therefore never superseded. Locked by
        ``test_superseded_prunes_older_read_even_across_a_write``.

        Never raises: any unexpected shape aborts the pass and returns the
        input unchanged (compaction must never fail).
        """
        try:
            return self._prune_redundant_tool_groups_inner(
                turns,
                tok_per_byte=tok_per_byte,
                bytes_cache=bytes_cache,
                outcome_sink=outcome_sink,
            )
        except Exception as exc:  # noqa: BLE001 — pruning is best-effort
            logger.warning(
                "context_compressor: Phase 0 prune aborted (%s: %s) — "
                "returning the wire unpruned; the 4 lossy phases still run",
                type(exc).__name__,
                exc,
            )
            return turns

    def _prune_redundant_tool_groups_inner(
        self,
        turns: list[list[dict[str, Any]]],
        *,
        tok_per_byte: float,
        bytes_cache: dict[int, int],
        outcome_sink: dict[str, Any] | None,
    ) -> list[list[dict[str, Any]]]:
        """Body of :meth:`_prune_redundant_tool_groups` (exception-wrapped)."""
        # Pass 1 — walk every group NEWEST-FIRST and record, per supersede
        # key, whether a later group already covers it. Newest-first is what
        # makes "keep the latest" fall out without a second ordering pass.
        groups_per_turn = [_split_atomic_groups(turn) for turn in turns]
        flat: list[list[dict[str, Any]]] = [
            group for groups in groups_per_turn for group in groups
        ]
        seen_keys: set[tuple] = set()
        drop_reason: dict[int, str] = {}
        for group in reversed(flat):
            verdict = self._classify_tool_group(group)
            if verdict is None:
                continue
            reason, keys = verdict
            if reason == "superseded":
                # Prunable ONLY if every one of this group's keys was already
                # claimed by a newer group; otherwise this group IS the
                # newest holder of at least one target and must survive.
                if keys and keys.issubset(seen_keys):
                    drop_reason[id(group)] = "superseded"
                seen_keys |= keys
            else:
                drop_reason[id(group)] = reason

        if not drop_reason:
            return turns

        # Pass 2 — rebuild the turn list without the doomed groups, keeping
        # the surviving groups' original order and identity (no copies, so
        # ``bytes_cache``'s ``id(msg)`` keys stay valid).
        superseded_count = sum(
            1 for reason in drop_reason.values() if reason == "superseded"
        )
        useless_count = len(drop_reason) - superseded_count
        saved_bytes = 0
        # Diagnostic tally of WHAT was pruned: ``tool:normalised-target`` ->
        # how many groups holding it were dropped. Verifies "N reads of the
        # same file collapsed to the newest" really happened, and that two
        # DIFFERENT pages of one file were NOT merged. Keys only — never
        # result content. Built inside the drop branch this loop already
        # walks, so no extra pass over the wire.
        pruned_targets: dict[str, int] = {}
        pruned: list[list[dict[str, Any]]] = []
        for groups in groups_per_turn:
            kept: list[dict[str, Any]] = []
            for group in groups:
                if id(group) in drop_reason:
                    saved_bytes += self._estimate_bytes_cached(
                        group, bytes_cache
                    )
                    if drop_reason[id(group)] == "superseded":
                        self._tally_pruned_targets(group, pruned_targets)
                    continue
                kept.extend(group)
            if kept:
                pruned.append(kept)

        logger.info(
            "context_compressor: Phase 0 pruned %d group(s) "
            "(superseded=%d, useless=%d, ~%d bytes / ~%d tokens reclaimed)",
            len(drop_reason),
            superseded_count,
            useless_count,
            saved_bytes,
            self._bytes_to_tokens(saved_bytes, tok_per_byte),
        )
        self._log_pruned_targets(pruned_targets)
        if outcome_sink is not None:
            # Observability contract for Phase 0 (kept OFF the ``phase_reached``
            # enum so the journal schema is untouched). Answers both diagnostic
            # questions a live incident asks: WHAT was pruned (per-reason
            # counts) and HOW MUCH it bought (bytes + tokens). Absent when
            # nothing was pruned, so a quiet pass adds no noise.
            outcome_sink["pruned_groups"] = {
                "total": len(drop_reason),
                "superseded": superseded_count,
                "useless": useless_count,
                "saved_bytes": saved_bytes,
                "saved_tokens": self._bytes_to_tokens(
                    saved_bytes, tok_per_byte
                ),
            }
        return pruned

    @staticmethod
    def _log_pruned_targets(tally: dict[str, int]) -> None:
        """Name WHAT Phase 0 dropped, ranked by how often, bounded in width.

        Empty tally → silent (nothing superseded, so there is nothing to
        report). Ranking by count first makes the truncated remainder the
        least interesting rows by construction.
        """
        if not tally:
            return
        ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
        shown = ranked[:_PHASE0_PRUNED_TARGETS_LOG_LIMIT]
        overflow = len(ranked) - len(shown)
        logger.info(
            "context_compressor: Phase 0 pruned superseded targets - %s%s",
            shown,
            f" (+{overflow} more)" if overflow > 0 else "",
        )

    @staticmethod
    def _tally_pruned_targets(
        group: list[dict[str, Any]],
        tally: dict[str, int],
    ) -> None:
        """Record one dropped superseded group's targets into ``tally``.

        Reuses the SAME pairing + key derivation the pruning decision used
        (:meth:`_pair_tool_group` / :func:`_supersede_key`), so the log names
        exactly the keys that drove the drop rather than a re-derived guess.
        Diagnostics only: any surprise shape is silently skipped — a logging
        tally must never break a compaction.
        """
        paired = ThreeLevelContextCompressor._pair_tool_group(group)
        if paired is None:
            return
        for tool_name, arguments, _result in paired:
            key = _supersede_key(tool_name, arguments)
            if key is None:
                continue
            # ``key`` is ``(tool, targets, pages)``. Render the targets and any
            # non-empty pagination so two different pages of one file read as
            # two DISTINCT rows (proving they were not conflated).
            _, targets, pages = key
            label = f"{tool_name}:" + "|".join(t for t in targets if t)
            page_part = "|".join(p for p in pages if p)
            if page_part:
                label = f"{label}@{page_part}"
            tally[label] = tally.get(label, 0) + 1

    @staticmethod
    def _pair_tool_group(
        group: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any], Any]] | None:
        """Pair an atomic group's calls with their results, or ``None``.

        Returns one ``(tool_name, arguments, result_content)`` triple per
        call, in call order. ``None`` means "this group's shape is not one we
        model, leave it alone" — a non-assistant head, no/empty
        ``tool_calls``, replies that do not pair 1:1 by id, an unreadable
        ``arguments`` payload, or assistant lead-in prose (real reasoning the
        model wrote, which dropping the group would erase).

        Separated from :meth:`_classify_tool_group` so the STRUCTURAL
        question ("can we read this group?") is answered independently of the
        POLICY question ("should it be pruned?").
        """
        head = group[0]
        if head.get("role") != "assistant":
            return None
        calls = head.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            return None
        # Assistant lead-in prose is real reasoning the model wrote; dropping
        # the group would erase it, so an annotated round is never prunable.
        content = head.get("content")
        if isinstance(content, str) and content.strip():
            return None
        by_id = _index_replies_by_call_id(group[1:], expected=len(calls))
        if by_id is None:
            return None

        paired: list[tuple[str, dict[str, Any], Any]] = []
        for call in calls:
            parsed = _parse_tool_call(call)
            call_id = call.get("id") if isinstance(call, dict) else None
            reply = by_id.get(call_id) if isinstance(call_id, str) else None
            if parsed is None or reply is None:
                return None
            tool_name, arguments = parsed
            paired.append((tool_name, arguments, reply.get("content")))
        return paired

    @staticmethod
    def _classify_tool_group(
        group: list[dict[str, Any]],
    ) -> tuple[str, set[tuple]] | None:
        """Classify one atomic group as ``superseded`` / ``useless`` / keep.

        Returns ``None`` for "never prune this group" — an unreadable shape
        (see :meth:`_pair_tool_group`), a group holding any error, or a mixed
        group where some result must be kept.

        ``("superseded", keys)`` means EVERY reply in the group is a
        superseding read; ``keys`` is the set of targets it holds, which the
        caller uses to decide whether a newer group already covers them.
        ``("useless", set())`` means every reply is information-free.
        """
        paired = ThreeLevelContextCompressor._pair_tool_group(group)
        if paired is None:
            return None

        keys: set[tuple] = set()
        all_superseding = True
        all_useless = True
        for tool_name, arguments, result in paired:
            # RED LINE: one failure anywhere in the group keeps the whole
            # group — the model must keep seeing what went wrong.
            if _result_is_error(result):
                return None
            key = _supersede_key(tool_name, arguments)
            if key is None:
                all_superseding = False
            else:
                keys.add(key)
            if not _result_is_useless(tool_name, result):
                all_useless = False
            if not (all_superseding or all_useless):
                return None

        if all_useless:
            return "useless", set()
        if all_superseding and keys:
            return "superseded", keys
        return None

    def _strip_tool_interactions(
        self, turns: list[list[dict[str, Any]]]
    ) -> list[list[dict[str, Any]]]:
        """Drop tool-call scaffolding, keeping user turns + final summary.

        Per turn:
        * KEEP every ``role=user`` message.
        * KEEP the LAST ``role=assistant`` message that has NO ``tool_calls``
          (the turn's final summary).
        * DROP every ``role=assistant`` message that carries ``tool_calls``
          (and, implicitly, its ``role=tool`` replies).
        * DROP earlier no-tool ``role=assistant`` messages (intermediate
          reasoning text).
        * DROP every ``role=tool`` message.

        Empty turns after filtering are removed from the returned list.
        """
        result: list[list[dict[str, Any]]] = []
        for turn in turns:
            last_summary_idx = -1
            for i in range(len(turn) - 1, -1, -1):
                msg = turn[i]
                if msg.get("role") == "assistant" and not msg.get(
                    "tool_calls"
                ):
                    last_summary_idx = i
                    break
            kept: list[dict[str, Any]] = []
            for i, msg in enumerate(turn):
                role = msg.get("role")
                if role == "user":
                    kept.append(msg)
                elif (
                    role == "assistant"
                    and not msg.get("tool_calls")
                    and i == last_summary_idx
                ):
                    kept.append(msg)
            if kept:
                result.append(kept)
        return result

    def _drop_oldest_turns(
        self,
        turns: list[list[dict[str, Any]]],
        *,
        target_tokens: int,
        fixed_tokens: int,
        tok_per_byte: float,
        bytes_cache: dict[int, int] | None = None,
    ) -> list[list[dict[str, Any]]]:
        """Drop the oldest turns until ``fixed + remaining <= target``.

        ``bytes_cache`` (P1-7) is an optional per-``compress()`` cache
        threaded from the caller so per-message UTF-8 encode work is
        reused across phases; passing ``None`` (or omitting it) falls back
        to the uncached static path — keeps external unit tests that
        exercise this helper standalone byte-for-byte compatible.
        """
        cache = bytes_cache if bytes_cache is not None else {}

        def _turn_tok(t: list[dict[str, Any]]) -> int:
            return self._bytes_to_tokens(
                self._estimate_bytes_cached(t, cache), tok_per_byte,
            )

        kept = list(turns)
        kept_tokens = sum(_turn_tok(t) for t in kept)
        while kept and fixed_tokens + kept_tokens > target_tokens:
            kept_tokens -= _turn_tok(kept[0])
            kept.pop(0)
        return kept

    # ------------------------------------------------------------------
    # Token density (real-measurement first) + target resolution
    # ------------------------------------------------------------------

    def _token_density(
        self, bytes_before: int, wire_actual_tokens: int | None
    ) -> float:
        """tokens-per-byte for THIS conversation.

        Preferred: provider's real measurement of the whole wire divided by
        the wire's UTF-8 byte count. Falls back to a fixed factor when no
        real measurement is available.
        """
        if (
            wire_actual_tokens is not None
            and wire_actual_tokens > 0
            and bytes_before > 0
        ):
            return wire_actual_tokens / bytes_before
        return 1.0 / _COMPRESS_BYTES_PER_TOKEN

    @staticmethod
    def _bytes_to_tokens(byte_count: int, tok_per_byte: float) -> int:
        return int(byte_count * tok_per_byte)

    def _resolve_target_tokens(
        self,
        *,
        tokens_before: int,
        target_ratio: float,
        budget_tokens: int | None,
        target_window_ratio: float | None = None,
    ) -> int:
        """Token target the pass aims for (window-anchored)."""
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
        tok_per_byte: float,
        *,
        bytes_cache: dict[int, int] | None = None,
    ) -> list[dict[str, Any]]:
        """Emit a finish-metrics log line and return the result unchanged.

        ``bytes_cache`` (P1-7) is the per-``compress()`` byte-count cache
        so the final ``tokens_after`` measurement reuses cached per-message
        entries populated by the phase loop.
        """
        cache = bytes_cache if bytes_cache is not None else {}
        tokens_after = self._bytes_to_tokens(
            self._estimate_bytes_cached(result, cache), tok_per_byte,
        )
        retain = (
            (tokens_after / tokens_before) if tokens_before > 0 else 1.0
        )
        logger.info(
            "context_compressor: finish - tokens_before=%d tokens_after=%d "
            "retain_ratio=%.3f messages_after=%d",
            tokens_before,
            tokens_after,
            retain,
            len(result),
        )
        return result

    # ------------------------------------------------------------------
    # Protection plan (turn-aware, window-anchored)
    # ------------------------------------------------------------------

    def _build_protection_plan(
        self,
        messages: list[dict[str, Any]],
        *,
        preserve_tail: int,
        protect_budget_tokens: int,
        tok_per_byte: float,
        bytes_cache: dict[int, int] | None = None,
    ) -> _ProtectionPlan:
        """Decide which recent turns are protected verbatim.

        Always protects the CURRENT turn (last user message + everything
        after it). Then walks older turns from the tail, accumulating until
        the protected size would exceed ``protect_budget_tokens``. A
        ``preserve_tail`` message-count floor is honoured.

        ``bytes_cache`` (P1-7) is the per-``compress()`` cache that lets
        the per-turn sizing reuse entries the compression loop populates.
        """
        cache = bytes_cache if bytes_cache is not None else {}
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
            return self._bytes_to_tokens(
                self._estimate_bytes_cached(turn, cache), tok_per_byte,
            )

        protect_from = len(turns) - 1
        protected_tokens = _turn_tokens(turns[protect_from])
        protected_msgs = len(turns[protect_from])

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

        if protected_tokens > protect_budget_tokens:
            logger.info(
                "context_compressor: protection_plan - preserve_tail "
                "message-count floor (%d msgs) overrode the protect_budget "
                "token cap (%d tok) — protected_tokens=%d exceeds the "
                "budget by %d tok. Downstream Phase 3/4 warnings that fire "
                "on this compaction are the EXPECTED consequence of the "
                "floor winning, not the protection budget being wrongly "
                "drawn.",
                preserve_tail,
                protect_budget_tokens,
                protected_tokens,
                protected_tokens - protect_budget_tokens,
            )

        return _ProtectionPlan(
            system_msgs=system_msgs,
            all_turns=turns,
            protect_from=protect_from,
            protected_tokens=protected_tokens,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_bytes(messages: list[dict[str, Any]]) -> int:
        """UTF-8 byte-count estimator; bytes/4 for token estimate.

        Uses UTF-8 bytes instead of Python chars so multi-byte texts (CJK,
        emoji) are not under-counted by 2-3× — the same coarse ``bytes/4``
        heuristic the reference tokenizer uses by default. Image / audio /
        other non-text parts contribute
        :data:`NON_TEXT_PART_BYTE_EQUIVALENT` (4800 bytes ≈ 1200 tokens per
        image, the greatest-common-divisor across OpenAI/Anthropic/Gemini).

        P0-1 (Task L): also counts ``tool_calls[*].function.arguments`` on
        every assistant row — matches ``estimate_wire_tokens`` in
        :mod:`_agentic_kernel` so trigger口径 (which DOES fold tool_calls
        args into ``bytes_estimate``) and compressor口径 (this helper) stop
        diverging on tool-heavy conversations. Without this, agent turns
        carrying huge JSON arguments would trip the trigger yet look small
        to the compressor, skewing its per-byte density and letting the
        compacted wire remain above threshold (mid-turn thrash).

        P1-1 (Task O): drop the legacy ``+20``-bytes-per-message overhead
        so this helper's口径 matches ``estimate_wire_tokens`` (which does
        not add any per-message overhead either). The old constant was a
        rough guess at OpenAI wire framing; tiktoken doesn't count it, and
        the two口径 must stay aligned or the compressor overshoots by
        ~500 tokens on a 100-message wire.
        """
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content.encode("utf-8"))
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            total += len(part.get("text", "").encode("utf-8"))
                        else:
                            total += _COMPRESS_NON_TEXT_PART_BYTES
            for tc in msg.get("tool_calls") or ():
                if isinstance(tc, dict):
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        args = fn.get("arguments") or ""
                        if not isinstance(args, str):
                            args = str(args)
                        total += len(args.encode("utf-8"))
        return total

    @staticmethod
    def _msg_bytes(msg: dict[str, Any]) -> int:
        """UTF-8 byte-count for ONE message (content + tool_calls args).

        Extracted from :meth:`_estimate_bytes` so callers can compute the
        per-message cost once and stash it in a per-``compress()`` cache
        (id(msg) → bytes). Kept as a static helper so the compressor stays
        stateless (``__slots__ = ()``); the cache lives in the calling
        stack frame, not on the instance. No per-message overhead (P1-1,
        Task O) —口径 identical to :meth:`_estimate_bytes` above.
        """
        total = 0
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content.encode("utf-8"))
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total += len(part.get("text", "").encode("utf-8"))
                    else:
                        total += _COMPRESS_NON_TEXT_PART_BYTES
        for tc in msg.get("tool_calls") or ():
            if isinstance(tc, dict):
                fn = tc.get("function")
                if isinstance(fn, dict):
                    args = fn.get("arguments") or ""
                    if not isinstance(args, str):
                        args = str(args)
                    total += len(args.encode("utf-8"))
        return total

    @classmethod
    def _estimate_bytes_cached(
        cls,
        messages: list[dict[str, Any]],
        cache: dict[int, int],
    ) -> int:
        """P1-7: cache-aware sibling of :meth:`_estimate_bytes`.

        Reuses per-message byte counts across the Phase 1-4 loop: each
        message's UTF-8 encode work runs exactly ONCE per ``compress()``
        call regardless of how many times ``_total_tokens`` / ``_tokens_of``
        / ``_turn_tok`` walk it (a 50-round conversation × 4 phases went
        from ~200 full-wire encodes to 1). Sums each message via
        ``cache[id(msg)]``, filling the cache lazily; callers hand in the
        SAME dict for the lifetime of one compression pass. Static
        ``_estimate_bytes`` is untouched so external unit tests still see
        the stateless口径.
        """
        total = 0
        for msg in messages:
            key = id(msg)
            hit = cache.get(key)
            if hit is None:
                hit = cls._msg_bytes(msg)
                cache[key] = hit
            total += hit
        return total


__all__ = [
    "ThreeLevelContextCompressor",
]

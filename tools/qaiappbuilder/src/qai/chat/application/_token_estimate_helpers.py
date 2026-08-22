# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cloud-first token-accounting helpers for the chat application layer.

These helpers replace the old local tiktoken BPE pass as the PRIMARY source
of "how many prompt tokens does the current context occupy". Cloud providers
already measure this exactly and return it on each turn's terminal frame as
``usage.prompt_tokens`` (normalized OpenAI shape, persisted on the assistant
:class:`~qai.chat.domain.message.Message`). The most-recent assistant turn's
``prompt_tokens`` IS the provider's authoritative measurement of the wire that
was just sent — strictly more accurate than any local re-tokenisation.

Both helpers are pure, read-only, and operate on the chat **domain** model
(``Conversation`` / ``Message``), so they stay in the application layer and
introduce NO cross-context or adapter imports (keeps import-linter happy and
respects Clean Arch layering — the adapter ``context_size_estimator`` is NOT
imported here; its char/overhead constants are re-declared locally below).

State-Truth-First (AGENTS.md 铁律 1): cloud ``prompt_tokens`` is the real
state the provider measured; the bytes-based estimate is only a fallback for
conversations that have never carried a provider usage block.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from qai.chat.domain.content import MessageRole
from qai.chat.domain.sub_agent_session import SubAgentSession
from qai.chat.domain.usage_math import finalize_cumulative_prompt_usage

# Byte-per-token / per-message overhead constants. These mirror the values in
# ``qai.chat.adapters.context_size_estimator`` (lines 58/68/72) but are
# RE-DECLARED here on purpose: importing the adapter module into the
# application layer would break the layered-chat import-linter contract
# (application must not depend on adapters). They are only used by the coarse
# fallback estimate, which is intentionally approximate.
_UNKNOWN_MODEL_BYTES_PER_TOKEN = 3
_DEFAULT_BYTES_PER_TOKEN = 4
_MESSAGE_OVERHEAD = 4

#: UTF-8 byte-equivalent budget for a non-text content part (image / audio /
#: other multimodal block) in an OpenAI-style wire ``content`` list.
#:
#: 4800 bytes / 4 ≈ 1200 tokens per non-text part — the "greatest common
#: divisor" across the providers we route to so a vision-enabled conversation
#: is not dramatically over- or under-counted here (OpenAI high-detail tiles
#: run ~765, Anthropic ~1024, Gemini 258-2592 depending on tile density).
#: Kept conservative on the LARGE side so a compaction TRIGGER never
#: under-fires on multimodal — the OpenAI ``_estimate_vision_tokens`` tile
#: formula (streaming.py:6149) is the precise path used at pre-send time.
#:
#: Kept in ONE place so every byte-based estimator on the compaction path
#: (compressor ``_estimate_bytes``, kernel ``estimate_wire_tokens``, and this
#: file's ``coarse_byte_estimate``) applies the SAME image-cost rule —
#: otherwise a vision-enabled conversation gets different trigger-vs-target
#: 口径 across paths (compaction fires but the compressor sees no work, or
#: vice versa).
NON_TEXT_PART_BYTE_EQUIVALENT: int = 4800


def non_text_content_bytes(content: Any) -> int:
    """UTF-8 byte-equivalent size for an OpenAI-style ``content`` list.

    Returns the summed byte-equivalent contribution of every part in a
    multimodal ``content`` list: text parts contribute their UTF-8 byte
    count; every other part type (image_url / input_image / input_audio /
    etc.) contributes :data:`NON_TEXT_PART_BYTE_EQUIVALENT`.

    Callers pass the ``content`` VALUE directly (str / list / None / other):

    * ``str`` → ``len(content.encode("utf-8"))``.
    * ``list`` → summed per-part contribution as above.
    * anything else / ``None`` → ``0``.

    Bytes (not Python chars) are used so multi-byte texts (CJK / emoji) are
    not under-counted by 2-3× — the same coarse ``bytes/4`` heuristic the
    reference tokenizer uses by default. Pure and never raises. Used by
    every byte-based estimator on the compaction path so multimodal
    conversations get consistent trigger and target口径.
    """
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    total += len(part.get("text", "").encode("utf-8"))
                else:
                    total += NON_TEXT_PART_BYTE_EQUIVALENT
        return total
    return 0


def _last_assistant_with_usage(
    conv: Any, *, after_message_id: str | None = None,
) -> Any | None:
    """Return the most-recent assistant ``Message`` carrying real usage.

    Walks ``conv.messages`` in reverse (append-order) and returns the first
    assistant turn whose ``usage["prompt_tokens"]`` is a positive int. This is
    the provider's authoritative measurement of the prompt wire size as of that
    turn. Returns ``None`` when no such turn exists (e.g. a brand-new
    conversation, or a purely local-model history that never emitted usage).

    CCD-1 (PENDING-WORK.md §1) — ``after_message_id`` filter: when set, the
    walk SKIPS every message whose id matches ``after_message_id`` AND every
    message before it (i.e. only messages STRICTLY AFTER the named anchor are
    considered). This is used by ``estimate_compacted_tokens`` to ignore the
    PRE-compaction assistant turn during the brief window between checkpoint
    creation and the first post-compaction usage block landing — without
    this filter, the badge would temporarily show the (much larger) pre-
    compaction wire size as the "compacted" figure.

    ``after_message_id=None`` keeps the legacy behaviour (no filter) for
    backward compatibility with all existing callers.
    """
    messages = getattr(conv, "messages", None)
    if not messages:
        return None
    # When ``after_message_id`` is set, find the anchor's index first (forward
    # scan): we want only messages STRICTLY after that index. If the anchor
    # is absent (e.g. message was rewound / deleted by a future edit route)
    # we treat the filter as a no-op (degrade gracefully — the caller is
    # already in a degraded state and the badge falls back further).
    anchor_idx: int = -1
    if after_message_id is not None:
        for i, m in enumerate(messages):
            try:
                if getattr(getattr(m, "id", None), "value", None) == after_message_id:
                    anchor_idx = i
                    break
            except Exception:  # noqa: BLE001 — best-effort id read
                continue
    for i in range(len(messages) - 1, -1, -1):
        if after_message_id is not None and anchor_idx >= 0 and i <= anchor_idx:
            # Reached the anchor (or earlier) — every remaining candidate is
            # PRE-compaction; stop walking, return None to signal "no post-
            # compaction usage yet".
            return None
        m = messages[i]
        if m.role is MessageRole.ASSISTANT:
            pt = int((m.usage or {}).get("prompt_tokens") or 0)
            if pt > 0:
                return m
    return None


def _bytes_per_token(model_id: str | None) -> int:
    """Pick bytes/token: 4 for a cloud model id, else 3 (unknown / local).

    Kept simple per spec: a non-empty model id that does NOT start with
    ``"local"`` is treated as a cloud model (≈4 UTF-8 bytes/token); everything
    else (empty / ``None`` / ``local::*``) uses the conservative unknown-local
    ratio of 3 bytes/token.
    """
    if model_id and not model_id.startswith("local"):
        return _DEFAULT_BYTES_PER_TOKEN
    return _UNKNOWN_MODEL_BYTES_PER_TOKEN


def is_anthropic_family(model_id: str | None) -> bool:
    """Return True when ``model_id`` is a Claude / Anthropic-family model.

    Shared口径 helper (mirrors ``streaming._is_anthropic_family``) so the
    compaction-decision staleness fallback measures the LAST assistant turn's
    effective prompt with the SAME provider-family cache rule the running
    full-history counter used. Anthropic/Claude split cache reads OUT of
    ``prompt_tokens`` (wire = ``prompt_tokens + cache_read_tokens``), whereas
    OpenAI / Azure / Gemini / Vertex already fold cache into ``prompt_tokens``.
    Keyed on the model id (``"claude"`` substring) — the authoritative selector
    (the client-supplied provider field is unvalidated).
    """
    return isinstance(model_id, str) and "claude" in model_id.lower()


def coarse_byte_estimate(conv: Any, model_id: str | None) -> int:
    """Coarse UTF-8-byte-based prompt-token estimate over the full history.

    Walks ``conv.messages`` and sums the UTF-8 byte length of each message's
    text content plus its tool-call payloads (tool name + JSON-encoded args +
    output — the parts replayed to the model), divides by the bytes/token
    ratio and adds a fixed per-message envelope overhead. Mirrors
    ``_message_part_texts`` (``context_size_estimator.py:642-679``) but for
    the chat domain ``Message`` shape. Best-effort and never raises.

    Bytes (not Python chars) are used so multi-byte texts (CJK / emoji) are
    not under-counted by 2-3×; this is the same ``bytes/4`` heuristic the
    reference tokenizer uses by default.

    This is the FALLBACK source used only when no assistant turn carries a
    provider ``usage`` block; otherwise the caller prefers the cloud truth.

    Multimodal correction (2026-07-26 audit follow-up): each
    :class:`MessageContent.media_refs` entry contributes
    :data:`NON_TEXT_PART_BYTE_EQUIVALENT` bytes so an image-heavy conversation
    on the cold-path fallback does not silently under-count itself out of a
    compaction trigger. The refs are opaque ids in the domain, but their COUNT
    is a reliable lower bound on the wire's image-part count.
    """
    messages = getattr(conv, "messages", None)
    if not messages:
        return 0
    bytes_per_token = _bytes_per_token(model_id)
    total_bytes = 0
    message_count = 0
    for m in messages:
        message_count += 1
        try:
            total_bytes += len(m.content.text.encode("utf-8"))
        except Exception:  # noqa: BLE001 — never break the estimate
            pass
        try:
            media_refs = getattr(m.content, "media_refs", None) or ()
            total_bytes += NON_TEXT_PART_BYTE_EQUIVALENT * len(media_refs)
        except Exception:  # noqa: BLE001 — never break the estimate
            pass
        for tc in getattr(m, "tool_calls", ()) or ():
            try:
                total_bytes += len(str(tc.get("tool") or "").encode("utf-8"))
            except Exception:  # noqa: BLE001
                pass
            try:
                total_bytes += len(
                    json.dumps(tc.get("args"), ensure_ascii=False).encode("utf-8"),
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                total_bytes += len(str(tc.get("output") or "").encode("utf-8"))
            except Exception:  # noqa: BLE001
                pass
    return int(total_bytes / bytes_per_token) + _MESSAGE_OVERHEAD * message_count


def assistant_eff_prompt(
    usage: dict[str, Any], is_anthropic_for_usage: bool,
) -> int:
    """Effective prompt-token size the provider measured for the LAST round.

    Shared口径 helper extracted from the running full-history counter
    (``streaming.py`` ~3431-3445) so the SAME formula drives BOTH the badge
    occupancy counter AND the new compaction-trigger ``实发`` decision —
    keeping a single source of truth.

    ``eff = last_round_prompt_tokens (or prompt_tokens fallback)
            + last_round_cache_read_tokens (Anthropic-family only)``

    Provider-family cache branching: Claude/Anthropic split cache reads OUT of
    ``prompt_tokens`` (so the real wire is ``prompt_tokens + cache_read``),
    whereas OpenAI / Azure / Gemini / Vertex already fold cache into
    ``prompt_tokens``. ``last_round_prompt_tokens`` is the per-round wire size
    (``prompt_tokens`` is the cross-round SUM for multi-round turns, hence the
    fallback only when the per-round value is absent).

    CCD-2 (PENDING-WORK.md §1): ``is_anthropic_for_usage`` MUST be judged
    against the model that ACTUALLY PRODUCED this usage block — i.e. the
    ``model_id`` on the assistant message the usage came from, NOT the
    current request's ``model_hint``. After a model switch (Claude →
    GPT, etc.) the historical assistant's usage still carries Claude's
    cache-read split-out and must add it back; conversely, a GPT-produced
    historical usage must NOT have any spurious add-back applied just
    because the current request is now Claude. Pass:

    * **in-flight / live ``last_round_usage`` of the CURRENT round** →
      based on ``request.model_hint`` (the live round IS this request);
    * **historical usage from a prior assistant message** →
      based on that message's ``model_id`` field (``_is_anthropic_family``
      on the message's own model id).

    Best-effort / never raises: a malformed ``usage`` yields 0.
    """
    try:
        lrp = int(
            usage.get("last_round_prompt_tokens")
            or usage.get("prompt_tokens")
            or 0
        )
        cr = int(usage.get("last_round_cache_read_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0
    eff = lrp + (cr if is_anthropic_for_usage else 0)
    # Clamp to >= 0: token counts are never negative; a malformed/negative
    # provider reading must not drag the trigger ``实发`` below the real size
    # (nor corrupt the full-history counter). The full-history path already
    # guards with ``if eff > 0`` so a clamped 0 keeps it byte-for-byte
    # equivalent (0 was a no-op there too).
    return eff if eff > 0 else 0


#: Sentinel for the ``source_key`` field of :class:`ProviderReading` naming
#: which usage-dict key produced the ``base_size`` figure. ``first_round`` is
#: the un-contaminated round-0 wire — the ONLY value that :func:`authoritative_
#: provider_size` promises is safe for cross-turn prediction; the remaining
#: keys are legacy fallbacks flagged by ``degraded=True``.
ProviderReadingSource = Literal[
    "first_round", "last_round", "prompt_tokens", "none",
]


@dataclass(frozen=True)
class ProviderReading:
    """Single-truth-source provider-measured wire reading.

    Returned by :func:`authoritative_provider_size` — the ONE helper every
    consumer of "how big was the last provider-measured wire?" now calls
    (trigger gate ``_presend_eff_estimate``, badge ``estimate_compacted_
    tokens`` / ``compacted_badge_detail``, A′ overhead reverse-compute
    ``compute_overhead_from_last_wire_measurement``, ``/compact`` force
    path). Keeping every consumer口径 identical is 铁律 1 (State-Truth-
    First) at the cross-decision level: badge-visible number and trigger
    number and reverse-computed overhead are all derived from the SAME
    provider reading, so the §8.7 76%→56% jitter class is structurally
    impossible instead of hand-audited every time a new consumer joins.

    Fields:

    * ``base_size`` — provider-measured wire prompt tokens. When
      ``source_key == "first_round"`` this is the round-0 stamped size
      (``first_round_prompt_tokens`` +cache_read for Anthropic-family
      SOURCE model) — the ONLY value uncontaminated by mid-turn
      in-flight tool outputs (§10.F). Otherwise it is the pre-CCD-2
      ``last_round_prompt_tokens`` / raw ``prompt_tokens``, retained for
      diagnostics only.
    * ``source_model_id`` — ``model_id`` on the assistant message the
      usage came from (CCD-2): after a Claude→GPT switch the historical
      Claude usage still carries Claude's cache-read split-out and the
      Anthropic add-back must key on THIS field, not the current
      request's model.
    * ``source_key`` — which usage-dict key produced ``base_size``. Only
      ``"first_round"`` is safe for cross-turn prediction; the rest
      always ship with ``degraded=True``.
    * ``degraded`` — ``True`` iff ``source_key != "first_round"``, i.e.
      the usage row is legacy (pre-2026-07 / sub-agent / any writer that
      skipped :func:`append_display_usage_fields`). Consumers MUST treat
      a degraded reading as "no cross-turn prediction available" and
      fall back to their bytes-based estimate; using a degraded reading
      re-introduces the §10.F 30x inflation the first_round base was
      created to eliminate.
    """

    base_size: int
    source_model_id: str | None
    source_key: ProviderReadingSource
    degraded: bool


def _read_provider_size_from_usage(
    usage: dict[str, Any],
    *,
    source_model_id: str | None,
    request_model_id: str | None,
) -> ProviderReading | None:
    """Per-usage picker: pure, no I/O, no conv walking.

    Shared between :func:`authoritative_provider_size` (which walks one
    assistant via :func:`_last_assistant_with_usage`) and
    :func:`compute_overhead_from_last_wire_measurement` (which walks up
    to :data:`_OVERHEAD_SCAN_MAX_CANDIDATES` assistants applying its
    era-matching + range checks). Keeping the picker in ONE place is
    the audit §3.8 fix: A′ overhead reverse-compute now reads the same
    round-0 base the trigger gate and the badge read, so the three
    consumers cannot drift.

    Rules (mirror :func:`authoritative_provider_size` docstring §1-3):

    * Prefer ``first_round_prompt_tokens`` (round-0, uncontaminated).
      Non-degraded.
    * Fall through to ``last_round_prompt_tokens`` then raw
      ``prompt_tokens`` — flag ``degraded=True``.
    * Anthropic-family cache-read add-back keyed on
      ``source_model_id`` with ``request_model_id`` fallback for rows
      lacking a ``model_id`` stamp. Prefer
      ``first_round_cache_read_display``; fall back to
      ``last_round_cache_read_tokens``.
    * All-zero usage → return ``None`` (nothing to report).
    """
    if not isinstance(usage, dict):
        return None
    try:
        first_round = int(usage.get("first_round_prompt_tokens") or 0)
    except (TypeError, ValueError):
        first_round = 0
    try:
        last_round = int(usage.get("last_round_prompt_tokens") or 0)
    except (TypeError, ValueError):
        last_round = 0
    try:
        raw_pt = int(usage.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        raw_pt = 0
    if first_round > 0:
        base = first_round
        source_key: ProviderReadingSource = "first_round"
        degraded = False
    elif last_round > 0:
        base = last_round
        source_key = "last_round"
        degraded = True
    elif raw_pt > 0:
        base = raw_pt
        source_key = "prompt_tokens"
        degraded = True
    else:
        return None
    # CCD-2: source model_id preferred; request model_id fallback for
    # rows lacking a model_id stamp (very old rows, unit-test fakes).
    family_model_id = source_model_id or request_model_id
    if is_anthropic_family(family_model_id):
        try:
            cr = int(
                usage.get("first_round_cache_read_display")
                or usage.get("last_round_cache_read_tokens")
                or 0
            )
        except (TypeError, ValueError):
            cr = 0
        if cr > 0:
            base += cr
    return ProviderReading(
        base_size=int(base),
        source_model_id=source_model_id,
        source_key=source_key,
        degraded=degraded,
    )


def authoritative_provider_size(
    *,
    conv: Any,
    checkpoint_anchor_message_id: str | None,
    request_model_id: str | None,
) -> ProviderReading | None:
    """Single source of truth for the provider-measured wire size.

    Every consumer of "what was the last provider-measured wire's size?"
    goes through this ONE function — trigger gate, badge, ``/compact``
    force path (A′ overhead reverse-compute uses the shared
    :func:`_read_provider_size_from_usage` picker directly on each of
    its multi-candidate era-matching windows). Keeping every口径
    identical is 铁律 1 (State-Truth-First) at the cross-decision
    level: the badge number, the trigger number, and the reverse-
    computed overhead are all derived from the SAME reading, so the
    §8.7 76%→56% jitter class is structurally impossible.

    Behaviour (audit report §4.1):

    1. Scan ``conv.messages`` newest → oldest for an assistant carrying
       a positive ``prompt_tokens``. When
       ``checkpoint_anchor_message_id`` is supplied, the walk skips every
       message at or before that anchor — so a pre-compaction assistant
       (much larger ``last_round_prompt_tokens``) can NEVER leak into
       the reading during the bootstrap window between checkpoint
       creation and the first post-compact turn (audit §3.10).
    2. Prefer ``first_round_prompt_tokens`` (round-0 wire, uncontaminated
       by in-flight tool outputs — §10.F). When absent, fall through to
       ``last_round_prompt_tokens`` then raw ``prompt_tokens``, but flag
       the reading ``degraded=True`` so callers know the base is
       contaminated and refuse to use it for cross-turn prediction
       (audit §3.5 — the pre-fix 30x inflation was exactly the
       consequence of trusting ``last_round`` on legacy rows).
    3. Anthropic-family cache-read add-back is judged against the
       SOURCE model — the ``model_id`` on the assistant message the
       usage came from — not the current request's model (CCD-2). After
       a Claude→GPT switch the historical Claude usage still carries
       Claude's cache-read split-out and must add it back; conversely,
       a GPT-produced row must NOT gain a spurious add-back just because
       the current request is now Claude. Prefer
       ``first_round_cache_read_display`` (round-0 stamped) with
       ``last_round_cache_read_tokens`` as the legacy fallback.
       Source-unknown falls back to ``request_model_id``.

    Returns ``None`` when no assistant with positive ``prompt_tokens``
    exists in the walkable range — a fresh conversation, a purely
    local-model history, or a bootstrap window whose only assistants
    are pre-anchor. Best-effort / never raises.
    """
    last_asst = _last_assistant_with_usage(
        conv, after_message_id=checkpoint_anchor_message_id,
    )
    if last_asst is None:
        return None
    usage = getattr(last_asst, "usage", None) or {}
    if not isinstance(usage, dict):
        return None
    source_model_id: str | None = getattr(last_asst, "model_id", None)
    return _read_provider_size_from_usage(
        usage,
        source_model_id=source_model_id,
        request_model_id=request_model_id,
    )


def effective_prompt_tokens(
    usage: dict[str, Any] | None,
    *,
    is_anthropic: bool,
    include_cache_write_fallback: bool = False,
) -> int:
    """本轮实发 wire prompt 大小（eff）。

    Shared口径 helper extracted from the four duplicated sub-agent eff
    calculations (``agent_tool._eff_prompt_from`` ①, ``agent_tool._on_round_end``
    inline ②, ``streaming._persist_subagent_takeover`` inline ③,
    ``streaming._context_usage_frame`` inline ④). The真正相同 part is collapsed
    here; the ONE故意的 difference (whether a Claude prompt-cache-WRITE round may
    substitute its ``cache_write_tokens`` for a missing ``cache_read_tokens``) is
    expressed by the explicit ``include_cache_write_fallback`` switch — NOT
    silently applied everywhere.

    ``eff = prompt_tokens + (cache_read_tokens when ``is_anthropic``)``

    Provider-family cache branching: Claude/Anthropic split cache reads OUT of
    ``prompt_tokens`` (so the real wire is ``prompt_tokens + cache_read``),
    whereas OpenAI / Azure / Gemini / Vertex already fold cache into
    ``prompt_tokens``. The caller decides ``is_anthropic`` (the existing local
    ``_is_anthropic_family(model_id)`` mirrors give the identical result as the
    canonical :func:`is_anthropic_family`).

    When ``include_cache_write_fallback=True`` AND ``cache_read_tokens <= 0``,
    fall back to ``prompt_tokens_details.cache_write_tokens`` as the cache-read
    figure — so a Claude prompt-cache-WRITE round (tiny ``prompt_tokens`` + real
    volume under ``cache_write_tokens``, no ``cache_read_tokens``) still reflects
    the true wire instead of "~0.0K". When ``False`` (callers ①④), NO cache_write
    add-back ever happens (their口径 must stay raw — do NOT偷偷加 a fallback).

    Non-anthropic never adds cache. ``usage`` of ``None`` / non-dict / malformed
    keys / a non-positive result → returns 0 (NEVER raises, NEVER returns
    negative). Callers needing the legacy ``None``-on-empty (① ``_eff_prompt_from``)
    or ``_raw_real``-fallback (④) semantics restore them at the call site from
    the 0 sentinel (byte-for-byte equivalent: 0 was a no-op there too).
    """
    if not isinstance(usage, dict):
        return 0
    try:
        _pt = int(usage.get("prompt_tokens") or 0)
        _cr = int(usage.get("cache_read_tokens") or 0)
    except (TypeError, ValueError):
        return 0
    if include_cache_write_fallback and _cr <= 0:
        _details = usage.get("prompt_tokens_details")
        if isinstance(_details, dict):
            try:
                _cw = int(_details.get("cache_write_tokens") or 0)
            except (TypeError, ValueError):
                _cw = 0
            if _cw > 0:
                _cr = _cw
    _eff = _pt + (_cr if is_anthropic else 0)
    return _eff if _eff > 0 else 0


def record_subagent_turn_usage(
    session: SubAgentSession,
    last_round_usage: dict[str, Any] | None,
    *,
    model_id: str | None,
    now: datetime,
) -> None:
    """用本轮真实 usage 更新 ``session`` 的 replace-last context badge figure.

    Single source of truth for the TWO byte-for-byte-identical sub-agent
    accounting segments — ``agent_tool._on_round_end`` ② and
    ``streaming._persist_subagent_takeover`` ③ — both of which feed a round's
    provider usage into :meth:`SubAgentSession.accumulate_usage` after correcting
    the effective wire size with the Anthropic cache split (cache_read, falling
    back to cache_write when the gateway reports the volume there).

    Behaviour (preserved exactly from the inline originals):

    * ``last_round_usage`` not a dict → no-op (do NOT write 0; the prior
      ``last_prompt_tokens`` value is preserved — never regresses to 0).
    * compute ``_pt = prompt_tokens`` and ``eff`` via
      :func:`effective_prompt_tokens` with ``include_cache_write_fallback=True``
      (the Anthropic cache口径 with the cache-write fallback);
    * ONLY when ``eff > _pt`` inject ``last_round_prompt_tokens=eff`` into a
      copy of the usage dict (which ``accumulate_usage`` PREFERS over
      ``prompt_tokens`` for its replace-last figure); otherwise pass the usage
      dict through unchanged — so the cumulative sum keeps folding the raw
      per-key values and the domain ``SubAgentSession`` stays provider-agnostic;
    * call ``session.accumulate_usage(that_dict, now=now)``.

    The ``is_anthropic`` decision keys on ``model_id`` (the sub-agent's OWN model
    for ②, the resolved take-over model for ③) via the canonical
    :func:`is_anthropic_family` — identical to the callers' local
    ``_is_anthropic_family`` mirrors.

    Audit §3.9 CORRECTION (batch 3, 2026-08-06): the initial audit noted
    that this helper does not emit ``first_round_prompt_tokens`` and
    conjectured a schema drift with main-loop usage rows. That concern
    turned out to be moot: sub-agent usage is written EXCLUSIVELY into
    the :class:`SubAgentSession` aggregate (``session.usage`` cumulative
    SUM dict + ``session.last_prompt_tokens`` replace-last int) via
    :meth:`SubAgentSession.accumulate_usage`, which SUMS every int key
    in ``delta`` (its provider-agnostic contract, pinned by domain
    tests). Adding ``first_round_prompt_tokens`` to the delta would
    quadratically inflate that key across rounds (replace-last-shaped
    field getting a raw-sum treatment). Meanwhile the four consumers
    of :func:`authoritative_provider_size` — trigger gate, badge, A′
    reverse-compute, ``/compact`` force path — all walk
    ``conv.messages[*].usage``, which sub-agent stamps NEVER populate
    (``_build_subagent_summary_message`` in ``streaming.py`` builds
    the injected ``[subagent_summary]`` assistant message WITHOUT any
    ``usage=`` kwarg, and ``orchestrate_discussion._persist_assistant_
    message`` likewise omits usage — verified batch 3). So there is no
    schema drift to fix here: sub-agent usage lives in a strictly
    disjoint aggregate. The single-truth-source guarantee holds
    because the truth source (``conv.messages``) is untouched by this
    path; the sub-agent tab's context badge reads its own aggregate
    directly, on its own口径, and that口径 is preserved unchanged.
    """
    if not isinstance(last_round_usage, dict):
        return
    _pt = int(last_round_usage.get("prompt_tokens") or 0)
    _eff = effective_prompt_tokens(
        last_round_usage,
        is_anthropic=is_anthropic_family(model_id),
        include_cache_write_fallback=True,
    )
    _delta: dict[str, Any] = (
        {**last_round_usage, "last_round_prompt_tokens": _eff}
        if _eff > _pt
        else last_round_usage
    )
    session.accumulate_usage(_delta, now=now)

    # PENDING-WORK #24 fix (2026-06-30): correct the cumulative
    # ``session.usage`` for cumulative-prompt families (Anthropic/Claude).
    # ``accumulate_usage`` is a provider-agnostic raw SUM of every integer key
    # (its domain contract + tests rely on that), so for Claude — which
    # RE-SENDS the full conversation each round, making a round's
    # ``prompt_tokens`` ALREADY the running wire size — summing it round over
    # round is quadratic (the same 10M-token bug the main agent's
    # ``_finalize_turn_usage`` corrects on the END frame). We fix it HERE, in
    # the application layer where ``model_id`` is known, using the SHARED pure
    # rule ``usage_math.finalize_cumulative_prompt_usage`` (single source of
    # truth with the main agent) — overriding ``session.usage`` cumulative
    # ``prompt_tokens`` / ``total_tokens`` with the last round's true wire size.
    # ``last_prompt_tokens`` (replace-last context badge) is set BY
    # ``accumulate_usage`` above and is NOT touched here. The domain
    # ``accumulate_usage`` raw-sum behaviour + its tests are unaffected (this
    # runs after it, only adjusting the already-folded cumulative dict).
    if is_anthropic_family(model_id) and isinstance(session.usage, dict):
        session.usage = finalize_cumulative_prompt_usage(
            session.usage,
            # The keystone last-round wire size: the eff-corrected prompt when
            # it exceeded the raw ``prompt_tokens`` (Anthropic cache split),
            # else the raw round prompt. Mirrors the ``last_prompt_tokens``
            # replace-last figure ``accumulate_usage`` just stored.
            {"prompt_tokens": _eff if _eff > _pt else _pt},
            is_cumulative=True,
        )


@lru_cache(maxsize=1)
def _find_repo_root() -> Path | None:
    """Locate repo root by STRUCTURE marker, never a fixed上溯层数.

    AGENTS.md State-Truth-First 铁律 4: resolve roots by a real structural
    marker (a directory holding BOTH ``src/`` and ``apps/``) rather than a
    brittle ``parents[N]`` assumption that breaks when the build CWD / package
    layout moves. Walks up from this module's location. Returns ``None`` when
    no such directory is found (caller degrades gracefully).
    """
    try:
        here = Path(__file__).resolve()
    except (OSError, ValueError):  # pragma: no cover - defensive
        return None
    for parent in (here, *here.parents):
        try:
            if (parent / "src").is_dir() and (parent / "apps").is_dir():
                return parent
        except OSError:  # pragma: no cover - defensive
            continue
    return None


def _tiktoken_encoding_name(model_id: str | None) -> str:
    """Pick a tiktoken encoding. ``cl100k_base`` is good enough for cloud口径.

    Per spec we do NOT try to precisely match every model — the trigger
    decision only needs an order-of-magnitude-accurate single-segment count,
    and ``cl100k_base`` is the vendored, offline-available BPE table.
    """
    return "cl100k_base"


@lru_cache(maxsize=4)
def _get_encoder(encoding_name: str) -> Any | None:
    """Module-cached tiktoken encoder, loaded OFFLINE from ``vendor/tiktoken``.

    Sets ``TIKTOKEN_CACHE_DIR`` to ``<repo_root>/vendor/tiktoken`` (resolved by
    structure marker) so ``tiktoken.get_encoding`` reads the vendored BPE files
    instead of hitting the network (offline → SSLError otherwise). Lazy import
    of tiktoken keeps module-load cost out of the hot path. Returns ``None`` on
    ANY failure (tiktoken missing / vocab not found / load error) — the caller
    then falls back to the ``len//2`` heuristic. Never raises.
    """
    try:
        repo_root = _find_repo_root()
        if repo_root is not None:
            cache_dir = repo_root / "vendor" / "tiktoken"
            if cache_dir.is_dir():
                # Only set when unset, so an operator override is respected.
                os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(cache_dir))
        import tiktoken  # noqa: PLC0415 — lazy: avoid module-load cost

        return tiktoken.get_encoding(encoding_name)
    except Exception:  # noqa: BLE001 — never break chat on token counting
        return None


def precise_text_tokens(text: str, model_id: str | None) -> int | None:
    """Exact BPE token count for a SINGLE text segment, or ``None``.

    Used by the presend trigger to precisely size a large new user message
    (``len(text) > 2000``) instead of the coarse ``len//2`` heuristic. Returns
    ``None`` when tiktoken / its vocab is unavailable so the caller can fall
    back. Wrapped entirely in try/except — must NEVER raise into the chat path.

    The encoder object is module-cached (:func:`_get_encoder` via lru_cache);
    only call this for genuinely large segments to amortise the encode cost.
    """
    if not text:
        return 0
    try:
        enc = _get_encoder(_tiktoken_encoding_name(model_id))
        if enc is None:
            return None
        return len(enc.encode(text))
    except Exception:  # noqa: BLE001 — never break chat on token counting
        return None


def append_display_usage_fields(
    usage: dict[str, Any],
    last_round_usage: dict[str, Any] | None,
    first_round_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tail-append the keystone last-round + DISPLAY-ONLY cache figures.

    SINGLE source of truth for the ``last_round_*`` tail keys that the token
    badge reads (↑new = input − cache_read − cache_write, i.e.
    ``Σ max(0, inputTokens − cacheRead − cacheWrite)``). The
    main agent's persisted-message path, its live END-frame path, AND the
    sub-agent per-round usage stamp all call this so the口径 can NEVER drift —
    the drift is exactly the "live ↑ shows the whole prompt (缓存未扣), reload ↑
    shows the adjusted value" / "sub-agent ↑ shows the full 4547" bug (a caller
    that omitted these fields → the front-end ``last_round_cache_*_display``
    fell back to 0 → Σ full prompt / no cache_write subtraction).

    口径 (byte-identical to the former inline block at the persist path):
      * ``last_round_prompt_tokens``       = last_round.prompt_tokens
      * ``last_round_cache_read_tokens``   = last_round.cache_read_tokens
        (the eff-prompt keystone — DELIBERATELY the possibly-ZEROED value;
        counter/billing math reads THIS, never the display field below)
      * ``last_round_cache_read_display``  = cache_read_observed
                                             ?? cache_read_tokens ?? 0
      * ``last_round_cache_write_display`` = cache_write_observed ?? 0
      * ``first_round_prompt_tokens``      = (first_round ?? last_round)
                                             .prompt_tokens
      * ``first_round_cache_read_display`` = (first_round ?? last_round)
                                             .cache_read_observed
                                             ?? .cache_read_tokens ?? 0
      * ``first_round_cache_write_display``= (first_round ?? last_round)
                                             .cache_write_observed ?? 0

    PER-ROUND ↑ FIX (main-agent 2-round turn "创建子Agent说hello" showing
    0/1 instead of ~4): the ↑new counter accumulates ``Σ max(0,
    input − cache_read − cache_write)`` PER-ROUND. Main-agent turns persist
    only ONE assistant message whose ``last_round_*`` bind to the FINAL
    round (Round 2 = cache-read hit → nets ~1), losing Round 1's net-new
    (write turn → nets ~3 = the user's sentence). Sub-agent stamps one
    message per round so first===last there. To let the front-end reproduce
    the per-round sum on ONE main-agent message we tail-append the
    first-round display figures too; the front-end sums firstNew + lastNew
    when first≠last (main agent multi-round) and only lastNew when
    first===last (single-round turn OR sub-agent per-round stamp — helper
    falls back to last_round when first_round_usage is None, keeping the
    two field sets byte-identical → front-end de-dup path activates).

    AGENTS.md §3.1: tail-only append (SHAPE unchanged, only adds keys). The
    display fields are DISPLAY-ONLY — counter/eff_prompt math never reads them,
    and ``cache_read_tokens`` stays whatever ``_extract_usage`` set (zeroed on a
    cache-hit turn to protect billing double-add). No-op when ``last_round_usage``
    is absent (returns ``usage`` unchanged) so legacy / no-usage turns keep the
    prior shape. Pure: reads only the passed dicts, no globals, no IO.
    """
    if not isinstance(last_round_usage, dict):
        return usage
    return {
        **usage,
        "last_round_prompt_tokens": int(
            last_round_usage.get("prompt_tokens") or 0
        ),
        "last_round_cache_read_tokens": int(
            last_round_usage.get("cache_read_tokens") or 0
        ),
        "last_round_cache_read_display": int(
            last_round_usage.get("cache_read_observed")
            or last_round_usage.get("cache_read_tokens")
            or 0
        ),
        "last_round_cache_write_display": int(
            last_round_usage.get("cache_write_observed") or 0
        ),
        "first_round_prompt_tokens": int(
            (first_round_usage or last_round_usage).get("prompt_tokens")
            or 0
        ),
        "first_round_cache_read_display": int(
            (first_round_usage or last_round_usage).get("cache_read_observed")
            or (first_round_usage or last_round_usage).get("cache_read_tokens")
            or 0
        ),
        "first_round_cache_write_display": int(
            (first_round_usage or last_round_usage).get("cache_write_observed")
            or 0
        ),
    }


#: Upper bound on how many provider readings the overhead reverse-computation
#: will examine before giving up (see
#: :func:`compute_overhead_from_last_wire_measurement`). The scan runs on
#: every compaction — a hot path — and a long conversation can carry hundreds
#: of assistant turns, so an unbounded walk would make compaction cost scale
#: with history length for no benefit: only the freshest few readings
#: plausibly describe the wire being sent now. Counts REJECTED readings only;
#: the first qualifying one returns immediately.
_OVERHEAD_SCAN_MAX_CANDIDATES = 8


def compute_overhead_from_last_wire_measurement(
    *,
    conv: Any,
    wire_estimate_before: int,
    wire_anchor_index: int | None,
) -> tuple[int, str]:
    """Reverse-compute the runtime wire overhead (badge口径 fold-in).

    The "compacted wire" the compaction engine sees is ONLY ``conv.messages``
    replayed as OpenAI wire dicts — it excludes the system prompt, the tool
    schemas the provider ingests as a separate payload field, the persona
    identity block and any skill instruction blocks the streaming path
    assembles per-turn. Those four extras (jointly ``overhead``) live outside
    the engine's scope but ARE part of the wire the provider actually
    measures. Without folding them in, ``estimate_compacted_tokens`` branch Y
    (bootstrap window before the first post-compact turn) returns the raw
    compacted-wire estimate (~82K in the diagnosed case) while branch X
    (provider-measured) returns ~147K → a 20-percentage-point badge jump.

    We reverse-compute ``overhead`` from the LAST assistant turn that ran on
    the SAME wire generation ``wire_estimate_before`` describes:

    ``overhead ≈ first_round_prompt_tokens - estimate_wire_tokens(that_wire)``

    ``wire_anchor_index`` is the anchor of the wire being measured — i.e. the
    anchor THIS compaction is about to write (``new_anchor_index``), NOT the
    prior checkpoint's. Every assistant at index < that anchor is an INPUT to
    this compaction, so its provider measurement describes the very wire
    ``wire_estimate_before`` estimates: same generation, same overhead.

    ERA MATCHING — WHY THIS MUST BE THE CURRENT ANCHOR (regression pin):
    passing the PRIOR checkpoint's anchor mixes two generations. The filter
    then stops one compaction short and selects an assistant whose
    ``last_round_prompt_tokens`` measured a wire that has since been
    compacted away, while ``wire_estimate_before`` describes the wire being
    sent NOW. The difference between those two generations is not overhead —
    it is the compaction gain — and folding it into
    ``checkpoint.estimated_tokens`` inflates the bootstrap figure. Diagnosed
    in the field: with ``wire_estimate_before=82887`` the prior anchor (229)
    selected the pre-compaction assistant (eff 136416) → overhead 53529 and a
    stashed estimate of 136416, while the current anchor (231) selects the
    same-generation assistant (eff 127882) → overhead 44995. The next turn's
    provider truth was 127897, so the badge numerator VISIBLY WENT BACKWARDS
    136416 → 127897 (8519 tokens) after the user sent one message. Using the
    current anchor lands within ~15 tokens of provider truth.

    A subtle failure mode this ordering also has to respect (docs §8.11):
    ``_last_assistant_with_usage(conv)`` without ANY filter can hit an
    assistant that already ran on a LATER (post-compact) wire — its
    ``last_round_prompt_tokens`` is much SMALLER than
    ``wire_estimate_before`` → negative overhead. The current anchor still
    excludes those: assistants at index >= the anchor are exactly the ones
    NOT folded into this compacted head, so the negative-overhead class stays
    filtered out. The four-layer guard:

    1. **anchor_prior** — an anchor was supplied: only consider assistants at
       index < ``wire_anchor_index`` (same wire generation as
       ``wire_estimate_before``). Name kept for log/diagnostic continuity.
    2. **first_compaction** — no anchor at all: any assistant is fine.
    3. **range check** — a candidate is rejected when ``overhead < 0`` or
       ``overhead * 2 >= first_round_pt`` (docs §8.11: overhead should not
       exceed half of the total measured wire).
    4. **fallback** — return ``(0, source)`` when no candidate survives; the
       badge falls back to the pre-fix口径 (jitter, but no worse than today).

    WHY LAYER 3 SELECTS INSTEAD OF ABORTING: the era filter narrows to one
    wire generation, but a generation can still contain readings that cannot
    describe THIS wire — a turn that errored early, a tool-only round, or (in
    the degenerate case where the anchor is ``len(messages)``) a turn whose
    measurement predates the messages now folded into the head. Aborting on
    the FIRST such reading threw away the perfectly good older ones behind
    it and collapsed to ``overhead = 0``. So we walk newest → oldest and take
    the first reading that is sanity-plausible: freshest wins, implausible
    readings are skipped rather than fatal. The ACCEPTANCE predicate is
    unchanged — Layer 3 decides what qualifies, this only decides how many
    candidates get asked.

    TWO INVARIANTS THIS SCAN MUST NEVER BREAK:

    * **Never cross the era boundary.** The scan is bounded above by
      ``wire_anchor_index``; when nothing inside that window qualifies we
      fall back honestly. Widening the window to "find something" would
      re-introduce exactly the cross-generation selection this function was
      fixed to prevent.
    * **Bounded work.** This runs on every compaction (hot path), and a long
      conversation can carry hundreds of assistants. At most
      ``_OVERHEAD_SCAN_MAX_CANDIDATES`` readings are examined; beyond that we
      fall back rather than walk the whole history. The freshest few readings
      are the only ones plausibly describing the current wire anyway, so a
      deeper walk would buy accuracy we do not have.

    Returns ``(overhead_tokens, source)`` where ``source`` is one of:
    ``anchor_prior``, ``first_compaction``, ``fallback_out_of_range``,
    ``fallback_no_usage``. ``overhead_tokens`` is always non-negative.
    """
    messages = getattr(conv, "messages", None)
    if not messages:
        return 0, "fallback_no_usage"

    # Layer 1: only assistants strictly BEFORE this wire's anchor — the same
    # generation ``wire_estimate_before`` measures (see ERA MATCHING above).
    # This upper bound is the era boundary and is NEVER widened.
    if wire_anchor_index is not None and wire_anchor_index > 0:
        upper = min(int(wire_anchor_index), len(messages))
        candidates = list(messages[:upper])
        source = "anchor_prior"
    else:
        # Layer 2: no anchor — first compaction. Any assistant with usage is
        # same-generation by definition.
        candidates = list(messages)
        source = "first_compaction"

    saw_usable_reading = False
    examined = 0
    # Newest → oldest: the freshest plausible measurement describes the wire
    # closest to the one being sent now (see WHY LAYER 3 SELECTS above).
    for m in reversed(candidates):
        if getattr(m, "role", None) is not MessageRole.ASSISTANT:
            continue
        usage = getattr(m, "usage", None) or {}
        # Audit §3.8 single-truth-source: pick the base the SAME way the
        # trigger gate + badge pick — via :func:`_read_provider_size_from_
        # usage`. Preferring ``first_round_prompt_tokens`` here matters
        # because A′'s subtraction is ``overhead = provider_wire -
        # wire_estimate_before``: ``wire_estimate_before`` is the BODY size
        # of the assembly (compacted head + AGED_tools + user), so the
        # provider figure must be the round-0 wire — the ONE that measured
        # THAT body plus overhead. ``last_round_prompt_tokens`` on a
        # multi-round agentic turn additionally carries the ROUND ≥ 1
        # in-flight tool outputs (not part of the compacted head, not
        # part of ``wire_estimate_before``), so the subtraction folds
        # that pollution INTO ``overhead`` — audit §3.8's regression
        # class. A degraded reading (legacy row, no first_round) is
        # skipped for the SAME reason ``_presend_eff_estimate`` returns 0
        # on a degraded reading (audit §3.5): a poisoned base plus this
        # subtraction re-injects the 30x class of numbers into
        # ``checkpoint.estimated_tokens``, corrupting the badge until the
        # next real compaction.
        reading = _read_provider_size_from_usage(
            usage,
            source_model_id=getattr(m, "model_id", None),
            # A′ does not know the CURRENT request's model; the cache
            # add-back must key on the SOURCE model_id alone (there is
            # no "current request" here — this is a history walk). Pass
            # ``None`` so the source_or_request fallback collapses to
            # source-only, preserving CCD-2 semantics.
            request_model_id=None,
        )
        if reading is None or reading.degraded:
            examined += 1
            if examined >= _OVERHEAD_SCAN_MAX_CANDIDATES:
                break
            continue
        first_round_pt = reading.base_size
        saw_usable_reading = True
        overhead = int(first_round_pt) - int(wire_estimate_before)
        # Layer 3: sanity range. Negative → this reading measured a SMALLER
        # wire than the one being sent, so it cannot carry this wire's
        # overhead. ``overhead * 2 >= first_round_pt`` → overhead cannot
        # plausibly be a majority of the total wire; the guess is wrong.
        # Either way: skip it and try the next-older reading.
        if overhead < 0 or overhead * 2 >= int(first_round_pt):
            examined += 1
            if examined >= _OVERHEAD_SCAN_MAX_CANDIDATES:
                break
            continue
        return overhead, source

    if not saw_usable_reading:
        return 0, "fallback_no_usage"
    return 0, "fallback_out_of_range"



__all__ = [
    "NON_TEXT_PART_BYTE_EQUIVALENT",
    "ProviderReading",
    "ProviderReadingSource",
    "_last_assistant_with_usage",
    "append_display_usage_fields",
    "assistant_eff_prompt",
    "authoritative_provider_size",
    "coarse_byte_estimate",
    "compute_overhead_from_last_wire_measurement",
    "effective_prompt_tokens",
    "is_anthropic_family",
    "non_text_content_bytes",
    "precise_text_tokens",
    "record_subagent_turn_usage",
]

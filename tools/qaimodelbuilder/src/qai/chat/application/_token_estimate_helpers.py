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
state the provider measured; the char-based estimate is only a fallback for
conversations that have never carried a provider usage block.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from qai.chat.domain.content import MessageRole
from qai.chat.domain.sub_agent_session import SubAgentSession
from qai.chat.domain.usage_math import finalize_cumulative_prompt_usage

# Char-per-token / per-message overhead constants. These mirror the values in
# ``qai.chat.adapters.context_size_estimator`` (lines 58/68/72) but are
# RE-DECLARED here on purpose: importing the adapter module into the
# application layer would break the layered-chat import-linter contract
# (application must not depend on adapters). They are only used by the coarse
# fallback estimate, which is intentionally approximate.
_UNKNOWN_MODEL_CHARS_PER_TOKEN = 3
_DEFAULT_CHARS_PER_TOKEN = 4
_MESSAGE_OVERHEAD = 4


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


def _chars_per_token(model_id: str | None) -> int:
    """Pick chars/token: 4 for a cloud model id, else 3 (unknown / local).

    Kept simple per spec: a non-empty model id that does NOT start with
    ``"local"`` is treated as a cloud model (≈4 chars/token); everything else
    (empty / ``None`` / ``local::*``) uses the conservative unknown-local ratio
    of 3 chars/token.
    """
    if model_id and not model_id.startswith("local"):
        return _DEFAULT_CHARS_PER_TOKEN
    return _UNKNOWN_MODEL_CHARS_PER_TOKEN


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


def coarse_char_estimate(conv: Any, model_id: str | None) -> int:
    """Coarse char-based prompt-token estimate over the full domain history.

    Walks ``conv.messages`` and sums the character length of each message's
    text content plus its tool-call payloads (tool name + JSON-encoded args +
    output — the parts replayed to the model), divides by the chars/token ratio
    and adds a fixed per-message envelope overhead. Mirrors
    ``_message_part_texts`` (``context_size_estimator.py:642-679``) but for the
    chat domain ``Message`` shape. Best-effort and never raises.

    This is the FALLBACK source used only when no assistant turn carries a
    provider ``usage`` block; otherwise the caller prefers the cloud truth.
    """
    messages = getattr(conv, "messages", None)
    if not messages:
        return 0
    chars_per_token = _chars_per_token(model_id)
    total_chars = 0
    message_count = 0
    for m in messages:
        message_count += 1
        try:
            total_chars += len(m.content.text)
        except Exception:  # noqa: BLE001 — never break the estimate
            pass
        for tc in getattr(m, "tool_calls", ()) or ():
            try:
                total_chars += len(str(tc.get("tool") or ""))
            except Exception:  # noqa: BLE001
                pass
            try:
                total_chars += len(
                    json.dumps(tc.get("args"), ensure_ascii=False),
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                total_chars += len(str(tc.get("output") or ""))
            except Exception:  # noqa: BLE001
                pass
    return int(total_chars / chars_per_token) + _MESSAGE_OVERHEAD * message_count


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


__all__ = [
    "_last_assistant_with_usage",
    "append_display_usage_fields",
    "assistant_eff_prompt",
    "coarse_char_estimate",
    "effective_prompt_tokens",
    "is_anthropic_family",
    "precise_text_tokens",
    "record_subagent_turn_usage",
]

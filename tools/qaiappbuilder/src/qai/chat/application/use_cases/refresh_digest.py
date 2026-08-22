# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Refresh the compaction checkpoint's session-digest asynchronously (P2).

CONTEXT-COMPRESSION-NEXT §6. A "session digest" is a structured Markdown
summary of the compacted head (the messages folded into
``CompactionCheckpoint.compacted_wire`` before every subsequent verbatim
increment). On every successful compaction the streaming use case fires this
task fire-and-forget; the use case:

1. Serialises the dropped-wire slice into a plain text ``<conversation>`` block.
2. Optionally wraps the prior digest into ``<previous-summary>`` (the
   incremental-update branch) and the freshly rendered ledger block into
   ``<additional-context>`` (both are pure text — the LLM never sees the wire
   shape).
3. Asks the LLM (via the same :class:`LLMStreamPort` the rest of the chat BC
   already goes through) for a NEW digest, budgeted via ``max_tokens`` on the
   API — never baked into the prompt (§6.5 explicit rule).
4. Persists the result onto the LATEST checkpoint via the write-through store,
   silently dropping the write if the conversation was deleted between the
   kick and the response (D3 semantics: no exception, no orphan row).

Cancellation (D8): the engine's ``kick_digest_refresh`` cancels a still-running
task when a NEW compaction fires; the newer task's inputs are strictly more
recent, so the older one is stale by construction. This use case therefore
lets :class:`asyncio.CancelledError` propagate — swallowing it would defeat the
"latest kick wins" contract.

Small-window degrade (D6): a compact context window (``< 32_000``) skips the
digest entirely — the wire is already short enough that the ledger + verbatim
increment cover the model's needs, and running a summariser inside a tiny
budget would blow through it. The engine's ``digest_enabled`` gate SHOULD keep
this branch from ever running for a small window, but the use case double-
checks so a mis-wired caller cannot regress the behaviour.

Layering (AGENTS.md §3.2): imports only ``domain`` + same-level ``application``
ports + platform logging. The concrete prompt bytes are injected at
construction time (:attr:`_prompts`) — the ``adapters/compaction_prompts/*.md``
files are read at the composition root (``_chat_di.py``) and passed in as a
plain ``dict[str, str]`` so this use case has NO filesystem coupling.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from collections.abc import Callable

from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrameType
from qai.platform.logging import get_logger

if TYPE_CHECKING:
    from qai.chat.application.ports import (
        CompactionCheckpointStorePort,
        ConversationRepositoryPort,
        LLMStreamPort,
    )
    from qai.chat.application.use_cases._agentic_kernel import (
        CompactionCheckpoint,
    )


__all__ = [
    "RefreshDigestInput",
    "RefreshDigestUseCase",
    # Prompt-dict keys the DI wiring MUST populate. Exposed so the composition
    # root can validate its own bundle before construction.
    "PROMPT_KEY_SUMMARIZATION_SYSTEM",
    "PROMPT_KEY_COMPACTION_SUMMARY",
    "PROMPT_KEY_COMPACTION_UPDATE_SUMMARY",
    "PROMPT_KEY_COMPACTION_TURN_PREFIX",
    "SMALL_WINDOW_THRESHOLD",
    "DIGEST_TOOL_RESULT_MAX_CHARS",
]


_log = get_logger(__name__)


# The digest is opt-in for models whose context is at least this many tokens.
# Below the threshold the ledger + verbatim increment already fit, and a
# recursive summariser would eat scarce budget for negligible gain. (§6, D6.)
SMALL_WINDOW_THRESHOLD: int = 32_000

# Reserve-token floor (§6.5 / D4): the LARGER of 15% of the window or 16 KiB
# of token budget, so tiny windows never round down to zero.
_RESERVE_TOKENS_FLOOR: int = 16_384
_RESERVE_TOKENS_FRACTION: float = 0.15

# Fraction of the reserve budget passed to the LLM as ``max_tokens``. Leaves
# ~20% headroom for the wrapper text (<conversation>...</conversation> +
# optional <previous-summary> and <additional-context>).
_MAX_TOKENS_FRACTION: float = 0.8

# Per-tool-result character cap applied to the DIGEST LLM's INPUT ONLY.
#
# A single tool result can run to tens of thousands of characters (a wide
# grep, a large read, a verbose exec). Feeding every one of them verbatim
# into the summariser pushed the digest request to 40-50K characters and made
# the background refresh take 21-40s — the dominant cost of the P2 latency.
#
# SCOPE (read this before touching the value): this cap governs the text
# handed to :func:`_serialize_dropped_wire`'s output, i.e. the
# ``<conversation>`` block of the digest prompt. It has NOTHING to do with
# the wire — the real history sent to the model is bounded by its own,
# independent mechanism and is never reshaped here. Lowering this number
# makes the digest cheaper/blurrier; it can never truncate what the model
# actually sees.
DIGEST_TOOL_RESULT_MAX_CHARS: int = 2_000

# Share of the cap kept from the HEAD of an oversized tool result; the
# remainder is kept from the TAIL. Head carries the call's context (what was
# searched / which file), tail carries the conclusion (the last rows, the
# error, the persisted-output footer) — the middle is the compressible part.
_DIGEST_TOOL_RESULT_HEAD_FRACTION: float = 0.6

# Markers a producing tool leaves behind when it bounded its own output and
# persisted the full body (``data/tool_results/``). These lines are the ONLY
# recovery path back to the untruncated text, so they are lifted out of the
# omitted middle and re-attached rather than dropped.
_DIGEST_RECOVERY_MARKERS: tuple[str, ...] = (
    "[full_output_saved]",
    "[truncated_in_memory]",
    "[truncation_advice]",
    "[truncation note]",
)

# Prompt-dict keys — single source of truth. The DI wiring reads the four
# files under ``src/qai/chat/adapters/compaction_prompts/`` and populates
# these keys; a missing key raises a KeyError at first refresh (fail-loud).
PROMPT_KEY_SUMMARIZATION_SYSTEM = "summarization_system"
PROMPT_KEY_COMPACTION_SUMMARY = "compaction_summary"
PROMPT_KEY_COMPACTION_UPDATE_SUMMARY = "compaction_update_summary"
PROMPT_KEY_COMPACTION_TURN_PREFIX = "compaction_turn_prefix"

# Sentinel prompts the ``LLMStreamPort`` requires but which never reach the
# wire — the real messages are handed in via ``extra["messages"]`` (the same
# pattern :class:`LlmIntentClassifier` / :class:`LlmFeatureItemExtractor`
# use). ``MessageContent`` rejects empty text.
_PROMPT_SENTINEL = MessageContent(text="(refresh-digest uses extra['messages'])")
_TAB_SENTINEL = TabId("refresh-digest")
_CONVERSATION_SENTINEL = ConversationId("refresh-digest")


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshDigestInput:
    """Inputs to :meth:`RefreshDigestUseCase.execute`.

    ``checkpoint_key`` is the ENGINE-side full key (``key_prefix + conv_key``)
    — the use case never rebuilds it, because the engine owns the prefix and
    could rename it. ``dropped_wire`` is the slice of the pre-compaction wire
    that got folded into the compacted head THIS turn (assembled_wire before
    the compressor ran); ``prev_digest`` is the LAST digest the checkpoint
    carried (``None`` on the first compaction). ``reference_ledger_block`` is
    already rendered by the ledger — the use case never re-renders it.
    """

    conversation_id: ConversationId
    checkpoint_key: str
    prev_digest: str | None
    dropped_wire: list[dict[str, Any]] = field(default_factory=list)
    reference_ledger_block: str | None = None
    model_id: str
    context_window: int


class RefreshDigestUseCase:
    """Refresh a conversation's session digest after a compaction.

    Fire-and-forget: :meth:`execute` catches every non-cancel exception so a
    bad LLM response / persistence blip never propagates out of the background
    task. :class:`asyncio.CancelledError` is intentionally re-raised so the
    engine's D8 "latest kick wins" contract is honoured.
    """

    __slots__ = (
        "_llm_stream",
        "_conversations",
        "_checkpoint_store",
        "_prompts",
        "_on_persisted",
    )

    def __init__(
        self,
        *,
        llm_stream: "LLMStreamPort",
        conversations: "ConversationRepositoryPort",
        checkpoint_store: "CompactionCheckpointStorePort",
        prompts: dict[str, str],
        on_persisted: (
            Callable[[str, str, str, int], bool] | None
        ) = None,
    ) -> None:
        self._llm_stream = llm_stream
        self._conversations = conversations
        self._checkpoint_store = checkpoint_store
        # Copy so the caller cannot mutate a live dict under our feet, and so
        # the fail-loud KeyError on execute surfaces the missing key with a
        # stable message. A missing prompt is a wiring bug, not a runtime
        # condition — surface it as a KeyError on the first refresh.
        self._prompts = dict(prompts)
        # Task R: after ``store.save_digest`` succeeds, notify the engine
        # so its cached frozen checkpoint's ``digest_text`` projection
        # tracks the newly-persisted fragment. The callback signature is
        # ``(checkpoint_key, digest_text, digest_updated_at, core_generation)``
        # — the engine checks ``core_generation`` against the currently
        # cached checkpoint's generation and refuses stale writes so a
        # peer ``invalidate`` / compaction cannot be resurrected by a
        # late-completing UC. ``None`` = tests that don't wire an engine.
        self._on_persisted = on_persisted

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    async def execute(self, input: RefreshDigestInput) -> None:
        """Generate and persist a refreshed digest for one compaction.

        See the module docstring for the full flow. All non-cancel exceptions
        are swallowed to structured debug logs; :class:`asyncio.CancelledError`
        propagates (D8).
        """
        try:
            await self._execute_inner(input)
        except Exception as exc:  # noqa: BLE001 — best-effort background task
            # ``CancelledError`` is BaseException (not Exception) and so
            # bypasses this handler, propagating to the task wrapper — that
            # is the intended D8 behaviour (a stale task must die on the next
            # kick, not silently persist an outdated digest).
            _log.warning(
                "chat.compaction.digest_refresh_failed",
                conversation_id=input.conversation_id.value,
                checkpoint_key=input.checkpoint_key,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # Core steps
    # ------------------------------------------------------------------
    async def _execute_inner(self, input: RefreshDigestInput) -> None:
        # D6: small windows skip the digest entirely. ``kick_digest_refresh``
        # SHOULD gate on ``digest_enabled`` first, but a mis-wired caller
        # cannot regress the contract because we double-check here.
        if input.context_window < SMALL_WINDOW_THRESHOLD:
            _log.debug(
                "chat.compaction.digest_refresh_skipped_small_window",
                conversation_id=input.conversation_id.value,
                context_window=input.context_window,
            )
            return

        reserve_tokens = max(
            int(_RESERVE_TOKENS_FRACTION * input.context_window),
            _RESERVE_TOKENS_FLOOR,
        )
        max_tokens = int(_MAX_TOKENS_FRACTION * reserve_tokens)

        # Serialise the dropped-wire slice into <conversation>...</conversation>
        # body text. The model never sees the raw wire dicts — a plain
        # ``role: content`` transcript is enough for the summariser, and it
        # avoids leaking tool-call ids / thought_signatures into the digest.
        _serialize_stats: dict[str, int] = {}
        conversation_body = _serialize_dropped_wire(
            input.dropped_wire, stats=_serialize_stats
        )
        # DIAG (per-tool-result digest-input truncation): the counters come
        # STRAIGHT out of the truncation code above, so "how much did the cap
        # actually buy in production" is answered without re-deriving the
        # threshold logic here (a second copy would silently drift the day the
        # real rule changes).
        _chars_before = _serialize_stats.get("tool_result_chars_before", 0)
        _chars_after = _serialize_stats.get("tool_result_chars_after", 0)
        _log.info(
            "chat.compaction.digest_input_truncation",
            conversation_id=input.conversation_id.value,
            dropped_message_count=len(input.dropped_wire),
            tool_result_count=_serialize_stats.get("tool_result_count", 0),
            tool_results_truncated_count=_serialize_stats.get(
                "tool_results_truncated_count", 0
            ),
            tool_result_chars_before=_chars_before,
            tool_result_chars_after=_chars_after,
            tool_result_chars_saved=_chars_before - _chars_after,
            recovery_lines_rescued=_serialize_stats.get(
                "recovery_lines_rescued", 0
            ),
            serialized_body_chars=len(conversation_body),
            max_chars_per_tool_result=DIGEST_TOOL_RESULT_MAX_CHARS,
        )

        digest = await self._summarise(
            conversation_body=conversation_body,
            prev_digest=input.prev_digest,
            reference_ledger_block=input.reference_ledger_block,
            model_id=input.model_id,
            max_tokens=max_tokens,
        )
        if digest is None:
            _log.debug(
                "chat.compaction.digest_refresh_empty",
                conversation_id=input.conversation_id.value,
            )
            return
        # Rough budget-overflow retry (§6.6 spirit — a compact form): if the
        # model handed back a text substantially larger than the budget it
        # was told to obey, re-summarise ONCE using the new digest as its own
        # ``previous-summary`` so the second pass compresses it. This is the
        # single-segment fallback for the general "input too large" case; a
        # multi-segment split is deferred to §6.6 proper.
        if _estimate_tokens(digest) > max_tokens * 2:
            _log.info(
                "chat.compaction.digest_refresh_oversize_retry",
                conversation_id=input.conversation_id.value,
                estimated_tokens=_estimate_tokens(digest),
                max_tokens=max_tokens,
            )
            retried = await self._summarise(
                conversation_body="",
                prev_digest=digest,
                reference_ledger_block=input.reference_ledger_block,
                model_id=input.model_id,
                max_tokens=max_tokens,
            )
            if retried is not None:
                digest = retried

        # Guard: the conversation may have been deleted between the kick and
        # the model's response — persisting the digest would resurrect a
        # ``chat_compaction_checkpoint`` row for a dead conversation (or
        # violate the FK). ``find`` returning ``None`` is the authoritative
        # "gone" signal; a persistence error is degraded to a warning above.
        conv = await self._conversations.find(input.conversation_id)
        if conv is None:
            _log.info(
                "chat.compaction.digest_refresh_conversation_deleted",
                conversation_id=input.conversation_id.value,
            )
            return

        # Read the LATEST checkpoint from the store so a concurrent compaction
        # that fired between the kick and now (rare — the engine's D8 cancel
        # SHOULD have killed us first — but not impossible under a slow
        # backend) has its state preserved. We only mutate the digest fields;
        # every other field on the row is overwritten with what we read.
        latest = await self._checkpoint_store.load(input.conversation_id)
        if latest is None:
            _log.info(
                "chat.compaction.digest_refresh_checkpoint_gone",
                conversation_id=input.conversation_id.value,
                checkpoint_key=input.checkpoint_key,
            )
            return
        core_generation = int(latest.generation)
        digest_updated_at = datetime.now(timezone.utc).isoformat()
        # Task R: FRAGMENT-scoped CAS write. The store refuses the write
        # when the core row's current generation no longer matches
        # ``core_generation`` (a peer ``maybe_compress`` advanced state,
        # or ``/compact clear`` dropped the core). A refusal is treated
        # as a stale-write no-op — no callback, no in-memory update.
        try:
            written = await self._checkpoint_store.save_digest(
                input.conversation_id,
                digest_text=digest,
                digest_updated_at=digest_updated_at,
                core_generation=core_generation,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort background task
            _log.warning(
                "chat.compaction.digest_refresh_persist_failed",
                conversation_id=input.conversation_id.value,
                checkpoint_key=input.checkpoint_key,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return
        if not written:
            _log.info(
                "chat.compaction.digest_refresh_cas_refused",
                conversation_id=input.conversation_id.value,
                checkpoint_key=input.checkpoint_key,
                core_generation=core_generation,
            )
            return
        # Push the fresh digest into the engine's in-memory cache so the
        # very next ``_inject_compaction_prefix_blocks`` reads it. The
        # engine independently CAS-checks ``core_generation`` against its
        # own cached checkpoint so a race between load-time and now cannot
        # resurrect a dropped checkpoint.
        if self._on_persisted is not None:
            try:
                self._on_persisted(
                    input.checkpoint_key,
                    digest,
                    digest_updated_at,
                    core_generation,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort callback
                _log.warning(
                    "chat.compaction.digest_on_persisted_failed",
                    conversation_id=input.conversation_id.value,
                    checkpoint_key=input.checkpoint_key,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        _log.info(
            "chat.compaction.digest_refresh_stored",
            conversation_id=input.conversation_id.value,
            checkpoint_key=input.checkpoint_key,
            digest_chars=len(digest),
            core_generation=core_generation,
        )

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------
    async def _summarise(
        self,
        *,
        conversation_body: str,
        prev_digest: str | None,
        reference_ledger_block: str | None,
        model_id: str,
        max_tokens: int,
    ) -> str | None:
        """Assemble the user prompt, run the LLM stream, return the text.

        Returns ``None`` on empty output / ERROR frame. ``max_tokens`` is
        passed via the request's ``extra`` so the adapter forwards it as the
        LLM API's own ``max_tokens`` parameter (§6.5: never baked into the
        prompt text).
        """
        # ---- System prompt (D) — the single guardrail. ----------------
        system_prompt = self._prompts[PROMPT_KEY_SUMMARIZATION_SYSTEM]

        # ---- Base prompt (A or B) — chosen by whether we have a prior. ---
        if prev_digest:
            base_prompt = self._prompts[PROMPT_KEY_COMPACTION_UPDATE_SUMMARY]
        else:
            base_prompt = self._prompts[PROMPT_KEY_COMPACTION_SUMMARY]

        # ---- Assemble the wrapped user prompt body (§6.5 ordering). -----
        parts: list[str] = []
        if conversation_body:
            parts.append("<conversation>")
            parts.append(conversation_body)
            parts.append("</conversation>")
        if prev_digest:
            parts.append("")
            parts.append("<previous-summary>")
            parts.append(prev_digest)
            parts.append("</previous-summary>")
        if reference_ledger_block:
            parts.append("")
            parts.append("<additional-context>")
            parts.append(reference_ledger_block)
            parts.append("</additional-context>")
        parts.append("")
        parts.append(base_prompt)
        user_body = "\n".join(parts)

        wire = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_body},
        ]

        # Deferred import to keep application-layer decoupled from adapters.
        from qai.chat.application.ports import LLMStreamRequest

        request = LLMStreamRequest(
            conversation_id=_CONVERSATION_SENTINEL,
            tab_id=_TAB_SENTINEL,
            prompt=_PROMPT_SENTINEL,
            history=(),
            model_hint=model_id,
            # ``extra["messages"]`` is the same override the intent
            # classifier / feature-item extractor use — the adapter sends
            # this list verbatim as the OpenAI ``messages`` array, ignoring
            # the sentinel ``prompt`` / empty ``history``. ``max_tokens``
            # rides through as a top-level payload override.
            extra={"messages": wire, "max_tokens": int(max_tokens)},
        )
        parts_out: list[str] = []
        # Diagnostic (P2 digest never persisted in the field): record the
        # resolved model hint + budget BEFORE the call so a provider-routing
        # miss (empty / unknown model_hint) is visible even when the adapter
        # degrades to a silent offline reply instead of an ERROR frame.
        _log.info(
            "chat.compaction.digest_summarise_start",
            model_hint=model_id,
            max_tokens=int(max_tokens),
            system_chars=len(system_prompt),
            user_chars=len(user_body),
            has_prev_digest=bool(prev_digest),
        )
        _frames = 0
        _err: str | None = None
        # Wall-clock of the digest LLM call only (prompt assembly above is
        # pure string work). Proves whether the digest-input cap moved this
        # from the tens-of-seconds range into single-digit seconds.
        _t0 = time.perf_counter()
        try:
            async for frame in self._llm_stream.stream(request):
                _frames += 1
                if frame.frame_type is StreamFrameType.CHUNK:
                    text = frame.payload.get("text", "")
                    if isinstance(text, str) and text:
                        parts_out.append(text)
                elif frame.frame_type is StreamFrameType.ERROR:
                    _err = str(frame.payload.get("message", ""))
                    _log.warning(
                        "chat.compaction.digest_refresh_stream_error",
                        model_hint=model_id,
                        message=_err,
                        code=frame.payload.get("code"),
                        elapsed_ms=int((time.perf_counter() - _t0) * 1000),
                    )
                    return None
                elif frame.frame_type is StreamFrameType.END:
                    break
        except Exception as exc:
            # The adapter may raise instead of emitting an ERROR frame (auth
            # failure, unroutable provider, transport error). Surface it —
            # the caller's ``except Exception`` would otherwise log a generic
            # ``digest_refresh_failed`` without the model context.
            _log.warning(
                "chat.compaction.digest_summarise_raised",
                model_hint=model_id,
                error=str(exc),
                error_type=type(exc).__name__,
                frames_seen=_frames,
                elapsed_ms=int((time.perf_counter() - _t0) * 1000),
            )
            return None
        text_out = "".join(parts_out).strip()
        _log.info(
            "chat.compaction.digest_summarise_done",
            model_hint=model_id,
            frames_seen=_frames,
            chunks=len(parts_out),
            text_chars=len(text_out),
            elapsed_ms=int((time.perf_counter() - _t0) * 1000),
        )
        return text_out or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _serialize_dropped_wire(
    dropped_wire: list[dict[str, Any]],
    *,
    stats: dict[str, int] | None = None,
) -> str:
    """Turn a wire-message list into a plain ``role: content`` transcript.

    Best-effort: unrecognised roles / non-string content collapse to their
    JSON dump so the summariser still sees the data. Empty ``content`` on an
    ``assistant`` message with ``tool_calls`` gets a synthetic
    ``[tool_calls: ...]`` line so the summariser knows the model made a
    tool call at that point (the ids / thought_signatures are stripped —
    the digest never carries them).

    ``stats`` (optional, observation-only): when a dict is supplied, the
    per-tool-result truncation counters are accumulated into it. The numbers
    are produced BY the truncation code itself (not re-derived from a second
    copy of the threshold logic), so they can never drift from the behaviour
    they describe. Omitting it — the default — leaves this function's output
    byte-for-byte unchanged and costs nothing.
    """
    lines: list[str] = []
    for msg in dropped_wire:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        raw = msg.get("content")
        if isinstance(raw, str):
            content = raw
        elif raw is None:
            content = ""
        else:
            try:
                content = json.dumps(raw, ensure_ascii=False)
            except (TypeError, ValueError):
                content = str(raw)
        # Digest-input-only guard: one oversized tool result (a wide grep, a
        # large read, a chatty exec) used to push the summariser request to
        # 40-50K chars single-handedly. Bound it HERE, where the digest's
        # ``<conversation>`` body is built. The wire is structurally out of
        # reach: this function only ever reads ``dropped_wire`` and emits NEW
        # strings, so the real history the model sees is unaffected.
        if role == "tool":
            content = _truncate_tool_result_for_digest(content, stats=stats)
        # Assistant tool_calls: surface the function names + args so the
        # summariser knows what the model DID, not just what it said.
        suffix = _render_tool_call_names(role, msg.get("tool_calls"))
        if suffix:
            content = f"{content}\n{suffix}" if content else suffix
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _render_tool_call_names(role: str, tool_calls: Any) -> str:
    """Render an assistant message's ``tool_calls`` as one ``[tool_calls: ...]`` line.

    Returns ``""`` for anything that is not an assistant message carrying a
    non-empty list of well-formed call dicts. Only ``function.name`` /
    ``function.arguments`` are read — ids and thought_signatures never reach
    the digest.
    """
    if role != "assistant" or not isinstance(tool_calls, list) or not tool_calls:
        return ""
    names: list[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if not isinstance(fn, dict):
            continue
        name = fn.get("name") or ""
        if not name:
            continue
        args = fn.get("arguments") or ""
        names.append(f"{name}({args})" if args else f"{name}()")
    if not names:
        return ""
    return "[tool_calls: " + "; ".join(names) + "]"


def _extract_recovery_lines(text: str) -> list[str]:
    """Return the de-duplicated recovery-hint lines found in ``text``.

    A producing tool that bounded its own output leaves a footer naming the
    persisted body (``[full_output_saved] path=...``) plus the ``read(path=...)``
    advice. Those lines are the summariser's only pointer back to the full
    text, so :func:`_truncate_tool_result_for_digest` must never let them fall
    into the omitted middle.
    """
    found: list[str] = []
    for line in text.splitlines():
        if any(marker in line for marker in _DIGEST_RECOVERY_MARKERS):
            stripped = line.strip()
            if stripped and stripped not in found:
                found.append(stripped)
    return found


def _truncate_tool_result_for_digest(
    text: str,
    *,
    max_chars: int = DIGEST_TOOL_RESULT_MAX_CHARS,
    stats: dict[str, int] | None = None,
) -> str:
    """Bound ONE tool result for the digest prompt, keeping head + tail.

    Contract:

    * ``len(text) <= max_chars`` → returned verbatim (no marker, no mutation).
    * Otherwise the result is ``head + omission marker + [recovery hints] +
      tail`` and is itself ``<= max_chars`` characters. Head keeps the call's
      context (what was searched, which file), tail keeps the conclusion (last
      rows, error text, the persisted-output footer); the middle is the part
      a summary can afford to lose.
    * Any recovery-hint line (:data:`_DIGEST_RECOVERY_MARKERS`) that would
      have landed in the omitted middle is lifted out and re-attached next to
      the marker — a digest that cannot name the persisted full output is
      worse than one that loses a few thousand characters of body.

    Digest-input only; see :data:`DIGEST_TOOL_RESULT_MAX_CHARS`.

    ``stats`` (optional, observation-only): accumulates
    ``tool_result_count`` / ``tool_results_truncated_count`` /
    ``tool_result_chars_before`` / ``tool_result_chars_after`` /
    ``recovery_lines_rescued`` as the REAL decisions are taken, so the
    reported effect can never diverge from the applied one. Never read back
    by this function — it influences no branch.
    """
    total = len(text)
    if stats is not None:
        stats["tool_result_count"] = stats.get("tool_result_count", 0) + 1
        stats["tool_result_chars_before"] = (
            stats.get("tool_result_chars_before", 0) + total
        )
    if total <= max_chars:
        if stats is not None:
            stats["tool_result_chars_after"] = (
                stats.get("tool_result_chars_after", 0) + total
            )
        return text

    def _marker(omitted: int) -> str:
        return (
            f"\n... [digest input truncated: omitted {omitted:,} of "
            f"{total:,} chars] ...\n"
        )

    # Worst-case marker width (``omitted`` can never exceed ``total``, so this
    # is an upper bound on the real marker) — reserved up front so the final
    # string honours ``max_chars``.
    reserved = len(_marker(total))
    recovery_cap = max_chars // 4

    def _split(budget: int) -> tuple[str, str]:
        body = max(budget, 0)
        head_len = int(body * _DIGEST_TOOL_RESULT_HEAD_FRACTION)
        return text[:head_len], text[total - (body - head_len):] if body else ""

    head, tail = _split(max_chars - reserved)
    # Only hints that survived neither end need re-attaching. The common case
    # (footer at the very end of the body) keeps them in ``tail`` for free and
    # spends nothing.
    missing = [
        line
        for line in _extract_recovery_lines(text)
        if line not in head and line not in tail
    ]
    if missing:
        block = "\n".join(missing)[:recovery_cap]
        head, tail = _split(max_chars - reserved - len(block) - 1)
        recovery = f"{block}\n"
    else:
        recovery = ""

    omitted = total - len(head) - len(tail)
    out = f"{head}{_marker(omitted)}{recovery}{tail}"
    # Defensive clamp: re-splitting for the recovery block can shift which
    # hints are present, so trim the tail rather than trust the arithmetic.
    excess = len(out) - max_chars
    if excess > 0:
        tail = tail[excess:]
        omitted = total - len(head) - len(tail)
        out = f"{head}{_marker(omitted)}{recovery}{tail}"
    if stats is not None:
        stats["tool_results_truncated_count"] = (
            stats.get("tool_results_truncated_count", 0) + 1
        )
        stats["tool_result_chars_after"] = (
            stats.get("tool_result_chars_after", 0) + len(out)
        )
        if missing:
            stats["recovery_lines_rescued"] = (
                stats.get("recovery_lines_rescued", 0) + len(missing)
            )
    return out


def _estimate_tokens(text: str) -> int:
    """Rough UTF-8 bytes/4 token estimate (matches ``_agentic_kernel.estimate_wire_tokens``)."""
    return max(0, len(text.encode("utf-8")) // 4)

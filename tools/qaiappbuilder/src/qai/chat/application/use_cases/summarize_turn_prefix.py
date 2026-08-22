# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Summarise a long-turn PREFIX asynchronously (P3, CONTEXT-COMPRESSION-NEXT §7.1).

A "long turn" is a single conversation turn whose rolling wire (the assistant's
tool-call rounds + their ``role:tool`` replies) crosses a configurable
occupancy threshold WITHIN a single turn. Compaction (P0–P2) already handles
cross-turn history summarisation; the turn-prefix summary handles the OTHER
axis — a single turn that itself blows past the protection budget because the
model kept dispatching tools inside it.

Contract (§7.1):

* Trigger point: ``streaming.py._compress_via_checkpoint`` fires this
  side-band after a successful compaction when the freshly-dropped wire
  contains a "long turn" (rolling tokens > ``0.6 × PROTECT_WINDOW_RATIO ×
  context_limit``). The trigger is intentionally hitched to the compaction
  code path rather than in-round: it inherits the compaction gate's small-
  window degrade and reuses the same measured / estimated token口径 that
  drives compaction itself. A future phase MAY move the trigger to an
  inter-round hook once the accounting is threaded there — the use case is
  agnostic to WHERE it is kicked from.
* Prefix boundary: the caller (``streaming.py``) picks the atomic-group
  boundary via :func:`_split_atomic_groups` so an ``assistant{tool_calls}``
  and its paired ``role:tool`` replies NEVER get separated. The prefix wire
  handed here is already sliced; the use case never re-cuts it.
* Storage: at most THREE summaries per checkpoint, FIFO cap. Each entry
  carries the ``user_message_id`` of the summarised turn so the wire-
  injection reader can address them by turn, and a ``created_at`` ISO-8601
  timestamp for diagnostics. Persistence rides on migration 062's
  ``turn_prefix_summaries_json`` column (opaque TEXT).
* Fire-and-forget: :meth:`execute` catches every non-cancel exception so a
  bad LLM response / persistence blip never propagates out of the background
  task. :class:`asyncio.CancelledError` is intentionally re-raised so the
  engine's "latest kick wins" contract is honoured.
* Small-window degrade: reuses the SAME
  :data:`qai.chat.application.use_cases.refresh_digest.SMALL_WINDOW_THRESHOLD`
  gate P2 uses (32k tokens). Below the threshold both gates close — a tiny
  window has no budget for a side-band summariser.
* Budget: the LLM call rides on the same reserve-token口径 as the digest
  refresh (§6.5 / D4), with a smaller ``max_tokens`` fraction (0.5 vs 0.8)
  because a turn-prefix summary should be terser than a full session digest.
* Oversize retry: when the model hands back a summary substantially larger
  than the budget (>2×), re-summarise ONCE using the oversize text as its
  own conversation body so the second pass compresses it (same
  single-segment fallback the digest use case uses).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from collections.abc import Callable

from qai.chat.application.use_cases.refresh_digest import (
    SMALL_WINDOW_THRESHOLD,
)
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
    "SummarizeTurnPrefixInput",
    "SummarizeTurnPrefixUseCase",
    "append_turn_prefix_summary",
    "TURN_PREFIX_MAX_ENTRIES",
]


_log = get_logger(__name__)


# FIFO cap on the number of turn-prefix summaries a checkpoint retains
# (§7.1). Three is a compromise: enough to cover the working-set span of
# multi-turn tool cascades, small enough that the checkpoint row stays
# bounded regardless of how tool-heavy a conversation gets.
TURN_PREFIX_MAX_ENTRIES: int = 3

# Reserve-token口径 mirrors the digest refresh (§6.5 / D4): the LARGER of
# 15% of the window or 16 KiB of budget so tiny windows never round to zero.
_RESERVE_TOKENS_FLOOR: int = 16_384
_RESERVE_TOKENS_FRACTION: float = 0.15

# Fraction of the reserve budget passed to the LLM as ``max_tokens``. Smaller
# than the digest refresh (0.5 vs 0.8) — a turn-prefix summary is per-turn
# and should stay terser than the full session digest.
_MAX_TOKENS_FRACTION: float = 0.5

# Sentinel prompts the ``LLMStreamPort`` requires but which never reach the
# wire — the real messages are handed in via ``extra["messages"]``. Matches
# the digest refresh use case's pattern.
_PROMPT_SENTINEL = MessageContent(text="(summarize-turn-prefix uses extra['messages'])")
_TAB_SENTINEL = TabId("summarize-turn-prefix")
_CONVERSATION_SENTINEL = ConversationId("summarize-turn-prefix")


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class SummarizeTurnPrefixInput:
    """Inputs to :meth:`SummarizeTurnPrefixUseCase.execute`.

    ``checkpoint_key`` is the engine-side full key (``key_prefix + conv_key``);
    the use case never rebuilds it. ``prefix_wire`` is the already-sliced
    prefix (atomic-group boundary observed) — the use case never re-cuts it.
    ``user_message_id`` associates the summary with the specific turn so the
    wire-injection reader can address entries by turn.
    """

    conversation_id: ConversationId
    checkpoint_key: str
    user_message_id: str
    prefix_wire: list[dict[str, Any]] = field(default_factory=list)
    model_id: str
    context_window: int


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------
class SummarizeTurnPrefixUseCase:
    """Generate and persist one turn-prefix summary asynchronously (P3).

    Follows the same lifecycle as :class:`RefreshDigestUseCase`:

    * :meth:`execute` swallows every non-cancel exception (background task
      must never break the caller's turn);
    * :class:`asyncio.CancelledError` is intentionally re-raised so the
      engine's per-key cancel-and-replace contract is honoured;
    * the LLM call rides on the ``extra["messages"]`` override pattern so
      the adapter forwards our composed wire verbatim (no chat-history
      concatenation).
    """

    __slots__ = (
        "_llm_stream",
        "_conversations",
        "_checkpoint_store",
        "_turn_prefix_prompt",
        "_system_prompt",
        "_on_persisted",
    )

    def __init__(
        self,
        *,
        llm_stream: "LLMStreamPort",
        conversations: "ConversationRepositoryPort",
        checkpoint_store: "CompactionCheckpointStorePort",
        turn_prefix_prompt: str,
        system_prompt: str,
        on_persisted: (
            Callable[[str, str, int], bool] | None
        ) = None,
    ) -> None:
        self._llm_stream = llm_stream
        self._conversations = conversations
        self._checkpoint_store = checkpoint_store
        # Both prompts are captured by value — the DI wiring reads the
        # Markdown files once at startup, so passing the loaded strings here
        # keeps the use case decoupled from the filesystem.
        self._turn_prefix_prompt = turn_prefix_prompt
        self._system_prompt = system_prompt
        # Task R: after ``store.save_turn_prefix`` succeeds, notify the
        # engine so its cached frozen checkpoint's turn-prefix projection
        # tracks the newly-persisted fragment. Callback signature is
        # ``(checkpoint_key, summaries_json, core_generation)``; the
        # engine CAS-checks ``core_generation`` against the cached
        # checkpoint's generation and refuses stale writes. Mirrors
        # ``RefreshDigestUseCase``.
        self._on_persisted = on_persisted

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    async def execute(self, input: SummarizeTurnPrefixInput) -> None:
        """Generate and persist a turn-prefix summary (best-effort)."""
        try:
            await self._execute_inner(input)
        except Exception as exc:  # noqa: BLE001 — best-effort background task
            _log.warning(
                "chat.compaction.turn_prefix_summary_failed",
                conversation_id=input.conversation_id.value,
                checkpoint_key=input.checkpoint_key,
                user_message_id=input.user_message_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # Core steps
    # ------------------------------------------------------------------
    async def _execute_inner(self, input: SummarizeTurnPrefixInput) -> None:
        # Small-window degrade — mirrors :class:`RefreshDigestUseCase`.
        if input.context_window < SMALL_WINDOW_THRESHOLD:
            _log.debug(
                "chat.compaction.turn_prefix_summary_skipped_small_window",
                conversation_id=input.conversation_id.value,
                context_window=input.context_window,
            )
            return

        reserve_tokens = max(
            int(_RESERVE_TOKENS_FRACTION * input.context_window),
            _RESERVE_TOKENS_FLOOR,
        )
        max_tokens = int(_MAX_TOKENS_FRACTION * reserve_tokens)

        # Serialise the atomic-group-safe prefix wire into a plain
        # ``role: content`` transcript so the model never sees raw
        # tool_call ids / thought_signatures. Matches the digest use
        # case's serialisation口径 for consistency.
        prefix_body = _serialize_prefix_wire(input.prefix_wire)

        summary = await self._summarise(
            prefix_body=prefix_body,
            model_id=input.model_id,
            max_tokens=max_tokens,
        )
        if summary is None:
            _log.debug(
                "chat.compaction.turn_prefix_summary_empty",
                conversation_id=input.conversation_id.value,
                user_message_id=input.user_message_id,
            )
            return
        # Oversize retry (§6.6 spirit): re-summarise ONCE feeding the oversize
        # text back in as its own "prefix" so the second pass compresses it.
        if _estimate_tokens(summary) > max_tokens * 2:
            _log.info(
                "chat.compaction.turn_prefix_summary_oversize_retry",
                conversation_id=input.conversation_id.value,
                user_message_id=input.user_message_id,
                estimated_tokens=_estimate_tokens(summary),
                max_tokens=max_tokens,
            )
            retried = await self._summarise(
                prefix_body=summary,
                model_id=input.model_id,
                max_tokens=max_tokens,
            )
            if retried is not None:
                summary = retried

        # Conversation may have been deleted between the kick and the model's
        # response — persisting the summary would resurrect a checkpoint row
        # for a dead conversation (or violate the FK). ``find`` returning
        # ``None`` is the authoritative "gone" signal.
        conv = await self._conversations.find(input.conversation_id)
        if conv is None:
            _log.info(
                "chat.compaction.turn_prefix_summary_conversation_deleted",
                conversation_id=input.conversation_id.value,
            )
            return

        # Read the LATEST checkpoint from the store so a concurrent compaction
        # that fired between the kick and now has its state preserved. We only
        # mutate ``turn_prefix_summaries_json``; every other field is
        # overwritten with what we just read.
        latest = await self._checkpoint_store.load(input.conversation_id)
        if latest is None:
            _log.info(
                "chat.compaction.turn_prefix_summary_checkpoint_gone",
                conversation_id=input.conversation_id.value,
                checkpoint_key=input.checkpoint_key,
            )
            return
        core_generation = int(latest.generation)
        created_at = datetime.now(timezone.utc).isoformat()
        # Task R: fold the fresh summary into the existing fragment
        # payload (FIFO cap at 3), then FRAGMENT-scoped CAS-write. Store
        # refusal (peer advanced core / dropped checkpoint) is a stale
        # no-op — no callback fires.
        merged_summaries_json = append_turn_prefix_summary(
            latest.turn_prefix_summaries_json,
            user_message_id=input.user_message_id,
            summary_text=summary,
            created_at=created_at,
        )
        try:
            written = await self._checkpoint_store.save_turn_prefix(
                input.conversation_id,
                summaries_json=merged_summaries_json,
                core_generation=core_generation,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort background task
            _log.warning(
                "chat.compaction.turn_prefix_summary_persist_failed",
                conversation_id=input.conversation_id.value,
                checkpoint_key=input.checkpoint_key,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return
        if not written:
            _log.info(
                "chat.compaction.turn_prefix_summary_cas_refused",
                conversation_id=input.conversation_id.value,
                checkpoint_key=input.checkpoint_key,
                core_generation=core_generation,
            )
            return
        if self._on_persisted is not None:
            try:
                self._on_persisted(
                    input.checkpoint_key,
                    merged_summaries_json,
                    core_generation,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort callback
                _log.warning(
                    "chat.compaction.turn_prefix_on_persisted_failed",
                    conversation_id=input.conversation_id.value,
                    checkpoint_key=input.checkpoint_key,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        _log.info(
            "chat.compaction.turn_prefix_summary_stored",
            conversation_id=input.conversation_id.value,
            checkpoint_key=input.checkpoint_key,
            user_message_id=input.user_message_id,
            summary_chars=len(summary),
            core_generation=core_generation,
        )

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------
    async def _summarise(
        self,
        *,
        prefix_body: str,
        model_id: str,
        max_tokens: int,
    ) -> str | None:
        """Assemble the user prompt, run the LLM stream, return the text.

        Returns ``None`` on empty output / ERROR frame. ``max_tokens`` is
        passed via the request's ``extra`` so the adapter forwards it as the
        LLM API's own ``max_tokens`` parameter.
        """
        parts: list[str] = []
        if prefix_body:
            parts.append("<turn-prefix>")
            parts.append(prefix_body)
            parts.append("</turn-prefix>")
            parts.append("")
        parts.append(self._turn_prefix_prompt)
        user_body = "\n".join(parts)

        wire = [
            {"role": "system", "content": self._system_prompt},
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
            # Same ``extra["messages"]`` override the intent classifier /
            # digest refresh use — the adapter sends this list verbatim
            # as the OpenAI ``messages`` array.
            extra={"messages": wire, "max_tokens": int(max_tokens)},
        )
        parts_out: list[str] = []
        async for frame in self._llm_stream.stream(request):
            if frame.frame_type is StreamFrameType.CHUNK:
                text = frame.payload.get("text", "")
                if isinstance(text, str) and text:
                    parts_out.append(text)
            elif frame.frame_type is StreamFrameType.ERROR:
                _log.warning(
                    "chat.compaction.turn_prefix_summary_stream_error",
                    message=frame.payload.get("message", ""),
                )
                return None
            elif frame.frame_type is StreamFrameType.END:
                break
        text_out = "".join(parts_out).strip()
        return text_out or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def append_turn_prefix_summary(
    existing_json: str | None,
    *,
    user_message_id: str,
    summary_text: str,
    created_at: str,
) -> str:
    """Append a summary to the checkpoint's turn-prefix list; FIFO cap at 3.

    Returns the fresh JSON payload (never ``None``). Malformed / missing prior
    payloads are treated as an empty list — the caller lost the prior history
    but the fresh entry still lands. The list is stored oldest-first; when the
    cap is exceeded the OLDEST entries are dropped so the newest three
    survive (FIFO — first in, first out).
    """
    entries: list[dict[str, Any]]
    if existing_json is None or not existing_json.strip():
        entries = []
    else:
        try:
            parsed = json.loads(existing_json)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            entries = [e for e in parsed if isinstance(e, dict)]
        else:
            entries = []
    entries.append(
        {
            "user_message_id": user_message_id,
            "summary_text": summary_text,
            "created_at": created_at,
        }
    )
    if len(entries) > TURN_PREFIX_MAX_ENTRIES:
        # Drop the OLDEST first so the newest ``TURN_PREFIX_MAX_ENTRIES``
        # survive. Slice-assignment keeps insertion order.
        entries = entries[-TURN_PREFIX_MAX_ENTRIES:]
    return json.dumps(entries, ensure_ascii=False)


def _serialize_prefix_wire(prefix_wire: list[dict[str, Any]]) -> str:
    """Render a wire-message list as a plain ``role: content`` transcript.

    Mirrors :func:`refresh_digest._serialize_dropped_wire` — kept as a local
    helper (rather than importing the private helper) so a future divergence
    in either summariser's serialisation contract is cheap. Tool-call blocks
    are rendered as a synthetic ``tool_call`` role line so the model sees the
    turn's structure (which tool got called with which args) without the raw
    ``tool_call_id`` / ``thought_signature`` fields that never round-trip.
    """
    lines: list[str] = []
    for msg in prefix_wire:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "unknown")
        content = msg.get("content")
        text: str
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Multimodal: concatenate text parts and mark non-text ones.
            fragments: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    if isinstance(part.get("text"), str):
                        fragments.append(part["text"])
                    elif "image_url" in part:
                        fragments.append("[image]")
                    else:
                        fragments.append("[non-text]")
                else:
                    fragments.append(str(part))
            text = " ".join(fragments)
        elif content is None:
            text = ""
        else:
            text = str(content)
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = fn.get("name") if isinstance(fn, dict) else None
                args = fn.get("arguments") if isinstance(fn, dict) else None
                lines.append(
                    f"tool_call: {name or 'tool'}({args or ''})"
                )
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    """Rough UTF-8 bytes/4 token estimate (matches ``estimate_wire_tokens``)."""
    return max(0, len(text.encode("utf-8")) // 4)

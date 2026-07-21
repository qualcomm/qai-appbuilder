# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Stream-frame value objects for the chat bounded context.

The :class:`StreamFrame` value object is the **wire-format-agnostic**
unit of data emitted by :class:`StreamChatUseCase`.  Adapters (SSE / WS
/ buffered HTTP) are responsible for serialising frames; the domain
itself only knows about the typed payload.

Why a domain-level VO instead of leaning on framework primitives?

* Use-case logic must be testable without a fastapi / sse_starlette
  dependency in scope.
* Multiple presentation layers (SSE today, WebSocket later -- see
  refactor-plan §10.4) must share the same payload contract.
* ``ai_coding`` may eventually want to reuse the type; isolating it as
  a small VO with no transport ties keeps that promotion cheap.

Each frame carries:

* ``frame_type`` -- routing tag.  See :class:`StreamFrameType`.
* ``sequence`` -- monotonically increasing 0-based index within a single
  stream.  Lets clients detect drops / re-orders.
* ``frame_id`` -- opaque unique id (typically minted by an
  :class:`~qai.platform.ids.IdGenerator`).
* ``payload`` -- frame-specific dict (chunk text, tool call args,
  error code, ...).  Required to be a dict so adapters can JSON-encode
  it without further introspection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from qai.chat.domain.error_disposition import retry_disposition_for


class StreamFrameType(str, Enum):
    """Routing tag for a :class:`StreamFrame`."""

    CHUNK = "chunk"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_MODE_CHANGED = "tool_mode_changed"
    TURN_WARNING = "turn_warning"
    ERROR = "error"
    END = "end"
    # ── Sub-agent event family (V1 chat_handler.py:2204-2343 parity) ──────
    # Emitted when the LLM dispatches the ``agent`` tool: each spawned
    # sub-agent produces a sequence of these frames so the UI can render
    # an in-progress sub-agent block in the assistant bubble (same DOM as
    # V1 useChat.js:1345-1408 / index.html:629-669).  ``index`` (0-based)
    # discriminates parallel sub-agents inside one parent turn — the LLM
    # may emit multiple ``agent`` tool_calls in one round and they run
    # concurrently via ``asyncio.gather`` (V1 chat_handler.py:642-647).
    # AGENTS.md §3.1: tail-only enum growth — existing values unchanged.
    SUBAGENT_START = "subagent_start"
    SUBAGENT_OUTPUT = "subagent_output"
    SUBAGENT_TOOL = "subagent_tool"
    # ``SUBAGENT_TOOL_RESULT`` (V2 enhancement, §3.1 tail-only enum growth) is
    # the sub-agent counterpart of the parent agent's ``TOOL_RESULT`` frame.
    # The sub-agent loop emits one per tool call AFTER it executes, so the UI
    # can render a structured, collapsible result panel under the matching
    # ``subagent_tool`` row — exactly like a main-agent tool card — instead of
    # the model re-narrating the raw tool output as plain assistant text (the
    # "大段大段文本" defect). Neither V1 nor the earlier V2 emitted a sub-agent
    # result event (the result went only into the next-round wire), which is
    # what let the model echo tool output verbatim. Existing values unchanged.
    SUBAGENT_TOOL_RESULT = "subagent_tool_result"
    SUBAGENT_DONE = "subagent_done"
    SUBAGENT_ERROR = "subagent_error"
    AGENT_SUMMARY = "agent_summary"
    # ── Multi-agent discussion (docs/70-multi-agent/multi-agent-conversation-design.md
    # §7) ─────────────────────────────────────────────────────────────────
    # Emitted by the discussion orchestrator each time the floor passes to a
    # new named-agent speaker, BEFORE that speaker's first chunk/tool frame.
    # Carries the new ``sender_id`` (+ display name / model) so the UI can
    # soft-reset the live streaming buffer and start a fresh bubble attributed
    # to that participant (front-end mirrors ``handleAgentSummary``). Absent in
    # ordinary single-assistant chat. AGENTS.md §3.1: tail-only enum growth —
    # existing values unchanged.
    SPEAKER_CHANGED = "speaker_changed"
    # ── DISC-1 implementation orchestration (§22.9 additive control-plane
    # frames) ───────────────────────────────────────────────────────────────
    # Emitted ONLY by the planned-implementation serial runner
    # (``OrchestrateDiscussionUseCase._run_planned_implementation`` /
    # ``_run_implementation_control``), which is reached ONLY behind the
    # OFF-by-default ``implementation_enabled`` flag + a persisted
    # ``planned``/``paused`` plan.  They give the front end a STRUCTURED progress
    # feed for the implementation run (which item is being worked, item
    # done/failed/skipped, run-phase transitions) WITHOUT having to infer it from
    # the chunk/tool stream.  Payloads carry ONLY a control-plane summary (§22.9 —
    # never the full tool output / diff; that stays in the message system).
    # AGENTS.md §3.1: tail-only enum growth — existing values unchanged; absent
    # in ordinary discussion / single-assistant chat (flag OFF ⇒ unreachable).
    PLAN_READY = "plan_ready"
    IMPLEMENTATION_ITEM_STARTED = "implementation_item_started"
    IMPLEMENTATION_ITEM_FINISHED = "implementation_item_finished"
    IMPLEMENTATION_PHASE_CHANGED = "implementation_phase_changed"
    # ── Reasoning / "thinking" stream (additive capability) ────────────────
    # Emitted for the model's *thinking* tokens (distinct from the visible
    # answer ``CHUNK`` text). Two producers feed it: (1) OpenAI-compatible
    # cloud reasoning models stream ``delta.reasoning_content`` (DeepSeek-R1 /
    # QwQ / ...), which the cloud adapter previously DISCARDED (counted-only,
    # ``llm_stream.py``) — now surfaced; (2) the internal-only query-service
    # adapter maps the upstream ``reasoning_content`` (after stripping
    # service-internal progress/DEBUG log noise) to this frame. The UI renders
    # it in a collapsible "思考中…" block ABOVE the answer bubble, separate
    # from the ``CHUNK`` answer text so it can be folded / hidden
    # independently. Payload mirrors ``CHUNK`` (``{"text", round_index?,
    # sender_id?}``). Absent for non-reasoning models / non-query-service
    # turns ⇒ the wire stays byte-for-byte unchanged for existing consumers.
    # AGENTS.md §3.1: tail-only enum growth — existing values unchanged; WS /
    # SSE adapters blind-forward it (no enum whitelist), so no transport edit.
    REASONING = "reasoning"
    # ── Mid-turn user injection (V2 enhancement) ───────────────────────────
    # Emitted when the user's "inject" button content is folded into the SAME
    # in-flight run at the inter-round seam (the gap between finishing one tool
    # round and opening the next LLM round). The use case appends the text to
    # the wire as a ``role:user`` message, persists it to the conversation, and
    # emits THIS frame so the frontend can flip the pending "queued/grey"
    # injection bubble into a committed user message (and drop its local
    # pending/queue fallback). Payload: ``{"text", "message_id"?, round_index?}``
    # — ``message_id`` is the persisted :class:`MessageId` so the client pairs
    # its optimistic grey bubble to the real one. Absent in every turn with no
    # mid-run injection ⇒ the wire stays byte-for-byte unchanged for existing
    # consumers. AGENTS.md §3.1: tail-only enum growth — existing values
    # unchanged; WS / SSE adapters blind-forward it (no enum whitelist), so no
    # transport edit is needed.
    INJECTED_MESSAGE = "injected_message"
    # ── Context-compaction progress (V2 enhancement) ───────────────────────
    # Emitted by the use case around the pre-send / mid-turn context-compaction
    # step when it takes long enough for the user to notice (compaction is
    # usually ~100ms pure-trim, but a turn that also runs the Level 2 LLM
    # summary can take seconds). It lets the UI surface a transient "compressing
    # context…" status while the model call is delayed by compaction, then
    # clear it. Payload: ``{"state": "compressing" | "done", "message"?: str}``
    # — ``state`` discriminates the start vs end of the visible compaction; the
    # optional ``message`` carries a pre-rendered status line (the client may
    # also render its own i18n string keyed off ``state``). Absent on every turn
    # where compaction is fast / does not trigger ⇒ the wire stays
    # byte-for-byte unchanged for existing consumers. AGENTS.md §3.1: tail-only
    # enum growth — existing values unchanged; WS / SSE adapters blind-forward
    # it (no enum whitelist), so no transport edit is needed.
    COMPACTION_PROGRESS = "compaction_progress"
    # ── Turn-internal context-usage refresh (V2 enhancement) ───────────────
    # Emitted by the agentic follow-up loop at each ROUND boundary inside a
    # SINGLE turn, carrying the round-just-completed's PROVIDER-MEASURED wire
    # prompt size (``real_prompt_tokens`` — State-Truth-First, AGENTS.md 铁律 1,
    # NOT an estimate) + the model's context window. A long multi-round tool
    # turn grows the real wire from e.g. 33K → 70K, but the main-conversation
    # context badge previously only refreshed at the turn boundary / when the
    # frontend re-fetched ``GET /context`` — so the user saw the badge frozen
    # at the prior turn's value while a long turn ran. This frame gives the
    # frontend an immediate, per-round live reading so the main badge tracks
    # the real growth WHILE the turn runs; the turn-boundary ``GET /context``
    # remains the authoritative fallback that corrects/overrides the live value
    # (State-Truth-First 铁律 3: optimistic instant feedback + probe override).
    # This is the main-agent counterpart of the sub-agent's per-round
    # ``used_tokens``/``context_limit`` live refresh. Payload:
    # ``{"used_tokens": int, "context_limit": int}``. Emitted ONLY when the
    # round produced a real measured prompt size (> 0); a round with no usage
    # (local model / no measurement) emits NOTHING (no estimate, no regression),
    # so the wire stays byte-for-byte unchanged for turns/providers that produce
    # no per-round measurement. AGENTS.md §3.1: tail-only enum growth — existing
    # values unchanged; WS / SSE adapters blind-forward it (no enum whitelist),
    # so no transport edit is needed.
    CONTEXT_USAGE = "context_usage"
    # ── Network auto-retry progress (V2 enhancement) ───────────────────────
    # Emitted by the use case BEFORE each transient-network-error backoff wait
    # (adapter codes ``chat.llm.connect_error`` / ``timeout`` / ``read_error``
    # / ``network_error``). The backend retries such failures indefinitely with
    # an escalating capped backoff (3s → 5s → 10s → 30s …) until the link
    # recovers or the user aborts; WITHOUT this frame the turn stays silently
    # in "streaming" for up to 30s per attempt with no user feedback (the
    # reported "执行完工具就停止了 / 没有任何提示" experience). This frame lets
    # the UI surface a transient "网络中断，正在等待恢复后自动重试 (N)…" banner
    # (driving the existing ``networkRetry`` tab state on BOTH the WS and SSE
    # paths) and doubles as a positive keep-alive during long gaps. Payload:
    # ``{"attempt": int, "delay_seconds": float, "message"?: str,
    # "code"?: str}`` — ``attempt`` is 1-based; ``delay_seconds`` is the wait
    # about to elapse before the next attempt. It is NON-terminal: a successful
    # retry then streams normal CHUNK/END frames, and the client clears the
    # banner on the next non-retry frame. Absent on every turn with no network
    # failure ⇒ the wire stays byte-for-byte unchanged for existing consumers.
    # AGENTS.md §3.1: tail-only enum growth — existing values unchanged; WS /
    # SSE adapters blind-forward it (no enum whitelist), so no transport edit
    # is needed.
    NETWORK_RETRY = "network_retry"


@dataclass(frozen=True, slots=True)
class StreamFrame:
    """A single frame emitted by a streaming chat use case.

    Domain rules:

    * ``sequence`` must be >= 0.
    * ``frame_id`` must be a non-empty string.
    * ``payload`` must be a ``dict`` (possibly empty).  Frames whose
      semantics carry no extra data (e.g. ``END`` with default reason)
      are still required to provide ``payload={}`` for serialisation
      uniformity.
    """

    frame_id: str
    frame_type: StreamFrameType
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.frame_id, str) or not self.frame_id:
            raise ValueError("frame_id must be a non-empty string")
        if not isinstance(self.frame_type, StreamFrameType):
            raise TypeError(
                f"frame_type must be StreamFrameType, got "
                f"{type(self.frame_type).__name__}",
            )
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise TypeError(
                f"sequence must be int, got {type(self.sequence).__name__}",
            )
        if self.sequence < 0:
            raise ValueError(f"sequence must be >= 0, got {self.sequence}")
        if not isinstance(self.payload, dict):
            raise TypeError(
                f"payload must be dict, got {type(self.payload).__name__}",
            )

    def with_round_index(self, round_index: int) -> StreamFrame:
        """Return a copy of this frame stamped with ``round_index``.

        ``round_index`` (0-based) is the **agentic-loop round** the frame
        belongs to — the index of the LLM call (0-based) that produced
        the text / issued the tool call, or, for a ``tool_result`` frame,
        the round of the call it answers (result and call share a round).

        Why a copy-with-stamp helper instead of threading ``round_index``
        through every nested producer generator? :class:`StreamFrame` is
        the wire-format-agnostic unit minted *deep* inside the adapters
        (``adapters/local_model_stream.py`` / ``infrastructure/llm_stream.py``)
        which have **no notion of the agentic loop** — only the application
        use case (``application/use_cases/streaming.py``) knows the round
        structure (initial LLM call = round 0, each follow-up stream = the
        next round). Stamping at the single use-case forwarding boundary
        keeps the round authority in one place (zero inference downstream:
        the frontend groups strictly by ``round_index``) without polluting
        the transport-agnostic adapters with loop bookkeeping.

        AGENTS.md §3.1: ``round_index`` is an **optional appended field**
        (payloads only grow at the tail; existing keys untouched), so the
        wire frame stays byte-for-byte backward compatible — consumers
        that ignore it are unaffected, and persistence falls back to the
        prior ``lead_in`` boundary heuristic when it is absent (old data).

        ``round_index`` must be a non-negative ``int``.
        """
        if not isinstance(round_index, int) or isinstance(round_index, bool):
            raise TypeError(
                f"round_index must be int, got {type(round_index).__name__}",
            )
        if round_index < 0:
            raise ValueError(f"round_index must be >= 0, got {round_index}")
        new_payload = dict(self.payload)
        new_payload["round_index"] = round_index
        return StreamFrame(
            frame_id=self.frame_id,
            frame_type=self.frame_type,
            sequence=self.sequence,
            payload=new_payload,
        )

    @classmethod
    def chunk(
        cls,
        *,
        frame_id: str,
        sequence: int,
        text: str,
        round_index: int | None = None,
        sender_id: str | None = None,
    ) -> StreamFrame:
        """Convenience factory for a textual chunk frame.

        ``round_index`` is an **optional appended field** (AGENTS.md §3.1 —
        tail-only growth): the 0-based agentic-loop round (= index of the
        LLM call that produced this text). Only written into the payload
        when non-``None`` so the wire frame stays byte-for-byte compatible
        for callers that do not stamp it.

        ``sender_id`` is another **optional appended field** (§3.1): the
        :class:`ParticipantId` of the named agent speaking this chunk in a
        multi-agent discussion. Absent for ordinary single-assistant chat ⇒
        byte-for-byte unchanged.
        """
        payload: dict[str, Any] = {"text": text}
        if round_index is not None:
            payload["round_index"] = round_index
        if sender_id is not None:
            payload["sender_id"] = sender_id
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.CHUNK,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def reasoning(
        cls,
        *,
        frame_id: str,
        sequence: int,
        text: str,
        round_index: int | None = None,
        sender_id: str | None = None,
    ) -> StreamFrame:
        """Convenience factory for a reasoning ("thinking") frame.

        Payload mirrors :meth:`chunk` exactly (``{"text", round_index?,
        sender_id?}``) — the only difference is the ``frame_type``
        (:attr:`StreamFrameType.REASONING`), so the UI can route the model's
        thinking tokens into a collapsible block separate from the visible
        answer text. ``round_index`` / ``sender_id`` are the same optional
        appended fields as on :meth:`chunk` (AGENTS.md §3.1 — tail-only
        growth); only written when non-``None`` so the wire frame stays
        byte-for-byte compatible for callers that do not stamp them.
        """
        payload: dict[str, Any] = {"text": text}
        if round_index is not None:
            payload["round_index"] = round_index
        if sender_id is not None:
            payload["sender_id"] = sender_id
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.REASONING,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def injected_message(
        cls,
        *,
        frame_id: str,
        sequence: int,
        text: str,
        message_id: str | None = None,
        round_index: int | None = None,
    ) -> StreamFrame:
        """Convenience factory for a mid-turn user-injection frame.

        Emitted when the user's "inject" button content is folded into the
        SAME in-flight run at the inter-round seam (V2 enhancement). The use
        case has already appended ``text`` to the wire as a ``role:user``
        message and persisted it; this frame tells the frontend to commit its
        pending grey injection bubble into a real user message.

        ``message_id`` is an **optional appended field** (AGENTS.md §3.1 —
        tail-only growth): the persisted :class:`MessageId` so the client can
        pair its optimistic local bubble to the persisted one (and drop the
        local pending/queue fallback). ``round_index`` is the same optional
        appended field as on :meth:`chunk` (the round the injection landed
        before). Only written when non-``None`` so the wire frame stays
        byte-for-byte compatible for callers that do not stamp them.

        Image parity: an injection carries its image(s) the same way a normal
        submit does — as ``![](url)`` markdown inlined in ``text`` — so no
        separate media field is needed here; the committed bubble renders the
        markdown image and the backend already resolved the refs to vision
        blocks for the model.
        """
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a non-empty string")
        payload: dict[str, Any] = {"text": text}
        if message_id is not None:
            payload["message_id"] = message_id
        if round_index is not None:
            payload["round_index"] = int(round_index)
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.INJECTED_MESSAGE,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def compaction_progress(
        cls,
        *,
        frame_id: str,
        sequence: int,
        state: str,
        message: str | None = None,
    ) -> StreamFrame:
        """Factory for a ``compaction_progress`` status frame.

        Emitted around a context-compaction step that is slow enough for the
        user to notice (the Level 2 LLM summary can take seconds), so the UI can
        show a transient "compressing context…" indicator and then clear it.

        ``state`` is required and must be ``"compressing"`` (compaction is in
        progress, show the indicator) or ``"done"`` (compaction finished, clear
        the indicator). ``message`` is an **optional appended field** (AGENTS.md
        §3.1 — tail-only growth) carrying a pre-rendered status line; when
        absent the client renders its own i18n string keyed off ``state``.
        """
        if state not in ("compressing", "done"):
            raise ValueError(
                f"state must be 'compressing' or 'done', got {state!r}"
            )
        payload: dict[str, Any] = {"state": state}
        if message is not None:
            if not isinstance(message, str):
                raise TypeError(
                    f"message must be str, got {type(message).__name__}"
                )
            payload["message"] = message
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.COMPACTION_PROGRESS,
            sequence=sequence,
            payload=payload,
        )


    @classmethod
    def context_usage(
        cls,
        *,
        frame_id: str,
        sequence: int,
        used_tokens: int,
        context_limit: int,
    ) -> StreamFrame:
        """Factory for a ``context_usage`` turn-internal live-refresh frame.

        Emitted at each agentic ROUND boundary inside a single turn, carrying
        the round-just-completed's PROVIDER-MEASURED wire prompt size so the
        main-conversation context badge can refresh per round WHILE a long
        multi-round tool turn runs (the real wire grows e.g. 33K → 70K), instead
        of staying frozen at the prior turn's value until the turn-boundary
        ``GET /context`` re-fetch. State-Truth-First (AGENTS.md 铁律 1):
        ``used_tokens`` is the real measurement (``_CompletedRound.
        real_prompt_tokens``), never an estimate; the caller MUST NOT emit this
        frame when the round produced no measured prompt size (no estimate, no
        regression). The turn-boundary ``GET /context`` remains the
        authoritative override (铁律 3: optimistic feedback + probe correction).

        ``used_tokens`` (required, >= 0): the provider-measured prompt size of
        the wire the model actually saw this round. ``context_limit``
        (required, > 0): the model's context window, so the frontend can render
        the same "~used / window · pct%" badge it gets from ``GET /context``
        (no client-side window guessing).
        """
        if not isinstance(used_tokens, int) or isinstance(used_tokens, bool):
            raise TypeError(
                f"used_tokens must be int, got {type(used_tokens).__name__}"
            )
        if used_tokens < 0:
            raise ValueError(f"used_tokens must be >= 0, got {used_tokens}")
        if not isinstance(context_limit, int) or isinstance(context_limit, bool):
            raise TypeError(
                f"context_limit must be int, got "
                f"{type(context_limit).__name__}"
            )
        if context_limit <= 0:
            raise ValueError(
                f"context_limit must be > 0, got {context_limit}"
            )
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.CONTEXT_USAGE,
            sequence=sequence,
            payload={
                "used_tokens": used_tokens,
                "context_limit": context_limit,
            },
        )


    @classmethod
    def network_retry(
        cls,
        *,
        frame_id: str,
        sequence: int,
        attempt: int,
        delay_seconds: float,
        message: str | None = None,
        code: str | None = None,
    ) -> StreamFrame:
        """Factory for a ``network_retry`` progress frame.

        Emitted just BEFORE a transient-network-error backoff wait so the UI
        can show a "网络中断，正在等待恢复后自动重试 (N)…" banner and stays
        informed that the (otherwise silent) turn is recovering rather than
        stuck. Non-terminal: a successful retry resumes normal CHUNK/END
        frames and the client clears the banner on the next non-retry frame.

        ``attempt`` (required, >= 1): 1-based retry ordinal about to run.
        ``delay_seconds`` (required, >= 0): the wait about to elapse before
        that attempt. ``message`` / ``code`` are optional appended fields
        (AGENTS.md §3.1 tail-only growth): a pre-rendered status line and the
        originating adapter error code (e.g. ``chat.llm.connect_error``).
        """
        if not isinstance(attempt, int) or isinstance(attempt, bool):
            raise TypeError(
                f"attempt must be int, got {type(attempt).__name__}"
            )
        if attempt < 1:
            raise ValueError(f"attempt must be >= 1, got {attempt}")
        if not isinstance(delay_seconds, (int, float)) or isinstance(
            delay_seconds, bool
        ):
            raise TypeError(
                f"delay_seconds must be a number, got "
                f"{type(delay_seconds).__name__}"
            )
        if delay_seconds < 0:
            raise ValueError(
                f"delay_seconds must be >= 0, got {delay_seconds}"
            )
        payload: dict[str, Any] = {
            "attempt": attempt,
            "delay_seconds": float(delay_seconds),
        }
        if message is not None:
            if not isinstance(message, str):
                raise TypeError(
                    f"message must be str, got {type(message).__name__}"
                )
            payload["message"] = message
        if code is not None:
            if not isinstance(code, str):
                raise TypeError(
                    f"code must be str, got {type(code).__name__}"
                )
            payload["code"] = code
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.NETWORK_RETRY,
            sequence=sequence,
            payload=payload,
        )


    @classmethod
    def tool_call(
        cls,
        *,
        frame_id: str,
        sequence: int,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str | None = None,
        thought_signature: str | None = None,
        lead_in: str | None = None,
        round_index: int | None = None,
        generation_ms: int | None = None,
        sender_id: str | None = None,
    ) -> StreamFrame:
        # ``tool_call_id`` is an **optional appended field** (AGENTS.md §3.1 —
        # payloads may only grow at the tail). It carries the upstream
        # OpenAI ``tool_calls[].id`` so the agentic follow-up loop can rebuild
        # the standard ``assistant{tool_calls}`` + paired ``role:tool`` wire
        # messages (the model must see its own call+result pairing every
        # round, or it "forgets" and ends the turn early — V1 parity with
        # chat_handler.py:791-860). Absent ⇒ a stable id is synthesised
        # downstream from the frame_id.
        #
        # ``thought_signature`` is another **optional appended field**:
        # Vertex AI thinking models (via an OpenAI-compatible proxy) stream a
        # ``thought_signature`` on tool_call deltas that the NEXT turn must
        # echo back verbatim, or the API returns 400 ("content block is
        # missing a thought_signature"). V1 transparently forwards it
        # (chat_handler.py:677-679/787-789/1514-1516). Carry it through the
        # frame so the follow-up loop can re-attach it to the rebuilt
        # ``assistant.tool_calls[i].thought_signature`` (V1 fidelity instead
        # of the lossy flatten-to-text fallback).
        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "arguments": dict(arguments),
        }
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id
        if thought_signature:
            payload["thought_signature"] = thought_signature
        # ``lead_in`` is another **optional appended field**: the assistant text
        # the model emitted in THIS round *before* the tool call (its "lead-in",
        # e.g. "Phase 8: 推理 …"). It is also streamed as ordinary ``chunk``
        # frames, but carrying it on the tool_call frame too lets the UI commit
        # the round's lead-in as a standalone, permanently-visible assistant
        # message even if the preceding chunk frame(s) had not yet been applied
        # to the live buffer when the tool_call arrived — so the text never
        # "disappears the instant the tool runs" (V1 keeps each round's lead-in
        # visible; useChat.js:2460-2470). The UI uses the live buffer when
        # present and falls back to this field otherwise (no double-render).
        if lead_in:
            payload["lead_in"] = lead_in
        # ``round_index`` (optional appended field, §3.1): the 0-based
        # agentic-loop round whose LLM call issued this tool call.
        if round_index is not None:
            payload["round_index"] = round_index
        # ``generation_ms`` (optional appended field, §3.1): wall-clock time the
        # model spent STREAMING this tool call's arguments (from the first
        # ``generating_args`` progress frame to the moment the assembled
        # TOOL_CALL is drained).  A long tool call (e.g. a big code block as a
        # write/edit argument) can take tens of seconds to generate while the
        # tool's actual execution (``duration_ms`` on the tool_result) is only a
        # few ms.  Persisting this lets the UI show a tool's TOTAL time
        # (generation + execution) in BOTH live and history modes, instead of
        # the misleading execution-only ``duration_ms`` (e.g. "11ms" for a
        # 20s-to-generate write).  Absent on tool calls with no preceding
        # generation phase (short args that arrived in one delta).
        if generation_ms is not None:
            payload["generation_ms"] = generation_ms
        # ``sender_id`` (optional appended field, §3.1): the ParticipantId of
        # the named agent issuing this tool call in a multi-agent discussion.
        # Absent for ordinary single-assistant chat.
        if sender_id is not None:
            payload["sender_id"] = sender_id
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.TOOL_CALL,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def tool_result(
        cls,
        *,
        frame_id: str,
        sequence: int,
        tool_name: str,
        result: Any,
        size: int | None = None,
        truncated: bool | None = None,
        partial: bool | None = None,
        delta: str | None = None,
        tool_call_id: str | None = None,
        round_index: int | None = None,
        duration_ms: int | None = None,
        phase: str | None = None,
        sender_id: str | None = None,
        cancelled: bool | None = None,
    ) -> StreamFrame:
        """Factory for a ``tool_result`` frame.

        ``size`` / ``truncated`` are **optional appended fields**
        (AGENTS.md §3.1 — payloads may only grow at the tail; the
        existing ``{tool_name, result}`` shape is unchanged). They let
        the chat UI render the original-output size badge and a
        "truncated" warning (V1 ToolExecPanel.js:148-189) without the
        client having to guess from the truncated text:

        * ``size`` — the *original* (pre-truncation) length in
          characters of the raw tool output. Sourced from
          ``ToolResultTruncationResult.original_length`` so the badge
          reflects the true output size even when ``result`` carries the
          head+tail summary.
        * ``truncated`` — whether the adaptive truncator shortened the
          output. Drives the "已截断" badge + head/tail view tabs.

        ``partial`` / ``delta`` are the **WIRE-tools streaming pair**
        (also §3.1 tail-only appended).  They let one logical tool call
        emit several ``tool_result`` frames as the (exec) tool produces
        output in real time — V1 ``backend/tools/_exec.py:1010`` streamed
        ``{type:"output"}`` frames before the final ``{type:"done"}`` so
        the WebUI (``useChat.js:1041``) appended stdout/stderr lines live
        instead of waiting for the whole ``communicate()`` buffer.  We do
        **not** add a new :class:`StreamFrameType` enum value (the 13-value
        enum is locked, §3.1): the existing ``tool_result`` type is reused
        and the ``partial`` flag discriminates increments from the final:

        * ``partial=True`` (+ ``delta`` carrying the new stdout/stderr
          chunk) — an *incremental* frame.  ``result`` for these frames is
          the same ``delta`` text (so consumers ignoring ``partial`` still
          see content), but UI consumers append ``delta`` to a running
          buffer and keep the tool block in the "running" state.
        * ``partial=False`` / ``None`` — the *final* frame (the existing
          shape): ``result`` carries the head+tail summary, ``size`` /
          ``truncated`` the original-output metadata.  Only this frame
          feeds ``tool_results`` back to the LLM.

        All four are only added to the payload when explicitly provided
        (non-``None``); omitting them keeps the wire frame byte-for-byte
        identical to the pre-PR shape so existing consumers are
        unaffected.

        ``tool_call_id`` is another **optional appended field** (§3.1
        tail-only): the originating TOOL_CALL frame's id, carried back on
        the *final* tool_result frame so persistence can pair each result
        to its call by **id** (V1 ``useChat.js:2444/2576`` — assistant
        ``tool_calls[i].id`` == tool message ``tool_call_id``) instead of
        by positional index, which mis-pairs once a streaming (exec) tool
        emits several partial frames before its final one.
        """
        payload: dict[str, Any] = {"tool_name": tool_name, "result": result}
        if size is not None:
            payload["size"] = size
        if truncated is not None:
            payload["truncated"] = truncated
        if partial is not None:
            payload["partial"] = partial
        if delta is not None:
            payload["delta"] = delta
        if tool_call_id is not None:
            payload["tool_call_id"] = tool_call_id
        # ``round_index`` (optional appended field, §3.1): the 0-based
        # agentic-loop round of the tool *call* this result answers (the
        # result shares its call's round — V1 ``HTTP request == round``).
        if round_index is not None:
            payload["round_index"] = round_index
        # ``duration_ms`` (optional appended field, §3.1): the tool's
        # wall-clock execution time in milliseconds, measured by the use case
        # around the ``ToolInvocationPort.invoke`` call. Carried on the FINAL
        # frame so persistence + UI can show "this tool took N ms" in BOTH live
        # and history modes (a V2 enhancement over V1, which only timed the
        # live tool_indicator and never persisted the duration — its history
        # cards fell back to a near-useless wall-clock timestamp). The UI
        # formats it (``Nms`` / ``N.Ns``) like V1's live ``elapsedText()``.
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        # ``phase`` (optional appended field, §3.1 tail-only): discriminates a
        # tool_result frame that is NOT a real tool execution result but a
        # *progress* signal emitted WHILE the model is still streaming the
        # tool-call arguments (``phase="generating_args"``).  A long tool call
        # (e.g. a big code block as a write/edit argument) is accumulated
        # silently by the cloud SSE adapter and previously produced ZERO UI
        # feedback until the whole argument was assembled — the card only
        # appeared at the end, so the user saw "frozen".  These partial frames
        # (``partial=True`` + ``delta`` carrying the new argument fragment) let
        # the UI surface the tool card early in a "generating arguments" state
        # with a live char-count / typewriter effect, proving the model is
        # working.  ``phase`` lets the client tell these apart from a real
        # (exec-tool) streaming ``tool_result`` so it does NOT feed them back to
        # the LLM.  Absent on every existing frame → byte-for-byte unchanged.
        if phase is not None:
            payload["phase"] = phase
        # ``sender_id`` (optional appended field, §3.1): the ParticipantId of
        # the named agent whose tool call this result answers in a multi-agent
        # discussion. Absent for ordinary single-assistant chat.
        if sender_id is not None:
            payload["sender_id"] = sender_id
        # ``cancelled`` (optional appended field, §3.1 tail-only): marks a
        # terminal tool_result synthesized because the user cancelled THIS one
        # tool (per-call stop) — as opposed to a normal result or a whole-turn
        # abort. The turn continues; this result IS fed back to the model
        # (``ok=False`` on the paired ``tool_results`` dict). The UI renders the
        # card in a distinct "已取消/cancelled" state. Absent on every existing
        # frame → byte-for-byte unchanged for current consumers.
        if cancelled is not None:
            payload["cancelled"] = cancelled
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.TOOL_RESULT,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def error(
        cls,
        *,
        frame_id: str,
        sequence: int,
        code: str,
        message: str,
    ) -> StreamFrame:
        # ``retry_disposition`` (additive, §3.1 tail-only) is derived from the
        # stable ``code`` via the single source of truth in
        # ``error_disposition`` so every ERROR frame — whether built here or
        # via the adapter's ``_error_then_end`` — carries a consistent
        # frontend-facing auto-retry/action hint.
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.ERROR,
            sequence=sequence,
            payload={
                "code": code,
                "message": message,
                "retry_disposition": retry_disposition_for(code),
            },
        )

    @classmethod
    def end(
        cls,
        *,
        frame_id: str,
        sequence: int,
        reason: str = "completed",
        usage: dict[str, Any] | None = None,
        request_id: str | None = None,
        sender_id: str | None = None,
        convergence_reason: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> StreamFrame:
        """Factory for a terminal ``end`` frame.

        ``request_id`` is an **optional appended field** (AGENTS.md §3.1 —
        payloads may only grow at the tail; the existing ``{reason, usage}``
        shape is unchanged).  When present it carries the UUID of the prompt
        snapshot saved by :meth:`StreamChatUseCase._save_prompt_snapshot` so
        the frontend can surface the "Prompt Snapshot" button on the
        assistant message (V1 parity: ``backend/main.py:6716-6720`` done
        frame payload contains ``request_id``).  Omitting it keeps the wire
        frame byte-for-byte identical to the pre-fix shape.

        ``convergence_reason`` is likewise an **optional appended field**
        (§3.1): a discussion convergence early-stop cause (e.g.
        ``"manager_end"`` — the Manager moderator concluded the discussion when
        ``manager_early_end_enabled`` was on; §22A.3).  The top-level ``reason``
        value is UNCHANGED (still ``"completed"`` for a normal/early-stop end),
        so existing front-end ``reason`` branches keep working; omitting it
        leaves the payload byte-for-byte identical.

        ``extra`` (optional) tail-appends arbitrary payload keys (§3.1) without
        touching ``reason``. Used by the ``budget_exceeded`` END to carry the
        interactive budget-decision metadata (current usage, the cap that was
        hit, and the cap a "continue" would apply) so the frontend can render
        the continue/stop dialog and, on continue, PATCH the raised cap + resend
        a continuation turn.
        """
        payload: dict[str, Any] = {"reason": reason}
        if usage is not None:
            payload["usage"] = usage
        if request_id is not None:
            payload["request_id"] = request_id
        if sender_id is not None:
            payload["sender_id"] = sender_id
        if convergence_reason is not None:
            payload["convergence_reason"] = convergence_reason
        if extra:
            # Tail-append arbitrary extra keys (AGENTS.md §3.1) — used e.g. by the
            # ``budget_exceeded`` END to carry the budget-decision metadata
            # (``budget_used_tokens`` / ``budget_max_tokens`` /
            # ``budget_next_max_tokens`` / ``budget_raise_pct``) so the frontend
            # can render the continue/stop dialog. Never overwrites ``reason``.
            for _k, _v in extra.items():
                if _k != "reason":
                    payload[_k] = _v
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.END,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def tool_mode_changed(
        cls,
        *,
        frame_id: str,
        sequence: int,
        mode: str,
        previous_mode: str | None = None,
    ) -> StreamFrame:
        """Factory for a ``tool_mode_changed`` notification frame.

        Emitted BEFORE the first CHUNK frame when the effective tool mode
        differs from the explicit ``tool_mode`` in the request (e.g. auto-
        detection promoted the turn to ``model_build``).
        """
        payload: dict[str, Any] = {"mode": mode}
        if previous_mode is not None:
            payload["previous_mode"] = previous_mode
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.TOOL_MODE_CHANGED,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def turn_warning(
        cls,
        *,
        frame_id: str,
        sequence: int,
        turn_count: int,
        threshold: int | None = None,
        message: str | None = None,
    ) -> StreamFrame:
        """Factory for a ``turn_warning`` advisory frame.

        Emitted at the end of a streaming turn when the cumulative count
        of user messages in the conversation crosses one of the V1
        per-conversation thresholds (20 / 25 / 30 / ...).  V1 parity:
        ``backend/main.py:6722-6731`` (``_compute_webui_turn_warning``)
        — V1 emits this frame *before* the terminal ``[DONE]`` SSE
        event so the client-side consumer (``useChat.js:1422-1432``,
        re-implemented in V2 as ``stores/chatTabs.ts`` ``case "turn_warning"``)
        can append an inline system-styled notice while the tab is
        still in the ``streaming`` state.

        Payload field semantics (V1 main.py:6731):

        * ``turn_count`` (required): cumulative user-message count in
          the conversation, used by the client to compose a localised
          notice via ``chat.turnLimitWarn`` when ``message`` is absent.
        * ``threshold`` (optional appended): the threshold value that
          was just crossed (20 / 25 / 30 / ...).  Carried for
          telemetry / observability; the V2 client does not currently
          render it.
        * ``message`` (optional appended): pre-rendered server-side
          text (V1 emits a Chinese template).  When present the
          client renders it verbatim; when absent the client falls
          back to the i18n key with ``turn_count`` as the placeholder.

        ``threshold`` and ``message`` follow the §3.1 tail-only-growth
        rule: only added when explicitly provided so existing
        consumers ignoring them remain byte-compatible.
        """
        if not isinstance(turn_count, int) or isinstance(turn_count, bool):
            raise TypeError(
                f"turn_count must be int, got {type(turn_count).__name__}",
            )
        if turn_count < 0:
            raise ValueError(f"turn_count must be >= 0, got {turn_count}")
        payload: dict[str, Any] = {"turn_count": turn_count}
        if threshold is not None:
            payload["threshold"] = threshold
        if message is not None:
            payload["message"] = message
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.TURN_WARNING,
            sequence=sequence,
            payload=payload,
        )

    # ── Sub-agent event family factories (V1 wire-shape parity) ───────────
    # Field names mirror V1 chat_handler.py exactly so the frontend
    # ``stores/chatTabs.ts`` consumer (V1 useChat.js:1345-1408) reads the
    # same keys without per-field translation:
    #
    #   subagent_start  {index, total, prompt_preview}
    #   subagent_output {index, content}
    #   subagent_tool   {index, tool_name, tool_args}
    #   subagent_done   {index, result, rounds}
    #   subagent_error  {index, message}
    #   agent_summary   {total_agents}
    #
    # ``index`` is required on the per-agent five (it disambiguates
    # parallel sub-agents); ``agent_summary`` is global-scoped (one per
    # parent turn after all sub-agents finish) and carries no index.

    @staticmethod
    def _validate_index(index: int) -> None:
        if not isinstance(index, int) or isinstance(index, bool):
            raise TypeError(
                f"index must be int, got {type(index).__name__}",
            )
        if index < 0:
            raise ValueError(f"index must be >= 0, got {index}")

    @classmethod
    def subagent_start(
        cls,
        *,
        frame_id: str,
        sequence: int,
        index: int,
        total: int,
        prompt_preview: str = "",
        subagent_id: str | None = None,
        subagent_type: str | None = None,
        name: str | None = None,
        round_index: int | None = None,
    ) -> StreamFrame:
        """Factory for ``subagent_start`` — sub-agent N of M just spawned.

        V1 parity: ``backend/chat_handler.py:598-605``.  Emitted once per
        sub-agent before any output frames so the UI can pre-allocate a
        block entry with the prompt preview (truncated to 500 chars in V1).

        ``subagent_id`` (optional, V2 enhancement, §3.1 tail-appended) carries
        the resumable / openable session id when persistence is wired, so the
        RUNNING block can show its "open in new tab" / "stop" affordances
        immediately (not only after ``subagent_done``). Omitted from the
        payload when ``None`` so the wire stays byte-identical for the
        no-persistence path.

        ``subagent_type`` (optional, V2 UX enhancement, §3.1 tail-appended)
        carries the resolved profile name (``general`` / ``explore``) so the
        RUNNING card can render its i18n type-badge next to the title
        immediately. Omitted when the caller did not resolve a profile
        (legacy / stub parity).

        ``name`` (optional, V2 UX enhancement, §3.1 tail-appended) carries the
        LLM-supplied human-readable task label (``SubAgentSession.title``) so
        the RUNNING card shows a meaningful title instead of the generic
        ``SubAgent N`` fallback. Omitted when empty so the card falls back
        (no regression).

        ``round_index`` (optional, V2 UX fix, §3.1 tail-appended) carries the
        parent agent's round number at which this sub-agent was dispatched.
        Without it, the front-end ``handleSubagentStart`` cannot route
        SUBAGENT_START to a per-round message and falls back to
        ``activeSubAgentMessageId`` (which persists across rounds) — so two
        sub-agents spawned in DIFFERENT rounds of the same turn (e.g. main
        Agent dispatches A in round 0, waits, then dispatches B in round 1)
        both land on the SAME message's ``subAgentBlocks[]`` and B's
        ``index=0`` deduplication-filter drops A's block — the reported "A
        card disappears when B starts" bug. With ``round_index`` on the
        payload the UI opens a fresh per-round message for B, keeping A's
        card intact. Omitted when ``None`` so the wire stays byte-identical
        for legacy callers (front-end then falls back to legacy behaviour
        — the historical bug). NOTE: unlike ``subagent_output`` / ``tool`` /
        ``tool_result``, SUBAGENT_START is NOT covered by ``_stamp_round``
        in the caller (it is emitted from ``agent_tool.iter_events`` before
        any per-round context is attached), so this factory kwarg is the
        SOLE channel — the caller must pass it explicitly.
        """
        cls._validate_index(index)
        if not isinstance(total, int) or isinstance(total, bool):
            raise TypeError(f"total must be int, got {type(total).__name__}")
        if total < 1:
            raise ValueError(f"total must be >= 1, got {total}")
        if index >= total:
            raise ValueError(
                f"index ({index}) must be < total ({total})",
            )
        if not isinstance(prompt_preview, str):
            raise TypeError(
                f"prompt_preview must be str, got "
                f"{type(prompt_preview).__name__}",
            )
        payload: dict[str, Any] = {
            "index": index,
            "total": total,
            "prompt_preview": prompt_preview,
        }
        if subagent_id is not None:
            if not isinstance(subagent_id, str):
                raise TypeError(
                    f"subagent_id must be str, got {type(subagent_id).__name__}"
                )
            payload["subagent_id"] = subagent_id
        if subagent_type is not None:
            if not isinstance(subagent_type, str):
                raise TypeError(
                    f"subagent_type must be str, got "
                    f"{type(subagent_type).__name__}"
                )
            if subagent_type:
                payload["subagent_type"] = subagent_type
        if name is not None:
            if not isinstance(name, str):
                raise TypeError(
                    f"name must be str, got {type(name).__name__}"
                )
            if name:
                payload["name"] = name
        if round_index is not None:
            if not isinstance(round_index, int) or isinstance(round_index, bool):
                raise TypeError(
                    f"round_index must be int, got {type(round_index).__name__}"
                )
            if round_index < 0:
                raise ValueError(
                    f"round_index must be >= 0, got {round_index}"
                )
            payload["round_index"] = round_index
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.SUBAGENT_START,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def subagent_output(
        cls,
        *,
        frame_id: str,
        sequence: int,
        index: int,
        content: str,
        round_index: int | None = None,
    ) -> StreamFrame:
        """Factory for ``subagent_output`` — incremental text chunk.

        V1 parity: ``backend/chat_handler.py:2256``.  Field is named
        ``content`` (not ``text``) to match V1 wire shape verbatim.

        ``round_index`` (optional appended, §3.1 — tail-only growth): the
        0-based agentic round this text chunk belongs to. Lets the UI fold the
        text into the SAME ordered per-round turn as the round's tool cards
        (so a sub-agent block renders "text → tools → text → tools" in real
        time, identical to the main agent's per-round rendering) instead of
        piling all narration at the end. Absent ⇒ the client treats it as the
        current/last round (legacy single-bucket behaviour).
        """
        cls._validate_index(index)
        if not isinstance(content, str):
            raise TypeError(
                f"content must be str, got {type(content).__name__}",
            )
        payload: dict[str, Any] = {"index": index, "content": content}
        if round_index is not None:
            payload["round_index"] = int(round_index)
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.SUBAGENT_OUTPUT,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def subagent_tool(
        cls,
        *,
        frame_id: str,
        sequence: int,
        index: int,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str | None = None,
        round_index: int | None = None,
    ) -> StreamFrame:
        """Factory for ``subagent_tool`` — tool call inside the sub-agent.

        V1 parity: ``backend/chat_handler.py:2299-2304``.  Wire field is
        ``tool_args`` (not ``arguments``) — matches V1 useChat.js:1376
        consumer.

        ``tool_call_id`` (optional appended, §3.1) — pairs this call row to
        its later ``subagent_tool_result`` frame by id (parity with the
        parent loop's id pairing). Omitted when absent so the wire stays
        byte-identical for callers that don't supply it.

        ``round_index`` (optional appended, §3.1) — the 0-based agentic round
        this tool call belongs to, so the UI groups it into the SAME ordered
        turn as that round's narration text (see :meth:`subagent_output`).
        """
        cls._validate_index(index)
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(tool_args, dict):
            raise TypeError(
                f"tool_args must be dict, got {type(tool_args).__name__}",
            )
        payload: dict[str, Any] = {
            "index": index,
            "tool_name": tool_name,
            "tool_args": dict(tool_args),
        }
        if tool_call_id is not None:
            payload["tool_call_id"] = tool_call_id
        if round_index is not None:
            payload["round_index"] = int(round_index)
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.SUBAGENT_TOOL,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def subagent_tool_result(
        cls,
        *,
        frame_id: str,
        sequence: int,
        index: int,
        tool_name: str,
        result: Any,
        ok: bool = True,
        tool_call_id: str | None = None,
        size: int | None = None,
        truncated: bool | None = None,
        duration_ms: int | None = None,
        round_index: int | None = None,
    ) -> StreamFrame:
        """Factory for ``subagent_tool_result`` — a sub-agent tool's output.

        V2 enhancement (§3.1 tail-only frame-family growth): the sub-agent
        counterpart of the parent agent's :meth:`tool_result`. Emitted once
        per tool call AFTER the sub-agent executes it, so the UI can render a
        structured, collapsible result panel under the matching
        ``subagent_tool`` row (visual parity with a main-agent tool card)
        instead of the model re-narrating the raw output as plain text.

        Payload mirrors the parent ``tool_result`` shape so the frontend can
        reuse the same ``ToolExecPanel`` card:

        * ``index`` — discriminates parallel sub-agents (as on every other
          per-agent frame).
        * ``tool_name`` — the tool that produced this result.
        * ``result`` — the (already-truncated) result text.
        * ``ok`` — ``False`` when the tool failed (``[tool_error] …`` /
          ``[guardrail_blocked] …`` sentinel) so the card flags itself error.
        * ``tool_call_id`` (optional appended) — pairs the result to its
          originating ``subagent_tool`` row by id when available; absent ⇒
          the client pairs by tool name + order.
        * ``size`` / ``truncated`` (optional appended) — original-output
          metadata for the size badge / "已截断" warning (parity with the
          parent tool card).
        """
        cls._validate_index(index)
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(ok, bool):
            raise TypeError(f"ok must be bool, got {type(ok).__name__}")
        payload: dict[str, Any] = {
            "index": index,
            "tool_name": tool_name,
            "result": result,
            "ok": ok,
        }
        if tool_call_id is not None:
            payload["tool_call_id"] = tool_call_id
        if size is not None:
            payload["size"] = size
        if truncated is not None:
            payload["truncated"] = truncated
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        if round_index is not None:
            payload["round_index"] = int(round_index)
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.SUBAGENT_TOOL_RESULT,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def subagent_done(
        cls,
        *,
        frame_id: str,
        sequence: int,
        index: int,
        result: str,
        rounds: int,
        subagent_id: str | None = None,
    ) -> StreamFrame:
        """Factory for ``subagent_done`` — sub-agent finished cleanly.

        V1 parity: ``backend/chat_handler.py:2338-2343``.  Wire field is
        ``result`` (not ``text``); ``rounds`` is the number of agentic
        rounds the sub-agent used (used by V1 UI to show "(3 rounds)").

        ``subagent_id`` is an **optional appended field** (AGENTS.md §3.1 —
        tail-only growth; mirrors ``round_index`` / ``duration_ms`` on the
        tool frames): the persisted :class:`SubAgentSession` id of the
        sub-agent that just finished. It is the resumable handle the main
        agent re-passes as the ``agent`` tool's ``resume_subagent_id`` to
        wake the SAME sub-agent (with its prior wire history) for a related
        follow-up task, and the id the user clicks to open the sub-agent in a
        new tab. Only written into the payload when non-``None`` so the wire
        frame stays byte-for-byte compatible for callers that do not persist
        sub-agent sessions (legacy stubs / no repo wired).
        """
        cls._validate_index(index)
        if not isinstance(result, str):
            raise TypeError(
                f"result must be str, got {type(result).__name__}",
            )
        if not isinstance(rounds, int) or isinstance(rounds, bool):
            raise TypeError(
                f"rounds must be int, got {type(rounds).__name__}",
            )
        if rounds < 0:
            raise ValueError(f"rounds must be >= 0, got {rounds}")
        if subagent_id is not None and (
            not isinstance(subagent_id, str) or not subagent_id
        ):
            raise ValueError("subagent_id must be a non-empty string or None")
        payload: dict[str, Any] = {
            "index": index,
            "result": result,
            "rounds": rounds,
        }
        if subagent_id is not None:
            payload["subagent_id"] = subagent_id
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.SUBAGENT_DONE,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def subagent_error(
        cls,
        *,
        frame_id: str,
        sequence: int,
        index: int,
        message: str,
    ) -> StreamFrame:
        """Factory for ``subagent_error`` — sub-agent failed.

        V1 parity: ``backend/chat_handler.py:2261``.  Wire field is
        ``message`` (not ``error``) — matches V1 useChat.js:1397 consumer.
        """
        cls._validate_index(index)
        if not isinstance(message, str) or not message:
            raise ValueError("message must be a non-empty string")
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.SUBAGENT_ERROR,
            sequence=sequence,
            payload={"index": index, "message": message},
        )

    @classmethod
    def agent_summary(
        cls,
        *,
        frame_id: str,
        sequence: int,
        total_agents: int,
    ) -> StreamFrame:
        """Factory for ``agent_summary`` — emitted after all sub-agents done.

        V1 parity: ``backend/chat_handler.py:694``.  Marker frame between
        the last per-sub-agent event and the parent agent's follow-up
        text; lets the UI insert a "main agent summary" separator and
        reset its streaming-content buffer (V1 useChat.js:1402-1408).
        """
        if not isinstance(total_agents, int) or isinstance(total_agents, bool):
            raise TypeError(
                f"total_agents must be int, got "
                f"{type(total_agents).__name__}",
            )
        if total_agents < 1:
            raise ValueError(
                f"total_agents must be >= 1, got {total_agents}",
            )
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.AGENT_SUMMARY,
            sequence=sequence,
            payload={"total_agents": total_agents},
        )

    @classmethod
    def speaker_changed(
        cls,
        *,
        frame_id: str,
        sequence: int,
        sender_id: str,
        display_name: str,
        model_id: str | None = None,
    ) -> StreamFrame:
        """Factory for ``speaker_changed`` — discussion floor passed to a new
        named-agent speaker.

        Emitted by the discussion orchestrator BEFORE the new speaker's first
        chunk/tool frame so the UI can soft-reset the live streaming buffer
        and start a fresh bubble attributed to ``sender_id``
        (docs/70-multi-agent/multi-agent-conversation-design.md §7). ``model_id`` is an
        **optional appended field** (§3.1) carrying the speaker's backing
        model for display.
        """
        if not isinstance(sender_id, str) or not sender_id:
            raise ValueError("sender_id must be a non-empty string")
        if not isinstance(display_name, str):
            raise TypeError("display_name must be a str")
        payload: dict[str, Any] = {
            "sender_id": sender_id,
            "display_name": display_name,
        }
        if model_id is not None:
            payload["model_id"] = model_id
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.SPEAKER_CHANGED,
            sequence=sequence,
            payload=payload,
        )

    # ── DISC-1 implementation orchestration factories (§22.9) ─────────────────
    # The four control-plane frames the planned-implementation serial runner
    # emits.  Each payload carries ONLY a SHORT control-plane summary (item ids /
    # titles / statuses / phase) — never the full tool output / diff (§22.9).
    # ``run_id`` / ``item_id`` are appended on the payload (§3.1 tail-only).
    # ``sender_id`` is an **optional appended field** (§3.1) carrying the
    # ParticipantId of the role implementing the item, so the UI can attribute
    # the progress to the same speaker bubble; absent ⇒ unattributed.

    @classmethod
    def plan_ready(
        cls,
        *,
        frame_id: str,
        sequence: int,
        run_id: str,
        items: list[dict[str, Any]],
        sender_id: str | None = None,
    ) -> StreamFrame:
        """Factory for ``plan_ready`` — the implementation run is starting.

        Emitted once when a ``planned`` plan transitions to ``implementing`` so
        the UI can render the full item list up-front.  ``items`` is a list of
        SHORT item summaries (``id`` / ``title`` / ``status`` / ``assigned_role``
        / ``suggested_role``) — NOT the full :class:`FeatureItem` (no
        ``description`` / ``acceptance_criteria`` / large fields), keeping the
        control-plane frame small (§22.9).
        """
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(items, list):
            raise TypeError(
                f"items must be list, got {type(items).__name__}",
            )
        payload: dict[str, Any] = {
            "run_id": run_id,
            "items": [dict(it) for it in items if isinstance(it, dict)],
        }
        if sender_id is not None:
            payload["sender_id"] = sender_id
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.PLAN_READY,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def implementation_item_started(
        cls,
        *,
        frame_id: str,
        sequence: int,
        run_id: str,
        item_id: str,
        title: str,
        assigned_role: str | None = None,
        sender_id: str | None = None,
    ) -> StreamFrame:
        """Factory for ``implementation_item_started`` — item N began.

        Emitted right after the serial runner marks an item ``in_progress`` +
        sets it as the run's ``current_item`` (§22.6 step3c), so the UI can flip
        that item's row to a working state and reflect ``current_item``.
        """
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("item_id must be a non-empty string")
        if not isinstance(title, str):
            raise TypeError("title must be a str")
        payload: dict[str, Any] = {
            "run_id": run_id,
            "item_id": item_id,
            "title": title,
        }
        if assigned_role is not None:
            payload["assigned_role"] = assigned_role
        if sender_id is not None:
            payload["sender_id"] = sender_id
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.IMPLEMENTATION_ITEM_STARTED,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def implementation_item_finished(
        cls,
        *,
        frame_id: str,
        sequence: int,
        run_id: str,
        item_id: str,
        status: str,
        result_summary: str | None = None,
        last_error: str | None = None,
        sender_id: str | None = None,
    ) -> StreamFrame:
        """Factory for ``implementation_item_finished`` — item N settled.

        Emitted after the serial runner persists an item's terminal status
        (``done`` / ``failed`` / ``skipped``).  ``result_summary`` / ``last_error``
        are SHORT control-plane strings (the full output lives in the message
        system, §22.9), only added when present (§3.1 tail-only).
        """
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("item_id must be a non-empty string")
        if not isinstance(status, str) or not status:
            raise ValueError("status must be a non-empty string")
        payload: dict[str, Any] = {
            "run_id": run_id,
            "item_id": item_id,
            "status": status,
        }
        if result_summary is not None:
            payload["result_summary"] = result_summary
        if last_error is not None:
            payload["last_error"] = last_error
        if sender_id is not None:
            payload["sender_id"] = sender_id
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.IMPLEMENTATION_ITEM_FINISHED,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def implementation_phase_changed(
        cls,
        *,
        frame_id: str,
        sequence: int,
        run_id: str,
        phase: str,
        current_item: str | None = None,
        sender_id: str | None = None,
    ) -> StreamFrame:
        """Factory for ``implementation_phase_changed`` — run-phase transition.

        Emitted on every run-phase transition the serial runner / control
        dispatcher drives (``implementing`` / ``completed`` / ``failed`` /
        ``paused``).  ``current_item`` is the id of the item in flight (``None``
        when idle / terminal), only added when present (§3.1 tail-only).
        """
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(phase, str) or not phase:
            raise ValueError("phase must be a non-empty string")
        payload: dict[str, Any] = {"run_id": run_id, "phase": phase}
        if current_item is not None:
            payload["current_item"] = current_item
        if sender_id is not None:
            payload["sender_id"] = sender_id
        return cls(
            frame_id=frame_id,
            frame_type=StreamFrameType.IMPLEMENTATION_PHASE_CHANGED,
            sequence=sequence,
            payload=payload,
        )


__all__ = [
    "StreamFrameType",
    "StreamFrame",
]

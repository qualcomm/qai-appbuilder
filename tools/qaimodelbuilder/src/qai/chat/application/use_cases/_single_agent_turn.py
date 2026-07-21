# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared single-agent turn kernel (main + sub-agent + discussion loops).

Both the **main agent** loop (``streaming.py:_run_followup_loop`` +
``_drain_followup_round``) and the **sub-agent** loop
(``agent_tool.py:_iter_loop`` + ``_stream_round``) run the SAME
round-iteration skeleton:

    for round_no in 1..max_rounds:
        ① abort_check  → stop if aborted
        ② maybe_compress_wire (shared threshold/ratio)
        ③ build the SEND wire (blank the ``[tool_calls]`` sentinel,
           strip the display-only ``created_at`` key) from the growing wire
        ④ request_provider(round_no, send_wire) → one LLM stream
        ⑤ drain that stream, classifying frames into CHUNK / TOOL_CALL /
           END / ERROR (caller's emitter renders chunk/error progress)
        ⑥ no tool calls → finish the turn; tool calls present →
           build_assistant_tool_calls_block + tool_executor (parallel
           execute + truncate) + build_tool_reply_blocks → grow the wire
        ⑦ reach max_rounds → surface a cap notice

Historically that skeleton was re-implemented twice; this module makes it
ONE shared producer so a fix/limit-tweak reaches every caller (judgement 1:
remove the duplicate loops; judgement 2: each caller's user-visible
behaviour is preserved byte-for-byte because the kernel is a pure producer
of neutral :class:`KernelEvent` values and every shell-specific concern is
injected or kept in the shell).

DESIGN — what the kernel DELIBERATELY does NOT do (kept in each shell, per
``docs/70-multi-agent/multi-agent-conversation-design.md`` §15.2 务实边界):

* no SSE ``StreamFrame`` stamping (seq / round / request_id) — the caller's
  ``emitter`` adapts each :class:`KernelEvent` into its own wire shape
  (main = ``StreamFrame``; sub = ``subagent_*`` dict);
* no DB persistence / per-round prompt snapshots / shared-prefix segments /
  compaction dual-track checkpoints / SubAgentSession CAS / broadcaster /
  wake-takeover — those live in the shell;
* no ``empty-completion retry`` / ``no-progress`` circuit breaker — the
  main loop keeps those in its shell (optional, not modelled here);
* no decision about WHERE the system prompt / tool schemas come from — the
  caller assembles the initial wire + provides ``request_provider`` and
  ``tool_executor``;
* no knowledge of the ``agent`` tool recursion guard or parallel sub-agent
  dispatch — the caller's ``tool_executor`` owns that.

The kernel's tool-execution injection point is a **producer**: it may yield
several PARTIAL results (the main loop's exec streaming) followed by exactly
one FINAL result, OR just one FINAL result (the sub-agent). The kernel never
assumes partials exist and never drops them — it forwards each partial as a
:class:`KernelToolPartial` event and grows the wire from the FINAL only.

Layering: this module lives in ``application/use_cases`` and depends only on
``application.ports`` Protocols + ``domain`` types + the neutral
``_agentic_kernel`` building blocks, so the application-layer main loop, the
adapters-layer sub-agent loop, and a future ``OrchestrateDiscussionUseCase``
may all import it without breaking the ``layered-chat`` /
``context-isolation`` import-linter contracts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from qai.chat.application.ports import (
    ContextCompressionPort,
    ToolResultTruncatorPort,
)
from qai.chat.application.use_cases._agentic_kernel import (
    COMPRESS_PRESERVE_TAIL,
    INTER_ROUND_COMPRESS_THRESHOLD_RATIO,
    TOOL_CALLS_CONTENT_SENTINEL,
    build_assistant_tool_calls_block,
    build_tool_reply_blocks,
    is_self_contained_agent_hint,
    maybe_compress_wire,
)
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType

__all__ = [
    "KernelAborted",
    "KernelChunk",
    "KernelError",
    "KernelEvent",
    "KernelEventKind",
    "KernelFinished",
    "KernelMaxRoundsReached",
    "KernelRoundStarted",
    "KernelStreamPassthrough",
    "KernelToolCallsIssued",
    "KernelToolCallSeen",
    "KernelToolPartial",
    "KernelToolResult",
    "RoundEndDecision",
    "RoundStreamOpener",
    "SingleAgentTurnKernel",
    "ToolExecutionItem",
    "ToolExecutor",
    "build_send_wire",
]


# ---------------------------------------------------------------------------
# Send-wire assembly (shared by both loops) — blank the sentinel + strip
# the display-only ``created_at`` key WITHOUT mutating the growing wire.
# ---------------------------------------------------------------------------
def build_send_wire(
    wire_messages: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the wire actually SENT to the model for one round.

    Two transforms, both pre-existing in BOTH loops (sub:
    ``agent_tool._iter_loop`` 948-963; main: ``_streaming_helpers`` blanking):

    1. blank the internal ``[tool_calls]`` content sentinel (it must NEVER
       reach the LLM as real text);
    2. strip the display-only fields we attach to the PERSISTED wire
       (``created_at`` per-turn timestamp; ``request_id`` per-round prompt-
       snapshot id + ``usage`` per-round token usage, both stamped onto the
       sub-agent's persisted assistant turns for 回看 parity; ``duration_ms``
       per-call execution time stamped onto persisted ``role:tool`` blocks so
       a reloaded sub-agent tab shows "took N ms") so non-standard keys never
       enter the provider payload.

    Shallow per-message copies keep the growing ``wire_messages`` (which
    carries the sentinel + display-only fields for UI parity) untouched.
    """
    _display_only = ("created_at", "request_id", "usage", "duration_ms")
    send: list[dict[str, Any]] = []
    for m in wire_messages:
        if not isinstance(m, dict):
            send.append(m)
            continue
        needs_copy = m.get("content") == TOOL_CALLS_CONTENT_SENTINEL or any(
            k in m for k in _display_only
        )
        if needs_copy:
            clean = {k: v for k, v in m.items() if k not in _display_only}
            if clean.get("content") == TOOL_CALLS_CONTENT_SENTINEL:
                clean["content"] = ""
            send.append(clean)
        else:
            send.append(m)
    return send


# ---------------------------------------------------------------------------
# Neutral kernel events (the caller's emitter adapts each into its wire shape)
# ---------------------------------------------------------------------------
class KernelEventKind(str, Enum):
    """Discriminator for a :class:`KernelEvent`."""

    ROUND_STARTED = "round_started"
    CHUNK = "chunk"
    TOOL_CALL_SEEN = "tool_call_seen"
    ERROR = "error"
    TOOL_CALLS_ISSUED = "tool_calls_issued"
    TOOL_PARTIAL = "tool_partial"
    TOOL_RESULT = "tool_result"
    STREAM_PASSTHROUGH = "stream_passthrough"
    FINISHED = "finished"
    MAX_ROUNDS_REACHED = "max_rounds_reached"
    ABORTED = "aborted"


@dataclass(slots=True)
class KernelRoundStarted:
    """A new agentic round is about to open its LLM stream."""

    kind: KernelEventKind = field(
        default=KernelEventKind.ROUND_STARTED, init=False
    )
    round_no: int = 0


@dataclass(slots=True)
class KernelChunk:
    """Incremental assistant text from the active LLM turn."""

    kind: KernelEventKind = field(default=KernelEventKind.CHUNK, init=False)
    round_no: int = 0
    text: str = ""


@dataclass(slots=True)
class KernelToolCallSeen:
    """One TOOL_CALL frame observed DURING the round's drain (inline order).

    Emitted only when ``forward_tool_calls_inline`` is set (the main loop,
    which historically forwarded each TOOL_CALL frame inline as it arrived —
    interleaved with chunks — so the frontend's per-round grouping keeps the
    exact wire order). The sub-agent / discussion loops leave the flag off and
    never see this event (they collect calls silently and emit their own
    ``subagent_tool`` events after the drain). ``frame`` is the original
    :class:`StreamFrame` so the caller forwards it byte-for-byte.
    """

    kind: KernelEventKind = field(
        default=KernelEventKind.TOOL_CALL_SEEN, init=False
    )
    round_no: int = 0
    frame: Any = None


@dataclass(slots=True)
class KernelError:
    """The LLM stream surfaced an ERROR frame; the turn stops after this.

    ``frame`` is the ORIGINAL :class:`StreamFrame` (``frame_type=ERROR``) the
    round's LLM stream produced, carried verbatim so a caller that owns its own
    outbound stream (the main follow-up emitter) can re-emit it byte-for-byte —
    preserving the diagnostic ``code`` / ``message`` / ``retryable`` payload the
    frontend needs to surface the error and offer a manual retry. Callers that
    only need a human string (sub-agent / discussion) keep reading ``message``.
    ``None`` only if a future producer raises a KernelError without an upstream
    frame.
    """

    kind: KernelEventKind = field(default=KernelEventKind.ERROR, init=False)
    round_no: int = 0
    message: str = ""
    frame: Any = None


@dataclass(slots=True)
class KernelToolCallsIssued:
    """This round's tool calls were dispatched (emitted BEFORE execution).

    ``tool_metas`` is the round's ``(tool_name, arguments, call_id)`` list in
    original order. ``assistant_text`` is the lead-in text the round streamed
    before its first TOOL_CALL (folded into the persisted assistant turn).

    ``thought_signatures`` (tail-additive, default empty) is the round's
    ``call_id -> Vertex AI thought_signature`` map the kernel already derived
    from the originating TOOL_CALL frame payloads when it grew its built-in
    wire (the SAME map it passes to ``build_assistant_tool_calls_block``). It is
    surfaced here so a caller that persists its OWN structured tool cards (the
    discussion orchestrator, which records ``Message.tool_calls`` per speaker
    turn for 历史回看) can re-attach the signature losslessly — parity with the
    main loop's ``tc_frames`` carrying ``thought_signature`` on the wire. Empty
    for non-thinking models / callers that never read it (sub-agent / main loop
    ignore the field, so their behaviour is byte-for-byte unchanged).
    """

    kind: KernelEventKind = field(
        default=KernelEventKind.TOOL_CALLS_ISSUED, init=False
    )
    round_no: int = 0
    tool_metas: list[tuple[str, dict[str, Any], str]] = field(
        default_factory=list
    )
    assistant_text: str = ""
    thought_signatures: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class KernelToolPartial:
    """A live PARTIAL result fragment from a streaming tool (e.g. exec).

    The sub-agent's executor never yields these; the main loop's exec
    streaming yields one per stdout/stderr increment. The kernel forwards
    them verbatim and does NOT fold them into the wire (only the FINAL does).
    """

    kind: KernelEventKind = field(
        default=KernelEventKind.TOOL_PARTIAL, init=False
    )
    round_no: int = 0
    tool_name: str = ""
    call_id: str = ""
    delta: str = ""
    #: Optional original frame to forward verbatim (main loop); ``None`` for
    #: sub-agent / discussion (emitter reconstructs from ``delta``).
    frame: Any = None


@dataclass(slots=True)
class KernelToolResult:
    """One tool's FINAL (post-truncation) result, paired by ``call_id``."""

    kind: KernelEventKind = field(
        default=KernelEventKind.TOOL_RESULT, init=False
    )
    round_no: int = 0
    tool_name: str = ""
    call_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result_text: str = ""
    ok: bool = True
    truncated: bool = False
    original_length: int | None = None
    duration_ms: int = 0
    #: Optional original frame to forward verbatim (main loop); ``None`` for
    #: sub-agent / discussion (emitter builds its own result event).
    frame: Any = None
    #: Tail-appended marker (§3.1): True when this FINAL result was produced
    #: because the user cancelled THIS one tool (single-tool ``cancel_tool``,
    #: NOT a whole-turn abort). Lets a caller's emitter tag the tool card in a
    #: distinct "已取消/cancelled" state — parity with the main-agent takeover
    #: path's ``StreamFrame.tool_result(cancelled=True)``. Default False keeps
    #: every existing consumer byte-for-byte unchanged.
    cancelled: bool = False


@dataclass(slots=True)
class KernelStreamPassthrough:
    """A non-control LLM-stream frame to forward verbatim (no decision impact).

    Emitted by :meth:`SingleAgentTurnKernel._drain_round` when the round's LLM
    stream yields a frame that is neither CHUNK / TOOL_CALL / ERROR / END — most
    importantly the cloud SSE adapter's throttled ``tool_result`` progress
    frames carrying ``phase="generating_args"`` + ``partial=True`` (a long
    tool-call argument being streamed; see
    ``infrastructure/llm_stream.py:_emit_args_progress``).  These are pure UI
    progress feedback: the kernel forwards the ORIGINAL ``frame`` byte-for-byte
    (the main loop's emitter ``_stamp_round`` / ``_stamp_request_id`` it like
    any other live frame) and does NOT collect it as a tool call, fold it into
    the wire, or let it influence finish / execute / round-end decisions.

    Sub-agent / discussion loops never produce these (their streams don't emit
    such progress frames); when they do appear, those loops simply ignore the
    event (no ``isinstance`` branch handles it → no-op), so this is inert for
    every consumer except the main loop's live UI.
    """

    kind: KernelEventKind = field(
        default=KernelEventKind.STREAM_PASSTHROUGH, init=False
    )
    round_no: int = 0
    #: The original :class:`StreamFrame` to forward verbatim.
    frame: Any = None


@dataclass(slots=True)
class KernelFinished:
    """The model produced a no-tool round — the turn is complete."""

    kind: KernelEventKind = field(default=KernelEventKind.FINISHED, init=False)
    round_no: int = 0
    final_text: str = ""
    #: The terminal END frame's payload (main loop preserves its ``reason`` /
    #: ``request_id`` when rebuilding the END frame with accumulated usage);
    #: empty for sub-agent / discussion (they build their own done event).
    end_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class KernelMaxRoundsReached:
    """The round budget was exhausted while tool calls were still pending."""

    kind: KernelEventKind = field(
        default=KernelEventKind.MAX_ROUNDS_REACHED, init=False
    )
    round_no: int = 0
    max_rounds: int = 0
    last_text: str = ""


@dataclass(slots=True)
class KernelAborted:
    """The abort check fired before a round opened its LLM stream."""

    kind: KernelEventKind = field(default=KernelEventKind.ABORTED, init=False)
    round_no: int = 0


KernelEvent = (
    KernelRoundStarted
    | KernelChunk
    | KernelToolCallSeen
    | KernelError
    | KernelToolCallsIssued
    | KernelToolPartial
    | KernelToolResult
    | KernelStreamPassthrough
    | KernelFinished
    | KernelMaxRoundsReached
    | KernelAborted
)


# ---------------------------------------------------------------------------
# Tool-execution producer item (caller yields these from ``tool_executor``)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ToolExecutionItem:
    """One item yielded by a :class:`ToolExecutor` producer.

    The producer yields zero or more PARTIAL items (``partial=True``,
    carrying a ``delta`` for live UI — the main loop's exec streaming) and
    then exactly one FINAL item per ``call_id`` (``partial=False``) carrying
    the consolidated, ALREADY-TRUNCATED ``result_text`` the kernel folds into
    the wire.

    The sub-agent executor yields ONLY FINAL items (one per call). The kernel
    must work with both shapes and never assume partials exist.

    A FINAL item's ``result_text`` is the final string the LLM should see
    (the caller already applied :func:`truncate_tool_result`); the kernel
    feeds it verbatim to :func:`build_tool_reply_blocks`.
    """

    call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    partial: bool = False
    delta: str = ""
    result_text: str = ""
    ok: bool = True
    truncated: bool = False
    original_length: int | None = None
    duration_ms: int = 0
    #: Optional caller-owned passthrough payload (tail-append, §3.1). The main
    #: loop attaches the ORIGINAL :class:`StreamFrame` produced by its
    #: dispatcher here so its emitter forwards it byte-for-byte (preserving
    #: ``size`` / ``truncated`` / ``tool_call_id`` / ``delta`` / SUBAGENT_* /
    #: AGENT_SUMMARY frames the neutral kernel fields cannot fully model). The
    #: sub-agent / discussion loops leave it ``None`` (they reconstruct from
    #: the neutral fields). The kernel forwards it verbatim on the matching
    #: :class:`KernelToolPartial` / :class:`KernelToolResult` event.
    frame: Any = None
    #: Optional marker (tail-append): when True this item carries ONLY a
    #: passthrough ``frame`` to forward (e.g. a SUBAGENT_* / AGENT_SUMMARY
    #: frame from an ``agent`` tool dispatch) and is NOT a real tool result —
    #: the kernel forwards its ``frame`` but does not count it as a final.
    passthrough: bool = False
    #: Tail-appended marker (§3.1): True when this FINAL result was produced
    #: because the user cancelled THIS one tool (single-tool ``cancel_tool``).
    #: The kernel copies it onto the matching :class:`KernelToolResult` so the
    #: caller's emitter can tag the tool card "已取消/cancelled" (parity with
    #: the takeover path). Default False → unchanged for every other producer.
    cancelled: bool = False


@dataclass(slots=True)
class _RoundDrain:
    """Mutable accumulator for one round's drained LLM stream.

    Filled by :meth:`SingleAgentTurnKernel._drain_round` while it yields the
    round's chunk/error events; read back by :meth:`run` to decide finish vs
    execute-tools. Kept private to the kernel.

    Note: the drained stream may also yield TOOL_RESULT frames (the cloud SSE
    adapter's ``phase="generating_args"`` + ``partial=True`` progress frames
    while a long tool-call argument is being streamed). Those are forwarded
    verbatim as :class:`KernelStreamPassthrough` and do NOT touch this
    accumulator — they are pure UI progress, not real tool calls / results.
    """

    text_parts: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_frames: list[Any] = field(default_factory=list)
    error_seen: bool = False
    aborted: bool = False
    end_payload: dict[str, Any] = field(default_factory=dict)
    ended: bool = False


# ---------------------------------------------------------------------------
# Injection-point Protocols
# ---------------------------------------------------------------------------
@runtime_checkable
class RoundStreamOpener(Protocol):
    """Builds one round's LLM request AND opens its stream from the SEND wire.

    Combining request-building + stream-opening into one injection point
    keeps the kernel free of any ``LLMStreamPort`` dependency (it never needs
    to know HOW the stream is opened) and lets each caller absorb its own
    request shape:

    * main = ``_build_llm_request`` (system prompt, sampling, tools,
      ``followup_round`` / ``tool_results`` overrides) with
      ``extra["messages"]`` set to ``send_wire`` → ``self._llm.stream(req)``;
    * sub = a bare ``LLMStreamRequest`` whose ``extra["messages"]`` is
      ``send_wire`` (+ ``tools_schemas``) → ``self._llm.stream(req)``;
    * discussion = a request shaped by the speaking participant
      (model + persona + allowed tools) → ``self._llm.stream(req)``.

    Returns the round's :class:`~qai.chat.domain.stream_frame.StreamFrame`
    async iterator (CHUNK / TOOL_CALL / END / ERROR — and possibly TOOL_RESULT
    frames carrying ``phase="generating_args"`` + ``partial=True``, the cloud
    SSE adapter's "generating arguments" progress frames, which the kernel
    forwards verbatim as :class:`KernelStreamPassthrough` without letting them
    influence any decision), exactly as
    :meth:`~qai.chat.application.ports.LLMStreamPort.stream` yields.
    """

    def __call__(
        self, *, round_no: int, send_wire: list[dict[str, Any]]
    ) -> AsyncIterator[StreamFrame]: ...


@runtime_checkable
class ToolExecutor(Protocol):
    """Executes one round's tool calls, yielding partials + finals.

    Given the round's ``tool_metas`` (``(name, args, call_id)`` in order),
    yields :class:`ToolExecutionItem` values: zero or more partials (live
    streaming) interleaved with exactly one FINAL per call. The caller owns
    guardrail / exec streaming / ``agent`` recursion / parallel dispatch +
    the per-result truncation, so the kernel stays neutral.
    """

    def __call__(
        self,
        *,
        round_no: int,
        tool_metas: list[tuple[str, dict[str, Any], str]],
    ) -> AsyncIterator[ToolExecutionItem]: ...


# ``abort_check`` / ``tool_schemas_provider`` are simple callables.
AbortCheck = Callable[[], bool]
ToolSchemasProvider = Callable[[], list[dict[str, Any]]]
# ``build_tool_metas`` turns a round's raw TOOL_CALL payloads into
# ``(name, args, call_id)`` — the caller owns the id-synthesis convention
# (main ``call_{round}_{i}`` vs sub ``sub_{round}_{i}``).
ToolMetasBuilder = Callable[
    [list[dict[str, Any]], int], list[tuple[str, dict[str, Any], str]]
]


@dataclass(slots=True)
class RoundEndDecision:
    """The caller's verdict after a round's LLM stream reaches END.

    Returned by the optional ``on_round_end`` hook so the caller (the main
    loop) can absorb the END-frame policy the kernel itself stays neutral
    about — usage accumulation, empty-completion retry, experience
    extraction (a pure side effect inside the hook), and the no-progress
    circuit breaker — WITHOUT the kernel knowing any of it.

    Mutually-exclusive outcomes (checked in this order):

    * ``retry=True`` + ``retry_wire`` → the model finished with no usable
      output but a tool ran earlier; the hook built a synthetic-nudge wire.
      The kernel REPLACES its growing wire with ``retry_wire`` and opens
      ANOTHER round WITHOUT executing tools (empty-completion retry). The
      round's collected tool calls (if any) are ignored on this path.
    * ``stop=True`` → close the turn now WITHOUT executing this round's
      pending calls (no-progress breaker / graceful cap). The kernel yields
      a :class:`KernelFinished` carrying ``final_text`` (the caller's
      emitter renders its own notice + END frame).
    * default (neither) → proceed with the kernel's normal branch (no-tool
      finish, or execute this round's tool calls).

    The default instance (all-false) is what a caller that passes no
    ``on_round_end`` hook implicitly gets, so the kernel behaviour is
    unchanged for the sub-agent / discussion loops.
    """

    retry: bool = False
    retry_wire: list[dict[str, Any]] | None = None
    stop: bool = False
    final_text: str = ""


# Optional main-loop hooks (sub-agent / discussion pass ``None`` → unchanged).
#: ``compact_hook(round_no, wire) -> new_wire | None`` — replaces step ②'s
#: built-in ``maybe_compress_wire``. Return a full rebuilt wire to REPLACE the
#: growing wire (the main loop's dual-track checkpoint + completed-rounds
#: replay returns the rebuilt wire; ``None`` = leave the wire unchanged).
CompactHook = Callable[
    [int, list[dict[str, Any]]], Awaitable[list[dict[str, Any]] | None]
]
#: ``on_round_open(round_no, send_wire) -> None`` — fires after the SEND wire
#: is computed, BEFORE the stream opens (the main loop saves its per-round
#: prompt snapshot here; the returned request_id is stashed by the caller's
#: own closure for its emitter, the kernel never touches request_id).
RoundOpenHook = Callable[[int, list[dict[str, Any]]], Awaitable[None]]
#: ``on_round_end(round_no, end_payload, round_text, tool_calls) ->
#: RoundEndDecision`` — fires when the round's stream reaches END (carries the
#: END payload + the round's accumulated text + the round's collected raw
#: TOOL_CALL payloads). The main loop does usage accumulation / empty-retry /
#: experience extraction / no-progress here.
RoundEndHook = Callable[
    [int, dict[str, Any], str, list[dict[str, Any]]],
    Awaitable[RoundEndDecision],
]
#: ``grow_wire_hook(round_no, assistant_text, tool_metas, finals) -> None`` —
#: when provided, the kernel DELEGATES this round's wire growth to the caller
#: (it does NOT append its own ``assistant{tool_calls}`` + ``role:tool``
#: blocks). The main loop owns wire growth (``_append_tool_round`` precedence
#: for ids / thought_signatures / ``content: None`` vs the sentinel, plus its
#: ``completed_rounds`` bookkeeping), so its outbound wire stays byte-for-byte
#: identical. ``None`` (sub-agent / discussion) → kernel grows the wire itself.
GrowWireHook = Callable[
    [int, str, list[tuple[str, dict[str, Any], str]], list["ToolExecutionItem"]],
    Awaitable[None],
]
#: ``inject_hook(round_no) -> list[InjectedContent]`` — mid-turn user-injection
#: seam (V2 enhancement). Fires once per round AFTER the abort check +
#: inter-round compaction and BEFORE the send wire is built, i.e. in the gap
#: between finishing one tool round and opening the next LLM round. Returns the
#: list of pending user injections (FIFO, already drained from the registry) as
#: ready-to-send wire ``content`` values: a plain ``str`` for a text-only
#: injection, or an OpenAI-Vision multimodal block list
#: (``[{"type":"text",...}, {"type":"image_url",...}]``) for a text+image
#: injection. The kernel appends each as a ``role:user`` message to
#: ``wire_messages`` so the model sees them on this round. The hook owns
#: persistence + frame emission for each injected message (the kernel only
#: grows the wire). ``None`` (the default — sub-agent / discussion / legacy
#: callers) disables the seam, so their behaviour is byte-for-byte unchanged.
#:
#: ``InjectedContent`` mirrors the wire ``content`` shape the LLM transport
#: already accepts for both text-only and multimodal user messages (the same
#: shape ``assemble_multimodal_messages`` / the ``question``-answer image path
#: produce), so the kernel needs no special-casing — it just sets ``content``.
InjectedContent = str | list[dict[str, Any]]
InjectHook = Callable[[int], Awaitable[list[InjectedContent]]]


class SingleAgentTurnKernel:
    """The shared round-iteration skeleton for a single agent's turn.

    Stateless across calls: all per-turn state lives in :meth:`run`'s frame
    so one kernel instance is safe to share / call concurrently (the
    sub-agent loop fans out parallel sub-agents). The kernel is a pure
    PRODUCER of :class:`KernelEvent` values — the caller's emitter adapts
    each into its own wire shape and owns ALL persistence / SSE stamping /
    shell-specific machinery (§15.2 务实边界).
    """

    __slots__ = (
        "_compress_threshold_ratio",
        "_compressor",
        "_truncator",
    )

    def __init__(
        self,
        *,
        compressor: ContextCompressionPort | None = None,
        truncator: ToolResultTruncatorPort | None = None,
        compress_threshold_ratio: float = (
            INTER_ROUND_COMPRESS_THRESHOLD_RATIO
        ),
    ) -> None:
        self._compressor = compressor
        self._truncator = truncator
        self._compress_threshold_ratio = min(
            max(compress_threshold_ratio, 0.1), 1.0
        )

    @property
    def truncator(self) -> ToolResultTruncatorPort | None:
        """The shared model-aware truncator (the caller may reuse it)."""
        return self._truncator

    async def run(
        self,
        *,
        wire_messages: list[dict[str, Any]],
        open_round_stream: RoundStreamOpener,
        tool_executor: ToolExecutor,
        build_tool_metas: ToolMetasBuilder,
        max_rounds: int,
        abort_check: AbortCheck | None = None,
        model_hint: str | None = None,
        include_tool_name_in_reply: bool = False,
        assistant_timestamp: Callable[[], str] | None = None,
        compress_log_context: dict[str, Any] | None = None,
        on_tool_round_complete: Callable[[int], Awaitable[None]] | None = None,
        compact_hook: CompactHook | None = None,
        on_round_open: RoundOpenHook | None = None,
        on_round_end: RoundEndHook | None = None,
        grow_wire_hook: GrowWireHook | None = None,
        inject_hook: InjectHook | None = None,
        forward_tool_calls_inline: bool = False,
    ) -> AsyncIterator[KernelEvent]:
        """Drive the agentic round loop, mutating ``wire_messages`` in place.

        ``wire_messages`` is the growing OpenAI wire history the CALLER seeded
        (system + user [+ prior wake history]); by DEFAULT the kernel appends
        one ``assistant{tool_calls}`` + paired ``role:tool`` block per tool
        round and (on a no-tool finish) the final assistant turn, so the
        caller can persist the post-run ``wire_messages`` verbatim. When
        ``grow_wire_hook`` is provided the caller owns wire growth instead.

        Optional hooks absorb the three callers' differences (all ``None`` ⇒
        the sub-agent / discussion behaviour is unchanged):

        * ``compact_hook(round_no, wire)`` REPLACES step ②'s built-in
          ``maybe_compress_wire`` — return a rebuilt wire to swap in
          (dual-track checkpoint + completed-rounds replay), or ``None`` to
          leave the wire unchanged. When absent the kernel runs its built-in
          ``maybe_compress_wire`` (sub-agent parity).
        * ``on_round_open(round_no, send_wire)`` fires after the SEND wire is
          computed, before the stream opens (main loop saves its per-round
          prompt snapshot here).
        * ``on_round_end(round_no, end_payload, round_text, tool_calls)``
          fires when the round's stream reaches END; its
          :class:`RoundEndDecision` drives empty-completion retry / graceful
          stop (usage accumulation + experience extraction happen as side
          effects inside the hook).
        * ``grow_wire_hook(round_no, assistant_text, tool_metas, finals)``
          (main loop) takes over wire growth so the outbound bytes match the
          main loop's ``_append_tool_round`` precedence exactly.

        ``assistant_timestamp`` / ``on_tool_round_complete`` are the
        sub-agent's per-turn display timestamp + per-round persist hook
        (§15.2 State-Truth-First abort ordering).

        Yields neutral :class:`KernelEvent` values. The caller MUST consume
        the iterator to completion (the wire grows as a side effect).
        """
        for round_no in range(1, max_rounds + 1):
            # ① abort check — stop BEFORE opening another LLM turn.
            if abort_check is not None and abort_check():
                yield KernelAborted(round_no=round_no)
                return

            # ② inter-round context management. ``compact_hook`` (main loop's
            # dual-track checkpoint) takes precedence + REPLACES the wire when
            # it rebuilds; otherwise the built-in token-gated in-place
            # ``maybe_compress_wire`` runs (sub-agent / discussion parity).
            if compact_hook is not None:
                rebuilt = await compact_hook(round_no, wire_messages)
                if rebuilt is not None:
                    wire_messages[:] = rebuilt
            else:
                wire_messages[:] = await maybe_compress_wire(
                    wire_messages,
                    compressor=self._compressor,
                    model_hint=model_hint,
                    threshold_ratio=self._compress_threshold_ratio,
                    preserve_tail=COMPRESS_PRESERVE_TAIL,
                    log_context=compress_log_context,
                )

            # ②-bis mid-turn user injection seam (V2 enhancement). Fires in the
            # gap between finishing the previous tool round and opening this
            # round's LLM stream — i.e. the inter-round seam the user's "inject"
            # button targets. The hook drains the tab's pending injections
            # (FIFO) + owns their persistence / frame emission; the kernel only
            # appends each as a ``role:user`` message so the model sees them on
            # THIS round. Runs AFTER compaction so an injection is never dropped
            # by a wire rebuild, and BEFORE ``build_send_wire`` so it is part of
            # the bytes actually sent. ``None`` (sub-agent / discussion / legacy)
            # → no-op, behaviour unchanged.
            if inject_hook is not None:
                for injected_content in await inject_hook(round_no):
                    # ``injected_content`` is the ready-to-send wire content:
                    # a plain ``str`` (text-only injection) or a multimodal
                    # block list (text+image injection). Either way it is a
                    # valid ``role:user`` ``content`` — the kernel does not
                    # special-case (mirrors the question-answer image path).
                    injected_msg: dict[str, Any] = {
                        "role": "user",
                        "content": injected_content,
                    }
                    if assistant_timestamp is not None:
                        injected_msg["created_at"] = assistant_timestamp()
                    wire_messages.append(injected_msg)

            yield KernelRoundStarted(round_no=round_no)

            # ③ build the wire actually sent (blank sentinel / strip created_at).
            send_wire = build_send_wire(wire_messages)

            # Per-round prompt-snapshot hook (main loop): the caller stashes
            # the returned request_id in its own closure for its emitter; the
            # kernel never touches request_id.
            if on_round_open is not None:
                await on_round_open(round_no, send_wire)

            # ④ open this round's LLM stream + ⑤ drain & classify its frames.
            drain = _RoundDrain()
            stream = open_round_stream(round_no=round_no, send_wire=send_wire)
            async for ev in self._drain_round(
                round_no, stream, drain, forward_tool_calls_inline, abort_check
            ):
                yield ev
            if drain.aborted:
                # The user aborted mid-stream — stop now (the caller's abort
                # path handles persistence / retag; parity with the old
                # ``_drain_followup_round`` per-frame abort short-circuit).
                yield KernelAborted(round_no=round_no)
                return
            if drain.error_seen:
                return
            round_text = "".join(drain.text_parts).strip()

            # END-frame policy hook (main loop): usage accumulation / empty-
            # completion retry / experience extraction / no-progress. Fires
            # only when the stream actually ended (parity with the main loop,
            # whose END logic ran only on a terminal END frame).
            if on_round_end is not None and drain.ended:
                decision = await on_round_end(
                    round_no,
                    drain.end_payload,
                    round_text,
                    list(drain.tool_calls),
                )
                if decision.retry and decision.retry_wire is not None:
                    # Empty-completion retry: swap in the synthetic-nudge wire
                    # and open ANOTHER round WITHOUT executing this round's
                    # (empty) tool calls — mirrors the main loop's `continue`.
                    wire_messages[:] = decision.retry_wire
                    continue
                if decision.stop:
                    # Graceful close (no-progress / cap) WITHOUT executing this
                    # round's pending calls. The caller's emitter renders its
                    # own notice + END frame off this KernelFinished.
                    yield KernelFinished(
                        round_no=round_no,
                        final_text=decision.final_text,
                        end_payload=drain.end_payload,
                    )
                    return

            # ⑥a self-contained agent (``query::*`` — MB Pro / CEBot) OR no
            # tool calls → finish the turn.
            #
            # A self-contained agent runs its OWN agentic loop server-side and
            # streams the final answer + ``tool_call`` / ``tool_result`` frames
            # as DISPLAY cards plus a terminal END. Its ``tool_calls`` are NOT a
            # request for THIS kernel to execute tools and open another round —
            # doing so re-prompts the agent every round and never terminates
            # (the "MB Pro 推理完成但界面一直忙碌 / 第N轮" stuck-busy bug in
            # discussion / implementation mode). So we finish after the agent's
            # stream ends regardless of any display tool_calls present.
            if is_self_contained_agent_hint(model_hint) or not drain.tool_calls:
                if grow_wire_hook is None and round_text:
                    msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": round_text,
                    }
                    if assistant_timestamp is not None:
                        msg["created_at"] = assistant_timestamp()
                    wire_messages.append(msg)
                yield KernelFinished(
                    round_no=round_no,
                    final_text=round_text,
                    end_payload=drain.end_payload,
                )
                return

            # ⑥b tool calls present.
            tool_metas = build_tool_metas(drain.tool_calls, round_no)
            # Re-derive the Vertex AI ``thought_signature`` the model emitted on
            # each TOOL_CALL frame, keyed by the meta's resolved ``call_id``
            # (``tool_metas`` is 1:1 in order with ``drain.tool_calls``). Hoisted
            # OUT of the wire-growth branch below so it is available BOTH for the
            # built-in wire growth (``build_assistant_tool_calls_block``) AND for
            # the :class:`KernelToolCallsIssued` event a caller may read to
            # persist its OWN structured tool cards with the signature intact
            # (the discussion orchestrator's per-speaker ``Message.tool_calls``).
            # Empty for non-thinking models → every downstream use is unchanged.
            _round_signatures: dict[str, str] = {}
            for (_n, _a, _cid), _payload in zip(
                tool_metas, drain.tool_calls, strict=False
            ):
                if not isinstance(_payload, dict):
                    continue
                _sig = _payload.get("thought_signature")
                if _sig:
                    _round_signatures[_cid] = _sig
            if grow_wire_hook is None:
                # Built-in wire growth (sub-agent / discussion): append the
                # round's ``assistant{tool_calls}`` block now; the paired
                # ``role:tool`` replies follow inside ``_execute_tool_round``.
                #
                # Re-attach the Vertex AI ``thought_signature`` the model
                # emitted on each TOOL_CALL frame onto the rebuilt wire entry —
                # IDENTICAL to the main loop's ``_append_tool_round``
                # (``streaming.py``: ``build_assistant_tool_calls_block(...,
                # thought_signatures=...)``). WHY THIS MATTERS (父子统一 / bug
                # fix): a model whose catalog params declare
                # ``thought_signature.required`` (e.g. Claude-4 thinking via
                # Bedrock) triggers the pre-send
                # ``flatten_tool_calls_without_signature`` guard. If the
                # assistant ``tool_calls`` lack signatures, that guard FOLDS the
                # ``assistant{tool_calls}`` + ``tool`` pair into a single
                # trailing PLAIN-TEXT assistant message — so the wire then ENDS
                # WITH AN ASSISTANT message and Bedrock rejects the follow-up
                # ("does not support assistant message prefill; the conversation
                # must end with a user message" → HTTP 400). The main loop never
                # hit this because it always re-attached signatures; the
                # sub-agent / discussion built-in path did NOT, so ONLY they
                # 400'd on such models. ``_round_signatures`` (computed above)
                # keeps the signatures aligned. Empty → block unchanged.
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": round_text or TOOL_CALLS_CONTENT_SENTINEL,
                    "tool_calls": build_assistant_tool_calls_block(
                        tool_metas,
                        thought_signatures=(
                            _round_signatures if _round_signatures else None
                        ),
                    ),
                }
                if assistant_timestamp is not None:
                    assistant_msg["created_at"] = assistant_timestamp()
                wire_messages.append(assistant_msg)

            yield KernelToolCallsIssued(
                round_no=round_no,
                tool_metas=list(tool_metas),
                assistant_text=round_text,
                thought_signatures=dict(_round_signatures),
            )

            finals: list[ToolExecutionItem] = []
            async for ev in self._execute_tool_round(
                round_no=round_no,
                tool_metas=tool_metas,
                tool_executor=tool_executor,
                wire_messages=wire_messages,
                include_tool_name_in_reply=include_tool_name_in_reply,
                grow_wire=grow_wire_hook is None,
                finals_out=finals,
            ):
                yield ev

            # Caller-owned wire growth (main loop): hand it this round's
            # lead-in text + metas + finals so it grows the wire exactly like
            # ``_append_tool_round`` (id precedence / thought_signature /
            # ``content: None`` vs sentinel) and updates its bookkeeping.
            if grow_wire_hook is not None:
                await grow_wire_hook(round_no, round_text, tool_metas, finals)

            # Per-round persist hook (§15.2): fire AFTER the round's wire is
            # complete and BEFORE the next round's abort check (sub-agent
            # ``_record_round``; State-Truth-First abort ordering).
            if on_tool_round_complete is not None:
                await on_tool_round_complete(round_no)

            # ⑦ round budget exhausted while still issuing tool calls.
            if round_no == max_rounds:
                yield KernelMaxRoundsReached(
                    round_no=round_no,
                    max_rounds=max_rounds,
                    last_text=round_text,
                )
                return

    async def _drain_round(
        self,
        round_no: int,
        stream: AsyncIterator[StreamFrame],
        drain: _RoundDrain,
        forward_tool_calls_inline: bool = False,
        abort_check: AbortCheck | None = None,
    ) -> AsyncIterator[KernelEvent]:
        """Drain one LLM stream into ``drain``, yielding chunk/error events.

        Classifies frames into CHUNK (accumulate text + yield
        :class:`KernelChunk`) / TOOL_CALL (collect payload + frame; yield
        :class:`KernelToolCallSeen` inline when ``forward_tool_calls_inline``,
        the main loop's byte-for-byte inline forwarding) / ERROR (yield
        :class:`KernelError`, set ``error_seen``, stop) / END (stop).

        TOOL_RESULT frames produced DURING the drain — the cloud SSE adapter's
        throttled ``phase="generating_args"`` + ``partial=True`` progress frames
        emitted while the model streams a long tool-call argument
        (``infrastructure/llm_stream.py:_emit_args_progress``) — are forwarded
        verbatim as :class:`KernelStreamPassthrough`. They are pure UI progress
        (so the turn doesn't look frozen for ~minutes while a big argument is
        generated) and MUST NOT touch ``drain`` (not collected as tool calls,
        not folded into the wire) or influence any finish / execute / round-end
        decision — only passed through to the main loop's emitter, which stamps
        + forwards them like the pre-kernel ``_drain_followup_round`` did.

        ``abort_check`` is polled PER FRAME (the main loop aborts mid-stream:
        the user clicks stop while the round is streaming). When it fires the
        drain sets ``drain.aborted`` and returns immediately so ``run`` yields
        :class:`KernelAborted` and the caller's abort path handles persistence
        (parity with the old ``_drain_followup_round`` per-frame check).
        """
        async for frame in stream:
            if abort_check is not None and abort_check():
                drain.aborted = True
                return
            if frame.frame_type is StreamFrameType.CHUNK:
                text = frame.payload.get("text", "")
                if isinstance(text, str) and text:
                    drain.text_parts.append(text)
                    yield KernelChunk(round_no=round_no, text=text)
            elif frame.frame_type is StreamFrameType.REASONING:
                # Model "thinking" tokens: pure live-UI progress. Forward
                # verbatim so the follow-up round's reasoning reaches the UI
                # (same as the initial round), but do NOT append to
                # ``drain.text_parts`` — reasoning is not the assistant's
                # answer and must never pollute the persisted message content
                # or the next round's wire history. Passthrough keeps it off
                # the wire / out of control-flow decisions, exactly like the
                # generating-args TOOL_RESULT case below. The emitter's
                # KernelStreamPassthrough branch round-stamps it (REASONING is
                # in _ROUND_STAMPED_FRAME_TYPES) so it binds to this round's
                # assistant message.
                yield KernelStreamPassthrough(round_no=round_no, frame=frame)
            elif frame.frame_type is StreamFrameType.TOOL_CALL:
                drain.tool_calls.append(frame.payload)
                drain.tool_call_frames.append(frame)
                if forward_tool_calls_inline:
                    yield KernelToolCallSeen(round_no=round_no, frame=frame)
            elif frame.frame_type is StreamFrameType.TOOL_RESULT:
                # Drain-phase TOOL_RESULT == the cloud adapter's
                # ``generating_args`` progress frame (a long tool-call argument
                # being streamed). Forward it verbatim for live UI WITHOUT
                # collecting it as a tool call / result, growing the wire, or
                # affecting any control-flow decision (it is not a real tool
                # result — those arrive later from the tool-execution producer).
                yield KernelStreamPassthrough(round_no=round_no, frame=frame)
            elif frame.frame_type is StreamFrameType.ERROR:
                message = frame.payload.get("message", "unknown error")
                drain.error_seen = True
                # Carry the ORIGINAL error frame so the main follow-up emitter
                # can re-emit it verbatim (code / retryable / message). Without
                # this the follow-up loop swallowed every post-tool-call network
                # error: the turn ended silently with a misleading ``done`` and
                # no retry hint reached the UI (the reported bug).
                yield KernelError(
                    round_no=round_no, message=message, frame=frame
                )
                return
            elif frame.frame_type is StreamFrameType.END:
                if isinstance(frame.payload, dict):
                    drain.end_payload = frame.payload
                drain.ended = True
                return
            else:
                # Any other frame type is a NON-control, blind-forward progress
                # frame (e.g. NETWORK_RETRY emitted by the network auto-retry
                # wrapper, or any future tail-only §3.1 frame). Forward it
                # verbatim as live UI progress WITHOUT touching ``drain`` or any
                # finish / execute / round-end decision — same contract as the
                # REASONING / generating-args TOOL_RESULT passthroughs above.
                # Without this default, such a frame reaching a FOLLOW-UP round
                # would be silently dropped (the kernel only handled the control
                # frame types), so the UI would lose the retry banner mid-turn.
                yield KernelStreamPassthrough(round_no=round_no, frame=frame)

    async def _execute_tool_round(
        self,
        *,
        round_no: int,
        tool_metas: list[tuple[str, dict[str, Any], str]],
        tool_executor: ToolExecutor,
        wire_messages: list[dict[str, Any]],
        include_tool_name_in_reply: bool,
        grow_wire: bool = True,
        finals_out: list[ToolExecutionItem] | None = None,
    ) -> AsyncIterator[KernelEvent]:
        """Run the round's tools (producer), yield events, grow the wire.

        Forwards each PARTIAL item as a :class:`KernelToolPartial` (live UI;
        never folded into the wire) and each FINAL as a
        :class:`KernelToolResult`. When ``grow_wire`` is True the kernel
        appends one ``role:tool`` reply per call IN ORIGINAL ORDER (strict id
        pairing) onto ``wire_messages`` (sub-agent / discussion); when False
        the caller owns wire growth via ``grow_wire_hook`` (main loop). The
        FINAL items are collected into ``finals_out`` (in original call order)
        so the caller's ``grow_wire_hook`` can reuse them.
        """
        finals: dict[str, ToolExecutionItem] = {}
        async for item in tool_executor(
            round_no=round_no, tool_metas=tool_metas
        ):
            if item.partial:
                yield KernelToolPartial(
                    round_no=round_no,
                    tool_name=item.tool_name,
                    call_id=item.call_id,
                    delta=item.delta,
                    frame=item.frame,
                )
                continue
            if item.passthrough:
                # A non-result passthrough frame (main loop's SUBAGENT_* /
                # AGENT_SUMMARY): forward it as a KernelToolPartial carrying the
                # frame, and do NOT count it as a final.
                yield KernelToolPartial(
                    round_no=round_no,
                    tool_name=item.tool_name,
                    call_id=item.call_id,
                    delta=item.delta,
                    frame=item.frame,
                )
                continue
            finals[item.call_id] = item
            yield KernelToolResult(
                round_no=round_no,
                tool_name=item.tool_name,
                call_id=item.call_id,
                arguments=item.arguments,
                result_text=item.result_text,
                ok=item.ok,
                truncated=item.truncated,
                original_length=item.original_length,
                duration_ms=item.duration_ms,
                frame=item.frame,
                cancelled=item.cancelled,
            )

        if finals_out is not None:
            # Original call order (a missing final is simply absent).
            for (_n, _a, cid) in tool_metas:
                if cid in finals:
                    finals_out.append(finals[cid])

        if not grow_wire:
            return

        # Pair finals back to ``tool_metas`` IN ORIGINAL ORDER for the
        # ``role:tool`` reply blocks (strict id pairing). A missing final
        # (executor bug) surfaces as an empty result so the orphan guard
        # never drops the assistant.tool_calls entry.
        ordered_results: list[Any] = [
            finals[cid].result_text if cid in finals else ""
            for (_n, _a, cid) in tool_metas
        ]
        # Per-call execution time (ms), paired 1:1 with ``ordered_results``,
        # persisted as a display-only ``duration_ms`` on each ``role:tool``
        # block so a reloaded sub-agent tab card shows "took N ms" (parity with
        # the main agent's persisted ``durationMs``). Stripped before the wire
        # is sent to the model (``build_send_wire`` ``_display_only``).
        ordered_durations: list[int | None] = [
            finals[cid].duration_ms if cid in finals else None
            for (_n, _a, cid) in tool_metas
        ]
        wire_messages.extend(
            build_tool_reply_blocks(
                tool_metas,
                ordered_results,
                include_name=include_tool_name_in_reply,
                durations_ms=ordered_durations,
            )
        )

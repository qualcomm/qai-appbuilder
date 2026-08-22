# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain entities for the ai_coding bounded context.

Aggregates / entities defined here:

* :class:`CodingSession` — the aggregate root.  Encapsulates the state
  machine that unifies Claude Code (CC) and OpenCode (OC) sessions; the
  inventory documents (``08-business-capabilities.md`` §4) flag the
  legacy ``ClaudeCodeSessionManager`` (2,600 LoC) and OC-specific
  manager as the project's single biggest code smell.  This rewrite
  collapses both into one aggregate parameterised by :class:`Provider`.
* :class:`PermissionRequest` — request the user must approve before
  the agent runs a privileged tool.
* :class:`ToolInvocation` — record of one tool call, used both to
  drive the SSE stream and to feed the audit log.
* :class:`Skill` — registered capability; mirror of the legacy
  ``skill_policy`` spec without any security coupling.

The classes are deliberately *not* frozen: aggregates evolve through
state transitions.  Each public mutator (``mark_active``,
``request_permission``, ...) enforces the relevant invariants and
appends a :class:`DomainEvent` to ``pending_events``; the application
layer is then responsible for flushing those onto the
:class:`EventBus`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .errors import (
    CodingSessionAlreadyTerminatedError,
    InvalidSessionStateError,
    PermissionRequestAlreadyDecidedError,
    PermissionRequestNotFoundError,
)
from .events import (
    CodingSessionInterruptedEvent,
    CodingSessionRenamedEvent,
    CodingSessionRestoredEvent,
    CodingSessionStartedEvent,
    CodingSessionStatusChangedEvent,
    CodingSessionStreamFrameEvent,
    CodingSessionTerminatedEvent,
    EffortChangedEvent,
    MessageHistoryTruncatedEvent,
    PermissionDecidedEvent,
    NotifyBindingChangedEvent,
    PermissionRequestedEvent,
    SkillRegisteredEvent,
    ToolCompletedEvent,
    ToolFailedEvent,
    ToolInvokedEvent,
    WorkspaceChangedEvent,
)
from .value_objects import (
    CodingSessionConfig,
    CodingSessionId,
    CodingStreamFrame,
    MessageContent,
    PermissionDecision,
    PermissionRequestId,
    Provider,
    SessionStatus,
    ToolInvocationId,
    ToolName,
    Workspace,
)

__all__ = [
    "CodingSession",
    "PermissionRequest",
    "Skill",
    "ToolInvocation",
]


# ---------------------------------------------------------------------------
# PermissionRequest
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PermissionRequest:
    """A pending or finalised authorisation prompt for a tool invocation.

    A :class:`CodingSession` may have at most one *pending* permission
    request at a time (enforced by :meth:`CodingSession.request_permission`).
    """

    request_id: PermissionRequestId
    tool_name: ToolName
    args: dict[str, Any]
    requested_at: datetime
    decision: PermissionDecision = PermissionDecision.PENDING
    decided_at: datetime | None = None

    def decide(self, decision: PermissionDecision, *, now: datetime) -> None:
        """Transition the request to its final state.

        ``decision`` must be either ``APPROVED`` or ``REJECTED``;
        passing ``PENDING`` is a programming error.
        """
        if decision is PermissionDecision.PENDING:
            raise ValueError("decision must be APPROVED or REJECTED")
        if self.decision is not PermissionDecision.PENDING:
            raise PermissionRequestAlreadyDecidedError(
                message=(
                    f"permission request {self.request_id} already decided "
                    f"as {self.decision.value}"
                ),
                details={
                    "request_id": str(self.request_id),
                    "current_decision": self.decision.value,
                },
            )
        self.decision = decision
        self.decided_at = now

    def is_expired(self, *, now: datetime, ttl_seconds: float) -> bool:
        """Return ``True`` when this PENDING request has outlived ``ttl_seconds``.

        2-H14 (pending TTL).  V1 wrapped the approval future in
        ``asyncio.wait_for(future, timeout=permission_approval_timeout_seconds)``
        (default 120s; ``session_manager.py:1475,1586``) and
        auto-rejected on expiry (line 1644-1648).  A non-pending request
        is never "expired" (it already has a terminal decision).  A
        non-positive ``ttl_seconds`` disables expiry (returns ``False``).
        """
        if self.decision is not PermissionDecision.PENDING:
            return False
        if ttl_seconds <= 0:
            return False
        age = (now - self.requested_at).total_seconds()
        return age >= ttl_seconds


# ---------------------------------------------------------------------------
# ToolInvocation
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ToolInvocation:
    """One tool call dispatched on behalf of a coding session.

    Status values are deliberately string literals (not an enum) to
    keep persistence trivial; the small set is documented here:

    * ``"running"`` — bridge accepted the call; awaiting result.
    * ``"completed"`` — tool returned successfully.
    * ``"failed"`` — tool raised or returned a failure marker.
    """

    invocation_id: ToolInvocationId
    tool_name: ToolName
    args: dict[str, Any]
    started_at: datetime
    status: str = "running"
    finished_at: datetime | None = None
    duration_ms: int | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None

    def complete(
        self,
        *,
        finished_at: datetime,
        result: dict[str, Any],
    ) -> None:
        if self.status != "running":
            raise InvalidSessionStateError(
                message=(
                    f"tool invocation {self.invocation_id} cannot complete "
                    f"from status {self.status!r}"
                ),
            )
        self.status = "completed"
        self.finished_at = finished_at
        self.result = result
        delta = finished_at - self.started_at
        self.duration_ms = int(delta.total_seconds() * 1000)

    def fail(self, *, finished_at: datetime, error_code: str) -> None:
        if self.status != "running":
            raise InvalidSessionStateError(
                message=(
                    f"tool invocation {self.invocation_id} cannot fail "
                    f"from status {self.status!r}"
                ),
            )
        self.status = "failed"
        self.finished_at = finished_at
        self.error_code = error_code
        delta = finished_at - self.started_at
        self.duration_ms = int(delta.total_seconds() * 1000)


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True, kw_only=True)
class Skill:
    """A coding capability that can be advertised to the agent.

    The new model intentionally drops the security coupling that the
    legacy ``backend/security/skill_policy.py`` carried; security
    decisions are routed through ``PermissionDecisionPort`` instead so
    the ai_coding context never imports ``qai.security.*``.
    """

    name: str
    description: str
    spec: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# CodingSession (aggregate root)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CodingSession:
    """Aggregate root for a single coding agent session.

    The state machine (see :class:`SessionStatus`) is enforced by the
    public mutators; callers MUST NOT poke at attributes directly.
    Pending domain events are accumulated in
    :attr:`pending_events` and consumed by the application layer.
    """

    session_id: CodingSessionId
    provider: Provider
    workspace: Workspace
    created_at: datetime
    status: SessionStatus = SessionStatus.PENDING
    title: str | None = None
    messages: list[MessageContent] = field(default_factory=list)
    permission_requests: dict[PermissionRequestId, PermissionRequest] = field(
        default_factory=dict
    )
    tool_invocations: dict[ToolInvocationId, ToolInvocation] = field(
        default_factory=dict
    )
    last_stream_sequence: int = -1
    terminated_at: datetime | None = None
    termination_reason: str | None = None
    pending_events: list[Any] = field(default_factory=list)
    # PR-107: SDK 12 enhancements (legacy ``backend/ai_coding`` parity).
    # Append-only per §3.1 field-name lock; default empty config keeps
    # the historical PR-046 / PR-102 spawn paths working unchanged.
    config: CodingSessionConfig = field(default_factory=CodingSessionConfig)
    # Cross-process session continuity: the upstream provider's session
    # identifier (e.g. Anthropic conversation id).  Persisted so that
    # after a process restart the provider adapter can resume the
    # upstream conversation instead of starting a fresh context window.
    claude_session_id: str | None = None
    # Dual-channel notify binding (legacy ``backend/ai_coding`` parity):
    # when set, WebUI turns are mirror-pushed to the bound WeChat user /
    # Feishu open-id so the user can follow the conversation from the
    # channel app.  Append-only per §3.1 field-name lock; both default
    # ``None`` (no binding).  Backs the legacy
    # ``POST /api/cc/sessions/{id}/wechat_notify`` +
    # ``.../feishu_notify`` routes.
    wechat_notify_user_id: str | None = None
    feishu_notify_user_id: str | None = None
    # V1 parity (legacy ``backend/ai_coding/session_manager.py``:2138-2140 +
    # 2401-2416): every CC/OC streaming turn measured a wall-clock duration
    # via ``_time.monotonic()`` and emitted it on the ``done`` frame as
    # ``duration_s`` (rounded to 1 decimal). The user-facing surface in V1
    # showed "本次会话耗时 X.X s" on the session badge / header. Persist the
    # most-recent turn duration on the aggregate so:
    #
    # * the SSE ``done`` frame can carry ``duration_s`` (wire parity);
    # * the REST ``CodingSessionResponse`` exposes it tail-appended as
    #   ``last_duration_s`` for after-the-fact display.
    #
    # ``None`` means "no completed streaming turn yet" (fresh session) —
    # the value is only ever written by
    # :class:`StreamCodingSessionUseCase` after a successful stream.
    # Append-only per §3.1 field-name lock.
    last_duration_s: float | None = None
    # U-010 / 2-H2 (token + context full chain).  V1 parity: both the
    # CC manager (``backend/ai_coding/session_models.py:62-66``) and the
    # OC manager (``opencode_session_models.py:62`` + ``total_cost``)
    # tracked per-session cumulative token usage so the WebUI could
    # render the "上下文占用 / 累计用量" badge.  V1 surfaced these via
    # ``ClaudeCodeSession.to_dict`` (``total_tokens`` / ``total_input_tokens``
    # / ``total_output_tokens`` / ``total_tool_calls`` / ``last_input_tokens``
    # / ``context_window``) and the ``context_size`` route.  V2 lifts the
    # six counters onto the aggregate (append-only per §3.1) so the
    # streaming use case can write them back from the provider's
    # ``usage`` frame and the context-usage / context-size endpoints can
    # read them off persistence.  Semantics (mirroring V1):
    #
    #   total_input_tokens  — cumulative input tokens across all turns.
    #   total_output_tokens — cumulative output tokens across all turns.
    #   total_tool_calls    — cumulative tool-use blocks across all turns.
    #   last_input_tokens   — input tokens of the MOST RECENT turn (the
    #                         best approximation of "current context
    #                         occupancy" — what the next turn will resend).
    #   context_window      — model context-window size (e.g. 200000),
    #                         learned from the provider's usage metadata.
    #   total_cost          — cumulative cost (OC ``StepFinishPart`` cost
    #                         accumulator; CC leaves it at 0.0).
    #
    # All default 0 / 0.0 (fresh session) — never None — so callers can
    # always read a concrete number.  Written only by
    # :meth:`record_token_usage`.
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tool_calls: int = 0
    last_input_tokens: int = 0
    context_window: int = 0
    total_cost: float = 0.0
    # 2-H10: OpenCode per-session provider / model selection.  V1 stored
    # the operator's runtime ``/oc model`` choice on the session record
    # (``opencode_session_models.py:67-68`` ``current_provider`` /
    # ``current_model``) so it survived a daemon restart — without it the
    # next turn fell back to OpenCode's server default and silently
    # changed model.  V2 lifts the two fields onto the aggregate
    # (append-only per §3.1) + persists them (migration 025) so the
    # OpenCode adapter resolves the same provider/model after a restart.
    # Both default ``None`` (use the server default) and are only set for
    # OpenCode sessions; the Claude Code path leaves them ``None``.
    oc_current_provider: str | None = None
    oc_current_model: str | None = None
    # 2-H3 / RE-OC-7: the OpenCode-native message ids learned per turn,
    # in turn order, so a revert / rewind can call OpenCode's native
    # ``POST /session/{id}/revert`` with the right ``messageID`` AFTER a
    # daemon restart (V1 forwarded the frontend-supplied messageID
    # directly — ``opencode_session_manager.py:1138-1169`` — and so worked
    # for any historical turn; V2's provider only cached them in-process,
    # silently no-op'ing native revert after a restart).  Persisting them
    # on the aggregate (migration 033) makes the native revert restart-safe.
    # Empty tuple for fresh / non-OpenCode sessions; written only by
    # :meth:`record_oc_message_ids`.  Append-only per §3.1 field-name lock.
    oc_message_ids: tuple[str, ...] = ()
    # 2-H12: turn-count + over-turn-warning bookkeeping.  V1 counted
    # completed streaming turns and emitted an escalating "⚠️ N 轮对话"
    # warning at 20 / 25 / 30 / … turns, tracking the last-emitted
    # threshold so each tier fires exactly once
    # (``session_manager.py:107-130,2141-2155``).  V2 lifts the two
    # counters onto the aggregate (append-only per §3.1) + persists them
    # (migration 026) so the warning cadence survives a daemon restart.
    # Both default 0 (fresh session); written only by
    # :meth:`record_turn_completed`.
    turn_count: int = 0
    last_turn_warning_threshold: int = 0

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------
    @classmethod
    def spawn(
        cls,
        *,
        session_id: CodingSessionId,
        provider: Provider,
        workspace: Workspace,
        now: datetime,
        title: str | None = None,
        config: CodingSessionConfig | None = None,
    ) -> CodingSession:
        """Construct a fresh session in the ``ACTIVE`` state.

        The session begins life as ``ACTIVE`` rather than ``PENDING``
        because by the time this factory is called the application
        layer has already acquired a workspace lock and validated the
        provider availability.  ``PENDING`` is reserved for cases where
        a session is rehydrated from persistence with no live process.

        ``config`` (PR-107) carries the SDK 12-item enhancements
        (mcp_servers / hooks / fallback_model / …); pass ``None`` for
        the historical "no extras" behaviour.
        """
        session = cls(
            session_id=session_id,
            provider=provider,
            workspace=workspace,
            created_at=now,
            status=SessionStatus.ACTIVE,
            title=title,
            config=config or CodingSessionConfig(),
        )
        session.pending_events.append(
            CodingSessionStartedEvent(
                session_id=session_id,
                provider=provider,
                workspace=workspace,
            )
        )
        return session

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_not_terminated(self) -> None:
        if self.status is SessionStatus.TERMINATED:
            raise CodingSessionAlreadyTerminatedError(
                message=f"coding session {self.session_id} is terminated",
                details={"session_id": str(self.session_id)},
            )

    def _change_status(self, new_status: SessionStatus) -> None:
        if self.status is new_status:
            return
        old = self.status
        self.status = new_status
        self.pending_events.append(
            CodingSessionStatusChangedEvent(
                session_id=self.session_id,
                old_status=old,
                new_status=new_status,
            )
        )

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def mark_active(self) -> None:
        """Move from ``IDLE`` / ``STREAMING`` / ``PERMISSION_REQUESTED`` back to ``ACTIVE``."""
        self._ensure_not_terminated()
        if self.status not in (
            SessionStatus.PENDING,
            SessionStatus.IDLE,
            SessionStatus.STREAMING,
            SessionStatus.PERMISSION_REQUESTED,
            SessionStatus.ACTIVE,
        ):
            raise InvalidSessionStateError(
                message=f"cannot transition from {self.status.value} to active",
            )
        self._change_status(SessionStatus.ACTIVE)

    def mark_idle(self) -> None:
        self._ensure_not_terminated()
        if self.status not in (SessionStatus.ACTIVE, SessionStatus.STREAMING):
            raise InvalidSessionStateError(
                message=f"cannot transition from {self.status.value} to idle",
            )
        self._change_status(SessionStatus.IDLE)

    def mark_streaming(self) -> None:
        self._ensure_not_terminated()
        if self.status is not SessionStatus.ACTIVE:
            raise InvalidSessionStateError(
                message=f"cannot transition from {self.status.value} to streaming",
            )
        self._change_status(SessionStatus.STREAMING)

    def append_message(self, content: MessageContent) -> None:
        self._ensure_not_terminated()
        self.messages.append(content)

    def record_stream_frame(self, frame: CodingStreamFrame) -> None:
        """Persist the metadata of one streaming frame on the aggregate.

        We deliberately do NOT keep frame payloads on the aggregate
        (they can be megabytes); only the sequence number and an event
        for downstream subscribers.
        """
        self._ensure_not_terminated()
        if self.status not in (SessionStatus.STREAMING, SessionStatus.ACTIVE):
            raise InvalidSessionStateError(
                message=(
                    f"cannot record stream frame from status {self.status.value}"
                ),
            )
        if frame.sequence <= self.last_stream_sequence:
            raise InvalidSessionStateError(
                message=(
                    f"frame sequence {frame.sequence} is not strictly "
                    f"increasing (last={self.last_stream_sequence})"
                ),
            )
        self.last_stream_sequence = frame.sequence
        self.pending_events.append(
            CodingSessionStreamFrameEvent(
                session_id=self.session_id,
                kind=frame.kind,
                sequence=frame.sequence,
            )
        )

    def record_turn_completed(self, *, threshold: int) -> bool:
        """Increment the completed-turn count; arm an over-turn warning.

        2-H12.  Called once per completed streaming turn by
        :class:`StreamCodingSessionUseCase`.  ``threshold`` is the
        application-computed over-turn-warning threshold for the NEW
        ``turn_count`` (0 when below the warning floor).  Mirrors V1
        ``session_manager.py:2141-2155``: a warning frame is emitted only
        when a NEW (higher) threshold tier is reached, so each tier (20 /
        25 / 30 / …) fires exactly once.

        Returns ``True`` when the caller should emit a ``turn_warning``
        frame (a fresh threshold tier was reached), ``False`` otherwise.
        Allowed in any non-terminated state; no domain event is emitted
        (turn telemetry is read back through the snapshot, like
        :meth:`record_stream_duration`).
        """
        self._ensure_not_terminated()
        self.turn_count += 1
        if threshold > self.last_turn_warning_threshold:
            self.last_turn_warning_threshold = threshold
            return True
        return False

    def record_stream_duration(self, duration_s: float) -> None:
        """Persist the wall-clock duration of the most recent streaming turn.

        V1 parity: the legacy
        ``backend/ai_coding/session_manager.py:2138-2140 + 2401-2416``
        emitted ``duration_s`` on every ``done`` frame (rounded to 1
        decimal place). The application layer measures the elapsed time
        across the streaming use case and calls this mutator before the
        final ``save`` so the value is durable and visible to the REST
        ``CodingSessionResponse``.

        Rules:

        * ``duration_s`` must be non-negative; we clamp to ``0.0`` on a
          tiny negative due to clock skew rather than raising — losing
          one telemetry point should not abort the turn.
        * The value is rounded to 1 decimal (V1 wire format).
        * Allowed in any non-terminated state — typically called from
          the ``finally`` of the stream use case which then transitions
          the session back to ``ACTIVE``.  Calling on a ``TERMINATED``
          session raises :class:`CodingSessionAlreadyTerminatedError`.
        * No domain event is emitted: turn-level telemetry is not a
          domain concern; the value is read back through the
          aggregate's snapshot (REST GET / list).
        """
        self._ensure_not_terminated()
        if duration_s < 0:
            duration_s = 0.0
        self.last_duration_s = round(float(duration_s), 1)

    def record_token_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        tool_calls: int = 0,
        context_window: int | None = None,
        cost: float = 0.0,
    ) -> None:
        """Fold one turn's provider usage into the cumulative counters.

        U-010 / 2-H2 (token + context full chain).  Called by
        :class:`StreamCodingSessionUseCase` when a provider stream
        surfaces a ``usage`` payload (Anthropic ``input_tokens`` /
        ``output_tokens`` / ``cache_*`` collapsed into a single
        ``input_tokens`` by the provider; OpenCode ``StepFinishPart``
        token + cost totals).

        V1 parity (``session_manager.py:2130-2135`` +
        ``opencode_session_manager.py:928``):

        * ``total_input_tokens`` / ``total_output_tokens`` /
          ``total_tool_calls`` / ``total_cost`` ACCUMULATE across turns.
        * ``last_input_tokens`` is REPLACED with this turn's input count
          (only when ``input_tokens > 0`` — a turn that reports no input
          usage must not zero the best-known context occupancy, matching
          V1's ``if input_t: session.last_input_tokens = input_t``).
        * ``context_window`` is REPLACED only when a positive value is
          learned (``if _context_window > 0`` in V1) so a turn that omits
          the window keeps the previously-learned size.

        Negative values are clamped to 0 (defensive — a malformed usage
        payload must not corrupt the aggregate).  No domain event is
        emitted: token telemetry is read back through the snapshot
        (REST GET / context_usage), mirroring
        :meth:`record_stream_duration`.  Allowed in any non-terminated
        state (typically called mid-stream from the streaming loop).
        """
        self._ensure_not_terminated()
        self.total_input_tokens += max(0, int(input_tokens))
        self.total_output_tokens += max(0, int(output_tokens))
        self.total_tool_calls += max(0, int(tool_calls))
        if input_tokens and int(input_tokens) > 0:
            self.last_input_tokens = int(input_tokens)
        if context_window is not None and int(context_window) > 0:
            self.context_window = int(context_window)
        if cost:
            self.total_cost += max(0.0, float(cost))

    # ------------------------------------------------------------------
    # Permission flow
    # ------------------------------------------------------------------
    def request_permission(
        self,
        *,
        request_id: PermissionRequestId,
        tool_name: ToolName,
        args: dict[str, Any],
        now: datetime,
    ) -> PermissionRequest:
        """Issue a new permission request and pause the session.

        At most one pending permission request may be live at a time;
        nested prompts are a misuse of the protocol and surface as a
        :class:`InvalidSessionStateError`.
        """
        self._ensure_not_terminated()
        if self.status is SessionStatus.PERMISSION_REQUESTED:
            raise InvalidSessionStateError(
                message=(
                    "session already has a pending permission request; "
                    "decide or reject it before issuing another"
                ),
            )
        request = PermissionRequest(
            request_id=request_id,
            tool_name=tool_name,
            args=dict(args),
            requested_at=now,
        )
        self.permission_requests[request_id] = request
        self._change_status(SessionStatus.PERMISSION_REQUESTED)
        self.pending_events.append(
            PermissionRequestedEvent(
                session_id=self.session_id,
                request_id=request_id,
                tool_name=tool_name,
            )
        )
        return request

    def decide_permission(
        self,
        *,
        request_id: PermissionRequestId,
        decision: PermissionDecision,
        now: datetime,
    ) -> PermissionRequest:
        """Resolve a pending permission request.

        On a successful decision the session returns to ``ACTIVE``
        regardless of approve/reject — the agent is then free to either
        continue the original tool call (on approve) or fall back to
        text reasoning (on reject).
        """
        self._ensure_not_terminated()
        request = self.permission_requests.get(request_id)
        if request is None:
            raise PermissionRequestNotFoundError(
                message=f"permission request {request_id} not found",
                details={"request_id": str(request_id)},
            )
        request.decide(decision, now=now)
        self._change_status(SessionStatus.ACTIVE)
        self.pending_events.append(
            PermissionDecidedEvent(
                session_id=self.session_id,
                request_id=request_id,
                decision=decision,
            )
        )
        return request

    def expire_stale_permissions(
        self,
        *,
        now: datetime,
        ttl_seconds: float,
    ) -> list[PermissionRequestId]:
        """Auto-reject pending permission requests older than ``ttl_seconds``.

        2-H14 (pending TTL + idle cleanup).  V1 auto-rejected a
        permission prompt once the approval wait exceeded
        ``permission_approval_timeout_seconds`` (default 120s), resetting
        the session so the agent could continue (the rejected tool call
        falls back to text reasoning).  V2 lifts this to the aggregate:
        every PENDING request that has outlived the TTL is decided
        ``REJECTED`` (emitting :class:`PermissionDecidedEvent` for the
        audit trail), and if the session was paused on a
        ``PERMISSION_REQUESTED`` gate it returns to ``ACTIVE``.

        Returns the ids of the requests that were expired (empty list
        when none / disabled).  No-op on a TERMINATED session (returns
        empty) — a closed session has no live gate to clear.  A
        non-positive ``ttl_seconds`` disables expiry.
        """
        if self.status is SessionStatus.TERMINATED:
            return []
        expired: list[PermissionRequestId] = []
        for request_id, request in self.permission_requests.items():
            if request.is_expired(now=now, ttl_seconds=ttl_seconds):
                request.decide(PermissionDecision.REJECTED, now=now)
                expired.append(request_id)
                self.pending_events.append(
                    PermissionDecidedEvent(
                        session_id=self.session_id,
                        request_id=request_id,
                        decision=PermissionDecision.REJECTED,
                    )
                )
        if expired and self.status is SessionStatus.PERMISSION_REQUESTED:
            self._change_status(SessionStatus.ACTIVE)
        return expired

    # ------------------------------------------------------------------
    # Tool flow
    # ------------------------------------------------------------------
    def start_tool_invocation(
        self,
        *,
        invocation_id: ToolInvocationId,
        tool_name: ToolName,
        args: dict[str, Any],
        now: datetime,
    ) -> ToolInvocation:
        self._ensure_not_terminated()
        if invocation_id in self.tool_invocations:
            raise InvalidSessionStateError(
                message=f"tool invocation {invocation_id} already exists",
            )
        invocation = ToolInvocation(
            invocation_id=invocation_id,
            tool_name=tool_name,
            args=dict(args),
            started_at=now,
        )
        self.tool_invocations[invocation_id] = invocation
        self.pending_events.append(
            ToolInvokedEvent(
                session_id=self.session_id,
                invocation_id=invocation_id,
                tool_name=tool_name,
            )
        )
        return invocation

    def complete_tool_invocation(
        self,
        *,
        invocation_id: ToolInvocationId,
        result: dict[str, Any],
        now: datetime,
    ) -> ToolInvocation:
        self._ensure_not_terminated()
        invocation = self.tool_invocations.get(invocation_id)
        if invocation is None:
            raise InvalidSessionStateError(
                message=f"tool invocation {invocation_id} not found",
            )
        invocation.complete(finished_at=now, result=result)
        # ``invocation.duration_ms`` is set by ``complete``.
        duration = invocation.duration_ms or 0
        self.pending_events.append(
            ToolCompletedEvent(
                session_id=self.session_id,
                invocation_id=invocation_id,
                duration_ms=duration,
            )
        )
        return invocation

    def fail_tool_invocation(
        self,
        *,
        invocation_id: ToolInvocationId,
        error_code: str,
        now: datetime,
    ) -> ToolInvocation:
        self._ensure_not_terminated()
        invocation = self.tool_invocations.get(invocation_id)
        if invocation is None:
            raise InvalidSessionStateError(
                message=f"tool invocation {invocation_id} not found",
            )
        invocation.fail(finished_at=now, error_code=error_code)
        self.pending_events.append(
            ToolFailedEvent(
                session_id=self.session_id,
                invocation_id=invocation_id,
                error_code=error_code,
            )
        )
        return invocation

    # ------------------------------------------------------------------
    # Workspace mutation (PR-106 / U1 decision)
    # ------------------------------------------------------------------
    def change_workspace(
        self,
        new_workspace: Workspace,
        *,
        now: datetime,  # noqa: ARG002 — kept for API symmetry with other mutators
    ) -> None:
        """Reassign the session's workspace at runtime.

        Implements the U1 decision recorded in
        ``docs/90-refactor/S8-parity-audit.md`` §4: the legacy
        ``POST /sessions/{id}/working_dir`` route mutates the working
        directory of a live coding session, so the previously-immutable
        ``CodingSession.workspace`` invariant is **relaxed** and replaced
        by a controlled mutator + :class:`WorkspaceChangedEvent`.

        Rules:

        * Calling on a ``TERMINATED`` session raises
          :class:`CodingSessionAlreadyTerminatedError` (delegated through
          :meth:`_ensure_not_terminated`).
        * If ``new_workspace`` equals the current ``workspace``, this is
          a no-op: no mutation, no event, no exception.  This matches
          the legacy idempotent semantics where re-POSTing the same
          ``working_dir`` returns 200 without side effects.
        * The workspace lock swap (release old, acquire new, rollback
          on failure) is the **application layer's** responsibility;
          the domain method only updates the aggregate field and queues
          the event.  See
          :class:`qai.ai_coding.application.use_cases.ChangeWorkspaceUseCase`.

        ``now`` is accepted for signature symmetry with the other
        aggregate mutators even though :class:`WorkspaceChangedEvent`
        does not carry an explicit timestamp (the event envelope adds
        one); future schema additions are therefore non-breaking.
        """
        self._ensure_not_terminated()
        if new_workspace == self.workspace:
            return
        old = self.workspace
        self.workspace = new_workspace
        self.pending_events.append(
            WorkspaceChangedEvent(
                session_id=self.session_id,
                old_workspace=old,
                new_workspace=new_workspace,
            )
        )

    # ------------------------------------------------------------------
    # Session metadata mutation (PR-104a)
    # ------------------------------------------------------------------
    def rename(self, new_title: str) -> None:
        """Update the session's display ``title``.

        Backs the legacy ``POST /sessions/{id}/rename`` route.

        Rules:

        * ``new_title`` must be a non-empty string after stripping
          whitespace; the application layer is expected to validate
          and pass a clean value (the route layer raises 400 on empty
          input before calling here).
        * Renaming a ``TERMINATED`` session is allowed — the legacy
          behaviour permits renaming closed sessions in history view.
          We therefore do NOT call :meth:`_ensure_not_terminated`
          here.
        """
        cleaned = new_title.strip()
        if not cleaned:
            raise InvalidSessionStateError(
                message="title must be a non-empty string",
            )
        if cleaned == (self.title or ""):
            # Idempotent: same title → no event, no mutation.
            return
        old = self.title
        self.title = cleaned
        self.pending_events.append(
            CodingSessionRenamedEvent(
                session_id=self.session_id,
                old_title=old,
                new_title=cleaned,
            )
        )

    def set_effort(self, new_effort: str | None) -> None:
        """Set or clear the session-level thinking-depth override.

        Backs the legacy ``POST /sessions/{id}/effort`` route. Allowed
        values are ``"low"`` / ``"medium"`` / ``"high"`` / ``"max"``;
        :data:`None` clears the override and the global default
        applies.

        Rules:

        * Calling on a ``TERMINATED`` session raises
          :class:`CodingSessionAlreadyTerminatedError`.
        * No-op when ``new_effort`` equals the current value.
        * Validation of allowed values is delegated to
          :class:`CodingSessionConfig.__post_init__`; passing an
          invalid value here surfaces as :class:`ValueError`.
        """
        self._ensure_not_terminated()
        if new_effort == self.config.effort:
            return
        old = self.config.effort
        # ``CodingSessionConfig`` is frozen — replace the whole value
        # object.  ``replace`` re-runs ``__post_init__`` so the
        # ``effort`` value is validated against the legacy CLI set.
        self.config = replace(self.config, effort=new_effort)
        self.pending_events.append(
            EffortChangedEvent(
                session_id=self.session_id,
                old_effort=old,
                new_effort=new_effort,
            )
        )

    def set_oc_model_selection(
        self,
        *,
        provider: str | None,
        model: str | None,
    ) -> None:
        """Persist the OpenCode per-session provider / model selection.

        2-H10.  Backs the legacy ``/oc model`` choice
        (``opencode_session_models.py:67-68``) so the selection survives
        a daemon restart instead of resetting to OpenCode's server
        default.  Either argument may be ``None`` to clear that half of
        the selection (e.g. provider chosen but model left to default).

        Rules:

        * Allowed on a ``TERMINATED`` session (the legacy choice was
          recorded on the history record), so we do NOT call
          :meth:`_ensure_not_terminated`.
        * No-op when both values already equal the current selection.
        * No domain event is emitted: like :meth:`record_stream_duration`
          / token telemetry, this is read back through the aggregate
          snapshot, not a cross-context concern.
        """
        if (
            provider == self.oc_current_provider
            and model == self.oc_current_model
        ):
            return
        self.oc_current_provider = provider
        self.oc_current_model = model

    def record_oc_message_ids(self, message_ids: Sequence[str]) -> None:
        """Persist the OpenCode-native message ids learned this turn.

        2-H3 / RE-OC-7.  Backs restart-safe native revert: the OpenCode
        adapter learns each turn's ``messageID`` from the event stream and
        the streaming use case writes the cumulative ordered list here so
        a later :class:`RevertMessageUseCase` / rewind can resolve a
        ``marker_index`` to the right native ``messageID`` even after a
        daemon restart (V1 ``opencode_session_manager.py:1138-1169``
        forwarded the frontend messageID directly and thus worked for any
        historical turn).

        Rules:

        * Allowed on a ``TERMINATED`` session (revert/rewind metadata, like
          the token/notify telemetry) — no :meth:`_ensure_not_terminated`.
        * No-op when the list is unchanged.
        * Stored as a tuple (immutable snapshot); empty input clears it.
        * No domain event — read back through the aggregate snapshot.
        """
        new_ids = tuple(str(m) for m in message_ids if m)
        if new_ids == self.oc_message_ids:
            return
        self.oc_message_ids = new_ids

    def set_wechat_notify(self, user_id: str | None) -> None:
        """Bind or clear the WeChat dual-channel notify user id.

        Backs the legacy ``POST /sessions/{id}/wechat_notify`` route.
        An empty string or :data:`None` clears the binding (the
        application/route layer normalises ``""`` → ``None`` to mirror
        the legacy ``body.get("wechat_user_id") or None`` semantics).

        Rules:

        * Renaming-style metadata mutation: allowed on a ``TERMINATED``
          session (the legacy route lets users (un)bind from the
          history view), so we do NOT call
          :meth:`_ensure_not_terminated`.
        * No-op when ``user_id`` equals the current value; emits a
          :class:`NotifyBindingChangedEvent` otherwise.
        """
        if user_id == self.wechat_notify_user_id:
            return
        old = self.wechat_notify_user_id
        self.wechat_notify_user_id = user_id
        self.pending_events.append(
            NotifyBindingChangedEvent(
                session_id=self.session_id,
                channel="wechat",
                old_user_id=old,
                new_user_id=user_id,
            )
        )

    def set_feishu_notify(self, user_id: str | None) -> None:
        """Bind or clear the Feishu dual-channel notify open id.

        Backs the legacy ``POST /sessions/{id}/feishu_notify`` route;
        structurally symmetric to :meth:`set_wechat_notify`.  Empty
        string / :data:`None` clears the binding.
        """
        if user_id == self.feishu_notify_user_id:
            return
        old = self.feishu_notify_user_id
        self.feishu_notify_user_id = user_id
        self.pending_events.append(
            NotifyBindingChangedEvent(
                session_id=self.session_id,
                channel="feishu",
                old_user_id=old,
                new_user_id=user_id,
            )
        )

    def interrupt(self) -> None:
        """Soft-interrupt the session: cancel current turn, return to ``IDLE``.

        Backs the legacy ``POST /sessions/{id}/interrupt`` route.  The
        application layer is expected to terminate any in-flight
        provider turn / stream BEFORE invoking this domain method;
        the mutator only updates the aggregate state.

        Rules:

        * Calling on a ``TERMINATED`` session raises
          :class:`CodingSessionAlreadyTerminatedError`.
        * Already in ``IDLE``: no-op (no event, no exception).
        * From any other live state (``ACTIVE`` / ``STREAMING`` /
          ``PERMISSION_REQUESTED`` / ``PENDING``): transition to
          ``IDLE`` and emit a :class:`CodingSessionInterruptedEvent`.
        """
        self._ensure_not_terminated()
        if self.status is SessionStatus.IDLE:
            return
        self._change_status(SessionStatus.IDLE)
        self.pending_events.append(
            CodingSessionInterruptedEvent(session_id=self.session_id)
        )

    def restore(self, *, forked: bool = False) -> None:
        """Re-activate a previously-terminated session from history.

        Backs the legacy ``POST /sessions/{id}/restore`` route.  The
        legacy ``fork`` flag is ferried through unchanged; in the new
        domain it is informational on the event (no separate fork
        flow on the aggregate — providers detect the next message
        and decide to fork their backend session).

        Rules:

        * If the session is not ``TERMINATED``, this is a no-op (no
          mutation, no event) — matches the legacy idempotent
          semantics where restoring an active session returns
          ``restored=False``.
        * On a ``TERMINATED`` session: clear ``terminated_at`` /
          ``termination_reason``, transition status back to
          ``ACTIVE``, and emit a
          :class:`CodingSessionRestoredEvent`.
        """
        if self.status is not SessionStatus.TERMINATED:
            return
        # Restore: clear terminal markers + bring status back to ACTIVE.
        self.terminated_at = None
        self.termination_reason = None
        old = self.status
        self.status = SessionStatus.ACTIVE
        self.pending_events.append(
            CodingSessionStatusChangedEvent(
                session_id=self.session_id,
                old_status=old,
                new_status=SessionStatus.ACTIVE,
            )
        )
        self.pending_events.append(
            CodingSessionRestoredEvent(
                session_id=self.session_id,
                forked=forked,
            )
        )

    def truncate_history_after(
        self,
        *,
        marker_index: int,
        include_self: bool = False,
    ) -> int:
        """Drop the trailing slice of :attr:`messages` after ``marker_index``.

        Backs the legacy ``POST /sessions/{id}/truncate_history``
        route.  ``marker_index`` is the 0-based index in
        :attr:`messages` of the user-message anchor; messages strictly
        AFTER it are dropped, plus the anchor itself when
        ``include_self`` is ``True`` (Edit & Resend mode).

        Returns the count of messages removed.

        Rules:

        * Calling on a ``TERMINATED`` session raises
          :class:`CodingSessionAlreadyTerminatedError`.
        * ``marker_index`` out of bounds → :class:`InvalidSessionStateError`.
        * No messages to remove (already at end without ``include_self``)
          → 0 returned, no event emitted.
        """
        self._ensure_not_terminated()
        if marker_index < 0 or marker_index >= len(self.messages):
            raise InvalidSessionStateError(
                message=(
                    f"marker_index {marker_index} is out of bounds "
                    f"(messages len={len(self.messages)})"
                ),
            )
        keep_until = marker_index if include_self else marker_index + 1
        removed = len(self.messages) - keep_until
        if removed <= 0:
            return 0
        self.messages = self.messages[:keep_until]
        self.pending_events.append(
            MessageHistoryTruncatedEvent(
                session_id=self.session_id,
                removed=removed,
                include_self=include_self,
            )
        )
        return removed

    # ------------------------------------------------------------------
    # Termination
    # ------------------------------------------------------------------
    def terminate(self, *, reason: str, now: datetime) -> None:
        """Move the aggregate to the terminal ``TERMINATED`` state.

        Idempotent against double-termination — a re-invocation with
        the same reason is a no-op so the application layer can call
        ``terminate`` from both the explicit close path and a cleanup
        finally clause without worrying about ordering.
        """
        if self.status is SessionStatus.TERMINATED:
            return
        old = self.status
        self.status = SessionStatus.TERMINATED
        self.terminated_at = now
        self.termination_reason = reason
        self.pending_events.append(
            CodingSessionStatusChangedEvent(
                session_id=self.session_id,
                old_status=old,
                new_status=SessionStatus.TERMINATED,
            )
        )
        self.pending_events.append(
            CodingSessionTerminatedEvent(
                session_id=self.session_id,
                reason=reason,
            )
        )

    # ------------------------------------------------------------------
    # Event dispatch helper
    # ------------------------------------------------------------------
    def drain_events(self) -> list[Any]:
        """Return and clear the buffered domain events.

        The application layer calls this after a successful repository
        save so events are only published when persistence succeeds.
        """
        events = list(self.pending_events)
        self.pending_events.clear()
        return events


# ---------------------------------------------------------------------------
# Helpers shared across entities
# ---------------------------------------------------------------------------
def make_skill_registered_event(skill: Skill) -> SkillRegisteredEvent:
    """Construct the canonical ``SkillRegisteredEvent`` for ``skill``.

    Kept in the entities module so the ``register`` use case can stay
    framework-free.
    """
    return SkillRegisteredEvent(skill_name=skill.name)

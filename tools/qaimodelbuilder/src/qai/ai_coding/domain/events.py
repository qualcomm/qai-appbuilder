# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain events emitted by the ai_coding aggregate.

Events use the same dataclass conventions as platform events
(``frozen=True``, ``slots=True``, ``kw_only=True``) and inherit from
:class:`qai.platform.events.types.DomainEvent` so the in-process
:class:`EventBus` can route them to subscribers.

The :pyattr:`event_type` strings follow the
``ai_coding.<verb>_<subject>`` convention (S2 spec §9).  They are part
of the bounded-context's stable contract: subscribers in S3+
(``apps/api`` audit logging, channels notifications, etc.) match on
these strings via ``fnmatch``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from qai.platform.events.types import DomainEvent

from .value_objects import (
    CodingSessionId,
    PermissionDecision,
    PermissionRequestId,
    Provider,
    SessionStatus,
    StreamFrameKind,
    ToolInvocationId,
    ToolName,
    Workspace,
)

__all__ = [
    "CodingSessionInterruptedEvent",
    "CodingSessionRenamedEvent",
    "CodingSessionRestoredEvent",
    "CodingSessionStartedEvent",
    "CodingSessionStatusChangedEvent",
    "CodingSessionStreamFrameEvent",
    "CodingSessionTerminatedEvent",
    "EffortChangedEvent",
    "MessageHistoryTruncatedEvent",
    "PermissionDecidedEvent",
    "NotifyBindingChangedEvent",
    "PermissionRequestedEvent",
    "SkillRegisteredEvent",
    "ToolCompletedEvent",
    "ToolFailedEvent",
    "ToolInvokedEvent",
    "WorkspaceChangedEvent",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class CodingSessionStartedEvent(DomainEvent):
    """A new :class:`CodingSession` has been spawned and is now ``ACTIVE``."""

    event_type: ClassVar[str] = "ai_coding.started_coding_session"
    session_id: CodingSessionId
    provider: Provider
    workspace: Workspace


@dataclass(frozen=True, slots=True, kw_only=True)
class CodingSessionTerminatedEvent(DomainEvent):
    """A :class:`CodingSession` has reached the ``TERMINATED`` state.

    ``reason`` is a free-form string used for audit logging; the domain
    does not constrain its values.
    """

    event_type: ClassVar[str] = "ai_coding.terminated_coding_session"
    session_id: CodingSessionId
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CodingSessionStatusChangedEvent(DomainEvent):
    """A session moved between two non-terminal states."""

    event_type: ClassVar[str] = "ai_coding.changed_status_coding_session"
    session_id: CodingSessionId
    old_status: SessionStatus
    new_status: SessionStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class CodingSessionStreamFrameEvent(DomainEvent):
    """One streaming frame was produced by the provider.

    Subscribers (audit logger, channels bridge) typically only care
    about ``kind`` and the bare ``sequence`` for ordering; the full
    payload is intentionally not duplicated into the event to keep the
    wire size of the event bus small.
    """

    event_type: ClassVar[str] = "ai_coding.emitted_stream_frame"
    session_id: CodingSessionId
    kind: StreamFrameKind
    sequence: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionRequestedEvent(DomainEvent):
    """The provider asked the user to authorise a tool invocation."""

    event_type: ClassVar[str] = "ai_coding.requested_permission"
    session_id: CodingSessionId
    request_id: PermissionRequestId
    tool_name: ToolName


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionDecidedEvent(DomainEvent):
    """The user (or automation) decided on a pending permission request."""

    event_type: ClassVar[str] = "ai_coding.decided_permission"
    session_id: CodingSessionId
    request_id: PermissionRequestId
    decision: PermissionDecision


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolInvokedEvent(DomainEvent):
    """A tool was invoked through :class:`ToolBridgePort`."""

    event_type: ClassVar[str] = "ai_coding.invoked_tool"
    session_id: CodingSessionId
    invocation_id: ToolInvocationId
    tool_name: ToolName


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCompletedEvent(DomainEvent):
    """A previously invoked tool returned successfully."""

    event_type: ClassVar[str] = "ai_coding.completed_tool"
    session_id: CodingSessionId
    invocation_id: ToolInvocationId
    duration_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFailedEvent(DomainEvent):
    """A previously invoked tool raised or returned a failure marker."""

    event_type: ClassVar[str] = "ai_coding.failed_tool"
    session_id: CodingSessionId
    invocation_id: ToolInvocationId
    error_code: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillRegisteredEvent(DomainEvent):
    """A new skill became available to the agent.

    Skills are the legacy ``backend/security/skill_policy.py`` concept;
    the new domain models them as plain entities with a name and a
    serialisable spec.
    """

    event_type: ClassVar[str] = "ai_coding.registered_skill"
    skill_name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceChangedEvent(DomainEvent):
    """A :class:`CodingSession`'s workspace was reassigned at runtime.

    Emitted by :meth:`CodingSession.change_workspace` (PR-106 / U1
    decision, see ``docs/90-refactor/S8-parity-audit.md`` §4) which
    relaxes the previously-immutable ``CodingSession.workspace`` field
    so the legacy ``POST /sessions/{id}/working_dir`` route can be
    re-implemented 1:1 against the new aggregate.

    The application layer is responsible for swapping the workspace
    lock (release old → acquire new, with rollback) before invoking
    this method on the aggregate.
    """

    event_type: ClassVar[str] = "ai_coding.changed_workspace"
    session_id: CodingSessionId
    old_workspace: Workspace
    new_workspace: Workspace


# ---------------------------------------------------------------------------
# PR-104a — session lifecycle extras (legacy parity)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CodingSessionRenamedEvent(DomainEvent):
    """A :class:`CodingSession`'s display title was updated.

    Emitted by :meth:`CodingSession.rename` (PR-104a) which backs the
    legacy ``POST /sessions/{id}/rename`` route.  The aggregate's
    persisted ``title`` field is the canonical source of truth; the
    event carries both the previous and new titles so audit
    subscribers can render the change.
    """

    event_type: ClassVar[str] = "ai_coding.renamed_coding_session"
    session_id: CodingSessionId
    old_title: str | None
    new_title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EffortChangedEvent(DomainEvent):
    """A session-level thinking-depth override was set or cleared.

    Emitted by :meth:`CodingSession.set_effort` (PR-104a) which backs
    the legacy ``POST /sessions/{id}/effort`` route.  ``new_effort``
    of :data:`None` means the session-level override was cleared and
    the global default applies.
    """

    event_type: ClassVar[str] = "ai_coding.changed_effort"
    session_id: CodingSessionId
    old_effort: str | None
    new_effort: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CodingSessionInterruptedEvent(DomainEvent):
    """A live session was soft-interrupted (current turn cancelled).

    Emitted by :meth:`CodingSession.interrupt` (PR-104a) which backs
    the legacy ``POST /sessions/{id}/interrupt`` route.  The aggregate
    transitions back to ``IDLE`` so the user can immediately send a
    new message; per-turn approvals (the legacy "本会话允许" grants)
    are NOT cleared by this event — only the in-flight stream is
    abandoned.
    """

    event_type: ClassVar[str] = "ai_coding.interrupted_coding_session"
    session_id: CodingSessionId


@dataclass(frozen=True, slots=True, kw_only=True)
class CodingSessionRestoredEvent(DomainEvent):
    """A previously-terminated session was re-activated from history.

    Emitted by :meth:`CodingSession.restore` (PR-104a) which backs the
    legacy ``POST /sessions/{id}/restore`` route.  The aggregate
    transitions from ``TERMINATED`` back to ``ACTIVE`` and the
    ``terminated_at`` / ``termination_reason`` fields are cleared so
    list_active() picks the session up again.
    """

    event_type: ClassVar[str] = "ai_coding.restored_coding_session"
    session_id: CodingSessionId
    forked: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageHistoryTruncatedEvent(DomainEvent):
    """A trailing slice of the message history was truncated.

    Emitted by :meth:`CodingSession.truncate_history_after` (PR-104a)
    which backs the legacy ``POST /sessions/{id}/truncate_history``
    route.  ``removed`` is the count of messages dropped from the
    aggregate; ``include_self`` mirrors the legacy ``include_self``
    request flag (Edit & Resend mode).
    """

    event_type: ClassVar[str] = "ai_coding.truncated_message_history"
    session_id: CodingSessionId
    removed: int
    include_self: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class NotifyBindingChangedEvent(DomainEvent):
    """A session's dual-channel notify binding was set or cleared.

    Emitted by :meth:`CodingSession.set_wechat_notify` /
    :meth:`CodingSession.set_feishu_notify` which back the legacy
    ``POST /sessions/{id}/wechat_notify`` + ``.../feishu_notify``
    routes.  ``channel`` is ``"wechat"`` or ``"feishu"``;
    ``new_user_id`` of :data:`None` means the binding was cleared and
    WebUI turns are no longer mirror-pushed to that channel.
    """

    event_type: ClassVar[str] = "ai_coding.changed_notify_binding"
    session_id: CodingSessionId
    channel: str
    old_user_id: str | None
    new_user_id: str | None

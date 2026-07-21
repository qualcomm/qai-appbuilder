# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain errors for the chat bounded context.

All errors here inherit from :class:`qai.platform.errors.DomainError`
(business-rule violations, no technical coupling) or from
:class:`qai.platform.errors.ApplicationError` when they correspond to a
canonical use-case error category (not-found / conflict / validation).

Error code namespace: ``chat.<reason>``.
"""

from __future__ import annotations

from typing import Any

from qai.platform.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PreconditionFailedError,
    ValidationError,
)


class ConversationNotFoundError(NotFoundError):
    """Raised by application code when a conversation lookup misses."""

    default_code = "chat.conversation_not_found"

    def __init__(self, conversation_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="conversation",
            resource_id=conversation_id,
            message=message,
        )


class TabNotFoundError(NotFoundError):
    """Raised when a tab lookup misses."""

    default_code = "chat.tab_not_found"

    def __init__(self, tab_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="tab",
            resource_id=tab_id,
            message=message,
        )


class ExperienceNotFoundError(NotFoundError):
    """Raised when an experience lookup misses."""

    default_code = "chat.experience_not_found"

    def __init__(self, experience_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="experience",
            resource_id=experience_id,
            message=message,
        )


class SubAgentSessionNotFoundError(NotFoundError):
    """Raised when a sub-agent session lookup misses."""

    default_code = "chat.sub_agent_session_not_found"

    def __init__(self, session_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="sub_agent_session",
            resource_id=session_id,
            message=message,
        )


class ParticipantNotFoundError(NotFoundError):
    """Raised when a conversation participant lookup misses."""

    default_code = "chat.participant_not_found"

    def __init__(self, participant_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="participant",
            resource_id=participant_id,
            message=message,
        )


class RosterTemplateNotFoundError(NotFoundError):
    """Raised when a roster-template lookup misses."""

    default_code = "chat.roster_template_not_found"

    def __init__(self, template_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="roster_template",
            resource_id=template_id,
            message=message,
        )


class AgentTemplateNotFoundError(NotFoundError):
    """Raised when a single-role agent-template lookup misses."""

    default_code = "chat.agent_template_not_found"

    def __init__(self, template_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="agent_template",
            resource_id=template_id,
            message=message,
        )


class ModeTemplateNotFoundError(NotFoundError):
    """Raised when a collaboration-mode template lookup misses."""

    default_code = "chat.mode_template_not_found"

    def __init__(self, template_id: str, message: str | None = None) -> None:
        super().__init__(
            self.default_code,
            resource_type="mode_template",
            resource_id=template_id,
            message=message,
        )


class ConversationLockedError(ConflictError):
    """Raised when a tab tries to start streaming on a conversation that is
    already being streamed by another tab.

    Multi-tab parallel chat allows distinct conversations to stream in
    parallel, but a single conversation must serialise its writes -- two
    tabs cannot append to the same message log simultaneously without
    a merge conflict.
    """

    default_code = "chat.conversation_locked"

    def __init__(
        self,
        conversation_id: str,
        held_by_tab_id: str,
        message: str | None = None,
    ) -> None:
        msg = (
            message
            if message is not None
            else (
                f"conversation {conversation_id!r} is currently being "
                f"streamed by tab {held_by_tab_id!r}"
            )
        )
        super().__init__(
            self.default_code,
            msg,
            details={
                "conversation_id": conversation_id,
                "held_by_tab_id": held_by_tab_id,
            },
        )
        self.conversation_id: str = conversation_id
        self.held_by_tab_id: str = held_by_tab_id


class SubAgentSessionConflictError(ConflictError):
    """Raised when a sub-agent session save loses an optimistic-lock CAS.

    Block 4: under the SHARED ownership model the main agent (resume) and the
    user (take-over) can drive the SAME ``subagent_id`` concurrently; both
    whole-row UPSERT the single-row aggregate. The repository performs a
    compare-and-swap on the ``version`` column and raises this when the stored
    version no longer matches the loaded one, so the second writer is told to
    reload + retry instead of silently clobbering the first writer's turns.
    """

    default_code = "chat.subagent_session_conflict"

    def __init__(
        self,
        subagent_id: str,
        *,
        expected_version: int,
        message: str | None = None,
    ) -> None:
        msg = (
            message
            if message is not None
            else (
                f"sub-agent session {subagent_id!r} was modified concurrently "
                f"(expected version {expected_version})"
            )
        )
        super().__init__(
            self.default_code,
            msg,
            details={
                "subagent_id": subagent_id,
                "expected_version": expected_version,
            },
        )
        self.subagent_id: str = subagent_id
        self.expected_version: int = expected_version


class TabStateError(PreconditionFailedError):
    """Raised when a state transition on :class:`ConversationTab` is illegal.

    Examples:
    * Calling ``start_stream()`` on a tab that is already streaming.
    * Calling ``abort()`` on a tab that is not streaming.
    """

    default_code = "chat.tab_state_invalid"

    def __init__(self, message: str, *, current_status: str, attempted: str) -> None:
        super().__init__(self.default_code, message)
        self.current_status: str = current_status
        self.attempted: str = attempted


class ChatStreamAbortedError(DomainError):
    """Domain-level signal that a chat stream was deliberately stopped.

    This is **not** a bug -- it carries the abort reason up to the
    use-case layer so that final ``end`` frames can be emitted with the
    correct status.  Callers must NOT swallow this silently.
    """

    default_code = "chat.stream_aborted"

    def __init__(
        self,
        tab_id: str,
        *,
        reason: str = "user_requested",
        details: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {"tab_id": tab_id, "reason": reason}
        if details:
            merged.update(details)
        super().__init__(
            self.default_code,
            f"chat stream aborted on tab {tab_id!r} (reason={reason})",
            details=merged,
        )
        self.tab_id: str = tab_id
        self.reason: str = reason


class InvalidContextSizeError(ValidationError):
    """Raised when a context-size value is rejected (non-positive / overflow)."""

    default_code = "chat.invalid_context_size"

    def __init__(self, message: str) -> None:
        super().__init__(self.default_code, message)


class InvalidMessageContentError(ValidationError):
    """Raised when message content fails domain-level validation."""

    default_code = "chat.invalid_message_content"

    def __init__(self, message: str) -> None:
        super().__init__(self.default_code, message)


class InvalidConversationTitleError(ValidationError):
    """Raised when a conversation title is empty or too long."""

    default_code = "chat.invalid_conversation_title"

    def __init__(self, message: str) -> None:
        super().__init__(self.default_code, message)


__all__ = [
    "ConversationNotFoundError",
    "TabNotFoundError",
    "ExperienceNotFoundError",
    "SubAgentSessionNotFoundError",
    "ParticipantNotFoundError",
    "RosterTemplateNotFoundError",
    "AgentTemplateNotFoundError",
    "ModeTemplateNotFoundError",
    "ConversationLockedError",
    "SubAgentSessionConflictError",
    "TabStateError",
    "ChatStreamAbortedError",
    "InvalidContextSizeError",
    "InvalidMessageContentError",
    "InvalidConversationTitleError",
]

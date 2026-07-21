# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain errors for the ai_coding bounded context.

These errors are raised exclusively by code under
``src/qai/ai_coding/domain`` and ``src/qai/ai_coding/application``;
adapters / infrastructure code translates infrastructure failures
(timeouts, subprocess crashes, etc.) into the :mod:`qai.platform.errors`
hierarchy *before* re-entering the domain.

All classes here inherit from :class:`qai.platform.errors.DomainError`
so callers can filter coarsely::

    try:
        await use_case.execute(...)
    except DomainError:
        ...
"""

from __future__ import annotations

from qai.platform.errors import DomainError

__all__ = [
    "CodingSessionAlreadyTerminatedError",
    "CodingSessionNotFoundError",
    "InvalidSessionStateError",
    "PermissionRequestAlreadyDecidedError",
    "PermissionRequestNotFoundError",
    "ProviderNotAvailableError",
    "SkillNotRegisteredError",
    "WorkspaceLockedError",
]


class CodingSessionNotFoundError(DomainError):
    """Raised when a referenced session id cannot be resolved."""

    default_code = "ai_coding.coding_session_not_found"


class CodingSessionAlreadyTerminatedError(DomainError):
    """Raised when an operation is attempted on a terminated session.

    A terminated session is final; no further state transitions are
    allowed.  Callers should create a new session instead.
    """

    default_code = "ai_coding.coding_session_already_terminated"


class ProviderNotAvailableError(DomainError):
    """Raised when no implementation is registered for a :class:`Provider`.

    Typical cause: the Claude Code CLI is not installed or the OpenCode
    service is disabled in configuration.  The application layer must
    map this to a human-friendly diagnostic.
    """

    default_code = "ai_coding.provider_not_available"


class WorkspaceLockedError(DomainError):
    """Raised when the requested workspace is already exclusively held.

    AI coding sessions assume single-writer access to the workspace
    directory.  Spawning a second session for the same workspace must
    either wait or be rejected; the domain encodes the latter behaviour
    by raising this error.
    """

    default_code = "ai_coding.workspace_locked"


class PermissionRequestNotFoundError(DomainError):
    """Raised when a permission request id cannot be resolved on a session."""

    default_code = "ai_coding.permission_request_not_found"


class PermissionRequestAlreadyDecidedError(DomainError):
    """Raised when ``decide`` is called on a non-pending request.

    Decisions are immutable once made.  Re-prompting the user is a
    workflow concern handled by issuing a *new* permission request.
    """

    default_code = "ai_coding.permission_request_already_decided"


class SkillNotRegisteredError(DomainError):
    """Raised when the agent invokes a skill that is not registered."""

    default_code = "ai_coding.skill_not_registered"


class InvalidSessionStateError(DomainError):
    """Raised when an operation is attempted in a wrong state.

    Used by the :class:`CodingSession` state machine when, for example,
    ``mark_streaming`` is called on a session that has not yet
    transitioned to ``ACTIVE``.
    """

    default_code = "ai_coding.invalid_session_state"

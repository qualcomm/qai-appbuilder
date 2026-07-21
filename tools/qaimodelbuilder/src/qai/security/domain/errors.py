# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain-level errors for the security bounded context.

All errors here inherit from :class:`qai.platform.errors.DomainError` (or
one of the application-layer subclasses where the semantics naturally
match — e.g. ``NotFoundError`` for "request id not found").

Each subclass sets ``default_code`` to a stable string of the form
``"security.<reason>"`` so log analysis and HTTP error mapping have a
deterministic identifier independent of the exception class name.
"""

from __future__ import annotations

from typing import Any

from qai.platform.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PreconditionFailedError,
)

__all__ = [
    "AskRateLimitedError",
    "ChannelPolicyNotFoundError",
    "PermissionRequestAlreadyResolvedError",
    "PermissionRequestNotFoundError",
    "PathGrantConflictError",
    "PolicyRuleConflictError",
    "SecurityPolicyInvalidError",
    "SmartApprovalUnavailableError",
]


class SecurityPolicyInvalidError(DomainError):
    """Raised when an attempt is made to construct an invalid policy.

    Examples include duplicate rule ids, contradictory rules with the
    same scope+pattern, or empty rule sets where a non-empty set is
    required by the use case.
    """

    default_code = "security.policy.invalid"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(self.default_code, message, details=details)


class PolicyRuleConflictError(DomainError):
    """Raised when two policy rules contradict at the same scope+pattern."""

    default_code = "security.policy.rule_conflict"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(self.default_code, message, details=details)


class PathGrantConflictError(ConflictError):
    """Raised when an incoming path grant conflicts with an existing one.

    Inherits from :class:`ConflictError` so HTTP adapters can map it to
    409 Conflict directly.
    """

    default_code = "security.path_grant.conflict"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(self.default_code, message, details=details)


class PermissionRequestNotFoundError(NotFoundError):
    """Raised when a referenced permission request id does not exist."""

    default_code = "security.permission_request.not_found"

    def __init__(self, request_id: str) -> None:
        super().__init__(
            self.default_code,
            "permission_request",
            request_id,
        )


class PermissionRequestAlreadyResolvedError(PreconditionFailedError):
    """Raised when approve/reject is invoked on a non-pending request."""

    default_code = "security.permission_request.already_resolved"

    def __init__(self, message: str) -> None:
        super().__init__(self.default_code, message)


class SmartApprovalUnavailableError(DomainError):
    """Raised by the smart-approval port when the upstream service errored.

    The use case typically catches this and either retries or falls back
    to manual approval; surfacing a domain error keeps that decision
    inside the application layer.
    """

    default_code = "security.smart_approval.unavailable"

    def __init__(self, message: str) -> None:
        super().__init__(self.default_code, message)


class AskRateLimitedError(PreconditionFailedError):
    """Raised when an ASK exceeds a channel's :class:`AskQuotaWindow`.

    Inherits from :class:`PreconditionFailedError` so HTTP adapters
    map it to ``412 Precondition Failed`` (matching the legacy
    ``PolicyCenter`` behaviour where an ask hitting the quota was
    rejected with a clear "too many asks" message rather than silently
    deadlocking).
    """

    default_code = "security.ask.rate_limited"

    def __init__(self, message: str) -> None:
        super().__init__(self.default_code, message)


class ChannelPolicyNotFoundError(NotFoundError):
    """Raised when a referenced channel name has no :class:`ChannelPolicy`.

    Channel policies are seeded by ``install`` (one row per
    :attr:`Channel._ALLOWED_NAMES`) and operators may add custom rows
    later; the use cases treat a missing row as "fail closed" rather
    than synthesising a permissive default, so this error is raised
    explicitly when callers reference a channel that isn't configured
    yet.
    """

    default_code = "security.channel_policy.not_found"

    def __init__(self, channel_name: str) -> None:
        super().__init__(
            self.default_code,
            "channel_policy",
            channel_name,
        )


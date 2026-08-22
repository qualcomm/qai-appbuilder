# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer error subclasses.

These cover the bulk of errors raised by use-case / service code.
All of them inherit from :class:`ApplicationError`, so callers can still
filter coarsely with::

    try:
        ...
    except ApplicationError as exc:
        ...
"""

from __future__ import annotations

from typing import Any

from .base import ApplicationError


class NotFoundError(ApplicationError):
    """Raised when a referenced resource cannot be located.

    Parameters
    ----------
    code:
        Machine-readable identifier, e.g. ``"chat.conversation_not_found"``.
    resource_type:
        Logical name of the resource (e.g. ``"conversation"``).
    resource_id:
        The identifier the caller used; converted to ``str`` for the
        ``details`` payload.
    message:
        Optional human-readable message; if omitted a sensible default
        is generated.
    """

    default_code = "application.not_found"

    def __init__(
        self,
        code: str,
        resource_type: str,
        resource_id: Any,
        message: str | None = None,
    ) -> None:
        resolved_message = (
            message
            if message is not None
            else f"{resource_type} with id {resource_id!r} was not found"
        )
        super().__init__(
            code,
            resolved_message,
            details={
                "resource_type": resource_type,
                "resource_id": str(resource_id),
            },
        )
        self.resource_type: str = resource_type
        self.resource_id: Any = resource_id


class ConflictError(ApplicationError):
    """Raised when an operation conflicts with current state.

    Typical example: trying to create a tab with a name that already
    exists.
    """

    default_code = "application.conflict"

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, details=details)


class ValidationError(ApplicationError):
    """Raised when user-supplied input fails validation.

    ``field_errors`` maps a field name to a list of human-readable error
    messages.  The map (when provided) is also surfaced via
    ``details["field_errors"]`` for serialisation.
    """

    default_code = "application.validation"

    def __init__(
        self,
        code: str,
        message: str,
        field_errors: dict[str, list[str]] | None = None,
    ) -> None:
        details: dict[str, Any] | None = None
        if field_errors is not None:
            details = {"field_errors": field_errors}
        super().__init__(code, message, details=details)
        self.field_errors: dict[str, list[str]] | None = field_errors


class UnauthorizedError(ApplicationError):
    """Raised when the caller is not authenticated."""

    default_code = "application.unauthorized"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class ForbiddenError(ApplicationError):
    """Raised when the caller is authenticated but lacks permission."""

    default_code = "application.forbidden"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class RateLimitedError(ApplicationError):
    """Raised when a caller exceeds an applicable rate limit.

    ``retry_after_s`` is the suggested back-off in seconds and is also
    available under ``details["retry_after_s"]``.
    """

    default_code = "application.rate_limited"

    def __init__(self, code: str, retry_after_s: float) -> None:
        message = f"Rate limit exceeded; retry after {retry_after_s:.3f}s"
        super().__init__(
            code,
            message,
            details={"retry_after_s": retry_after_s},
        )
        self.retry_after_s: float = retry_after_s


class PreconditionFailedError(ApplicationError):
    """Raised when a precondition (e.g. state-machine state) is not met."""

    default_code = "application.precondition_failed"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


__all__ = [
    "NotFoundError",
    "ConflictError",
    "ValidationError",
    "UnauthorizedError",
    "ForbiddenError",
    "RateLimitedError",
    "PreconditionFailedError",
]

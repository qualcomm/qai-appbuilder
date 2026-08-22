# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Base error hierarchy for the qai platform layer.

This module defines the three top-level error categories used across the
codebase:

* :class:`DomainError`         -- pure business-rule violations
* :class:`ApplicationError`    -- use-case layer errors (validation,
  not-found, conflict, etc.)
* :class:`InfrastructureError` -- technical errors raised by adapters
  (DB / network / FS / third-party APIs)

All three inherit from :class:`QaiError`, which is the single root every
piece of qai code may catch.

Design notes
------------
* These classes are deliberately plain Python ``Exception`` subclasses --
  no registry, no metaclass magic, no singletons.
* The ``cause`` argument is just sugar around ``raise ... from old``;
  we still set ``__cause__`` so standard library tooling keeps working.
* ``to_dict`` produces a JSON-serialisable payload suitable for log
  records or HTTP error responses.  It intentionally does **not** include
  the chained cause -- presentation layers decide whether to expose it.
"""

from __future__ import annotations

from typing import Any


class QaiError(Exception):
    """Root of the qai exception hierarchy.

    Direct instantiation is allowed but discouraged: prefer one of the
    three subclasses (:class:`DomainError`, :class:`ApplicationError`,
    :class:`InfrastructureError`) so callers can do coarse-grained
    ``except`` filtering.
    """

    #: Default machine-readable code, used when callers don't override.
    default_code: str = "qai.error"

    def __init__(
        self,
        code: str | None = None,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        resolved_code = code if code is not None else self.default_code
        super().__init__(message)
        self.code: str = resolved_code
        self.message: str = message
        self.details: dict[str, Any] | None = details
        # Preserve the original exception chain.  We assign __cause__
        # explicitly so the behaviour matches ``raise ... from cause`` even
        # when callers construct the exception without a ``raise from``
        # statement (e.g. when serialising over a queue).
        if cause is not None:
            self.__cause__ = cause

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------
    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def __repr__(self) -> str:
        cls = type(self).__name__
        return (
            f"{cls}(code={self.code!r}, message={self.message!r}, "
            f"details={self.details!r})"
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the error.

        The shape is intentionally stable: ``type``, ``code``, ``message``
        and an optional ``details`` mapping.  Chained causes are *not*
        included; presentation layers must opt-in if they want them.
        """

        payload: dict[str, Any] = {
            "type": type(self).__name__,
            "code": self.code,
            "message": self.message,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload


class DomainError(QaiError):
    """A business-rule violation expressed in pure domain terms.

    Domain errors must not leak technical details (no DB names, HTTP
    statuses, etc.).  Each bounded context typically defines its own
    subclasses (e.g. ``ConversationLockedError`` in the chat context).
    """

    default_code = "domain.error"


class ApplicationError(QaiError):
    """An error raised by an application/use-case layer.

    Subclasses cover the most common cases (not-found, conflict,
    validation, etc.) -- see :mod:`qai.platform.errors.application`.
    """

    default_code = "application.error"


class InfrastructureError(QaiError):
    """An error originating from a technical adapter.

    Subclasses cover persistence, external services, timeouts and
    configuration -- see :mod:`qai.platform.errors.infrastructure`.
    """

    default_code = "infrastructure.error"


__all__ = [
    "QaiError",
    "DomainError",
    "ApplicationError",
    "InfrastructureError",
]

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Infrastructure-layer error subclasses.

These wrap technical failures coming from adapters: databases, network
calls, file systems, third-party services, configuration loading, etc.

Naming note
-----------
The timeout error is named :class:`TimeoutError_` (trailing underscore)
on purpose so it does not shadow the built-in :class:`TimeoutError`.
"""

from __future__ import annotations

from typing import Any

from .base import InfrastructureError


class PersistenceError(InfrastructureError):
    """Raised when a persistence operation (DB / file store) fails.

    ``operation`` is a short identifier of what was attempted, e.g.
    ``"insert_conversation"`` or ``"sqlite.commit"``.
    """

    default_code = "infrastructure.persistence"

    def __init__(
        self,
        code: str,
        message: str,
        *,
        operation: str,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            code,
            message,
            details={"operation": operation},
            cause=cause,
        )
        self.operation: str = operation


class ExternalServiceError(InfrastructureError):
    """Raised when a call to an external service fails.

    ``service`` is the logical name of the service (``"openai"``,
    ``"github"``, ...) and ``status`` is the protocol-level status code
    when applicable.
    """

    default_code = "infrastructure.external_service"

    def __init__(
        self,
        code: str,
        message: str,
        *,
        service: str,
        status: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        details: dict[str, Any] = {"service": service}
        if status is not None:
            details["status"] = status
        super().__init__(code, message, details=details, cause=cause)
        self.service: str = service
        self.status: int | None = status


class TimeoutError_(InfrastructureError):
    """Raised when an infrastructure operation exceeds its deadline.

    The trailing underscore avoids shadowing the built-in
    :class:`TimeoutError`.  ``timeout_s`` is the configured budget that
    was exceeded, in seconds.
    """

    default_code = "infrastructure.timeout"

    def __init__(
        self,
        code: str,
        message: str,
        *,
        timeout_s: float,
    ) -> None:
        super().__init__(
            code,
            message,
            details={"timeout_s": timeout_s},
        )
        self.timeout_s: float = timeout_s


class ConfigurationError(InfrastructureError):
    """Raised when configuration is missing or malformed.

    Typically thrown during process bootstrap.
    """

    default_code = "infrastructure.configuration"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


__all__ = [
    "PersistenceError",
    "ExternalServiceError",
    "TimeoutError_",
    "ConfigurationError",
]

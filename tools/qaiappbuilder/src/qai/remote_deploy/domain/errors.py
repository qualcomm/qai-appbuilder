# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain errors for the ``remote_deploy`` bounded context.

All errors inherit from one of the platform error roots so the unified
``apps/api`` error handler (``error_handlers.py`` ``_STATUS_MAP``) maps them
to the right HTTP status and the front-end ``ApiError`` can dispatch on
``code``.

Naming follows ``<Context><Reason>Error``; ``default_code`` follows
``"remote_deploy.<reason>"``.
"""
from __future__ import annotations

from qai.platform.errors import (
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)

__all__ = [
    "RemoteSshConnectError",
    "RemotePortInUseError",
    "RemoteInstanceNotFoundError",
    "RemoteHostValidationError",
]


class RemoteSshConnectError(ExternalServiceError):
    """Raised when the SSH handshake / authentication to the remote host fails.

    Maps to HTTP 503 (InfrastructureError subclass via ExternalServiceError).
    """

    default_code = "remote_deploy.ssh_connect_failed"

    def __init__(self, host: str, message: str | None = None) -> None:
        self.host = host
        super().__init__(
            self.default_code,
            message if message is not None else f"Cannot connect to {host!r} over SSH.",
            service="ssh",
            extra_details={"host": host},
        )


class RemotePortInUseError(ConflictError):
    """Raised when the target port on the remote host is already occupied.

    Maps to HTTP 409.
    """

    default_code = "remote_deploy.port_in_use"

    def __init__(self, host: str, port: int, message: str | None = None) -> None:
        self.host = host
        self.port = port
        super().__init__(
            self.default_code,
            message
            if message is not None
            else (
                f"Port {port} is already in use on {host!r}. "
                "Stop the existing service or choose a different port."
            ),
            {"host": host, "port": port},
        )


class RemoteInstanceNotFoundError(NotFoundError):
    """Raised when the requested instance_id does not exist.

    Maps to HTTP 404.
    """

    default_code = "remote_deploy.instance_not_found"

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        super().__init__(
            self.default_code,
            "remote_instance",
            instance_id,
            f"Remote instance {instance_id!r} not found.",
        )


class RemoteHostValidationError(ValidationError):
    """Raised when the host / port / username fails basic validation.

    Maps to HTTP 400.
    """

    default_code = "remote_deploy.invalid_host_params"

    def __init__(self, message: str, field_errors: dict | None = None) -> None:
        super().__init__(
            self.default_code,
            message,
            field_errors or {},
        )

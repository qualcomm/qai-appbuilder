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
    "RemoteCommandFailedError",
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


class RemoteCommandFailedError(ExternalServiceError):
    """Raised when a remote command exits with a non-zero status.

    Maps to HTTP 503 (InfrastructureError subclass via ExternalServiceError).

    ``output_tail`` carries the last lines of the merged stdout/stderr stream.
    On a headless SSH target the deploy UI is the only place an operator can
    see *why* a step failed, so the diagnostic travels with the error rather
    than being left behind in a log file on the remote host.
    """

    default_code = "remote_deploy.command_failed"

    def __init__(
        self,
        host: str,
        command: str,
        exit_code: int,
        output_tail: str = "",
    ) -> None:
        self.host = host
        self.command = command
        self.exit_code = exit_code
        self.output_tail = output_tail
        summary = command if len(command) <= 120 else command[:117] + "..."
        message = f"Remote command failed on {host!r} (exit {exit_code}): {summary}"
        if output_tail:
            message = f"{message}\n{output_tail}"
        super().__init__(
            self.default_code,
            message,
            service="ssh",
            extra_details={
                "host": host,
                "command": summary,
                "exit_code": exit_code,
                "output_tail": output_tail,
            },
        )


class RemotePortInUseError(ConflictError):
    """Raised when the target port is already occupied.

    Used for both the remote application port and the local tunnel port —
    ``host`` distinguishes which side ("127.0.0.1" for the local listener).

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

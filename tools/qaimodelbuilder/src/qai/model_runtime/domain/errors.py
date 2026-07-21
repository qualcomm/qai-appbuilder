# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain errors for the ``model_runtime`` bounded context.

All errors here inherit from one of the platform error roots so the unified
``apps/api`` error handler (``error_handlers.py`` ``_STATUS_MAP``) maps them to
the right HTTP status and the front-end ``ApiError`` can dispatch on ``code``.

Naming follows ``<Context><Reason>Error``; ``default_code`` follows
``"model_runtime.<reason>"``.
"""

from __future__ import annotations

from qai.platform.errors import ConflictError

__all__ = ["ServicePortInUseError"]


class ServicePortInUseError(ConflictError):
    """Raised by ``start`` when the target port is already occupied.

    Single-instance guard (real-state-first, AGENTS.md): before spawning
    GenieAPIService we probe whether anything is already listening on the
    chosen port. If so we do NOT spawn a competing daemon (it would fight for
    the port, or — if adopted — lose stdout/PID/uptime). We raise this instead
    so the UI shows a friendly "already running / port busy" message and the
    user can stop the existing service or pick another port.

    Maps to HTTP 409 (ConflictError). ``port`` is carried both as an attribute
    and in ``details`` so the route/UI can render a specific message.
    """

    default_code = "model_runtime.service_port_in_use"

    def __init__(self, port: int, message: str | None = None) -> None:
        self.port = port
        super().__init__(
            self.default_code,
            message
            if message is not None
            else (
                f"Port {port} is already in use — a GenieAPIService (or another "
                f"process) appears to be running on it. Stop the existing "
                f"service or choose a different port before starting."
            ),
            details={"port": port},
        )

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.platform.logging — Structured logging with request_id propagation.

Wraps `structlog` to emit JSON logs by default (line-delimited, ASCII-safe)
with two context vars merged into every record:

- ``request_id`` — propagated from HTTP / WS handlers (interfaces layer)
- ``correlation_id`` — propagated across context boundaries (events, jobs)

Both are stored in `contextvars.ContextVar` so that they automatically flow
across ``await`` and ``asyncio.create_task`` boundaries within the same
request handling task.

Design constraints (refactor-plan v2.5 §6 / §15.1):
- No module-level mutable singletons. Configuration is explicit and
  idempotent through ``configure_logging(...)``.
- ``print(`` is forbidden in business code; this module is the supported
  way to emit any output.
- The Clock abstraction is injectable so tests can produce deterministic
  timestamps via :class:`qai.platform.time.FrozenClock`.
"""

from __future__ import annotations

from .config import configure_logging, get_logger
from .context import (
    bind_correlation_id,
    bind_request_id,
    clear_context,
    current_correlation_id,
    current_request_id,
)

__all__ = [
    "bind_correlation_id",
    "bind_request_id",
    "clear_context",
    "configure_logging",
    "current_correlation_id",
    "current_request_id",
    "get_logger",
]

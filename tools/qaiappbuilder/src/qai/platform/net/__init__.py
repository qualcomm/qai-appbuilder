# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Network primitives shared across contexts (``qai.platform.net``).

Currently exposes :mod:`qai.platform.net.port_allocator` -- the single
source of truth for bind-based TCP port allocation used by both the
daemon supervisor (``apps/cli/serve.py``) and the App Builder
standalone-app run manager.
"""

from __future__ import annotations

from .port_allocator import (
    DEFAULT_FALLBACK_PORTS,
    NoBindablePortError,
    PortAllocationError,
    PortInUseError,
    can_bind,
    resolve_bindable_port,
)

__all__ = [
    "DEFAULT_FALLBACK_PORTS",
    "NoBindablePortError",
    "PortAllocationError",
    "PortInUseError",
    "can_bind",
    "resolve_bindable_port",
]

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public API for ``qai.platform.time``.

Exports a Clock abstraction (Protocol + SystemClock + FrozenClock) and a small
set of timezone-aware datetime helpers. See ``clock.py`` and ``conversions.py``
for details.
"""

from __future__ import annotations

from .clock import Clock, FrozenClock, SystemClock
from .conversions import ensure_aware_utc, from_iso8601, to_iso8601, utcnow

__all__ = [
    "Clock",
    "SystemClock",
    "FrozenClock",
    "utcnow",
    "to_iso8601",
    "from_iso8601",
    "ensure_aware_utc",
]

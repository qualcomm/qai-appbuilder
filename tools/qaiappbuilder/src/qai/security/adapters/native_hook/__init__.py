# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-package copy of the native guard64.dll Python wrapper (PR-1).

The production package must not import from the ``native/file-guard/``
source tree at runtime (release packaging excludes native sources), so
this package holds a verbatim copy of the upstream ctypes wrapper as
:mod:`qai.security.adapters.native_hook.guard_wrapper`. PR-2's
:class:`qai.security.adapters.native_file_guard.NativeFileGuard`
consumes it.
"""

from __future__ import annotations

from .guard_wrapper import (
    Event,
    FilterEventV2,
    Guard,
    GuardLoadError,
    PreFilterFunc,
    PreFilterFuncV2,
)

__all__ = [
    "Event",
    "FilterEventV2",
    "Guard",
    "GuardLoadError",
    "PreFilterFunc",
    "PreFilterFuncV2",
]

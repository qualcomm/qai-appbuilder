# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Process-architecture probe for the running interpreter.

Exposes :func:`current_arch`, which reports whether *this* Python
process is ARM64 or x64 based on :func:`platform.machine` (the running
interpreter — NOT the host OS). On Windows-on-ARM an x64 ``python.exe``
runs under x64 emulation and must resolve x64 artefacts (venv, QNN
runtime subdir), which is exactly what ``platform.machine()`` reflects.

Why this lives in ``qai.platform`` and duplicates the semantics of
``qai.security.adapters.native_file_guard.current_guard_arch``
--------------------------------------------------------------------
The ``context-isolation`` import-linter contract (see ``.importlinter``)
forbids one bounded context from importing another; only the platform
shared kernel (``qai.platform.**``) is a legal shared dependency for
every context. ``qai.app_builder`` needs the same arch probe to pick a
runtime venv + QNN backend subdir, but importing the ``qai.security``
helper would be a cross-context import and break the contract. So the
probe is re-homed here in the platform layer where every context may
depend on it. The security copy stays as-is (zero-regression) for its
own DLL-injection path; both intentionally share the same rule.
"""

from __future__ import annotations

import platform
from typing import Literal

__all__ = ["current_arch"]


def current_arch() -> Literal["arm64", "x64"]:
    """Return ``"arm64"`` / ``"x64"`` for the current *process* arch.

    Uses ``platform.machine()`` of the running interpreter. ``arm64`` /
    ``aarch64`` map to ``"arm64"``; every other machine maps to ``"x64"``
    (the only other artefact family we ship).
    """
    machine = (platform.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x64"

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Process-architecture probe for the running interpreter.

Exposes :func:`current_arch`, which reports whether *this* Python
process is ARM64 or x64 — NOT the host OS. On Windows-on-ARM an x64
``python.exe`` runs under x64 emulation and must resolve x64 artefacts
(venv, QNN runtime subdir); the process is x64 even though the CPU is
ARM64. ``platform.machine()`` reports the *OS* CPU (``ARM64``) under
that emulation, so it is the wrong source — it would pick arm64
artefacts for an x64 process. Windows exposes the true *process* arch
via ``PROCESSOR_ARCHITECTURE`` (``AMD64`` for an emulated x64 process),
so we key off that.

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

import os
import platform
from typing import Literal

__all__ = ["current_arch"]


def current_arch() -> Literal["arm64", "x64"]:
    """Return ``"arm64"`` / ``"x64"`` for the current *process* arch.

    Keys off ``PROCESSOR_ARCHITECTURE`` (the Windows *process* arch:
    ``AMD64`` for an emulated x64 process, ``ARM64`` for a native arm64
    process). Only ``ARM64`` maps to ``"arm64"``; everything else maps
    to ``"x64"`` (the only other artefact family we ship). Falls back to
    ``platform.machine()`` when the env var is unset (non-Windows).
    """
    proc = (os.environ.get("PROCESSOR_ARCHITECTURE") or "").lower()
    if proc:
        return "arm64" if proc == "arm64" else "x64"
    machine = (platform.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x64"

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Model-conversion environment: detection + one background installer job."""

from __future__ import annotations

from qai.platform.builder_env.detect import (
    SDK_MARKER_REL,
    BuilderEnvProblem,
    BuilderEnvStatus,
    detect_builder_env,
    read_release_version,
)
from qai.platform.builder_env.service import (
    AlreadyRunningError,
    BuilderEnvService,
    InstallJob,
    InstallState,
    ScriptMissingError,
)

__all__ = [
    "SDK_MARKER_REL",
    "AlreadyRunningError",
    "BuilderEnvProblem",
    "BuilderEnvService",
    "BuilderEnvStatus",
    "InstallJob",
    "InstallState",
    "ScriptMissingError",
    "detect_builder_env",
    "read_release_version",
]

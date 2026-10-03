# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""QAIRT SDK hot-switch: canonical versioning, wheel policy, and job models.

Narrow public surface only. Downstream tasks (installer, config repository,
switch service, HTTP adapter) import from here rather than reaching into
``versioning``/``models`` submodules directly.
"""

from __future__ import annotations

from qai.platform.qairt_switch.models import (
    SUCCESS_PHASES,
    TERMINAL_PHASES,
    CandidateAttempt,
    CandidateOutcome,
    DownloadProgress,
    Phase,
    SwitchDiagnostics,
)
from qai.platform.qairt_switch.versioning import (
    AppBuilderVersion,
    QairtVersion,
    UnsupportedPlatformError,
    appbuilder_wheel_asset_name,
)

__all__ = [
    "SUCCESS_PHASES",
    "TERMINAL_PHASES",
    "AppBuilderVersion",
    "CandidateAttempt",
    "CandidateOutcome",
    "DownloadProgress",
    "Phase",
    "QairtVersion",
    "SwitchDiagnostics",
    "UnsupportedPlatformError",
    "appbuilder_wheel_asset_name",
]

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared utilities for ``tools/init/*``.

Mirrors :mod:`tools.build.factory_compiler._common` in spirit (Mode literal, report
type) but cannot directly import from it — :mod:`tools.build.factory_compiler` is a
peer package and v2.6 keeps the two trees independent so each can be
deleted in isolation if a stage is dropped.

Re-uses :class:`tools.build.factory_compiler._common.modes.Mode` aliasing only because
the literal value set is identical; nothing else.
"""

from __future__ import annotations

from .modes import Mode, parse_mode, VALID_MODES
from .report import InitReport, InitReportEntry

__all__ = [
    "InitReport",
    "InitReportEntry",
    "Mode",
    "VALID_MODES",
    "parse_mode",
]

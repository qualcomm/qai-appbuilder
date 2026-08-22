# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Background process manager - platform shared kernel.

Provides :class:`BackgroundProcessManagerPort` and its default
implementation :class:`SubprocessBackgroundProcessManager` for managing
long-running background subprocesses with structured lifecycle
(``session`` lifetime only), ready-probe detection, and event-bus
integration.

Background processes spawned through this manager are tied to the
owning daemon: when the daemon exits (gracefully or via abnormal
termination) the OS-backed kill-group safeguard reaps any surviving
children, so users never have to clean up orphaned subprocesses.

See ``docs/90-refactor/background-process-design.md`` for the full
design.
"""

from __future__ import annotations

from .events import BackgroundProcessDeleted, BackgroundProcessUpdated
from .factory import build_background_process_manager
from .manager import SubprocessBackgroundProcessManager
from .ports import (
    TERMINAL_STATUSES,
    BackgroundProcessManagerPort,
    Info,
    InvalidReadyPattern,
    Lifetime,
    Logs,
    ManagerError,
    ProcessNotFound,
    Ready,
    ReadyPortInUse,
    StartInput,
    Status,
    Time,
)
from .tool_handlers import handle_background_process, info_to_dict
from .tool_schemas import (
    BACKGROUND_PROCESS_TOOL_DESCRIPTION,
    BACKGROUND_PROCESS_TOOL_SCHEMA,
    validate_params,
)

__all__ = [
    "BACKGROUND_PROCESS_TOOL_DESCRIPTION",
    "BACKGROUND_PROCESS_TOOL_SCHEMA",
    "BackgroundProcessDeleted",
    "BackgroundProcessManagerPort",
    "BackgroundProcessUpdated",
    "Info",
    "InvalidReadyPattern",
    "Lifetime",
    "Logs",
    "ManagerError",
    "ProcessNotFound",
    "Ready",
    "ReadyPortInUse",
    "StartInput",
    "Status",
    "SubprocessBackgroundProcessManager",
    "TERMINAL_STATUSES",
    "Time",
    "build_background_process_manager",
    "handle_background_process",
    "info_to_dict",
    "validate_params",
]

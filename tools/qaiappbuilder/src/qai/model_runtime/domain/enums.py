# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain enumerations for the ``model_runtime`` bounded context."""

from __future__ import annotations

from enum import Enum


class ServiceState(str, Enum):
    """State of the local inference daemon."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


__all__ = ["ServiceState"]

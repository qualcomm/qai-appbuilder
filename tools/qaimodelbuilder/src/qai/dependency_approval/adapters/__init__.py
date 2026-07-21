# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Adapters for ``qai.dependency_approval``."""
from __future__ import annotations

from qai.dependency_approval.adapters.in_memory import InMemoryDepBroker

__all__ = ["InMemoryDepBroker"]

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Adapters for ``qai.remote_deploy``.

Re-exports the in-memory repository adapter.
"""
from __future__ import annotations

from qai.remote_deploy.adapters.in_memory import InMemoryRemoteInstanceRepository

__all__ = ["InMemoryRemoteInstanceRepository"]

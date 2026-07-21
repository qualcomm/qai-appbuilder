# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Thin re-export shim for the aria2c RPC-daemon download engine.

The implementation was lifted to
:mod:`qai.platform.download.aria2c_rpc_engine` (the platform shared
kernel) so it can be shared across bounded contexts without violating
the ``context-isolation`` import-linter contract. This module re-exports
the public names so every existing
``from qai.model_catalog.infrastructure.aria2c_rpc_download_engine import ...``
caller keeps working unchanged.

``_DaemonHandle`` and ``_to_progress`` are re-exported too because the
existing model_catalog unit tests import them by name from this path.
"""

from __future__ import annotations

from qai.platform.download.aria2c_rpc_engine import (
    Aria2cRpcDownloadEngine,
    DaemonSpawnerLike,
    RpcClientLike,
    _DaemonHandle,
    _to_progress,
)

__all__ = [
    "Aria2cRpcDownloadEngine",
    "RpcClientLike",
    "DaemonSpawnerLike",
    "_DaemonHandle",
    "_to_progress",
]

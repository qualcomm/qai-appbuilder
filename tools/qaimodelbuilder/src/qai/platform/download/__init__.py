# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Multi-threaded download shared kernel (aria2c engines + VOs + port).

This sub-package is the cross-context home of the ``aria2c`` download
engines, their download value objects, and the :class:`DownloadEnginePort`
contract. It lives under ``qai.platform.*`` (and not under any single
bounded context) on purpose:

* ``qai.model_catalog`` (PR-044 / PR-E1) drives model/blob downloads
  through :class:`Aria2cRpcDownloadEngine` (RPC daemon, real incremental
  progress) with :class:`Aria2cDownloadEngine` as its single-frame
  fallback.
* Any future context needing multi-connection HTTP downloads can reuse
  the **same** engines + port without importing ``qai.model_catalog``.

The ``context-isolation`` import-linter contract forbids cross-context
imports but exempts ``qai.** -> qai.platform.**``; placing the engines
here lets multiple contexts share them through the platform shared
kernel without any rule violation. Because the port types its ``job``
argument against the structural :class:`DownloadJobLike` Protocol,
platform never imports any concrete bounded-context entity.

Public surface (curated)
------------------------

* :class:`DownloadProgress` / :class:`SourceUrl` / :class:`StorageKey` --
  frozen download value objects.
* :class:`DownloadEnginePort` -- transport-agnostic engine Protocol.
* :class:`DownloadJobLike` -- structural Protocol describing what the
  engine reads from a download job (``job_id.value`` + ``progress``).
* :class:`Aria2cDownloadEngine` + :class:`ProcessRunnerLike` +
  :class:`Aria2cBinaryNotFoundError` -- single-frame CLI engine.
* :class:`Aria2cRpcDownloadEngine` + :class:`RpcClientLike` +
  :class:`DaemonSpawnerLike` -- RPC daemon engine (production default).
"""

from __future__ import annotations

from .aria2c_engine import (
    Aria2cBinaryNotFoundError,
    Aria2cDownloadEngine,
    ProcessRunnerLike,
)
from .aria2c_rpc_engine import (
    Aria2cRpcDownloadEngine,
    DaemonSpawnerLike,
    RpcClientLike,
)
from .ports import (
    DownloadEnginePort,
    DownloadJobLike,
)
from .value_objects import (
    DownloadProgress,
    SourceUrl,
    StorageKey,
)

__all__ = [
    "Aria2cBinaryNotFoundError",
    "Aria2cDownloadEngine",
    "Aria2cRpcDownloadEngine",
    "DaemonSpawnerLike",
    "DownloadEnginePort",
    "DownloadJobLike",
    "DownloadProgress",
    "ProcessRunnerLike",
    "RpcClientLike",
    "SourceUrl",
    "StorageKey",
]

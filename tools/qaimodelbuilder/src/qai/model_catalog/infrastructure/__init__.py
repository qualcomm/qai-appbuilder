# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Infrastructure adapters for the ``model_catalog`` bounded context (PR-044).

Holds non-SQLite real adapters: filesystem blob storage, ``aria2c``
subprocess engine, and the ``httpx``-backed release-manifest fetcher.

Adapters that ARE SQLite-backed live under
:mod:`qai.model_catalog.adapters` instead.
"""

from __future__ import annotations

from .aria2c_download_engine import (
    Aria2cBinaryNotFoundError,
    Aria2cDownloadEngine,
    ProcessRunnerLike,
)
from .aria2c_rpc_download_engine import (
    Aria2cRpcDownloadEngine,
    DaemonSpawnerLike,
    RpcClientLike,
)
from .file_system_blob_store import FileSystemBlobStore
from .http_provider_probe import HttpProviderProbe
from .http_release_manifest_fetcher import HttpReleaseManifestFetcher

__all__ = [
    "Aria2cBinaryNotFoundError",
    "Aria2cDownloadEngine",
    "Aria2cRpcDownloadEngine",
    "DaemonSpawnerLike",
    "FileSystemBlobStore",
    "HttpProviderProbe",
    "HttpReleaseManifestFetcher",
    "ProcessRunnerLike",
    "RpcClientLike",
]

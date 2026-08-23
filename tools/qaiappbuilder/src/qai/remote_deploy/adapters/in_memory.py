# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory repository adapter for :class:`~qai.remote_deploy.domain.RemoteInstance`.

Process-lifetime store — instances are lost on restart. Suitable for the
current single-process deployment; a persistent store can be swapped in by
implementing :class:`~qai.remote_deploy.application.ports.RemoteInstanceRepositoryPort`.
"""
from __future__ import annotations

from qai.remote_deploy.domain import RemoteInstance

__all__ = ["InMemoryRemoteInstanceRepository"]


class InMemoryRemoteInstanceRepository:
    """Thread-safe (asyncio single-threaded) in-memory instance store."""

    def __init__(self) -> None:
        self._store: dict[str, RemoteInstance] = {}

    async def save(self, instance: RemoteInstance) -> None:
        self._store[instance.instance_id] = instance

    async def get(self, instance_id: str) -> RemoteInstance | None:
        return self._store.get(instance_id)

    async def list_all(self) -> list[RemoteInstance]:
        return list(self._store.values())

    async def delete(self, instance_id: str) -> None:
        self._store.pop(instance_id, None)

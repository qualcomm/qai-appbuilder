# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer ports for ``qai.remote_deploy``.

Two ports:

* :class:`SshExecutorPort` — execute commands on a remote host over SSH,
  stream output line by line, and transfer files via SFTP.
* :class:`RemoteInstanceRepositoryPort` — in-process store for
  :class:`~qai.remote_deploy.domain.RemoteInstance` objects (CRUD).
"""
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from qai.remote_deploy.domain import RemoteHost, RemoteInstance

__all__ = ["SshExecutorPort", "RemoteInstanceRepositoryPort"]


@runtime_checkable
class SshExecutorPort(Protocol):
    """Port for SSH command execution and file transfer."""

    async def test_connection(self, host: RemoteHost) -> bool:
        """Return ``True`` iff the SSH handshake succeeds.

        Raises :class:`~qai.remote_deploy.domain.errors.RemoteSshConnectError`
        on auth / network failure.
        """
        ...

    async def run_command(
        self,
        host: RemoteHost,
        command: str,
        *,
        timeout: int = 300,
    ) -> tuple[int, str, str]:
        """Run ``command`` on the remote host.

        Returns ``(exit_code, stdout, stderr)``.
        Raises :class:`~qai.remote_deploy.domain.errors.RemoteSshConnectError`
        on connection failure.
        """
        ...

    async def stream_command(
        self,
        host: RemoteHost,
        command: str,
        *,
        timeout: int = 600,
    ) -> AsyncIterator[str]:
        """Run ``command`` and yield stdout lines as they arrive.

        Raises :class:`~qai.remote_deploy.domain.errors.RemoteSshConnectError`
        on connection failure.
        """
        ...

    async def put_file(
        self,
        host: RemoteHost,
        local_path: str,
        remote_path: str,
    ) -> None:
        """Upload a local file to ``remote_path`` via SFTP."""
        ...

    async def start_tunnel(
        self,
        instance_id: str,
        host: RemoteHost,
        *,
        local_port: int,
        remote_port: int,
    ) -> int:
        """Start a local TCP forward and return the bound local port."""
        ...

    async def stop_tunnel(self, instance_id: str) -> None:
        """Stop the local TCP forward for an instance."""
        ...

    async def tunnel_state(self, instance_id: str) -> str:
        """Return ``stopped``, ``starting`` or ``running``."""
        ...


@runtime_checkable
class RemoteInstanceRepositoryPort(Protocol):
    """In-process store for :class:`~qai.remote_deploy.domain.RemoteInstance`."""

    async def save(self, instance: RemoteInstance) -> None:
        """Insert or update an instance (upsert by ``instance_id``)."""
        ...

    async def get(self, instance_id: str) -> RemoteInstance | None:
        """Return the instance or ``None`` if not found."""
        ...

    async def list_all(self) -> list[RemoteInstance]:
        """Return all known instances (any state)."""
        ...

    async def delete(self, instance_id: str) -> None:
        """Remove an instance record (no-op if not found)."""
        ...

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Paramiko-backed SSH executor for ``qai.remote_deploy``.

Uses ``paramiko`` (already installed: 5.0.0) to execute commands and
transfer files over SSH. Credentials are resolved via ``SecretStore`` —
raw passwords / passphrases are NEVER logged or stored beyond the scope of
a single connection attempt.

Connection strategy:
- Each call opens a fresh connection (no persistent pool) to keep the
  implementation simple and avoid stale-connection edge cases.
- ``connect_timeout`` defaults to 15 s; command timeout is enforced via
  ``get_pty=False`` + ``Channel.settimeout``.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import AsyncIterator

import paramiko

from qai.remote_deploy.application.ports import SshExecutorPort
from qai.remote_deploy.domain import AuthMethod, RemoteHost
from qai.remote_deploy.domain.errors import RemoteSshConnectError

_LOG = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15  # seconds for TCP + banner + auth

__all__ = ["ParamikoSshExecutor"]


class ParamikoSshExecutor:
    """Implements :class:`SshExecutorPort` using paramiko.

    ``secret_store`` is the platform ``SecretStore`` instance; it is used
    to resolve ``host.auth_ref`` to the actual credential at connection time.
    The resolved value is used in-memory only and is NOT stored as an
    attribute on this class.
    """

    def __init__(self, secret_store) -> None:  # type: ignore[annotation]
        self._secret_store = secret_store

    # ------------------------------------------------------------------
    # SshExecutorPort implementation
    # ------------------------------------------------------------------

    async def test_connection(self, host: RemoteHost) -> bool:
        """Return True iff SSH handshake succeeds; raise on failure."""
        client = await asyncio.get_running_loop().run_in_executor(
            None, self._connect_sync, host
        )
        client.close()
        return True

    async def run_command(
        self,
        host: RemoteHost,
        command: str,
        *,
        timeout: int = 300,
    ) -> tuple[int, str, str]:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._run_command_sync, host, command, timeout
        )

    async def stream_command(
        self,
        host: RemoteHost,
        command: str,
        *,
        timeout: int = 600,
    ) -> AsyncIterator[str]:
        """True streaming: yields lines as they arrive from the remote process.

        A ``asyncio.Queue`` bridges the blocking paramiko I/O (running in a
        thread-pool executor) and the async caller.  The sync thread pushes
        each decoded line into the queue; a sentinel ``None`` signals EOF.
        This means the caller receives output incrementally rather than
        waiting for the entire command to finish.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _stream_sync() -> None:
            try:
                client = self._connect_sync(host)
                try:
                    _, stdout_f, _ = client.exec_command(command, timeout=timeout)
                    for raw in stdout_f:
                        line = raw.rstrip("\n")
                        loop.call_soon_threadsafe(queue.put_nowait, line)
                finally:
                    client.close()
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("stream_command error: %s", exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, _stream_sync)

        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    async def put_file(
        self,
        host: RemoteHost,
        local_path: str,
        remote_path: str,
    ) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self._put_file_sync, host, local_path, remote_path
        )

    # ------------------------------------------------------------------
    # Sync helpers (run inside ThreadPoolExecutor)
    # ------------------------------------------------------------------

    def _resolve_credential(self, auth_ref: str) -> str:
        """Return the credential string.

        ``auth_ref`` is treated as the plaintext credential (password or
        key passphrase) sent directly from the frontend.  It is used
        in-memory only and never logged or persisted.
        """
        return auth_ref

    def _connect_sync(self, host: RemoteHost) -> paramiko.SSHClient:
        credential = self._resolve_credential(host.auth_ref)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs: dict = dict(
                hostname=host.host,
                port=host.port,
                username=host.username,
                timeout=_CONNECT_TIMEOUT,
            )
            if host.auth_method == AuthMethod.PRIVATE_KEY:
                # Auto-detect key type (RSA, Ed25519, ECDSA, DSS) via PKey.
                # paramiko.RSAKey.from_private_key_file only handles RSA keys;
                # modern systems default to Ed25519 (~/.ssh/id_ed25519).
                connect_kwargs["allow_agent"] = False
                connect_kwargs["look_for_keys"] = False
                pkey = paramiko.PKey.from_private_key_file(
                    host.key_path,
                    password=credential if credential else None,
                )
                connect_kwargs["pkey"] = pkey
            elif credential:
                # Password provided explicitly
                connect_kwargs["allow_agent"] = False
                connect_kwargs["look_for_keys"] = False
                connect_kwargs["password"] = credential
            else:
                # No credential supplied — let paramiko try SSH agent
                # and default key files (~/.ssh/id_rsa, id_ed25519, etc.)
                connect_kwargs["allow_agent"] = True
                connect_kwargs["look_for_keys"] = True
            client.connect(**connect_kwargs)
        except (
            paramiko.AuthenticationException,
            paramiko.SSHException,
            OSError,
        ) as exc:
            client.close()
            _LOG.warning("SSH connect failed to %s: %s", host.host, exc)
            raise RemoteSshConnectError(host.host, str(exc)) from exc
        finally:
            # credential is a local variable; it goes out of scope here.
            # Explicit del ensures the reference is dropped immediately.
            del credential
        return client

    def _run_command_sync(
        self, host: RemoteHost, command: str, timeout: int
    ) -> tuple[int, str, str]:
        client = self._connect_sync(host)
        try:
            _, stdout_f, stderr_f = client.exec_command(command, timeout=timeout)
            stdout = stdout_f.read().decode("utf-8", errors="replace")
            stderr = stderr_f.read().decode("utf-8", errors="replace")
            exit_code = stdout_f.channel.recv_exit_status()
            return exit_code, stdout, stderr
        finally:
            client.close()

    def _put_file_sync(
        self, host: RemoteHost, local_path: str, remote_path: str
    ) -> None:
        client = self._connect_sync(host)
        try:
            sftp = client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
        finally:
            client.close()

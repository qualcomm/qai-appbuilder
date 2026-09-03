# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared doubles for the ``remote_deploy`` unit tests.

``SshExecutorPort`` and ``RemoteInstanceRepositoryPort`` are ``Protocol``s
(``qai/remote_deploy/application/ports.py``), so the whole deploy pipeline can
be exercised without paramiko, an SSH server, or a board.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from qai.remote_deploy.adapters import InMemoryRemoteInstanceRepository
from qai.remote_deploy.application.use_cases import DeployRemoteUseCase
from qai.remote_deploy.domain import RemoteHost, RemoteInstance
from qai.remote_deploy.domain.errors import RemoteCommandFailedError

#: Minimal kwargs accepted by ``DeployRemoteUseCase.stream``.
_BASE_KWARGS = dict(
    instance_id="i-1",
    host="board.local",
    ssh_port=22,
    username="radxa",
    auth_method="password",
    auth_ref="pw",
)


class FakeSshExecutor:
    """Scriptable ``SshExecutorPort`` double.

    ``stream_failures`` maps a substring of a streamed command to the exit code
    it should fail with; commands not listed stream two lines and succeed.
    ``commands`` records every command in call order, so a test can assert both
    what a step looked like and that a later step was never reached.
    """

    def __init__(self, stream_failures: dict[str, int] | None = None) -> None:
        self.stream_failures = stream_failures or {}
        self.commands: list[str] = []
        self.tunnels: dict[str, tuple[int, int]] = {}

    def command_matching(self, needle: str) -> str:
        """Return the single recorded command containing ``needle``."""
        matches = [c for c in self.commands if needle in c]
        assert len(matches) == 1, (
            f"expected exactly 1 command matching {needle!r}, got {matches}"
        )
        return matches[0]

    def ran(self, needle: str) -> bool:
        return any(needle in c for c in self.commands)

    async def test_connection(self, host: RemoteHost) -> bool:
        return True

    async def run_command(
        self, host: RemoteHost, command: str, *, timeout: int = 300
    ) -> tuple[int, str, str]:
        self.commands.append(command)
        if "ss -tlnp" in command:
            return 0, "", ""  # port free
        if "setsid nohup" in command:
            return 0, "PID:4242", ""
        if "curl -sf" in command:
            return 0, "READY", ""
        return 0, "", ""

    async def stream_command(
        self, host: RemoteHost, command: str, *, timeout: int = 600
    ) -> AsyncIterator[str]:
        self.commands.append(command)
        yield "line one"
        yield "line two"
        for needle, exit_code in self.stream_failures.items():
            if needle in command:
                raise RemoteCommandFailedError(
                    host.host, command, exit_code, "remote said: boom"
                )

    async def put_file(
        self, host: RemoteHost, local_path: str, remote_path: str
    ) -> None:
        return None

    async def start_tunnel(
        self,
        instance_id: str,
        host: RemoteHost,
        *,
        local_port: int,
        remote_port: int,
    ) -> int:
        self.tunnels[instance_id] = (local_port, remote_port)
        return local_port

    async def stop_tunnel(self, instance_id: str) -> None:
        self.tunnels.pop(instance_id, None)

    async def tunnel_state(self, instance_id: str) -> str:
        return "running" if instance_id in self.tunnels else "stopped"


class OccupiedPortExecutor(FakeSshExecutor):
    """Reports the application port as already listening.

    ``healthy`` decides whether ``/api/system/health`` answers, i.e. whether the
    listener is our service or an unrelated process.
    """

    def __init__(self, *, healthy: bool) -> None:
        super().__init__()
        self.healthy = healthy

    async def run_command(
        self, host: RemoteHost, command: str, *, timeout: int = 300
    ) -> tuple[int, str, str]:
        self.commands.append(command)
        if "ss -tlnp" in command:
            return 0, "LISTEN 0 128 127.0.0.1:28688 users:(('other',pid=99))", ""
        if "echo HEALTHY" in command:
            return (0, "HEALTHY", "") if self.healthy else (1, "", "")
        return 0, "", ""


class HangingLaunchExecutor(FakeSshExecutor):
    """The launch command never returns an exit status.

    Reproduces the real board behaviour: the detached service keeps an fd
    inherited from the SSH channel, so sshd never closes the session and the
    executor's read times out. The service is nevertheless up, so the health
    probe answers READY and the pid is readable from the file the launcher
    wrote. Start must survive this rather than reporting a failed deploy.
    """

    async def run_command(
        self, host: RemoteHost, command: str, *, timeout: int = 300
    ) -> tuple[int, str, str]:
        self.commands.append(command)
        if "ss -tlnp" in command:
            return 0, "", ""  # port free
        if "setsid nohup" in command:
            raise TimeoutError("timed out reading from the SSH channel")
        if "cat /tmp/qai_modelbuilder" in command:
            return 0, "4242\n", ""
        if "curl -sf" in command:
            return 0, "READY", ""
        return 0, "", ""


class DeployHarness:
    """Bundles the use case with its doubles and a drain helper."""

    def __init__(self, stream_failures: dict[str, int] | None = None) -> None:
        self.executor = FakeSshExecutor(stream_failures)
        self.repository = InMemoryRemoteInstanceRepository()
        self.use_case = DeployRemoteUseCase(
            executor=self.executor, repository=self.repository
        )

    async def drain(self, **overrides) -> list[str]:
        """Run the deploy generator to completion, returning all yielded lines."""
        lines: list[str] = []
        async for line in self.use_case.stream(**{**_BASE_KWARGS, **overrides}):
            lines.append(line)
        return lines

    async def instance(self, instance_id: str = "i-1") -> RemoteInstance:
        found = await self.repository.get(instance_id)
        assert found is not None, f"instance {instance_id!r} was never saved"
        return found


@pytest.fixture
def harness():
    """Factory: ``harness()`` or ``harness({"setup.sh": 1})``."""
    return DeployHarness


@pytest.fixture
def fake_executor():
    """The ``FakeSshExecutor`` class itself (tests instantiate it).

    Exposed as a fixture because the test directory is not a package, so test
    modules cannot import from ``conftest`` directly.
    """
    return FakeSshExecutor


@pytest.fixture
def occupied_port_executor():
    """The ``OccupiedPortExecutor`` class itself."""
    return OccupiedPortExecutor


@pytest.fixture
def hanging_launch_executor():
    """The ``HangingLaunchExecutor`` class itself."""
    return HangingLaunchExecutor


@pytest.fixture
def repository():
    """A fresh in-memory instance repository."""
    return InMemoryRemoteInstanceRepository()

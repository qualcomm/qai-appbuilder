# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Unit tests for the install / start split.

Install and start used to be one generator, which meant a failed start forced a
re-download and a successful install could not be inspected before the service
came up. They are now two use cases sharing one instance record, with
``DeploymentState.INSTALLED`` as the resting state between them.

Also covers two behaviours that moved as part of the split:

* the port precheck no longer trusts "something is listening" as proof that the
  service is ours — it probes ``/api/system/health`` first, so an unrelated
  process on the port is a ``RemotePortInUseError`` rather than a reported
  success with a dead URL
* the remote kill lives in ``StopInstanceUseCase`` instead of the HTTP layer
"""

from __future__ import annotations

import pytest

from qai.remote_deploy.application.use_cases import (
    InstallRemoteUseCase,
    StartRemoteUseCase,
    StopInstanceUseCase,
)
from qai.remote_deploy.domain import (
    AuthMethod,
    DeploymentState,
    RemoteHost,
    RemoteInstance,
)
from qai.remote_deploy.domain.errors import (
    RemoteCommandFailedError,
    RemoteHostValidationError,
    RemoteInstanceNotFoundError,
    RemotePortInUseError,
)

pytestmark = pytest.mark.unit

_KWARGS = dict(
    instance_id="i-1",
    host="board.local",
    ssh_port=22,
    username="radxa",
    auth_method="password",
    auth_ref="pw",
)


async def _drain(gen) -> list[str]:
    return [line async for line in gen]


def _remote() -> RemoteHost:
    return RemoteHost(
        host="board.local",
        port=22,
        username="radxa",
        auth_method=AuthMethod.PASSWORD,
        auth_ref="pw",
    )


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


async def test_install_ends_installed_and_starts_nothing(
    fake_executor, repository
) -> None:
    executor = fake_executor()
    install = InstallRemoteUseCase(executor=executor, repository=repository)

    await _drain(install.stream(**_KWARGS))

    instance = await repository.get("i-1")
    assert instance is not None
    assert instance.state is DeploymentState.INSTALLED
    assert not executor.ran("setsid nohup")
    assert not executor.ran("ss -tlnp")


async def test_install_validates_port_before_downloading(
    fake_executor, repository
) -> None:
    """Failing before a 45 MB download beats failing after it."""
    executor = fake_executor()
    install = InstallRemoteUseCase(executor=executor, repository=repository)

    with pytest.raises(RemoteHostValidationError):
        await _drain(install.stream(**_KWARGS, remote_port=9999))

    assert executor.commands == []


async def test_install_failure_does_not_reach_installed(
    fake_executor, repository
) -> None:
    executor = fake_executor({"setup.sh": 1})
    install = InstallRemoteUseCase(executor=executor, repository=repository)

    with pytest.raises(RemoteCommandFailedError):
        await _drain(install.stream(**_KWARGS))

    instance = await repository.get("i-1")
    assert instance is not None
    assert instance.state is DeploymentState.FAILED


async def test_install_clears_a_half_installed_directory_before_moving_in(
    fake_executor, repository
) -> None:
    """A previous interrupted install can leave ``~/qaiappbuilder`` without
    ``start.sh``. ``mv`` onto an existing directory nests the source inside
    it instead of replacing it, so the download step must clear the target
    first.
    """
    executor = fake_executor()
    install = InstallRemoteUseCase(executor=executor, repository=repository)

    await _drain(install.stream(**_KWARGS))

    download_cmd = executor.command_matching("curl -fsSL")
    assert "rm -rf ~/qaiappbuilder" in download_cmd
    assert download_cmd.index("rm -rf ~/qaiappbuilder") < download_cmd.index(
        'mv "$_TMP"'
    )


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


async def test_start_reuses_the_installed_record(fake_executor, repository) -> None:
    """Start must update the install's record, not create a second one."""
    executor = fake_executor()
    install = InstallRemoteUseCase(executor=executor, repository=repository)
    start = StartRemoteUseCase(executor=executor, repository=repository)

    await _drain(install.stream(**_KWARGS))
    await _drain(start.stream(**_KWARGS))

    assert len(await repository.list_all()) == 1
    instance = await repository.get("i-1")
    assert instance is not None
    assert instance.state is DeploymentState.RUNNING
    assert instance.remote_pid == 4242


async def test_start_survives_a_launch_command_that_never_returns(
    hanging_launch_executor, repository
) -> None:
    """A hung launch channel must not be reported as a failed start.

    On a real board the detached service keeps an fd inherited from the SSH
    channel, so sshd never closes the session and reading the launch command's
    exit status times out — while the service itself is up and healthy. Treating
    that as fatal aborted the SSE stream after a successful start, which left the
    tunnel unopened and no browser tab. The health probe, not the launch exit
    status, is the gate.
    """
    executor = hanging_launch_executor()
    start = StartRemoteUseCase(executor=executor, repository=repository)

    lines = await _drain(start.stream(**_KWARGS))

    instance = await repository.get("i-1")
    assert instance is not None
    assert instance.state is DeploymentState.RUNNING
    # stdout died with the channel, so the pid must come from the file the
    # launcher wrote — otherwise ``stop`` degrades to a port-scoped pkill.
    assert instance.remote_pid == 4242
    assert any("recovered pid 4242" in line for line in lines)
    # The anomaly is surfaced rather than hidden: this is the one clue an
    # operator has that the launch channel misbehaved.
    assert any("returned no exit status" in line for line in lines)
    assert any(line.startswith("[done]") for line in lines)


async def test_start_without_install_still_works(fake_executor, repository) -> None:
    """Start is usable standalone — e.g. restarting after a manual stop."""
    executor = fake_executor()
    start = StartRemoteUseCase(executor=executor, repository=repository)

    await _drain(start.stream(**_KWARGS))

    instance = await repository.get("i-1")
    assert instance is not None
    assert instance.state is DeploymentState.RUNNING


# ---------------------------------------------------------------------------
# Port precheck — occupied is not the same as "ours"
# ---------------------------------------------------------------------------


async def test_occupied_by_our_service_reports_running(
    occupied_port_executor, repository
) -> None:
    executor = occupied_port_executor(healthy=True)
    start = StartRemoteUseCase(executor=executor, repository=repository)

    await _drain(start.stream(**_KWARGS))

    instance = await repository.get("i-1")
    assert instance is not None
    assert instance.state is DeploymentState.RUNNING
    # It was already up — we must not have launched a second copy.
    assert not executor.ran("setsid nohup")


async def test_occupied_by_foreign_process_is_an_error(
    occupied_port_executor, repository
) -> None:
    """Previously this was reported as a successful deploy with a dead URL."""
    executor = occupied_port_executor(healthy=False)
    start = StartRemoteUseCase(executor=executor, repository=repository)

    with pytest.raises(RemotePortInUseError):
        await _drain(start.stream(**_KWARGS))

    instance = await repository.get("i-1")
    assert instance is not None
    assert instance.state is DeploymentState.FAILED
    assert "not QAI AppBuilder" in instance.error_message
    assert not executor.ran("setsid nohup")


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


async def _running_instance(repository, *, pid: int) -> RemoteInstance:
    instance = RemoteInstance(
        instance_id="i-1",
        host="board.local",
        port=28688,
        username="radxa",
        state=DeploymentState.RUNNING,
        remote_pid=pid,
    )
    await repository.save(instance)
    return instance


async def test_stop_kills_by_pid_when_known(fake_executor, repository) -> None:
    executor = fake_executor()
    await _running_instance(repository, pid=4242)
    stop = StopInstanceUseCase(executor=executor, repository=repository)

    result = await stop.execute(instance_id="i-1", remote=_remote())

    assert result.stopped
    assert executor.ran("kill 4242")
    instance = await repository.get("i-1")
    assert instance is not None
    assert instance.state is DeploymentState.STOPPED
    assert instance.remote_pid == 0


async def test_stop_falls_back_to_port_scoped_pkill(
    fake_executor, repository
) -> None:
    """The fallback must never be a broad ``pkill -f start.sh``."""
    executor = fake_executor()
    await _running_instance(repository, pid=0)
    stop = StopInstanceUseCase(executor=executor, repository=repository)

    await stop.execute(instance_id="i-1", remote=_remote())

    assert executor.ran("pkill -f 'start.sh --port 28688'")


async def test_stop_without_credentials_only_marks_the_record(
    fake_executor, repository
) -> None:
    executor = fake_executor()
    await _running_instance(repository, pid=4242)
    stop = StopInstanceUseCase(executor=executor, repository=repository)

    result = await stop.execute(instance_id="i-1", remote=None)

    assert result.stopped
    assert executor.commands == []
    instance = await repository.get("i-1")
    assert instance is not None
    assert instance.state is DeploymentState.STOPPED


async def test_stop_unknown_instance_raises_not_found(
    fake_executor, repository
) -> None:
    executor = fake_executor()
    stop = StopInstanceUseCase(executor=executor, repository=repository)

    with pytest.raises(RemoteInstanceNotFoundError):
        await stop.execute(instance_id="nope", remote=_remote())


async def test_stop_reports_failure_when_process_survives_kill(
    still_alive_executor, repository
) -> None:
    """``kill``/``pkill`` routinely exit 0 while the process lingers (slow
    shutdown, ignored SIGTERM) — a non-error exit must not be reported as a
    successful stop.
    """
    executor = still_alive_executor()
    await _running_instance(repository, pid=4242)
    stop = StopInstanceUseCase(executor=executor, repository=repository)

    result = await stop.execute(instance_id="i-1", remote=_remote())

    assert not result.stopped
    instance = await repository.get("i-1")
    assert instance is not None
    assert instance.state is DeploymentState.RUNNING
    assert instance.remote_pid == 4242
    assert "Could not confirm" in instance.error_message

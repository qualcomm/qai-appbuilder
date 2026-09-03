# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Unit tests for the ``remote_deploy`` deploy pipeline's failure handling.

Before this suite, ``ParamikoSshExecutor.stream_command`` swallowed every
exception and never checked the remote exit status, so a broken ``setup.sh``
was indistinguishable from a successful one: ``_deploy_gen`` marched on to
start a service that could not come up and the operator saw only the generic
"service did not become ready within 90 s".

These tests pin the corrected contract at the use-case boundary:

- a non-zero exit from the download step aborts before ``setup.sh`` runs
- a non-zero exit from ``setup.sh`` aborts before the start command runs
- in both cases the instance lands in ``FAILED`` with a step-specific
  ``error_message``, and the remote diagnostic reaches the log stream
- the happy path still reaches ``RUNNING``
"""

from __future__ import annotations

import pytest

from qai.remote_deploy.domain import DeploymentState
from qai.remote_deploy.domain.errors import RemoteCommandFailedError

pytestmark = pytest.mark.unit


async def test_setup_failure_aborts_before_start(harness) -> None:
    """A non-zero ``setup.sh`` must stop the pipeline, not start the service."""
    h = harness({"setup.sh": 1})

    with pytest.raises(RemoteCommandFailedError) as excinfo:
        await h.drain()

    assert excinfo.value.exit_code == 1
    instance = await h.instance()
    assert instance.state is DeploymentState.FAILED
    assert instance.error_message == "setup.sh failed on the remote host."
    assert not h.executor.ran("setsid nohup")


async def test_download_failure_aborts_before_setup(harness) -> None:
    """A failed release download must stop before ``setup.sh`` is attempted."""
    h = harness({"qaiappbuilder.zip": 2})

    with pytest.raises(RemoteCommandFailedError):
        await h.drain()

    instance = await h.instance()
    assert instance.state is DeploymentState.FAILED
    assert instance.error_message == (
        "Download / extract of the release bundle failed. The board must be "
        "able to reach github.com."
    )
    assert not h.executor.ran("setup.sh")


async def test_failure_diagnostic_reaches_log_stream(harness) -> None:
    """The remote output tail must be yielded before the error propagates."""
    h = harness({"setup.sh": 1})

    lines: list[str] = []
    with pytest.raises(RemoteCommandFailedError):
        async for line in h.use_case.stream(
            instance_id="i-1",
            host="board.local",
            ssh_port=22,
            username="radxa",
            auth_method="password",
            auth_ref="pw",
        ):
            lines.append(line)

    assert any("remote said: boom" in line for line in lines)
    assert any(line.startswith("[err]") for line in lines)
    # The same diagnostic is retained on the instance for the instances list.
    instance = await h.instance()
    assert any("remote said: boom" in line for line in instance.log_lines)


async def test_happy_path_reaches_running(harness) -> None:
    """With every step succeeding the instance ends up RUNNING."""
    h = harness()

    await h.drain()

    instance = await h.instance()
    assert instance.state is DeploymentState.RUNNING
    assert instance.remote_pid == 4242
    assert h.executor.ran("setsid nohup")

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Unit tests for the SSO wiring of the remote start command.

Completing an Okta login from the PC's browser against a board reached through
an SSH tunnel needs three things that do not hold by default on a board, all
supplied as environment variables on the remote ``start.sh`` invocation:

* ``QAI_AUTH__ENABLED=true`` — ``AuthSettings.enabled`` defaults to False on
  headless Linux (``settings._default_auth_enabled`` keys off DISPLAY /
  WAYLAND_DISPLAY), which is exactly the target shape.
* ``DISPLAY`` / ``WAYLAND_DISPLAY`` unset — otherwise ``start.sh`` launches a
  browser on the board that no one can see.
* ``QAI_AUTH__SESSION_COOKIE_NAME`` scoped to the port — cookies are scoped by
  host and ignore the port (RFC 6265), so the board's session (reached at
  ``localhost:<port>``) would otherwise overwrite the local instance's
  ``qai_session`` and log the operator out of it.

The port is also constrained: ``routes/auth.py`` derives the OIDC
``redirect_uri`` from the bound port and Okta strict-matches it, so only the
registered loopback ports in ``factory/config/ports.json`` ``fallbacks`` can
carry a login.
"""

from __future__ import annotations

import pytest

from qai.platform.config.ports import fallback_ports
from qai.remote_deploy.domain.errors import RemoteHostValidationError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Environment injection
# ---------------------------------------------------------------------------


async def test_sso_start_command_injects_auth_env(harness) -> None:
    h = harness()

    await h.drain(remote_port=28688)

    start = h.executor.command_matching("setsid nohup")
    assert "env -u DISPLAY -u WAYLAND_DISPLAY" in start
    assert "QAI_AUTH__ENABLED=true" in start
    assert "QAI_AUTH__SESSION_COOKIE_NAME=qai_session_28688" in start
    assert "bash start.sh --port 28688" in start


async def test_session_cookie_name_tracks_the_port(harness) -> None:
    """Two boards on different ports must not share a cookie slot."""
    h = harness()

    await h.drain(remote_port=8989)

    start = h.executor.command_matching("setsid nohup")
    assert "QAI_AUTH__SESSION_COOKIE_NAME=qai_session_8989" in start


async def test_env_prefix_precedes_bash_not_setsid(harness) -> None:
    """``env`` must wrap bash, leaving setsid/nohup as the outer detachers."""
    h = harness()

    await h.drain()

    start = h.executor.command_matching("setsid nohup")
    assert start.index("setsid nohup") < start.index("env -u DISPLAY")
    assert start.index("env -u DISPLAY") < start.index("bash start.sh")


async def test_sso_disabled_injects_nothing(harness) -> None:
    h = harness()

    await h.drain(enable_sso=False)

    start = h.executor.command_matching("setsid nohup")
    assert "QAI_AUTH__ENABLED" not in start
    assert "QAI_AUTH__SESSION_COOKIE_NAME" not in start
    assert "env -u DISPLAY" not in start
    assert "bash start.sh --port" in start


# ---------------------------------------------------------------------------
# Detachment from the SSH channel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enable_sso", [True, False])
async def test_start_detaches_all_three_fds(harness, enable_sso: bool) -> None:
    """The detached service must not inherit the SSH channel's stdin.

    If it does, sshd keeps the session open because a live fd remains, so the
    executor's ``stdout_f.read()`` never sees EOF, hits the channel timeout, and
    raises — aborting the SSE stream *after* the remote service is already
    listening. The operator then sees a start that "failed" with a running
    service, no tunnel (``_finish_running`` never runs) and no browser tab.

    This only reproduces when the service stays up: one that exits immediately
    releases the fd and lets read() return, which is why every earlier failing
    run masked it.
    """
    h = harness()

    await h.drain(enable_sso=enable_sso)

    start = h.executor.command_matching("setsid nohup")
    assert "< /dev/null" in start
    assert "2>&1" in start
    # stdin must be closed before stdout is pointed at the log file, so the
    # redirect applies to the detached service rather than to `echo PID:$!`.
    assert start.index("< /dev/null") < start.index("qai_modelbuilder")


# ---------------------------------------------------------------------------
# Port whitelist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port", fallback_ports())
async def test_okta_registered_ports_are_accepted(harness, port: int) -> None:
    h = harness()

    await h.drain(remote_port=port)

    instance = await h.instance()
    assert instance.port == port


async def test_unregistered_port_rejected_when_sso_on(harness) -> None:
    """An unregistered port would fail at Okta with a redirect_uri mismatch."""
    h = harness()

    with pytest.raises(RemoteHostValidationError) as excinfo:
        await h.drain(remote_port=9999)

    message = str(excinfo.value)
    assert "9999" in message
    assert "redirect_uri" in message
    # Nothing should have been attempted on the remote host.
    assert h.executor.commands == []


async def test_unregistered_port_allowed_when_sso_off(harness) -> None:
    """Without the login gate any port is fine — no redirect_uri is derived."""
    h = harness()

    await h.drain(remote_port=9999, enable_sso=False)

    instance = await h.instance()
    assert instance.port == 9999


async def test_default_port_is_okta_registered(harness) -> None:
    """The default must be usable with SSO out of the box, and not be 8989.

    8989 is what the local QAI instance serving this UI already binds, and the
    tunnel has to bind the same number the board listens on.
    """
    h = harness()

    await h.drain()

    instance = await h.instance()
    assert instance.port in fallback_ports()
    assert instance.port == 28688

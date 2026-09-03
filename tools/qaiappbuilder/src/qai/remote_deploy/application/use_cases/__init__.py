# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for ``qai.remote_deploy``.

* :class:`ConnectRemoteUseCase`  — test SSH connectivity.
* :class:`InstallRemoteUseCase`  — download the release bundle + run setup.sh.
* :class:`StartRemoteUseCase`    — start the service and wait for readiness.
* :class:`DeployRemoteUseCase`   — install then start (the original one-shot
  pipeline, kept as a thin composition of the two above).
* :class:`ListInstancesUseCase`  — list all known remote instances.
* :class:`StopInstanceUseCase`   — stop a running remote instance.

Install and start are separate because they have different costs and different
failure modes: installing downloads the release bundle and builds a venv (slow —
minutes, dominated by pip on an aarch64 board — but idempotent and independent of
the port), while starting is quick and depends on choices the operator makes per
run (port, SSO). Bundling them meant a failed start forced a re-download, and a
successful install could not be inspected before the service came up.
"""
from __future__ import annotations

from dataclasses import dataclass

from qai.platform.config.ports import fallback_ports
from qai.remote_deploy.application.ports import (
    RemoteInstanceRepositoryPort,
    SshExecutorPort,
)
from qai.remote_deploy.domain import (
    AuthMethod,
    DeploymentState,
    RemoteHost,
    RemoteInstance,
)
from qai.remote_deploy.domain.errors import (
    RemoteHostValidationError,
    RemoteInstanceNotFoundError,
    RemotePortInUseError,
    RemoteSshConnectError,
)

__all__ = [
    "ConnectRemoteUseCase",
    "ConnectRemoteResult",
    "InstallRemoteUseCase",
    "StartRemoteUseCase",
    "DeployRemoteUseCase",
    "DeployRemoteResult",
    "ListInstancesUseCase",
    "ListInstancesResult",
    "StopInstanceUseCase",
    "StopInstanceResult",
]

# ---------------------------------------------------------------------------
# Remote deploy constants
# ---------------------------------------------------------------------------
# External (open-source) edition: download a pre-built release zip from GitHub
# Releases rather than git-cloning the source. The zip ships a ready-to-run
# bundle (frontend/dist/ already built), so only `setup.sh --no-frontend`
# (Python venv + deps + data init) is needed — no Node/pnpm required.
_RELEASE_URL = "https://github.com/qualcomm/qai-appbuilder/releases/download/v3.0.0/qaiappbuilder.zip"
_REMOTE_INSTALL_DIR = "~/qaiappbuilder"

#: Default remote application port. 28688 rather than 8989 on purpose: the
#: local QAI instance that serves this deploy UI already owns 8989, and the
#: SSH tunnel must bind the SAME port locally as the board listens on (the
#: board derives its OIDC ``redirect_uri`` from its own bound port — see
#: ``interfaces/http/routes/auth.py``). 28688 is the second Okta-registered
#: loopback port (``factory/config/ports.json`` ``fallbacks``), so it keeps SSO
#: working without colliding with the local instance.
_DEFAULT_REMOTE_PORT = 28688


# ---------------------------------------------------------------------------
# ConnectRemote
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ConnectRemoteResult:
    """Return value of :class:`ConnectRemoteUseCase`."""

    success: bool
    host: str
    message: str


@dataclass(slots=True)
class ConnectRemoteUseCase:
    """Test SSH connectivity to a remote host."""

    executor: SshExecutorPort

    async def execute(
        self,
        *,
        host: str,
        ssh_port: int,
        username: str,
        auth_method: str,
        auth_ref: str,
        key_path: str = "",
    ) -> ConnectRemoteResult:
        _validate_host_params(host, ssh_port, username)
        remote = _build_remote_host(
            host, ssh_port, username, auth_method, auth_ref, key_path
        )
        ok = await self.executor.test_connection(remote)
        return ConnectRemoteResult(
            success=ok,
            host=host,
            message="Connection successful." if ok else "Connection failed.",
        )


# ---------------------------------------------------------------------------
# InstallRemote  (streaming)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class InstallRemoteUseCase:
    """Download the release bundle and run ``setup.sh`` on the remote host.

    Ends in :attr:`DeploymentState.INSTALLED`. Nothing is listening yet — call
    :class:`StartRemoteUseCase` for that.
    """

    executor: SshExecutorPort
    repository: RemoteInstanceRepositoryPort

    async def stream(
        self,
        *,
        instance_id: str,
        host: str,
        ssh_port: int,
        username: str,
        auth_method: str,
        auth_ref: str,
        key_path: str = "",
        remote_port: int = _DEFAULT_REMOTE_PORT,
        enable_sso: bool = True,
    ):
        """Async generator — yields log lines during install."""
        _validate_host_params(host, ssh_port, username)
        # Validated here too, even though only start uses the port: failing
        # before a 45 MB download beats failing after it.
        _validate_remote_port(remote_port, enable_sso=enable_sso)
        remote = _build_remote_host(
            host, ssh_port, username, auth_method, auth_ref, key_path
        )

        instance = await self.repository.get(instance_id) or RemoteInstance(
            instance_id=instance_id,
            host=host,
            port=remote_port,
            username=username,
            state=DeploymentState.CONNECTING,
        )
        instance.host = host
        instance.port = remote_port
        instance.username = username
        instance.state = DeploymentState.CONNECTING
        instance.error_message = ""
        await self.repository.save(instance)

        # --- Step 1: test connection ---
        yield f"[ssh] Connecting to {username}@{host}:{ssh_port} …"
        try:
            await self.executor.test_connection(remote)
        except RemoteSshConnectError:
            instance.state = DeploymentState.FAILED
            instance.error_message = f"SSH connection to {host} failed."
            await self.repository.save(instance)
            raise

        # --- Step 2: download and extract release zip (skip if already there) ---
        instance.state = DeploymentState.INSTALLING
        await self.repository.save(instance)
        yield (
            "[install] Downloading QAI AppBuilder release "
            "(~14 MB, no progress output — this can take a few minutes on a "
            "slow link) …"
        )
        # ``-s`` (not the default progress meter) is required, not cosmetic:
        # curl redraws progress with ``\r`` and no newline, so the streaming
        # executor — which splits on ``\n`` — sees ONE unterminated line for
        # the whole transfer. The UI then shows a frozen log and a stalled
        # download is indistinguishable from a working one. ``-S`` keeps the
        # error message that ``-s`` would otherwise suppress.
        #
        # Going silent means no channel traffic during the transfer, so the
        # step timeout below MUST outlive ``--max-time`` or paramiko's
        # inactivity timeout fires first and reports a misleading SSH error
        # instead of curl's own diagnosis.
        download_cmd = (
            f"if [ -f {_REMOTE_INSTALL_DIR}/start.sh ]; then "
            f"  echo 'Already installed — skipping download.'; "
            f"else "
            f"  _TMP=/tmp/qaiappbuilder_extract_$$ && "
            f"  mkdir -p \"$_TMP\" && "
            f"  curl -fsSL --connect-timeout 20 --max-time 600 "
            f"    '{_RELEASE_URL}' -o /tmp/qaiappbuilder.zip && "
            f"  unzip -qo /tmp/qaiappbuilder.zip -d \"$_TMP\" && "
            f"  rm -f /tmp/qaiappbuilder.zip && "
            # If we got here, {_REMOTE_INSTALL_DIR}/start.sh does not exist (the
            # ``if`` above would have skipped otherwise) — but the directory
            # itself might, as a half-finished install left by a previous
            # interrupted run. `mv` onto an existing directory nests the source
            # inside it rather than replacing it, so clear it first.
            f"  rm -rf {_REMOTE_INSTALL_DIR} && "
            # Handle both flat (start.sh at root) and nested (single subdir) zip layouts
            f"  if [ -f \"$_TMP/start.sh\" ]; then "
            f"    mv \"$_TMP\" {_REMOTE_INSTALL_DIR}; "
            f"  else "
            f"    _SUB=$(find \"$_TMP\" -maxdepth 1 -mindepth 1 -type d | head -1) && "
            f"    mv \"$_SUB\" {_REMOTE_INSTALL_DIR} && "
            f"    rm -rf \"$_TMP\"; "
            f"  fi && "
            f"  echo 'Download and extract complete.'; "
            f"fi"
        )
        yield f"[cmd] {download_cmd}"
        async for line in _stream_step(
            self.executor,
            self.repository,
            remote,
            instance,
            download_cmd,
            timeout=660,
            failure_message=(
                "Download / extract of the release bundle failed. The board "
                "must be able to reach github.com."
            ),
        ):
            yield line

        # --- Step 3: setup — Python venv + deps (no frontend build needed,
        #     the release zip ships pre-built frontend/dist/) ---
        yield "[setup] Running setup.sh --no-frontend …"
        setup_cmd = (
            f"cd {_REMOTE_INSTALL_DIR} && bash setup.sh --no-frontend 2>&1"
        )
        yield f"[cmd] {setup_cmd}"
        async for line in _stream_step(
            self.executor,
            self.repository,
            remote,
            instance,
            setup_cmd,
            timeout=600,
            failure_message="setup.sh failed on the remote host.",
        ):
            yield line

        instance.state = DeploymentState.INSTALLED
        await self.repository.save(instance)
        yield f"[done] QAI AppBuilder installed at {_REMOTE_INSTALL_DIR} on {host}."


# ---------------------------------------------------------------------------
# StartRemote  (streaming)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StartRemoteUseCase:
    """Start the installed service and wait until it answers /health."""

    executor: SshExecutorPort
    repository: RemoteInstanceRepositoryPort

    async def stream(
        self,
        *,
        instance_id: str,
        host: str,
        ssh_port: int,
        username: str,
        auth_method: str,
        auth_ref: str,
        key_path: str = "",
        remote_port: int = _DEFAULT_REMOTE_PORT,
        enable_sso: bool = True,
    ):
        """Async generator — yields log lines during start."""
        _validate_host_params(host, ssh_port, username)
        _validate_remote_port(remote_port, enable_sso=enable_sso)
        remote = _build_remote_host(
            host, ssh_port, username, auth_method, auth_ref, key_path
        )

        instance = await self.repository.get(instance_id) or RemoteInstance(
            instance_id=instance_id,
            host=host,
            port=remote_port,
            username=username,
            state=DeploymentState.STARTING,
        )
        instance.host = host
        instance.port = remote_port
        instance.username = username
        instance.error_message = ""
        await self.repository.save(instance)

        # --- Step 1: is something already on the port? ---
        port_check_cmd = f"ss -tlnp 2>/dev/null | grep ':{remote_port} ' | head -1"
        yield f"[ssh] Checking port {remote_port} …"
        port_code, port_stdout, port_stderr = await self.executor.run_command(
            remote, port_check_cmd, timeout=15,
        )
        yield f"[ssh] port_check_exit={port_code}"
        if port_stderr.strip():
            yield f"[ssh] port_check_stderr={port_stderr.strip()}"
        if port_stdout.strip():
            yield f"[ssh] Port {remote_port} is already in use."
            yield f"[out] {port_stdout.strip()}"
            # Occupied is not the same as "our service is up". Probe the health
            # endpoint before claiming success — otherwise any unrelated process
            # on this port is reported as a working deploy and the operator gets
            # a URL that never loads.
            healthy = await self._is_healthy(remote, remote_port)
            if not healthy:
                instance.state = DeploymentState.FAILED
                instance.error_message = (
                    f"Port {remote_port} on {host} is held by something that is "
                    f"not QAI AppBuilder."
                )
                await self.repository.save(instance)
                yield "[start] ERROR: port is held by a foreign process."
                raise RemotePortInUseError(host, remote_port)
            instance.state = DeploymentState.RUNNING
            instance.remote_url = f"http://{host}:{remote_port}"
            await self.repository.save(instance)
            yield f"[done] QAI AppBuilder already running at http://{host}:{remote_port}"
            return

        # --- Step 2: start service ---
        instance.state = DeploymentState.STARTING
        await self.repository.save(instance)
        yield f"[start] Starting QAI AppBuilder on port {remote_port} …"

        # setsid + nohup: fully detach from the SSH session so the process
        # survives after the SSH channel closes (plain nohup is not enough on
        # some Linux distros where SIGHUP is still delivered on session exit).
        # Wrapped in bash -c to avoid /bin/sh redirect ambiguity.
        #
        # The env prefix supplies the three things the loopback OIDC flow needs
        # on a board, none of which hold by default there:
        #   * QAI_AUTH__ENABLED — ``_default_auth_enabled()`` returns False on
        #     headless Linux (no DISPLAY), i.e. exactly our target.
        #   * DISPLAY / WAYLAND_DISPLAY unset — otherwise ``start.sh`` spawns a
        #     browser ON THE BOARD that nobody is looking at.
        #   * QAI_AUTH__SESSION_COOKIE_NAME — cookies are scoped by host and
        #     ignore the port, so the board's session (reached through the
        #     tunnel at localhost:<port>) would otherwise overwrite the local
        #     instance's cookie and log the operator out of it, and vice versa.
        # The CSRF cookie name is deliberately NOT changed: the SPA hardcodes it
        # (frontend/src/api/csrf.ts) and double-submit tolerates a shared token.
        env_prefix = ""
        if enable_sso:
            env_prefix = (
                "env -u DISPLAY -u WAYLAND_DISPLAY "
                "QAI_AUTH__ENABLED=true "
                f"QAI_AUTH__SESSION_COOKIE_NAME=qai_session_{remote_port} "
            )
        # ``< /dev/null`` and the log redirect keep the detached service off the
        # SSH channel's fds. That is required but not enough on its own — see the
        # session-teardown note below.
        start_cmd = (
            f"bash -c 'cd {_REMOTE_INSTALL_DIR} && "
            f"setsid nohup {env_prefix}bash start.sh --port {remote_port} "
            f"< /dev/null > /tmp/qai_modelbuilder_{remote_port}.log 2>&1 "
            f"& echo PID:$!'"
        )
        # Redirecting all three standard fds away from the SSH channel is
        # necessary but NOT sufficient: some descendant of the detached service
        # keeps an fd inherited from the channel, so sshd never sends EOF and the
        # launch command's session refuses to close. Verified with plain
        # ``ssh host "...setsid nohup ... </dev/null >log 2>&1 & echo $!"``,
        # which hangs on the board while the service comes up fine — so this is
        # a remote-shell/sshd property, not a paramiko artefact.
        #
        # The pid is therefore ALSO written to a file: when the channel hangs its
        # stdout is lost, and without the file ``stop`` would degrade to a
        # port-scoped pkill.
        pid_file = f"/tmp/qai_modelbuilder_{remote_port}.pid"
        start_cmd = (
            f"bash -c 'cd {_REMOTE_INSTALL_DIR} && "
            f"setsid nohup {env_prefix}bash start.sh --port {remote_port} "
            f"< /dev/null > /tmp/qai_modelbuilder_{remote_port}.log 2>&1 "
            f"& _P=$!; echo $_P > {pid_file}; echo PID:$_P'"
        )
        yield f"[cmd] {start_cmd}"
        # The launch command's exit status is NOT the gate — the health probe
        # below is. Treating a hung channel as a failure aborted the SSE stream
        # *after* a successful start, so the tunnel was never opened and no
        # browser tab appeared while a perfectly healthy service ran on the
        # board. Swallow the read failure and let the probe decide.
        start_stdout = ""
        try:
            start_code, start_stdout, start_stderr = await self.executor.run_command(
                remote, start_cmd, timeout=20
            )
            yield f"[ssh] start_command_exit={start_code}"
            if start_stdout.strip():
                yield f"[out] {start_stdout.strip()}"
            if start_stderr.strip():
                yield f"[err] {start_stderr.strip()}"
        except Exception as exc:  # noqa: BLE001 — deliberate; see above
            yield (
                f"[ssh] launch command returned no exit status "
                f"({type(exc).__name__}: {exc}) — expected when the detached "
                f"service holds the SSH channel open. Probing health anyway."
            )

        pid = 0
        for token in start_stdout.split():
            if token.startswith("PID:"):
                try:
                    pid = int(token[4:])
                except ValueError:
                    pass

        # --- Step 3: wait for service to become ready (up to 90 s) ---
        yield "[start] Waiting for service to become ready …"
        # POSIX-compatible loop (no seq, no bash-specific syntax)
        ready_cmd = (
            f"i=0; while [ $i -lt 45 ]; do "
            f"  curl -sf http://localhost:{remote_port}/api/system/health > /dev/null 2>&1 && echo READY && break; "
            f"  sleep 2; i=$((i+1)); "
            f"done"
        )
        yield f"[cmd] {ready_cmd}"
        ready_code, ready_out, ready_err = await self.executor.run_command(
            remote, ready_cmd, timeout=100
        )
        yield f"[ssh] readiness_exit={ready_code}"
        yield f"[out] ready_check={ready_out.strip()!r}"
        if ready_err.strip():
            yield f"[err] {ready_err.strip()}"
        if "READY" not in ready_out:
            # Dump the service log plus process/socket diagnostics. This is
            # deliberately verbose: the deploy UI is the only place an
            # operator can see what happened on a headless SSH target.
            diag_cmd = (
                f"echo '--- service log ---'; "
                f"if [ -f /tmp/qai_modelbuilder_{remote_port}.log ]; then "
                f"  tail -80 /tmp/qai_modelbuilder_{remote_port}.log; "
                f"else echo '(no service log file)'; fi; "
                f"echo '--- process ---'; ps -ef | grep '[s]tart.sh\\|[q]ai' || true; "
                f"echo '--- listening socket ---'; "
                f"(ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null || true) | "
                f"grep ':{remote_port}' || true"
            )
            diag_code, diag_out, diag_err = await self.executor.run_command(
                remote, diag_cmd, timeout=20,
            )
            yield f"[ssh] diagnostics_exit={diag_code}"
            for log_line in diag_out.splitlines():
                yield f"[remote] {log_line}"
            for log_line in diag_err.splitlines():
                yield f"[remote-stderr] {log_line}"
            instance.state = DeploymentState.FAILED
            instance.error_message = "Service did not become ready within 90 s."
            await self.repository.save(instance)
            yield "[start] ERROR: service did not become ready in time."
            return

        if pid == 0:
            # stdout was lost with the hung channel — recover the pid the
            # launcher wrote to disk, so ``stop`` can kill this exact process
            # instead of falling back to a port-scoped pkill.
            _code, pid_out, _err = await self.executor.run_command(
                remote, f"cat {pid_file} 2>/dev/null || true", timeout=15
            )
            token = pid_out.strip()
            if token.isdigit():
                pid = int(token)
                yield f"[ssh] recovered pid {pid} from {pid_file}"

        instance.state = DeploymentState.RUNNING
        instance.remote_url = f"http://{host}:{remote_port}"
        instance.remote_pid = pid
        await self.repository.save(instance)
        yield f"[done] QAI AppBuilder running at http://{host}:{remote_port}"

    async def _is_healthy(self, remote: RemoteHost, remote_port: int) -> bool:
        """True iff ``/api/system/health`` answers on ``remote_port``."""
        _code, out, _err = await self.executor.run_command(
            remote,
            f"curl -sf http://localhost:{remote_port}/api/system/health "
            f"> /dev/null 2>&1 && echo HEALTHY",
            timeout=15,
        )
        return "HEALTHY" in out


# ---------------------------------------------------------------------------
# DeployRemote  (install + start, streaming)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class DeployRemoteResult:
    """Return value of :class:`DeployRemoteUseCase` (final summary)."""

    instance_id: str
    remote_url: str
    state: str  # DeploymentState value


@dataclass(slots=True)
class DeployRemoteUseCase:
    """Install then start QAI AppBuilder on the remote host.

    A thin composition of :class:`InstallRemoteUseCase` and
    :class:`StartRemoteUseCase`, kept so the original one-click
    ``POST /api/remote-deploy/deploy`` endpoint keeps working unchanged.

    Usage::

        async for line in use_case.stream(host=..., ...):
            # each line is a log string
        result = await use_case.execute(host=..., ...)   # final state
    """

    executor: SshExecutorPort
    repository: RemoteInstanceRepositoryPort

    async def execute(
        self,
        *,
        instance_id: str,
        host: str,
        ssh_port: int,
        username: str,
        auth_method: str,
        auth_ref: str,
        key_path: str = "",
        remote_port: int = _DEFAULT_REMOTE_PORT,
        enable_sso: bool = True,
    ) -> DeployRemoteResult:
        """Run the full deploy pipeline and return the final state."""
        async for _line in self.stream(
            instance_id=instance_id,
            host=host,
            ssh_port=ssh_port,
            username=username,
            auth_method=auth_method,
            auth_ref=auth_ref,
            key_path=key_path,
            remote_port=remote_port,
            enable_sso=enable_sso,
        ):
            pass  # drain the generator (side-effects update the instance)
        instance = await self.repository.get(instance_id)
        if instance is None:
            raise RemoteInstanceNotFoundError(instance_id)
        return DeployRemoteResult(
            instance_id=instance.instance_id,
            remote_url=instance.remote_url,
            state=instance.state.value,
        )

    async def stream(self, **kwargs):
        """Async generator — yields log lines for install followed by start."""
        install = InstallRemoteUseCase(
            executor=self.executor, repository=self.repository
        )
        start = StartRemoteUseCase(
            executor=self.executor, repository=self.repository
        )
        async for line in install.stream(**kwargs):
            yield line
        async for line in start.stream(**kwargs):
            yield line


# ---------------------------------------------------------------------------
# ListInstances
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ListInstancesResult:
    """Return value of :class:`ListInstancesUseCase`."""

    instances: list[RemoteInstance]


@dataclass(slots=True)
class ListInstancesUseCase:
    """Return all known remote instances."""

    repository: RemoteInstanceRepositoryPort

    async def execute(self) -> ListInstancesResult:
        instances = await self.repository.list_all()
        return ListInstancesResult(instances=instances)


# ---------------------------------------------------------------------------
# StopInstance
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StopInstanceResult:
    """Return value of :class:`StopInstanceUseCase`."""

    instance_id: str
    stopped: bool


@dataclass(slots=True)
class StopInstanceUseCase:
    """Stop a running remote QAI AppBuilder instance.

    Killing the remote process needs SSH credentials, which the instance
    entity deliberately does not retain. The caller therefore passes the
    :class:`RemoteHost` it still holds (the HTTP layer caches it for the
    lifetime of a deployment); without it this degrades to marking the record
    STOPPED locally and the remote process keeps running.
    """

    executor: SshExecutorPort
    repository: RemoteInstanceRepositoryPort

    async def execute(
        self, *, instance_id: str, remote: RemoteHost | None = None
    ) -> StopInstanceResult:
        instance = await self.repository.get(instance_id)
        if instance is None:
            raise RemoteInstanceNotFoundError(instance_id)

        was_live = instance.state in (
            DeploymentState.RUNNING,
            DeploymentState.STARTING,
        )
        stopped = True
        if was_live and remote is not None:
            # Prefer the PID captured at start. The fallback is scoped to this
            # service's own port so it can never take down an unrelated
            # process — a bare ``pkill -f start.sh`` would.
            #
            # Neither branch trusts the kill/pkill exit status — both routinely
            # exit 0 while the process lingers (slow shutdown, ignored SIGTERM).
            # So each polls for up to 5s, escalates to -9, and only reports
            # ``DEAD`` once a final check confirms the process is actually gone.
            # Without this, a failed stop looked identical to a successful one.
            if instance.remote_pid > 0:
                pid = instance.remote_pid
                command = (
                    f"kill {pid} 2>/dev/null; "
                    f"for i in 1 2 3 4 5; do kill -0 {pid} 2>/dev/null || break; sleep 1; done; "
                    f"kill -0 {pid} 2>/dev/null && kill -9 {pid} 2>/dev/null; sleep 1; "
                    f"kill -0 {pid} 2>/dev/null && echo STILL_ALIVE || echo DEAD"
                )
            else:
                port = instance.port
                command = (
                    f"pkill -f 'start.sh --port {port}' 2>/dev/null; sleep 1; "
                    f"pkill -9 -f 'start.sh --port {port}' 2>/dev/null; sleep 1; "
                    f"ps -ef | grep '[s]tart.sh --port {port}' >/dev/null 2>&1 "
                    f"&& echo STILL_ALIVE || echo DEAD"
                )
            _code, stdout, _stderr = await self.executor.run_command(
                remote, command, timeout=20
            )
            stopped = "DEAD" in stdout

        if stopped:
            instance.state = DeploymentState.STOPPED
            instance.remote_pid = 0
        else:
            instance.error_message = (
                "Could not confirm the remote process stopped; it may still "
                "be running."
            )
        await self.repository.save(instance)
        return StopInstanceResult(instance_id=instance_id, stopped=stopped)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _stream_step(
    executor: SshExecutorPort,
    repository: RemoteInstanceRepositoryPort,
    remote: RemoteHost,
    instance: RemoteInstance,
    command: str,
    *,
    timeout: int,
    failure_message: str,
):
    """Stream ``command``; on failure mark the instance FAILED and re-raise.

    The remote diagnostic is yielded into the log stream *before* the error
    propagates, so the deploy UI shows why the step failed rather than only
    that something did. Re-raising is deliberate: the caller must not fall
    through to the next step after a failed one.
    """
    try:
        async for line in executor.stream_command(remote, command, timeout=timeout):
            instance.append_log(line)
            yield f"[out] {line}"
    except Exception as exc:
        for log_line in str(exc).splitlines():
            instance.append_log(log_line)
            yield f"[err] {log_line}"
        instance.state = DeploymentState.FAILED
        instance.error_message = failure_message
        await repository.save(instance)
        raise


def _validate_host_params(host: str, ssh_port: int, username: str) -> None:
    if not host or not host.strip():
        raise RemoteHostValidationError("host must not be empty.")
    if not (1 <= ssh_port <= 65535):
        raise RemoteHostValidationError(
            f"ssh_port must be 1-65535, got {ssh_port}.",
            {"ssh_port": ssh_port},
        )
    if not username or not username.strip():
        raise RemoteHostValidationError("username must not be empty.")


def _validate_remote_port(remote_port: int, *, enable_sso: bool) -> None:
    """Reject application ports Okta will not accept as a redirect_uri.

    ``interfaces/http/routes/auth.py`` derives the OIDC ``redirect_uri`` from
    the port the service actually bound (``http://localhost:<port>/callback``)
    and Okta strict-matches the whole URI. A port outside
    ``factory/config/ports.json`` ``fallbacks`` therefore makes SSO login
    impossible — the operator would get a redirect_uri-mismatch error from
    Okta with nothing in our logs to explain it.

    Only enforced when SSO is requested: an ungated instance can bind anything.
    """
    if not enable_sso:
        return
    allowed = fallback_ports()
    if remote_port not in allowed:
        raise RemoteHostValidationError(
            f"remote_port must be one of {sorted(allowed)} when SSO is enabled "
            f"(got {remote_port}). Okta only accepts redirect_uris on its "
            f"registered loopback ports, so login would fail with a "
            f"redirect_uri mismatch. Disable SSO to use an arbitrary port.",
            {"remote_port": remote_port, "allowed": sorted(allowed)},
        )


def _build_remote_host(
    host: str,
    ssh_port: int,
    username: str,
    auth_method: str,
    auth_ref: str,
    key_path: str,
) -> RemoteHost:
    try:
        method = AuthMethod(auth_method)
    except ValueError:
        raise RemoteHostValidationError(
            f"auth_method must be 'password' or 'private_key', got {auth_method!r}.",
            {"auth_method": auth_method},
        )
    return RemoteHost(
        host=host,
        port=ssh_port,
        username=username,
        auth_method=method,
        auth_ref=auth_ref,
        key_path=key_path,
    )

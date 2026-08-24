# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for ``qai.remote_deploy``.

* :class:`ConnectRemoteUseCase`      — test SSH connectivity.
* :class:`DeployRemoteUseCase`       — clone repo + install + start service,
  streaming log lines via async generator.
* :class:`ListInstancesUseCase`      — list all known remote instances.
* :class:`StopInstanceUseCase`       — stop a running remote instance.
"""
from __future__ import annotations

from dataclasses import dataclass, field

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
_DEFAULT_REMOTE_PORT = 8989


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
# DeployRemote  (streaming — caller iterates the async generator)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class DeployRemoteResult:
    """Return value of :class:`DeployRemoteUseCase` (final summary)."""

    instance_id: str
    remote_url: str
    state: str  # DeploymentState value


@dataclass(slots=True)
class DeployRemoteUseCase:
    """Clone repo, run setup.sh, start QAI ModelBuilder on the remote host.

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
    ) -> DeployRemoteResult:
        """Run the full deploy pipeline and return the final state."""
        instance: RemoteInstance | None = None
        async for _line in self._deploy_gen(
            instance_id=instance_id,
            host=host,
            ssh_port=ssh_port,
            username=username,
            auth_method=auth_method,
            auth_ref=auth_ref,
            key_path=key_path,
            remote_port=remote_port,
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
    ):
        """Async generator — yields log lines during deploy."""
        async for line in self._deploy_gen(
            instance_id=instance_id,
            host=host,
            ssh_port=ssh_port,
            username=username,
            auth_method=auth_method,
            auth_ref=auth_ref,
            key_path=key_path,
            remote_port=remote_port,
        ):
            yield line

    async def _deploy_gen(
        self,
        *,
        instance_id: str,
        host: str,
        ssh_port: int,
        username: str,
        auth_method: str,
        auth_ref: str,
        key_path: str,
        remote_port: int,
    ):
        _validate_host_params(host, ssh_port, username)
        remote = _build_remote_host(
            host, ssh_port, username, auth_method, auth_ref, key_path
        )

        instance = RemoteInstance(
            instance_id=instance_id,
            host=host,
            port=remote_port,
            username=username,
            state=DeploymentState.CONNECTING,
        )
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

        # --- Step 2: check if port already in use ---
        _port_check_cmd = f"ss -tlnp 2>/dev/null | grep ':{remote_port} ' | head -1"
        yield f"[ssh] Checking port {remote_port} …"
        port_code, port_stdout, port_stderr = await self.executor.run_command(
            remote, _port_check_cmd, timeout=15,
        )
        yield f"[ssh] port_check_exit={port_code}"
        if port_stderr.strip():
            yield f"[ssh] port_check_stderr={port_stderr.strip()}"
        if port_stdout.strip():
            # Port already listening — service is already running, just open it
            yield f"[ssh] Port {remote_port} is already in use — service appears to be running."
            yield f"[out] {port_stdout.strip()}"
            instance.state = DeploymentState.RUNNING
            instance.remote_url = f"http://{host}:{remote_port}"
            await self.repository.save(instance)
            yield f"[done] QAI ModelBuilder already running at http://{host}:{remote_port}"
            return

        # --- Step 3: download and extract release zip (or skip if already installed) ---
        instance.state = DeploymentState.INSTALLING
        await self.repository.save(instance)
        yield "[install] Downloading QAI AppBuilder release …"
        download_cmd = (
            f"if [ -f {_REMOTE_INSTALL_DIR}/start.sh ]; then "
            f"  echo 'Already installed — skipping download.'; "
            f"else "
            f"  _TMP=/tmp/qaiappbuilder_extract_$$ && "
            f"  mkdir -p \"$_TMP\" && "
            f"  curl -fSL '{_RELEASE_URL}' -o /tmp/qaiappbuilder.zip && "
            f"  unzip -qo /tmp/qaiappbuilder.zip -d \"$_TMP\" && "
            f"  rm -f /tmp/qaiappbuilder.zip && "
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
        async for line in self.executor.stream_command(remote, download_cmd, timeout=300):
            instance.append_log(line)
            yield f"[out] {line}"

        # --- Step 4: setup — Python venv + deps (no frontend build needed,
        #     the release zip ships pre-built frontend/dist/) ---
        yield "[setup] Running setup.sh --no-frontend …"
        setup_cmd = (
            f"cd {_REMOTE_INSTALL_DIR} && bash setup.sh --no-frontend 2>&1"
        )
        yield f"[cmd] {setup_cmd}"
        async for line in self.executor.stream_command(remote, setup_cmd, timeout=600):
            instance.append_log(line)
            yield f"[out] {line}"

        # --- Step 6: start service ---
        instance.state = DeploymentState.STARTING
        await self.repository.save(instance)
        yield f"[start] Starting QAI ModelBuilder on port {remote_port} …"

        # setsid + nohup: fully detach from the SSH session so the process
        # survives after the SSH channel closes (plain nohup is not enough on
        # some Linux distros where SIGHUP is still delivered on session exit).
        # Wrapped in bash -c to avoid /bin/sh redirect ambiguity.
        start_cmd = (
            f"bash -c 'cd {_REMOTE_INSTALL_DIR} && "
            f"setsid nohup bash start.sh --port {remote_port} "
            f"> /tmp/qai_modelbuilder_{remote_port}.log 2>&1 & echo PID:$!'"
        )
        yield f"[cmd] {start_cmd}"
        start_code, start_stdout, start_stderr = await self.executor.run_command(
            remote, start_cmd, timeout=30
        )
        yield f"[ssh] start_command_exit={start_code}"
        if start_stdout.strip():
            yield f"[out] {start_stdout.strip()}"
        if start_stderr.strip():
            yield f"[err] {start_stderr.strip()}"

        pid = 0
        for token in start_stdout.split():
            if token.startswith("PID:"):
                try:
                    pid = int(token[4:])
                except ValueError:
                    pass

        # --- Step 7: wait for service to become ready (up to 90 s) ---
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

        instance.state = DeploymentState.RUNNING
        instance.remote_url = f"http://{host}:{remote_port}"
        instance.remote_pid = pid
        await self.repository.save(instance)
        yield f"[done] QAI ModelBuilder running at http://{host}:{remote_port}"


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
    """Stop a running remote QAI ModelBuilder instance.

    LIMITATION: This use case only marks the instance as STOPPED in the local
    repository.  It does NOT send a kill signal to the remote process because
    the auth credential (password / key passphrase) is not persisted — it is
    only held in memory during the original deploy call.

    TODO: To implement true remote kill, either:
      (a) accept auth params here and reconstruct a RemoteHost, or
      (b) store a session token / re-use a persistent SSH connection pool.
    Until then, the remote process continues running; the operator must kill
    it manually (e.g. `kill <remote_pid>` or `pkill -f start.sh`).
    """

    executor: SshExecutorPort
    repository: RemoteInstanceRepositoryPort

    async def execute(self, *, instance_id: str) -> StopInstanceResult:
        instance = await self.repository.get(instance_id)
        if instance is None:
            raise RemoteInstanceNotFoundError(instance_id)

        if instance.state not in (DeploymentState.RUNNING, DeploymentState.STARTING):
            instance.state = DeploymentState.STOPPED
            await self.repository.save(instance)
            return StopInstanceResult(instance_id=instance_id, stopped=True)

        # Mark stopped in repository (best-effort — auth is not available here
        # to send a live kill signal; see class docstring for the limitation).
        instance.state = DeploymentState.STOPPED
        await self.repository.save(instance)
        return StopInstanceResult(instance_id=instance_id, stopped=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_host_params(host: str, ssh_port: int, username: str) -> None:
    if not host or not host.strip():
        raise RemoteHostValidationError("host must not be empty.")
    if not (1 <= ssh_port <= 65535):
        raise RemoteHostValidationError(
            f"ssh_port must be 1-65535, got {ssh_port}.",
            details={"ssh_port": ssh_port},
        )
    if not username or not username.strip():
        raise RemoteHostValidationError("username must not be empty.")


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
            details={"auth_method": auth_method},
        )
    return RemoteHost(
        host=host,
        port=ssh_port,
        username=username,
        auth_method=method,
        auth_ref=auth_ref,
        key_path=key_path,
    )

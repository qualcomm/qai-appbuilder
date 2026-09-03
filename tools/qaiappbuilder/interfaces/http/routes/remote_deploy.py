# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP routes for ``remote_deploy`` — SSH-based remote QAI AppBuilder management.

Endpoints
---------
POST   /api/remote-deploy/connect                    Test SSH connectivity.
POST   /api/remote-deploy/install                    Download + setup.sh (SSE).
POST   /api/remote-deploy/start                      Start + open tunnel (SSE).
POST   /api/remote-deploy/deploy                     Install + start (SSE).
POST   /api/remote-deploy/instances/{id}/tunnel/start Open the local tunnel.
POST   /api/remote-deploy/instances/{id}/tunnel/stop  Close the local tunnel.
GET    /api/remote-deploy/instances                  List all known instances.
DELETE /api/remote-deploy/instances/{id}             Stop a remote instance.

``install`` and ``start`` are the granular pair; ``deploy`` runs both in one
call and is what the original one-click button uses.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, AsyncIterator

from qai.remote_deploy.domain import AuthMethod, RemoteHost, RemoteInstance
from qai.remote_deploy.domain.errors import RemoteInstanceNotFoundError

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    host: str
    ssh_port: int = Field(default=22, ge=1, le=65535)
    username: str
    auth_method: str = Field(default="password", pattern="^(password|private_key)$")
    auth_ref: str = Field(description="SecretStore key for password or key passphrase")
    key_path: str = Field(default="", description="Path to private key (private_key auth only)")


class ConnectResponse(BaseModel):
    success: bool
    host: str
    message: str


class DeployRequest(BaseModel):
    host: str
    ssh_port: int = Field(default=22, ge=1, le=65535)
    username: str
    auth_method: str = Field(default="password", pattern="^(password|private_key)$")
    auth_ref: str
    key_path: str = ""
    # 28688 (not 8989) so the local tunnel can bind the same number the board
    # listens on without colliding with the QAI instance serving this UI.
    # Both are Okta-registered loopback ports (factory/config/ports.json
    # ``fallbacks``); the use case rejects anything else when SSO is on.
    remote_port: int = Field(default=28688, ge=1, le=65535)
    enable_sso: bool = Field(
        default=True,
        description=(
            "Start the remote service with the Okta login gate on "
            "(QAI_AUTH__ENABLED=true) plus a port-scoped session cookie. "
            "Requires remote_port to be an Okta-registered loopback port."
        ),
    )


class InstanceInfo(BaseModel):
    instance_id: str
    host: str
    port: int
    username: str
    state: str
    remote_url: str
    error_message: str
    local_port: int = 0
    local_url: str = ""
    tunnel_state: str = "stopped"


class ListInstancesResponse(BaseModel):
    instances: list[InstanceInfo]


class StopInstanceResponse(BaseModel):
    instance_id: str
    stopped: bool


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def _remote_host_from_instance(instance: RemoteInstance) -> RemoteHost:
    """Rebuild connection metadata for a tunnel.

    The current instance entity predates tunnel persistence and intentionally
    does not retain credentials. Tunnel start therefore uses the same host
    credentials supplied by the active deploy request, cached by the executor.
    See the route-level deploy cache below.
    """
    cached = _DEPLOY_HOSTS.get(instance.instance_id)
    if cached is None:
        raise ValueError("SSH credentials for this instance are no longer available; redeploy it")
    return cached


def _instance_info(instance: RemoteInstance) -> InstanceInfo:
    # ``localhost`` rather than ``127.0.0.1`` is required, not cosmetic: the
    # board registers ``http://localhost:<port>/callback`` with Okta, so the
    # browser must sit on the localhost origin or the session cookie the
    # callback sets lands on a different origin than the user's tab.
    # See ``start.sh`` (auto-open helper) for the same reasoning locally.
    local_url = (
        f"http://localhost:{instance.local_port}/chat"
        if instance.local_port > 0
        else ""
    )
    return InstanceInfo(
        instance_id=instance.instance_id,
        host=instance.host,
        port=instance.port,
        username=instance.username,
        state=instance.state.value,
        remote_url=instance.remote_url,
        error_message=instance.error_message,
        local_port=instance.local_port,
        local_url=local_url,
        tunnel_state=instance.tunnel_state,
    )


_DEPLOY_HOSTS: dict[str, RemoteHost] = {}


def build_router(*, container: "Container") -> APIRouter:
    router = APIRouter(prefix="/api/remote-deploy", tags=["remote-deploy"])

    # -- shared plumbing for the three streaming endpoints ------------------

    def _resolve_instance_id(existing_instances, req: DeployRequest) -> str:
        """One remote host + application port maps to one deployment record.

        A second click on Install (or a later Start) must not spawn another UI
        chip for the same service.
        """
        existing = next(
            (
                item
                for item in existing_instances
                if item.host == req.host and item.port == req.remote_port
            ),
            None,
        )
        return existing.instance_id if existing is not None else str(uuid.uuid4())

    def _cache_host(instance_id: str, req: DeployRequest) -> None:
        _DEPLOY_HOSTS[instance_id] = RemoteHost(
            host=req.host,
            port=req.ssh_port,
            username=req.username,
            auth_method=AuthMethod(req.auth_method),
            auth_ref=req.auth_ref,
            key_path=req.key_path,
        )

    def _use_case_kwargs(instance_id: str, req: DeployRequest) -> dict:
        return dict(
            instance_id=instance_id,
            host=req.host,
            ssh_port=req.ssh_port,
            username=req.username,
            auth_method=req.auth_method,
            auth_ref=req.auth_ref,
            key_path=req.key_path,
            remote_port=req.remote_port,
            enable_sso=req.enable_sso,
        )

    def _sse_response(stream_factory) -> StreamingResponse:
        return StreamingResponse(
            stream_factory(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def _progress(lines, steps: list[str]) -> AsyncIterator[bytes]:
        """Wrap a use-case line stream in ``event: progress`` frames.

        Progress is a monotonic best-effort estimate derived from the step
        keywords the use case emits; it is presentation only and never gates
        control flow.
        """
        step_idx = 0
        async for line in lines:
            for i, keyword in enumerate(steps):
                if keyword in line.lower():
                    step_idx = max(step_idx, i)
            percent = min(95, int(step_idx / len(steps) * 100))
            payload = json.dumps({"line": line, "percent": percent})
            yield f"event: progress\ndata: {payload}\n\n".encode()

    def _error_frame(exc: BaseException) -> bytes:
        payload = json.dumps({
            "type": type(exc).__name__,
            "code": getattr(exc, "default_code", "remote_deploy.unknown_error"),
            "message": str(exc),
        })
        return f"event: error\ndata: {payload}\n\n".encode()

    async def _finish_running(instance_id: str) -> bytes:
        """Open the tunnel for a RUNNING instance and build its done frame.

        The local port MUST equal the remote port. The board derives its OIDC
        redirect_uri from its own bound port (routes/auth.py), and Okta sends
        the browser to ``http://localhost:<that port>/callback`` — which only
        reaches the board if our listener owns the same number locally. A
        mismatched local port sends the callback to whatever runs on it here
        (usually this very instance) and SSO fails with an unknown-state error.
        """
        instance = await container.remote_deploy.repository.get(instance_id)
        if instance is None or not instance.is_running:
            message = instance.error_message if instance else "Deploy failed."
            payload = json.dumps({
                "type": "InfrastructureError",
                "code": "remote_deploy.deploy_failed",
                "message": message or "Deploy failed.",
            })
            return f"event: error\ndata: {payload}\n\n".encode()

        if instance.local_port <= 0:
            instance.local_port = instance.port
        await container.remote_deploy.tunnel_manager.start_tunnel(
            instance_id,
            _DEPLOY_HOSTS[instance_id],
            local_port=instance.local_port,
            remote_port=instance.port,
        )
        instance.tunnel_state = "running"
        await container.remote_deploy.repository.save(instance)
        payload = json.dumps({
            "instance_id": instance_id,
            "remote_url": instance.remote_url,
            "local_url": f"http://localhost:{instance.local_port}/chat",
            "local_port": instance.local_port,
            "tunnel_state": instance.tunnel_state,
            "state": instance.state.value,
        })
        return f"event: done\ndata: {payload}\n\n".encode()

    # -- endpoints ---------------------------------------------------------

    @router.post("/connect", response_model=ConnectResponse)
    async def connect(req: ConnectRequest) -> ConnectResponse:
        """Test SSH connectivity to a remote host."""
        result = await container.remote_deploy.connect_use_case.execute(
            host=req.host,
            ssh_port=req.ssh_port,
            username=req.username,
            auth_method=req.auth_method,
            auth_ref=req.auth_ref,
            key_path=req.key_path,
        )
        return ConnectResponse(
            success=result.success,
            host=result.host,
            message=result.message,
        )

    @router.post("/install")
    async def install(req: DeployRequest) -> StreamingResponse:
        """Download the release bundle and run setup.sh on a remote host.

        Nothing is started — the instance ends in ``installed``. SSE wire
        format matches ``/deploy``; the final frame is
        ``event: done data: {"instance_id": ..., "state": "installed"}``.
        """
        instance_id = _resolve_instance_id(
            await container.remote_deploy.repository.list_all(), req
        )
        _cache_host(instance_id, req)

        async def _stream() -> AsyncIterator[bytes]:
            try:
                async for frame in _progress(
                    container.remote_deploy.install_use_case.stream(
                        **_use_case_kwargs(instance_id, req)
                    ),
                    ["connecting", "downloading", "install", "setup", "done"],
                ):
                    yield frame
                instance = await container.remote_deploy.repository.get(instance_id)
                payload = json.dumps({
                    "instance_id": instance_id,
                    "state": instance.state.value if instance else "failed",
                })
                yield f"event: done\ndata: {payload}\n\n".encode()
            except Exception as exc:
                yield _error_frame(exc)

        return _sse_response(_stream)

    @router.post("/start")
    async def start(req: DeployRequest) -> StreamingResponse:
        """Start an already-installed remote service and open its tunnel."""
        instance_id = _resolve_instance_id(
            await container.remote_deploy.repository.list_all(), req
        )
        _cache_host(instance_id, req)

        async def _stream() -> AsyncIterator[bytes]:
            try:
                async for frame in _progress(
                    container.remote_deploy.start_use_case.stream(
                        **_use_case_kwargs(instance_id, req)
                    ),
                    ["checking port", "starting", "waiting", "done"],
                ):
                    yield frame
                yield await _finish_running(instance_id)
            except Exception as exc:
                yield _error_frame(exc)

        return _sse_response(_stream)

    @router.post("/deploy")
    async def deploy(req: DeployRequest) -> StreamingResponse:
        """Install and start QAI AppBuilder on a remote host (one shot).

        Returns an SSE stream of log lines followed by a final
        ``event: done`` or ``event: error`` frame.

        Wire format (QAI-native SSE, api-contract §3):
          event: progress
          data: {"line": "...", "percent": N}

          event: done
          data: {"instance_id": "...", "remote_url": "..."}
        """
        instance_id = _resolve_instance_id(
            await container.remote_deploy.repository.list_all(), req
        )
        _cache_host(instance_id, req)

        async def _stream() -> AsyncIterator[bytes]:
            try:
                async for frame in _progress(
                    container.remote_deploy.deploy_use_case.stream(
                        **_use_case_kwargs(instance_id, req)
                    ),
                    [
                        "connecting", "checking port", "downloading",
                        "install", "setup", "starting", "waiting", "done",
                    ],
                ):
                    yield frame
                yield await _finish_running(instance_id)
            except Exception as exc:
                yield _error_frame(exc)

        return _sse_response(_stream)

    @router.post("/instances/{instance_id}/tunnel/start", response_model=InstanceInfo)
    async def start_tunnel(instance_id: str) -> InstanceInfo:
        instance = await container.remote_deploy.repository.get(instance_id)
        if instance is None:
            raise RemoteInstanceNotFoundError(instance_id)
        # Local port mirrors the remote port — see the deploy path for why.
        if instance.local_port <= 0:
            instance.local_port = instance.port
        remote = _remote_host_from_instance(instance)
        await container.remote_deploy.tunnel_manager.start_tunnel(
            instance_id,
            remote,
            local_port=instance.local_port,
            remote_port=instance.port,
        )
        instance.tunnel_state = "running"
        await container.remote_deploy.repository.save(instance)
        return _instance_info(instance)

    @router.post("/instances/{instance_id}/tunnel/stop", response_model=InstanceInfo)
    async def stop_tunnel(instance_id: str) -> InstanceInfo:
        instance = await container.remote_deploy.repository.get(instance_id)
        if instance is None:
            raise RemoteInstanceNotFoundError(instance_id)
        await container.remote_deploy.tunnel_manager.stop_tunnel(instance_id)
        instance.tunnel_state = "stopped"
        await container.remote_deploy.repository.save(instance)
        return _instance_info(instance)

    @router.get("/instances", response_model=ListInstancesResponse)
    async def list_instances() -> ListInstancesResponse:
        """Return all known remote instances."""
        result = await container.remote_deploy.list_instances_use_case.execute()
        # Collapse legacy duplicate records created by repeated deploys before
        # host/port reuse was added. One remote host + application port maps to
        # one UI chip; keep the newest record in repository order.
        unique: dict[tuple[str, int], object] = {}
        for inst in result.instances:
            unique[(inst.host, inst.port)] = inst
        return ListInstancesResponse(
            instances=[_instance_info(inst) for inst in unique.values()]
        )

    @router.delete("/instances/{instance_id}", response_model=StopInstanceResponse)
    async def stop_instance(instance_id: str) -> StopInstanceResponse:
        """Stop a remote instance and close its local SSH tunnel."""
        await container.remote_deploy.tunnel_manager.stop_tunnel(instance_id)
        instance = await container.remote_deploy.repository.get(instance_id)
        if instance is not None:
            instance.tunnel_state = "stopped"
            await container.remote_deploy.repository.save(instance)
        # Killing the detached remote process needs SSH credentials, which the
        # instance entity deliberately does not retain — hand the use case the
        # cached RemoteHost so the kill logic lives in the application layer.
        result = await container.remote_deploy.stop_instance_use_case.execute(
            instance_id=instance_id,
            remote=_DEPLOY_HOSTS.get(instance_id),
        )
        # Drop the cached credentials for this instance — they are no
        # longer needed once the remote process and tunnel are stopped,
        # and leaving them would grow _DEPLOY_HOSTS unboundedly across
        # repeated deploy/stop cycles. Only on confirmed success: if the
        # remote process could not be confirmed stopped, a retry needs the
        # same credentials — dropping them here would strand it with no way
        # to try again short of a full redeploy.
        if result.stopped:
            _DEPLOY_HOSTS.pop(instance_id, None)
        return StopInstanceResponse(
            instance_id=result.instance_id,
            stopped=result.stopped,
        )

    return router

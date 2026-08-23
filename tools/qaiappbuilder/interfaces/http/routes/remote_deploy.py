# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP routes for ``remote_deploy`` — SSH-based remote QAI ModelBuilder management.

Endpoints
---------
POST   /api/remote-deploy/connect          Test SSH connectivity.
POST   /api/remote-deploy/deploy           Install + start (SSE progress stream).
GET    /api/remote-deploy/instances        List all known remote instances.
DELETE /api/remote-deploy/instances/{id}   Stop a remote instance.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, AsyncIterator

from qai.remote_deploy.domain import AuthMethod, RemoteHost, RemoteInstance

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
    remote_port: int = Field(default=8989, ge=1, le=65535)


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
    local_url = (
        f"http://127.0.0.1:{instance.local_port}/chat"
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

    @router.post("/deploy")
    async def deploy(req: DeployRequest) -> StreamingResponse:
        """Install and start QAI ModelBuilder on a remote host.

        Returns an SSE stream of log lines followed by a final
        ``event: done`` or ``event: error`` frame.

        Wire format (QAI-native SSE, api-contract §3):
          event: progress
          data: {"line": "...", "percent": N}

          event: done
          data: {"instance_id": "...", "remote_url": "..."}
        """
        # Reuse the existing record for the same remote host/port. A second
        # click on Install & Start must not create another UI chip for the
        # same QAI service (the remote port is the identity of a deployment).
        existing_instances = await container.remote_deploy.repository.list_all()
        existing = next(
            (
                item
                for item in existing_instances
                if item.host == req.host and item.port == req.remote_port
            ),
            None,
        )
        instance_id = existing.instance_id if existing is not None else str(uuid.uuid4())
        _DEPLOY_HOSTS[instance_id] = RemoteHost(
            host=req.host,
            port=req.ssh_port,
            username=req.username,
            auth_method=AuthMethod(req.auth_method),
            auth_ref=req.auth_ref,
            key_path=req.key_path,
        )

        async def _event_stream() -> AsyncIterator[bytes]:
            percent = 0
            steps = [
                "connecting", "checking port", "cloning", "installing",
                "starting", "waiting", "done",
            ]
            step_idx = 0
            try:
                async for line in container.remote_deploy.deploy_use_case.stream(
                    instance_id=instance_id,
                    host=req.host,
                    ssh_port=req.ssh_port,
                    username=req.username,
                    auth_method=req.auth_method,
                    auth_ref=req.auth_ref,
                    key_path=req.key_path,
                    remote_port=req.remote_port,
                ):
                    # Advance progress heuristically based on log line prefixes
                    for i, keyword in enumerate(steps):
                        if keyword in line.lower():
                            step_idx = max(step_idx, i)
                    percent = min(95, int(step_idx / len(steps) * 100))
                    payload = json.dumps({"line": line, "percent": percent})
                    yield f"event: progress\ndata: {payload}\n\n".encode()

                # Fetch final state
                instance = await container.remote_deploy.repository.get(instance_id)
                if instance is not None and instance.is_running:
                    # Deploy success automatically establishes the local
                    # Paramiko TCP tunnel before notifying the browser.
                    if instance.local_port <= 0:
                        used = {
                            item.local_port
                            for item in await container.remote_deploy.repository.list_all()
                            if item.local_port > 0 and item.instance_id != instance_id
                        }
                        instance.local_port = next(
                            port for port in range(8990, 8990 + 100)
                            if port not in used
                        )
                    await container.remote_deploy.tunnel_manager.start_tunnel(
                        instance_id,
                        _DEPLOY_HOSTS[instance_id],
                        local_port=instance.local_port,
                        remote_port=instance.port,
                    )
                    instance.tunnel_state = "running"
                    await container.remote_deploy.repository.save(instance)
                    done_payload = json.dumps({
                        "instance_id": instance_id,
                        "remote_url": instance.remote_url,
                        "local_url": f"http://127.0.0.1:{instance.local_port}/chat",
                        "local_port": instance.local_port,
                        "tunnel_state": instance.tunnel_state,
                        "state": instance.state.value,
                    })
                    yield f"event: done\ndata: {done_payload}\n\n".encode()
                else:
                    err_msg = instance.error_message if instance else "Deploy failed."
                    err_payload = json.dumps({
                        "type": "InfrastructureError",
                        "code": "remote_deploy.deploy_failed",
                        "message": err_msg,
                    })
                    yield f"event: error\ndata: {err_payload}\n\n".encode()

            except Exception as exc:
                err_payload = json.dumps({
                    "type": type(exc).__name__,
                    "code": getattr(exc, "default_code", "remote_deploy.unknown_error"),
                    "message": str(exc),
                })
                yield f"event: error\ndata: {err_payload}\n\n".encode()

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/instances/{instance_id}/tunnel/start", response_model=InstanceInfo)
    async def start_tunnel(instance_id: str) -> InstanceInfo:
        instance = await container.remote_deploy.repository.get(instance_id)
        if instance is None:
            raise ValueError(f"Remote instance {instance_id} not found")
        if instance.local_port <= 0:
            used = {
                item.local_port
                for item in await container.remote_deploy.repository.list_all()
                if item.local_port > 0
            }
            instance.local_port = next(
                port for port in range(8990, 8990 + 100) if port not in used
            )
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
            raise ValueError(f"Remote instance {instance_id} not found")
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
            # Stop the detached remote QAI process as well as the local tunnel.
            # The PID is captured during deploy; the fallback only targets the
            # service's own port and never uses a broad process kill.
            remote = _DEPLOY_HOSTS.get(instance_id)
            if remote is not None:
                if instance.remote_pid > 0:
                    await container.remote_deploy.executor.run_command(
                        remote,
                        f"kill {instance.remote_pid} 2>/dev/null || true",
                        timeout=10,
                    )
                else:
                    await container.remote_deploy.executor.run_command(
                        remote,
                        f"pkill -f 'start.sh --port {instance.port}' 2>/dev/null || true",
                        timeout=10,
                    )
            instance.tunnel_state = "stopped"
            await container.remote_deploy.repository.save(instance)
        # Drop the cached credentials for this instance — they are no
        # longer needed once the remote process and tunnel are stopped,
        # and leaving them would grow _DEPLOY_HOSTS unboundedly across
        # repeated deploy/stop cycles.
        _DEPLOY_HOSTS.pop(instance_id, None)
        result = await container.remote_deploy.stop_instance_use_case.execute(
            instance_id=instance_id
        )
        return StopInstanceResponse(
            instance_id=result.instance_id,
            stopped=result.stopped,
        )

    return router

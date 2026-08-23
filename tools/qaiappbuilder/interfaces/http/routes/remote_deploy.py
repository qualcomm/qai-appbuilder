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


class ListInstancesResponse(BaseModel):
    instances: list[InstanceInfo]


class StopInstanceResponse(BaseModel):
    instance_id: str
    stopped: bool


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

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
        instance_id = str(uuid.uuid4())

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
                    done_payload = json.dumps({
                        "instance_id": instance_id,
                        "remote_url": instance.remote_url,
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

    @router.get("/instances", response_model=ListInstancesResponse)
    async def list_instances() -> ListInstancesResponse:
        """Return all known remote instances."""
        result = await container.remote_deploy.list_instances_use_case.execute()
        return ListInstancesResponse(
            instances=[
                InstanceInfo(
                    instance_id=inst.instance_id,
                    host=inst.host,
                    port=inst.port,
                    username=inst.username,
                    state=inst.state.value,
                    remote_url=inst.remote_url,
                    error_message=inst.error_message,
                )
                for inst in result.instances
            ]
        )

    @router.delete("/instances/{instance_id}", response_model=StopInstanceResponse)
    async def stop_instance(instance_id: str) -> StopInstanceResponse:
        """Stop a remote QAI ModelBuilder instance."""
        result = await container.remote_deploy.stop_instance_use_case.execute(
            instance_id=instance_id
        )
        return StopInstanceResponse(
            instance_id=result.instance_id,
            stopped=result.stopped,
        )

    return router

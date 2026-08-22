# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP routes for the background_process platform module.

Routes (6; intentionally NO ``POST /start``):

* ``GET    /api/background_process``                           -> ``list[Info]``
* ``GET    /api/background_process/{process_id}``              -> ``Info`` | 404
* ``GET    /api/background_process/{process_id}/logs``         -> ``Logs`` | 404
* ``POST   /api/background_process/{process_id}/stop``         -> ``Info`` | 404
* ``POST   /api/background_process/{process_id}/restart``      -> ``Info`` | 404
* ``POST   /api/background_process/session/{session_id}/stop`` -> ``{"ok": true}``

The omission of ``POST /start`` is deliberate: spawning a background process
must go through the LLM tool layer (which honours the permission-ask + audit
pipeline). Exposing ``start`` on plain HTTP would let any local client bypass
that gate (``design.md`` section 9).

Live event push for the front-end is served by the existing global SSE
multiplexer at ``GET /api/events``; the manager publishes
``BackgroundProcessUpdated`` / ``BackgroundProcessDeleted`` envelopes onto
the shared :class:`qai.platform.events.EventBus`, so no per-resource SSE is
added here.

Path prefix uses an underscore (``background_process``) to align with the
Python module name; this is a fresh ``/api`` surface added in v2 and is not
constrained by AGENTS.md section 3.1 (no pre-existing route to keep stable).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container
    from qai.platform.background_process.ports import Info, Logs

__all__ = ["build_router"]


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _info_to_dict(info: "Info") -> dict[str, Any]:
    """Serialise an :class:`Info` value object to a JSON-safe dict.

    ``Info`` is a frozen dataclass with a nested ``Time`` dataclass, so a
    straight :func:`dataclasses.asdict` produces the right wire shape.
    """
    return dataclasses.asdict(info)


def _logs_to_dict(logs: "Logs") -> dict[str, Any]:
    return dataclasses.asdict(logs)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the ``background_process`` router bound to ``container``.

    The router holds a reference to the shared manager via the container so
    every request reads the same in-memory state (the manager is one-per-
    daemon; see ``manager.py`` docstring).
    """
    router = APIRouter(prefix="/api/background_process", tags=["background_process"])
    manager = container.background_process.manager

    @router.get("", response_model=None)
    async def list_processes() -> list[dict[str, Any]]:
        """Return a snapshot of every tracked background process.

        The platform port also accepts a ``session_id`` filter, but the
        HTTP surface intentionally exposes the global list only: filtering
        is a TUI / LLM-tool concern, and the route's primary consumer is
        the operator-facing sidebar.
        """
        items = await manager.list()
        return [_info_to_dict(info) for info in items]

    @router.get("/{process_id}", response_model=None)
    async def get_process(process_id: str) -> dict[str, Any]:
        info = await manager.get(process_id)
        if info is None:
            raise HTTPException(
                status_code=404,
                detail=f"Background process not found: {process_id}",
            )
        return _info_to_dict(info)

    @router.get("/{process_id}/logs", response_model=None)
    async def get_logs(process_id: str) -> dict[str, Any]:
        logs = await manager.logs(process_id)
        if logs is None:
            raise HTTPException(
                status_code=404,
                detail=f"Background process not found: {process_id}",
            )
        return _logs_to_dict(logs)

    @router.post("/{process_id}/stop", response_model=None)
    async def stop_process(process_id: str) -> dict[str, Any]:
        info = await manager.stop(process_id)
        if info is None:
            raise HTTPException(
                status_code=404,
                detail=f"Background process not found: {process_id}",
            )
        return _info_to_dict(info)

    @router.post("/{process_id}/restart", response_model=None)
    async def restart_process(process_id: str) -> dict[str, Any]:
        info = await manager.restart(process_id)
        if info is None:
            raise HTTPException(
                status_code=404,
                detail=f"Background process not found: {process_id}",
            )
        return _info_to_dict(info)

    @router.post("/session/{session_id}/stop", response_model=None)
    async def stop_session(session_id: str) -> dict[str, bool]:
        """Terminate every process belonging to ``session_id``.

        Idempotent: an unknown session yields ``{"ok": true}`` (no-op).
        """
        await manager.stop_session(session_id)
        return {"ok": True}

    return router

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Model runtime HTTP routes.

Exposes the application use cases for the ``model_runtime`` bounded
context under ``/api/service``. Behaviour is aligned with the v1
GenieAPIService control surface (the validated source of truth).

Routes
------
- ``POST /api/service/open-dir``    — open install directory in OS explorer
- ``POST /api/service/load-model``  — load/switch a model
- ``GET  /api/service/probe``       — health probe (optional host/port)
- ``GET  /api/service/status``      — detailed daemon status (+ path_warning)
- ``POST /api/service/start``       — start the inference daemon
- ``POST /api/service/stop``        — stop the inference daemon
- ``GET  /api/service/logs``        — SSE stream of daemon log lines
- ``POST /api/service/logs/clear``  — clear the log buffer (returns skip_from)
- ``GET  /api/service/models``      — list available models on disk
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---------------------------------------------------------------------------
# Request / Response DTOs
# ---------------------------------------------------------------------------


class LoadModelRequest(BaseModel):
    """Body for ``POST /api/service/load-model``."""

    model_name: str = Field(..., min_length=1, max_length=255)


class StartServiceRequest(BaseModel):
    """Body for ``POST /api/service/start``.

    Accepts the v1 ``svcParams`` superset: only ``model_name`` and ``port``
    drive the start path today, but the extra optional fields are accepted
    (and ignored) so the frontend can POST its full launch-parameter object
    without a 422. New fields are additive / optional — backward compatible
    with callers that only send ``{model_name, port}``.
    """

    model_config = {"extra": "ignore"}

    model_name: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    loglevel: int | None = Field(default=None, ge=1, le=5)
    host_mode: str | None = Field(default=None, max_length=16)
    load_model: bool | None = None


class StopServiceRequest(BaseModel):
    """Body for ``POST /api/service/stop`` (v1 sends ``{force}``)."""

    model_config = {"extra": "ignore"}

    force: bool = False


class SuccessResponse(BaseModel):
    """Generic success envelope."""

    success: bool = True


class StatusResponse(BaseModel):
    """Wire form of ``/api/service/status`` response."""

    status: str


class LoadModelResponse(BaseModel):
    """Wire form of ``/api/service/load-model`` response."""

    status: str
    model: str


class ProbeResponse(BaseModel):
    """Wire form of ``/api/service/probe`` response.

    ``reachable`` is the v1 field consumed by the Connection panel;
    ``alive`` / ``model`` are kept for callers probing the local daemon.
    """

    reachable: bool
    alive: bool
    model: str | None


class ClearLogsResponse(BaseModel):
    """Wire form of ``/api/service/logs/clear`` response."""

    status: str
    skip_from: int


class ModelsResponse(BaseModel):
    """Wire form of ``/api/service/models`` response."""

    models: list[dict[str, Any]]
    models_root_path: str = ""


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the model_runtime router bound to the given DI container.

    The router holds no module-level state; it is reconstructed every
    time the app is created.
    """
    router = APIRouter(prefix="/api/service", tags=["model_runtime"])

    @router.post("/open-dir", response_model=SuccessResponse)
    async def open_dir() -> SuccessResponse:
        # Resolve the directory; actual OS open delegated to the route
        # since subprocess calls are infrastructure concerns. The stub
        # simply returns success without opening anything.
        await container.model_runtime.open_service_dir_use_case.execute()
        return SuccessResponse(success=True)

    @router.post("/load-model", response_model=LoadModelResponse)
    async def load_model(body: LoadModelRequest) -> LoadModelResponse:
        result = await container.model_runtime.load_model_use_case.execute(
            body.model_name
        )
        return LoadModelResponse(status=result["status"], model=result["model"])

    @router.get("/probe", response_model=ProbeResponse)
    async def probe(
        host: str | None = Query(default=None),
        port: int | None = Query(default=None, ge=1, le=65535),
    ) -> ProbeResponse:
        # v1: GET /api/service/probe?host=&port= probes an arbitrary daemon
        # address (Connection-panel "Test"), bypassing browser CORS. With
        # neither argument we probe the locally-managed daemon.
        result = await container.model_runtime.probe_service_use_case.execute(
            host=host, port=port
        )
        return ProbeResponse(
            reachable=bool(result.get("reachable", result.get("alive", False))),
            alive=bool(result.get("alive", False)),
            model=result.get("model"),
        )

    @router.get("/status")
    async def get_status() -> dict[str, Any]:
        # The use case augments the adapter status with the V1
        # ``path_warning`` field (unsafe exe / models-root path detection);
        # the route only forwards the result onto the wire.
        return await container.model_runtime.get_status_use_case.execute()

    @router.post("/start", response_model=StatusResponse)
    async def start_service(body: StartServiceRequest | None = None) -> StatusResponse:
        model_name = body.model_name if body else None
        port = body.port if body else None
        loglevel = body.loglevel if body else None
        # When the caller does not pin a port, honour the user-configured
        # default from forge.config ``service_launch.local_port`` (the
        # value the ServiceConfigPanel persists). Falls back to the typed
        # Settings.default_port inside the adapter when no override is
        # present or the document is unreadable. Reading through the
        # apps/api container's user_prefs namespace keeps this within the
        # interfaces layer (no cross-context ``import qai.user_prefs``).
        if port is None:
            port = await _forge_config_local_port()
        # 8-M3 — apply the user-configured log buffer size before starting
        # the daemon (V1 ``main.py:5379-5380`` / ``service_manager.py``
        # parity: V1 reads ``service_launch.service_log_buffer_size``
        # (default 6000) and calls ``set_buffer_size`` before each start).
        # V2 keeps the buffer size hot-adjustable (the service rebuilds its
        # live deque preserving existing lines), so applying it here means a
        # config change takes effect on the next Start without an API
        # restart. Best-effort: a missing namespace / value leaves the
        # DI-constructed default in place.
        await _apply_log_buffer_size()
        # ``loglevel`` is forwarded to the V1 ``-d`` CLI flag inside the
        # adapter; passing ``None`` lets the adapter read forge.config via
        # its injected provider (``service_launch.loglevel``) and fall back
        # to V1 default ``3``. Aligns with V1 ``main.py:5266``.
        # Single-instance guard: the adapter raises ServicePortInUseError
        # (a ConflictError → 409) when the target port is already occupied.
        # We let it propagate; the unified QaiError handler maps it to the
        # standard 409 envelope (code="model_runtime.service_port_in_use",
        # details.port=N) which the Service page surfaces as a friendly toast.
        result = await container.model_runtime.start_service_use_case.execute(
            model_name=model_name, port=port, loglevel=loglevel
        )
        return StatusResponse(status=result["status"])

    async def _apply_log_buffer_size() -> None:
        """Push the configured log buffer size onto the inference service.

        8-M3 — reads ``service_launch.service_log_buffer_size`` from
        forge.config (V1 default 6000); falls back to the typed
        ``Settings.service.log_buffer_size`` when forge.config has no
        override. Any failure (missing service / method / malformed value)
        is swallowed: the buffer size is a tunable, never a hard dependency
        of the start path.
        """
        service = getattr(
            getattr(container, "model_runtime", None), "inference_service", None
        )
        if service is None or not hasattr(service, "set_buffer_size"):
            return
        try:
            launch = await _forge_config_service_launch()
            raw = launch.get("service_log_buffer_size")
            if raw is None:
                raw = getattr(
                    getattr(getattr(container, "settings", None), "service", None),
                    "log_buffer_size",
                    None,
                )
            if raw is None:
                return
            size = int(raw)
            if size <= 0:
                return
            service.set_buffer_size(size)
        except (ValueError, TypeError, AttributeError, KeyError):
            return

    async def _forge_config_service_launch() -> dict[str, Any]:
        """Return forge.config ``service_launch`` dict, or empty on failure."""
        user_prefs = getattr(container, "user_prefs", None)
        load_uc = getattr(user_prefs, "load_document_use_case", None)
        if load_uc is None:
            return {}
        try:
            doc = await load_uc.execute("forge.config")
            service_launch = doc.get("service_launch", {})
            if isinstance(service_launch, dict):
                return service_launch
        except (ValueError, TypeError, AttributeError, KeyError):
            return {}
        return {}

    async def _forge_config_local_port() -> int | None:
        """Read ``service_launch.local_port`` from forge.config, or None.

        Any failure (missing namespace, malformed doc, non-int value) is
        swallowed so the inference daemon still starts on the typed
        Settings default — the override is a convenience, never a hard
        dependency of the start path.
        """
        service_launch = await _forge_config_service_launch()
        raw = service_launch.get("local_port")
        if raw is None:
            return None
        try:
            value = int(raw)
        except (ValueError, TypeError):
            return None
        if value < 1 or value > 65535:
            return None
        return value

    async def _forge_config_models_root() -> str:
        """Return the active models-scan root for ``GET /api/service/models``.

        Design simplification (user mandate 2026-06-07): the models root is
        **fixed** to the default ``<data>/models`` and is no longer a
        user-settable path (the Service Config "models directory" input has
        been removed from the UI). This helper therefore always returns ``""``
        so :class:`ListModelsUseCase` falls back to the adapter's static
        default models root, ignoring any stale persisted
        ``service_launch.models_root_path`` from before the simplification.

        ``ModelsResponse.models_root_path`` (the wire field, §3.1) is still
        emitted — it now reflects the resolved default rather than a
        user-configured override, so the response shape is unchanged.
        """
        return ""

    @router.post("/stop", response_model=StatusResponse)
    async def stop_service(body: StopServiceRequest | None = None) -> StatusResponse:
        result = await container.model_runtime.stop_service_use_case.execute()
        return StatusResponse(status=result["status"])

    @router.get("/logs")
    async def get_logs(skip: int = Query(default=0, ge=0)) -> StreamingResponse:
        # v1: SSE stream of daemon log lines. Buffered lines (sequence >=
        # skip) are emitted first, then live lines until the daemon exits;
        # the stream terminates with a ``[DONE]`` sentinel.
        stream_uc = container.model_runtime.stream_logs_use_case

        async def event_stream() -> AsyncIterator[str]:
            try:
                async for line in stream_uc.execute(skip=skip):
                    yield f"data: {json.dumps({'line': line})}\n\n"
            except Exception as exc:  # noqa: BLE001
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            finally:
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/logs/clear", response_model=ClearLogsResponse)
    async def clear_logs() -> ClearLogsResponse:
        result = await container.model_runtime.clear_logs_use_case.execute()
        return ClearLogsResponse(
            status=str(result["status"]),
            skip_from=int(result["skip_from"]),
        )

    @router.get("/models", response_model=ModelsResponse)
    async def list_models() -> ModelsResponse:
        models_root = await _forge_config_models_root()
        models = await container.model_runtime.list_models_use_case.execute(
            models_root=models_root or None
        )
        # V1 parity (``/api/models`` returned ``is_running`` per entry,
        # ``useModels.js`` rendered the ● Running / ○ Stopped dot): derive the
        # currently-running local model from the daemon status and stamp each
        # entry. The chat model dropdown reads this to show a reliable running
        # state instead of a fragile client-side "status.model === name"
        # comparison. Best-effort — a status read failure leaves all entries
        # ``is_running: false`` (V1 "service stopped" appearance).
        running_model = ""
        try:
            status = await container.model_runtime.get_status_use_case.execute()
            if isinstance(status, dict) and status.get("running"):
                running_model = str(status.get("model") or "")
        except Exception:  # noqa: BLE001 — status is advisory; never break the list
            running_model = ""
        return ModelsResponse(
            models=[
                {
                    "name": m.name,
                    "path": m.path,
                    "size_mb": m.size_mb,
                    "config_path": m.config_path,
                    "format": m.model_format,
                    # NEW (V1 parity, §3.1 tail-append): ctx-window badge +
                    # running-state dot for the chat model dropdown.
                    "context_length": m.context_length,
                    "is_running": bool(running_model)
                    and m.name == running_model,
                }
                for m in models
            ],
            models_root_path=models_root,
        )

    return router


# ── Service Config (service_config.json) ─────────────────────────────────────

from pydantic import BaseModel as _BaseModel


class _ServiceConfigRequest(_BaseModel):
    config: dict


def build_config_router(*, container: "Container") -> APIRouter:
    """Build the /api/config router bound to the given DI container.

    Exposes ``GET /api/config`` and ``POST /api/config`` for reading and
    writing the GenieAPIService ``service_config.json``. The single source
    of truth is the copy next to ``GenieAPIService.exe`` inside the
    configured install root; when the service is not installed the GET
    returns in-memory defaults (read-only) and the POST fails with a 412
    (``PreconditionFailedError``, no on-disk fallback is created). All
    persistence, path resolution, deep-merge and api_key SecretStore
    handling live in the ``model_runtime`` application layer
    (``GetServiceConfigUseCase`` / ``SaveServiceConfigUseCase``); these
    handlers only map the request body onto the use case and serialise the
    result.
    """
    config_router = APIRouter(prefix="/api", tags=["model_runtime"])

    @config_router.get("/config")
    async def get_service_config() -> dict:
        """Return service_config.json with api_key values masked.

        V1 parity (forge_config_manager.py:348-388 + main.py:4040-4073):
        when ``genie_service.root_path`` is configured, the use case loads the
        service_config.json found inside the GenieAPIService install directory
        (up to 3 levels deep). When the service is not installed, in-memory
        defaults are returned (read-only, ``meta.config_file_path`` empty) —
        no on-disk fallback exists.
        """
        return await container.model_runtime.get_service_config_use_case.execute()

    @config_router.post("/config")
    async def save_service_config(req: _ServiceConfigRequest) -> dict:
        """Save service_config.json; api_key values are stored in SecretStore.

        The use case saves to the GenieAPIService install-dir copy (the single
        source of truth). When the service is not installed there is no file to
        write: the use case raises ``PreconditionFailedError`` -> HTTP 412.
        """
        return await container.model_runtime.save_service_config_use_case.execute(
            req.config
        )

    return config_router


__all__ = ["build_router", "build_config_router"]

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder HTTP routes (PR-034 + PR-304 + S9 close).

S3 scope (endpoints under ``/api/app-builder``) — covers the
runnable slice of the legacy ``02-routes.md`` §3.3 surface (37 routes
total). PR-304 added 16 use cases backing the lane-2 history / batch /
share / cache / metrics / manifest routes. PR-094 added
``GET /runs/{run_id}/export.md``. S9 close wires the previously
surface-only ``feedback`` / ``benchmark`` /
``history/runs/{run_id}`` delete routes to real persistence + use
cases (see :class:`SubmitFeedbackUseCase`,
:class:`RunBenchmarkUseCase`, :class:`DeleteRunHistoryUseCase`).

> Note (2026-07-21 dead-code cleanup): the zero-consumer
> ``files-local`` / ``system-prompt`` / ``tool-descriptor`` /
> ``import/candidates`` routes were removed; their use cases had no
> production wiring. See ``PROJECT-RULES.md §7``.

Route groups (split across sibling modules — pure architectural split,
zero behaviour change)
-----------------------------------------------------------------------

* :mod:`._models`  — ``GET /models`` / ``GET|DELETE /models/{model_id}``
* :mod:`._runs`    — ``POST /runs`` / ``GET|DELETE /runs/{run_id}`` /
                     ``GET /runs/{run_id}/stream`` (SSE) / artifacts +
                     streaming ``GET /artifacts/{run_id}/{path:path}/blob``
                     / upload / batch / list / history / export.md / metrics
* :mod:`._import`  — ``POST /import/dry-run`` + ``commit`` + ``rollback`` +
                     ``scan-bins`` + ``auto-export``
* :mod:`._share`   — ``POST /share`` / ``GET /share/{token}``
* :mod:`._voice`   — ``GET|PUT /voice-preference`` + ``voice-input/preload``
                     + ``feedback`` + ``benchmark`` (POST + status GET)
* :mod:`._catalog` — worker status / taxonomy / deps-status / cache /
                     manifest / schema

All groups register onto a SINGLE :class:`fastapi.APIRouter` built once
per :func:`build_router` call. The single-router shape keeps the
``dependencies=[Depends(_no_store)]`` cache-control directive set exactly
once (so every route emits ``Cache-Control: no-store``) and lets the
``POST /runs`` drainer and the ``GET /runs/{run_id}/stream`` replay share
the same per-call ``RunStreamBroadcaster`` fallback instance.

SSE frame contract (S3-spec §4.4 wire format)
---------------------------------------------

The ``/runs/{run_id}/stream`` endpoint emits ``text/event-stream``
frames. Each frame is one of four kinds; ``data`` is always JSON.

* ``event: state\\ndata: {"status": <RunStatus.value>, "run_id": "..."}``
  — emitted on every state-machine transition observed by the use case
  (``pending`` → ``running`` → ``streaming`` → terminal).
* ``event: frame\\ndata: {"sequence": int, "payload": {...}}``
  — one per :class:`qai.app_builder.domain.run.RunFrame` yielded by
  the runner.
* ``event: error\\ndata: <QaiError envelope>`` — raised when the use
  case bubbles an exception; the stream is closed afterwards.
* ``event: done\\ndata: {"status": "completed"|"cancelled", "run_id": ...}``
  — last frame on normal/cancelled completion. The stream is then
  closed.

No heartbeat is emitted by the route layer; per S3-spec §4.4 the use
case decides when to inject keepalives (none does today).
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

from . import _apps, _catalog, _import, _models, _runs, _share, _voice, _weights
from ._dto import _no_store

from qai.app_builder.application.run_stream_broadcaster import (
    RunStreamBroadcaster,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---------------------------------------------------------------------------
# Run-frame broadcast + SSE replay (R17 — moved to the application layer)
# ---------------------------------------------------------------------------
#
# The per-run SSE broadcast registry (a module-level mutable ``dict`` with
# TTL eviction), the register/publish/mark_terminal mutators, the
# ``POST /runs`` background drainer and the ``GET /runs/{run_id}/stream``
# replay state machine used to live here as route-module state. R17 hoisted
# all of that into
# :class:`qai.app_builder.application.run_stream_broadcaster.RunStreamBroadcaster`,
# a process-wide singleton held by DI (``services.run_stream_broadcaster``).
#
# Run orchestration + frame fan-out is an application-layer responsibility,
# not a route concern; moving it out also removes the module-level mutable
# state the §3.6 route-thinness advisory flags. The route handlers below are
# now thin: ``create_run`` hands the use-case iterator to the broadcaster's
# drainer; ``stream_run`` consumes the broadcaster's ``(event, payload)``
# tuples and encodes them to SSE wire bytes via ``_sse_event``. The wire
# frames, TTL value and replay semantics are unchanged (pure refactor).


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the App Builder router bound to the given DI container.

    No module-level state is captured; the router is reconstructed per
    ``apps.api.main.create_app`` call.

    When the ``qai_appbuilder`` SDK wheel is not installed (e.g. on Linux /
    Ubuntu where only the cloud-chat features are used), all endpoints under
    ``/api/app-builder`` return HTTP 503 with an explanatory message instead
    of crashing at startup.
    """

    router = APIRouter(
        prefix="/api/app-builder",
        tags=["app_builder"],
        dependencies=[Depends(_no_store)],
    )

    # Graceful degradation: qai_appbuilder is a Windows ARM64-only SDK wheel.
    # On Linux (Ubuntu x86_64 / aarch64) it is not installed; register a
    # catch-all 503 route so the API still starts and cloud-LLM features work.
    if importlib.util.find_spec("qai_appbuilder") is None:
        from fastapi import Response

        @router.api_route(
            "/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
        )
        async def _sdk_unavailable(path: str) -> Response:  # noqa: ARG001
            return Response(
                content='{"detail":"app_builder is not available: '
                'qai_appbuilder SDK is not installed on this platform"}',
                status_code=503,
                media_type="application/json",
            )

        return router

    # R17 — process-wide run-stream broadcaster. Prefer the DI-wired
    # singleton (``services.run_stream_broadcaster``); fall back to a
    # router-scoped instance for hand-built test namespaces that omit
    # the tail-appended field. The fallback is created once per router
    # so ``create_run`` and ``stream_run`` share the same frame buffers.
    _fallback_broadcaster = RunStreamBroadcaster()

    def _broadcaster() -> RunStreamBroadcaster:
        bc = getattr(container.app_builder, "run_stream_broadcaster", None)
        return bc if bc is not None else _fallback_broadcaster

    _models.register(router, container=container)
    _runs.register(router, container=container, broadcaster_getter=_broadcaster)
    _import.register(router, container=container)
    _share.register(router, container=container)
    _voice.register(router, container=container)
    _catalog.register(router, container=container)
    _weights.register(router, container=container)
    _apps.register(router, container=container)

    return router


__all__ = ["build_router"]

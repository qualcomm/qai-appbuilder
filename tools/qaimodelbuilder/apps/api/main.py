# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""FastAPI ``create_app()`` factory + ``main()`` entry point.

Constraints (refactor-plan v2.5 §6):
- This file stays a thin assembly skeleton (≤260 lines). The SPA + chat-image
  static mounts (~100 lines of pure wiring) live in ``apps/api/_spa_mount.py``
  (extracted 2026-06-06, dealign-fix-plan C1) so this file is back to wiring.
- No business logic; only wiring.
- No module-level mutable state; no top-level FastAPI() instance.

S3 final merge (main agent, post PR-030..036):
mounts the full union of context routers — system / security /
model_catalog / chat / app_builder / ai_coding / channels — over the
single ``apps.api.di.Container`` graph. ``register_error_handlers``
runs BEFORE any ``include_router`` so every subsequent route inherits
the unified ``QaiError`` envelope contract introduced in PR-030.

S7 PR-074 addition:
mounts the Vite-built SPA bundle from ``frontend/dist/``. Static
``/assets/*`` files are served by ``StaticFiles``; any other unknown
path falls through to a catch-all that returns the SPA entry HTML
(``index.html``). All
``/api/*``, ``/openapi.json``, ``/docs`` and other registered router
paths are matched first because ``include_router`` runs before the
SPA catch-all is registered (FastAPI evaluates routes in the order
they were added).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from fastapi.middleware.cors import CORSMiddleware

from interfaces.http.error_handlers import register_error_handlers
from interfaces.http.middleware import CsrfMiddleware, RequestContextMiddleware
from interfaces.http.middleware.auth import AuthMiddleware
from interfaces.http.routes.ai_coding import build_router as build_ai_coding_router
from interfaces.http.routes.app_builder import build_router as build_app_builder_router
from interfaces.http.routes.background_process import (
    build_router as build_background_process_router,
)
from interfaces.http.routes.channels import build_router as build_channels_router
from interfaces.http.routes.chat import build_router as build_chat_router
from interfaces.http.routes.model_catalog import build_router as build_model_catalog_router
from interfaces.http.routes.security import build_router as build_security_router
from interfaces.http.routes.system import build_router as build_system_router
from interfaces.http.routes.user_prefs import build_router as build_user_prefs_router
from interfaces.http.routes.brokers import build_router as build_brokers_router
from interfaces.http.routes.model_runtime import build_router as build_model_runtime_router
from interfaces.http.routes.model_runtime import build_config_router as build_service_config_router
from interfaces.http.routes.service_logs_sse import build_router as build_service_logs_sse_router
from interfaces.http.routes.uploads import build_router as build_uploads_router
from interfaces.http.routes.versions import build_router as build_versions_router
from interfaces.http.routes.aria2c import build_router as build_aria2c_router
from interfaces.http.routes.auth import build_router as build_auth_router
from interfaces.http.routes.service_catalog import (
    build_router as build_service_catalog_router,
)
from interfaces.http.routes.conversations_search import build_router as build_conversations_search_router
from interfaces.http.routes.events import build_router as build_events_router
from interfaces.http.routes.events import build_ws_router as build_events_ws_router
from interfaces.http.routes.mb_pro_session import build_router as build_mb_pro_session_router
from interfaces.http.routes.gomaster_session import build_router as build_gomaster_session_router
from interfaces.http.routes.gomaster import build_router as build_gomaster_router
from interfaces.http.routes.gomaster_optimize import build_router as build_gomaster_optimize_router
from qai.platform.config import Settings, load_settings
from qai.platform.logging import get_logger

from .di import Container
from ._global_proxy import build_ssl_verify_provider
from ._runtime_config_store import load_runtime_config_overrides
from .lifespan import make_lifespan
from ._spa_mount import mount_static_assets
from ._uploads_di import build_upload_store

_LOGGER = get_logger("apps.api.main")


def _load_settings_with_persisted_overrides(repo_root: Path) -> Settings:
    """Load Settings, layering operator-persisted security/tools switches on top.

    The ``GET/PUT /api/security/runtime-config`` surface persists the typed
    security/tools switches (``file_guard_enabled`` / ``sandbox_enabled`` /
    ``file_broker_enabled`` / ``ssl_verify`` / …) into the shared
    ``forge_config`` document so an operator edit survives a restart
    (decision 2A). The pydantic ``Settings`` model is immutable per process,
    so we feed those persisted values back as ``load_settings(overrides=...)``
    — they win over ``server.toml`` / env / defaults, which is exactly the
    "the UI is the authoritative source for these knobs" contract.

    Two-step because the data root that holds ``forge_config.json`` is itself a
    Settings field: a first default load discovers ``data.data_dir``, we read
    the persisted overrides from under it, then re-load with them applied. When
    nothing was persisted the override dict is empty and the second load is
    equivalent to the first.
    """
    base = load_settings(repo_root=repo_root)

    # API-ISO-1 (mirror CLI-ISO-1 in apps/cli/_runtime.py:93-101): anchor a
    # still-default *relative* ``data_dir`` to ``repo_root``. Without this the
    # daemon's DB path (``<data_dir>/db/qai.db``) is resolved against the
    # PROCESS CWD — so a launch from a different working directory (Desktop
    # shell / packaged run / `python -m apps.api` from elsewhere) points at a
    # DIFFERENT, empty ``data/db/qai.db`` and the user's whole chat history
    # "disappears" on refresh (AGENTS.md State-Truth-First 铁律 4 — same class
    # as the historical Desktop empty-DB bug). The CLI path already anchored
    # this; the API server path did not. Operator-set absolute / non-default
    # relative ``data_dir`` values are left untouched.
    anchor_data: dict[str, dict[str, str]] = {}
    if repo_root is not None and (not base.data.data_dir.is_absolute()) and str(
        base.data.data_dir
    ) == "data":
        anchor_data = {"data": {"data_dir": str(repo_root / "data")}}
        base = load_settings(repo_root=repo_root, overrides=anchor_data)

    overrides = load_runtime_config_overrides(base.data.data_dir)
    if not overrides:
        return base
    if anchor_data:
        # Keep the data_dir anchor; layer persisted typed-security switches.
        merged = dict(overrides)
        merged.setdefault("data", {})["data_dir"] = str(repo_root / "data")
        return load_settings(repo_root=repo_root, overrides=merged)
    return load_settings(repo_root=repo_root, overrides=overrides)


def create_app(
    *,
    settings: Settings | None = None,
    repo_root: Path | None = None,
) -> FastAPI:
    """Build a FastAPI instance.

    Tests usually pass an explicit ``settings`` and a ``tmp_path`` as
    ``repo_root``. Production callers leave both as ``None`` and let
    :func:`main` resolve them from CLI flags / env / config.
    """
    resolved_root = (repo_root or _detect_repo_root()).resolve()
    resolved_settings = settings or _load_settings_with_persisted_overrides(
        resolved_root
    )
    container = Container.build(settings=resolved_settings, repo_root=resolved_root)

    # S-8 (align D5): when the server binds all interfaces (0.0.0.0, LAN-
    # exposed) the OpenAPI docs surface is auto-disabled so the API schema
    # is not advertised to the LAN. On loopback (127.0.0.1) docs honour the
    # explicit ``server.docs_enabled`` toggle.
    _bind_host = resolved_settings.security.bind_host
    _public_bind = _bind_host == "0.0.0.0"
    _docs_enabled = resolved_settings.server.docs_enabled and not _public_bind
    if _public_bind and resolved_settings.server.docs_enabled:
        _LOGGER.warning(
            "binding %s — OpenAPI docs (/docs, /openapi.json, /redoc) "
            "auto-disabled to avoid LAN schema exposure",
            _bind_host,
        )

    app = FastAPI(
        title="QAIModelBuilder",
        version=_app_version(),
        docs_url="/docs" if _docs_enabled else None,
        openapi_url="/openapi.json" if _docs_enabled else None,
        redoc_url="/redoc" if _docs_enabled else None,
        lifespan=make_lifespan(container=container),
    )
    app.state.container = container

    # Unified error envelope (PR-030). Must run BEFORE include_router so
    # subsequently registered routers inherit the global handlers.
    register_error_handlers(app)

    # CSRF middleware (PR-040). Mounted AFTER register_error_handlers so
    # any ForbiddenError raised by the middleware propagates through the
    # unified envelope; mounted BEFORE include_router so every routed
    # request flows through it. ``settings.security.csrf_enabled = False``
    # short-circuits the middleware (dev / test opt-out).
    app.add_middleware(
        CsrfMiddleware,
        settings=resolved_settings.security,
    )

    # Okta SSO auth gate. Registered AFTER CsrfMiddleware so it sits at a
    # more OUTER position in the Starlette stack (Starlette runs middleware
    # in reverse registration order): Auth intercepts unauthenticated
    # requests EARLIER than CSRF, so we don't spend CSRF cookie churn on
    # a 303→/auth/login redirect. Both are inside RequestContextMiddleware
    # (registered last, below) so the request_id is bound before Auth logs
    # anything. ``settings.auth.enabled = False`` (default) short-circuits
    # the middleware entirely — an unconfigured deployment is unaffected.
    # See interfaces/http/middleware/auth.py for the public-path exemptions
    # (health / build-info / /auth/* / /callback / SPA static / /v1/ /
    # webhooks) that keep the API surface reachable without a session.
    app.add_middleware(
        AuthMiddleware,
        settings=resolved_settings.auth,
        data_root=container.data_paths.root,
    )

    # CORS middleware — S-2: explicit trusted-origin allow-list (no longer
    # ``allow_origins=['*']``, which is incompatible with credentialed
    # requests and would leak the CSRF cookie to any origin). Origins are
    # configurable via ``server.cors_allow_origins``.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.server.cors_allow_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request-ID propagation (E-1). Registered LAST so it is the OUTERMOST
    # middleware (Starlette runs middleware in reverse registration order):
    # the per-request ``request_id`` is bound into the structured-logging
    # context before CSRF / routing run, and echoed back via X-Request-ID.
    app.add_middleware(RequestContextMiddleware)

    # Mount routers — order is purely cosmetic (each router carries its own prefix).
    # SSO login endpoints registered first so /auth/login, /callback,
    # /auth/logout, /auth/signed-out, /api/auth/me are matched before the
    # SPA catch-all (mount_static_assets, below) can ever claim them.
    app.include_router(build_auth_router(
        settings=resolved_settings.auth,
        server_port=resolved_settings.server.port,
        data_root=container.data_paths.root,
        ssl_verify=resolved_settings.ssl_verify,
        # Live global Settings.ssl_verify provider: the auth router is built ONCE
        # here at startup, so its Okta / JWKS outbound clients must read the
        # toggle at REQUEST time (not this build-time snapshot). The provider
        # takes precedence over the ``ssl_verify`` bool above (which stays as the
        # standalone/test fallback). Okta IS included in the global toggle.
        ssl_verify_provider=build_ssl_verify_provider(container),
    ))
    app.include_router(build_system_router(container=container))
    app.include_router(build_security_router(container=container))
    app.include_router(build_model_catalog_router(container=container))
    app.include_router(build_chat_router(container=container))
    app.include_router(build_app_builder_router(container=container))
    app.include_router(build_ai_coding_router(container=container))
    app.include_router(build_channels_router(container=container))
    app.include_router(build_user_prefs_router(container=container))
    app.include_router(build_brokers_router(
        dependency_approval_services=container.dependency_approval,
        command_policy_services=container.command_policy,
        exec_profiles_dir=(
            container.repo_root / "factory" / "config" / "exec_profiles"
        ),
    ))
    app.include_router(build_model_runtime_router(container=container))
    app.include_router(build_service_config_router(container=container))
    # PR-095 / S9 A-26: live SSE tail of inference daemon logs.
    # Mounted as a separate router so the existing model_runtime
    # surface (snapshot ``GET /api/service/logs``) stays untouched.
    app.include_router(build_service_logs_sse_router(container=container))
    app.include_router(build_uploads_router(
        store=build_upload_store(container),
    ))
    # W1-H: additional route modules restoring missing legacy endpoints.
    app.include_router(build_versions_router(container=container))
    app.include_router(build_aria2c_router(container=container))
    app.include_router(build_service_catalog_router(container=container))
    app.include_router(build_conversations_search_router(container=container))
    app.include_router(build_events_router(container=container))
    app.include_router(build_events_ws_router(container=container))
    # background_process platform routes (6 endpoints; design.md §9). No
    # ``POST /start`` here on purpose — spawning a process must go through
    # the LLM tool layer so the permission-ask + audit pipeline runs.
    app.include_router(build_background_process_router(container=container))
    # MB Pro (Model Builder Pro) session-control routes — back the chat
    # composer's「Pro / 增强」mode connect/disconnect buttons. Handlers are
    # is_internal-gated (404 on external); messages still flow through the
    # normal chat WS/SSE via the ``query::mb_pro`` hint.
    app.include_router(build_mb_pro_session_router(container=container))
    # GoMaster online-integration session-control routes — back the chat
    # composer's「GoMaster 在线」mode connect/disconnect buttons + the "open the
    # original GoMaster site" link. Handlers are is_internal-gated (404 on
    # external; native-url never disclosed there); Agent chat messages still
    # flow through the normal chat WS/SSE via the ``query::gomaster`` hint, and
    # non-chat capabilities via the /api/gomaster/* REST proxy routes.
    app.include_router(build_gomaster_session_router(container=container))
    # GoMaster online-integration REST/stream capability-proxy routes
    # (/api/gomaster/*) — back the「GoMaster 在线」panels (graph optimize diff,
    # real-time QNN logs, model graph, benchmark, artifacts download). Handlers
    # are is_internal-gated (404 on external); SSE relayed byte-for-byte.
    app.include_router(build_gomaster_router(container=container))
    # GoMaster External Auto-Optimize proxy routes (/api/gomaster/optimize/*) —
    # the ``external`` link: upload ONNX → async optimize job → poll → download
    # optimized model + report. is_internal-gated + gomaster_mode-gated (404
    # when the external link is not wired).
    app.include_router(build_gomaster_optimize_router(container=container))

    # Chat image files + SPA static bundle (PR-074). Both must register
    # AFTER all API/WS routers so FastAPI matches /api/*, /openapi.json,
    # /docs, /v1/* etc. first; the SPA catch-all only fires for paths no
    # router claimed, and the image mount precedes it so uploaded images
    # are served directly. See apps/api/_spa_mount.py for details.
    mount_static_assets(
        app,
        data_root=container.data_paths.root,
        repo_root=resolved_root,
        strict=resolved_settings.server.is_production,
    )
    return app



def main(argv: list[str] | None = None) -> int:
    """CLI entry point — ``python -m apps.api`` (debug-only direct uvicorn).

    The previous standalone ``qai-api`` console-script was retired in CLI D3
    (Desktop App Plan §2.4); use ``python -m apps.api`` for direct uvicorn
    invocation, or ``qai-serve`` (kept as a documented exception) for the
    supervised production path.
    """
    parser = argparse.ArgumentParser(
        prog="python -m apps.api",
        description="QAIModelBuilder API server (direct uvicorn; debug-only)",
    )
    parser.add_argument("--host", help="bind host (overrides settings)")
    parser.add_argument("--port", type=int, help="bind port (overrides settings)")
    parser.add_argument("--reload", action="store_true", help="enable uvicorn reloader (dev only)")
    parser.add_argument("--config", type=Path, help="path to server.toml")
    args = parser.parse_args(argv)

    repo_root = _detect_repo_root()
    overrides: dict[str, dict[str, object]] = {}
    server_overrides: dict[str, object] = {}
    if args.host:
        # S-4: the public bind host is owned by ``security.bind_host`` (V1
        # parity: only 127.0.0.1 / 0.0.0.0 accepted, else loopback fallback
        # via the SecuritySettings validator). The CLI ``--host`` flag drives
        # it. ``server.host`` retains its (loopback) default for any consumer
        # that still reads it.
        overrides["security"] = {"bind_host": args.host}
    if args.port is not None:
        server_overrides["port"] = args.port
    if server_overrides:
        overrides["server"] = server_overrides

    settings = load_settings(
        config_file=args.config,
        repo_root=repo_root,
        overrides=overrides or None,
    )

    # Use an explicit Server instance rather than ``uvicorn.run`` so reboot /
    # exit requests can set ``server.should_exit`` and let uvicorn drain its
    # lifespan + websocket tasks normally.  Raising ``SystemExit(75)`` inside an
    # asyncio task races Python 3.13 async-generator shutdown (notably open
    # httpx streams behind chat WS) and can print
    # ``RuntimeError: aclose(): asynchronous generator is already running``.
    # The reboot contract is unchanged: after graceful shutdown we return the
    # requested code (75 for reboot, 0 for exit) to the supervisor.
    config = uvicorn.Config(
        "apps.api.main:_create_app_for_uvicorn",
        host=settings.security.bind_host,
        port=settings.server.port,
        reload=args.reload,
        factory=True,
        log_config=None,  # we configure structlog ourselves in lifespan
        # Disable uvicorn's per-request access log. It dumps the FULL
        # request line — including the entire URL query string — at INFO
        # on every request, which for the chat SSE endpoint
        # (``GET /api/chat/conversations/{id}/stream?...prompt=<url-encoded
        # whole prompt>...``) spams the backend log with a multi-KB line
        # per turn. The structured access record we actually care about is
        # emitted by our own middleware/structlog, so the uvicorn access
        # logger is pure noise here.
        access_log=False,
        timeout_graceful_shutdown=30,
        # WebSocket protocol-level keepalive (RFC 6455 Ping/Pong).
        #
        # ``ws="auto"`` selects the legacy ``websockets`` backend (the
        # ``websockets`` package is installed), which honours these two
        # kwargs. Uvicorn's DEFAULTS are an aggressive 20s interval + 20s
        # pong timeout: the server pings every ~20s and FAILS the connection
        # (``keepalive ping timeout`` -> internal 1011, observed by the ASGI
        # layer as an abnormal 1006 close) if a matching pong is not
        # processed within 20s of that ping.
        #
        # A long chat turn (many tool rounds over 10+ minutes) plus any
        # brief stall on the client side (Windows Modern Standby / sleep-
        # resume, browser Network Service pause, tab discard) or a server
        # event-loop hiccup can let the pong miss that 20s window, dropping
        # the socket mid-turn. Widening the window to 30s interval / 90s
        # timeout keeps genuine dead-connection detection while tolerating
        # transient suspend/resume gaps. The chat routes ALSO emit a 15s
        # application-level ``{"type":"ping"}`` heartbeat (``_ws.py`` /
        # ``_sse.py``) as a second, independent liveness signal.
        ws_ping_interval=30.0,
        ws_ping_timeout=90.0,
    )
    server = uvicorn.Server(config)

    from ._reboot_scheduler import (
        get_requested_exit_code,
        set_graceful_exit_handler,
    )

    import threading as _threading

    exit_requested = False
    # Lock protecting exit_requested + should_exit / force_exit writes.
    # Multiple paths can call _request_graceful_exit concurrently:
    # - SIGINT/SIGTERM/SIGBREAK (signal handler thread)
    # - Console ctrl handler (OS console-ctrl thread)
    # - Parent-process watchdog thread
    # Without a lock the read-modify-write on exit_requested is a data race
    # and a second concurrent call could spuriously escalate to force_exit,
    # cutting short the lifespan drain that cleans up App Builder children.
    _exit_lock = _threading.Lock()

    def _request_graceful_exit(code: int) -> bool:
        nonlocal exit_requested
        with _exit_lock:
            if exit_requested:
                # Already shutting down gracefully — this is a duplicate signal
                # from a concurrent path (console handler + SIGBREAK both fire
                # on window-close).  Ignore it: do NOT escalate to force_exit
                # here, because that would cut short the lifespan drain that
                # cleans up App Builder child processes.  force_exit is only
                # appropriate when the user explicitly sends a *second* signal
                # after the first graceful-exit request (uvicorn's own
                # second-signal escalation semantics), which is handled by the
                # SIGINT/SIGTERM handler registered via set_graceful_exit_handler
                # — not by the watchdog / console paths.
                return True
            exit_requested = True
            server.should_exit = True
        return True

    set_graceful_exit_handler(_request_graceful_exit)

    # 2026-07-09 — console / parent-process watchdog for ``apps.api``.
    # The supervisor (``apps.cli.serve``) already has a full console-ctrl
    # handler that forwards CTRL_BREAK to us; but when the user closes the
    # terminal window directly (or the supervisor dies unexpectedly), the
    # ``apps.api`` process becomes an orphan and never receives a shutdown
    # signal — its Job Object stays open, so App Builder child processes
    # (uvicorn preview servers) are never cleaned up.
    #
    # Fix: install a lightweight watchdog directly in ``apps.api`` so it can
    # detect "console closed / parent gone" and trigger its own graceful
    # shutdown (which runs lifespan.shutdown → manager.shutdown → taskkill
    # all child processes).  Two complementary mechanisms:
    #
    # 1. Console ctrl handler (Windows CTRL_CLOSE_EVENT / CTRL_LOGOFF /
    #    CTRL_SHUTDOWN): fires when the user closes the terminal window.
    #    Uses the same ``ConsoleCtrlInterceptor`` the supervisor uses, so
    #    the implementation is shared and tested.
    #
    # 2. Parent-process watchdog thread: polls whether the supervisor PID
    #    is still alive every 2 s.  Handles the case where the supervisor
    #    exits silently (e.g. killed with SIGKILL / TerminateProcess) without
    #    sending any console event.  Uses the existing ``_is_pid_alive`` helper
    #    from ``_endpoint_helper`` (PROCESS_QUERY_LIMITED_INFORMATION +
    #    GetExitCodeProcess) which is more reliable than a bare psutil
    #    pid_exists check and already handles the PID-reuse edge case better.
    #
    # Both mechanisms call ``_request_graceful_exit(0)`` which sets
    # ``server.should_exit`` and lets uvicorn drain lifespan normally.
    # Best-effort: a watchdog failure must never crash the server.
    import os as _os

    _supervisor_pid = _os.getppid()

    _log = __import__("logging").getLogger("qai.api.watchdog")

    def _on_console_close() -> None:
        """Called from the console ctrl handler thread on CTRL_CLOSE etc."""
        _log.info("api.watchdog.console_close pid=%d", _os.getpid())
        _request_graceful_exit(0)

    try:
        from apps.cli._console_ctrl import ConsoleCtrlInterceptor

        _ctrl = ConsoleCtrlInterceptor(
            on_ctrl_c=lambda: None,  # Ctrl+C handled by uvicorn's own signal handler
            on_close=_on_console_close,
        )
        installed = _ctrl.install()
        if not installed:
            _log.debug("api.watchdog.console_handler not installed (non-Windows or error)")
    except Exception:  # noqa: BLE001 — watchdog failure must not crash server
        _ctrl = None
        _log.debug("api.watchdog.console_handler install failed", exc_info=True)

    def _parent_watchdog() -> None:
        """Background thread: trigger graceful exit when supervisor dies."""
        import time

        # Reuse the existing _is_pid_alive helper which uses
        # PROCESS_QUERY_LIMITED_INFORMATION + GetExitCodeProcess on Windows —
        # more reliable than a bare psutil.pid_exists (which can return True
        # for a zombie / PID-reused process).  Falls back gracefully on POSIX.
        try:
            from apps.cli._endpoint_helper import _is_pid_alive as _alive
        except Exception:  # noqa: BLE001 — fall back to psutil
            try:
                import psutil

                def _alive(pid: int) -> bool:  # type: ignore[misc]
                    try:
                        return bool(psutil.pid_exists(pid))
                    except Exception:
                        return True  # uncertain → assume alive
            except ImportError:
                def _alive(pid: int) -> bool:  # type: ignore[misc]
                    return True  # can't check → assume alive (safe: no false trigger)

        _log.debug(
            "api.watchdog.thread started supervisor_pid=%d", _supervisor_pid
        )
        while True:
            try:
                time.sleep(2)
                if server.should_exit or server.force_exit:
                    _log.debug("api.watchdog.thread server already exiting, stopping")
                    return
                if not _alive(_supervisor_pid):
                    _log.info(
                        "api.watchdog.supervisor_gone pid=%d triggering graceful exit",
                        _supervisor_pid,
                    )
                    _request_graceful_exit(0)
                    return
            except Exception:  # noqa: BLE001 — watchdog must never die silently
                _log.warning(
                    "api.watchdog.thread error (continuing)", exc_info=True
                )

    import threading

    _watchdog_thread = threading.Thread(
        target=_parent_watchdog, daemon=True, name="api-parent-watchdog"
    )
    _watchdog_thread.start()

    try:
        server.run()
        return get_requested_exit_code(0)
    finally:
        set_graceful_exit_handler(None)
        if _ctrl is not None:
            try:
                _ctrl.uninstall()
            except Exception:  # noqa: BLE001
                pass


def _create_app_for_uvicorn() -> FastAPI:
    """Internal hook used by ``uvicorn.run(factory=True)``.

    Re-resolves repo_root + settings inside the worker process so that
    the same code path works under ``--reload`` (which spawns workers).

    Bug 4 fix: in ``--reload`` mode uvicorn spawns a fresh worker process
    that never executes the ``set_graceful_exit_handler`` call in
    ``_run_server()``.  We therefore install a minimal graceful-exit
    handler here, inside the factory, so every worker process (including
    reload workers) registers the handler before the first request is
    served.  The handler has no ``uvicorn.Server`` reference available at
    factory time, so it falls back to the safest portable action:
    ``sys.exit`` with the requested exit code, which uvicorn's reload
    supervisor interprets as a normal worker exit and triggers a reload
    cycle rather than treating it as an unhandled crash.
    """
    from ._reboot_scheduler import (
        has_graceful_exit_handler as _has_handler,
        set_graceful_exit_handler as _set_handler,
    )
    import sys as _sys

    def _reload_worker_graceful_exit(code: int) -> bool:
        """Graceful-exit handler for reload worker processes.

        Called when a reboot/exit is requested inside a ``--reload``
        worker.  Since we have no ``uvicorn.Server`` handle here, we
        perform a clean ``sys.exit`` so the reload supervisor can detect
        the exit and spawn a new worker.  ``sys.exit`` raises
        ``SystemExit``, so the ``return True`` below is unreachable; it
        exists only to satisfy the ``GracefulExitHandler`` return type.
        """
        sys.exit(code)
        return True  # unreachable; satisfies the GracefulExitHandler type

    # Critical: in normal ``python -m apps.api`` runs, ``main()`` installs a
    # handler bound to the live ``uvicorn.Server`` *before* the factory is
    # invoked.  Do NOT overwrite it here.  Overwriting it with ``sys.exit`` was
    # the root cause of "reboot scheduled but API never comes back": the reboot
    # task killed the ASGI worker with SystemExit instead of setting
    # ``server.should_exit``, so the parent process never returned exit code 75
    # to apps.cli.serve.  Only reload-worker entrypoints lack a handler and need
    # this fallback.
    if not _has_handler():
        _set_handler(_reload_worker_graceful_exit)
    return create_app()


def _detect_repo_root() -> Path:
    """Find the repository root from this file's location.

    apps/api/main.py is two levels below the repo root.
    """
    return Path(__file__).resolve().parents[2]


def _app_version() -> str:
    try:
        from importlib.metadata import version

        return version("qaimodelbuilder")
    except Exception:  # noqa: BLE001 — never let metadata lookup break startup
        return "0.0.0-dev"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""SPA + chat-image static mounts for the FastAPI app (extracted from main.py).

Background (AGENTS.md §6 / dealign-fix-plan C1, 2026-06-06):
``apps/api/main.py`` is the composition root and is meant to be a thin
assembly skeleton (refactor-plan §6 budgeted it at ≤150, later ≤260 lines).
The Vite-built SPA mount + the chat-image ``StaticFiles`` mount (~100 lines)
were inlined there and pushed it to 328 lines. They are pure wiring with no
business logic, so they live here; ``main.create_app`` calls
:func:`mount_static_assets`.

S7 PR-074: mounts the Vite-built SPA bundle from ``frontend/dist/``. Static
``/assets/*`` files are served by ``StaticFiles``; any other unknown path
falls through to a catch-all that returns the SPA entry HTML (``index.html``).
All ``/api/*``, ``/openapi.json``, ``/docs`` and other registered router paths
are matched first because ``include_router`` runs before the SPA catch-all is
registered (FastAPI evaluates routes in the order they were added).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse

# MIME-type overrides for Windows: the registry can map .js → text/plain,
# causing browsers to reject ES-module scripts with a strict-MIME error.
# _FixedMimeStaticFiles uses this table to patch the content-type header
# before the 304 Not-Modified decision is made, bypassing mimetypes entirely.
_MIME_OVERRIDES: dict[str, str] = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
}

# ETag suffix appended when a MIME override is applied.  Browsers that
# previously cached a response with the *wrong* text/plain MIME type will
# have stored the old ETag (without this suffix).  The mismatch forces a
# fresh 200 response so they update their cache to the correct MIME type.
# Once a browser has the suffixed ETag, subsequent 304 responses work
# normally and the cached correct MIME type is used.
_ETAG_MIME_SUFFIX = "-qm"


class _FixedMimeStaticFiles(StaticFiles):
    """StaticFiles that corrects wrong MIME types and busts stale browser caches.

    Starlette delegates content-type detection to Python's ``mimetypes``
    module, which on Windows reads from the registry.  Some machines map
    ``.js`` (and others) to ``text/plain``, causing browsers to refuse every
    ES-module script.

    We override ``file_response()`` — where the ``FileResponse`` is built
    and the 304 Not-Modified decision is made — so we can:

    1. Force the correct ``content-type`` header before any caching logic runs.
    2. Append ``_ETAG_MIME_SUFFIX`` to the ETag when a MIME fix is applied.
       A browser that cached the old text/plain response will have stored the
       original ETag; the suffix makes the ETags differ, bypassing the 304
       path and delivering a fresh 200 with the right MIME type.  The browser
       then caches the suffixed ETag + correct MIME type, and future requests
       use 304 correctly.
    """

    def file_response(
        self,
        full_path: str,
        stat_result,
        scope,
        status_code: int = 200,
    ) -> Response:
        request_headers = Headers(scope=scope)
        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)

        ext = Path(str(full_path)).suffix.lower()
        override = _MIME_OVERRIDES.get(ext)
        if override:
            ct = response.headers.get("content-type", "")
            # Always enforce the correct type when we know what it should be.
            # Using `ct != override` (rather than `ct.startswith("text/plain")`)
            # catches ALL wrong registry mappings, including the common
            # `application/octet-stream` case, not just `text/plain`.
            if ct != override:
                response.headers["content-type"] = override
                # Mutate the ETag to invalidate old cache entries that stored
                # the wrong MIME type.  Starlette ETags are strong: '"mtime-size"'.
                etag = response.headers.get("etag", "")
                if etag:
                    if etag.endswith('"'):
                        response.headers["etag"] = etag[:-1] + _ETAG_MIME_SUFFIX + '"'
                    else:
                        response.headers["etag"] = etag + _ETAG_MIME_SUFFIX

        if self.is_not_modified(response.headers, request_headers):
            return NotModifiedResponse(response.headers)
        return response

from qai.platform.config.paths import DataPaths
from qai.platform.logging import get_logger

_LOGGER = get_logger("apps.api.spa_mount")
_SPA_ENTRY_CANDIDATES: tuple[str, ...] = ("index.html",)

#: F-2(a): served at ``/`` (and any client-router path) in *strict* mode when
#: ``frontend/dist/`` is absent. A clear maintenance page beats either a blank
#: 404 or a silently-skipped mount in a packaged release. ``/api/*`` etc. keep
#: returning 404 (handled in ``_mount_spa_unavailable``) so the API stays usable.
_DIST_MISSING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QAIModelBuilder - service unavailable</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #0f1420; color: #e0e6f0;
         font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .box { text-align: center; max-width: 32rem; padding: 2rem; line-height: 1.7; }
  h1 { font-size: 1.35rem; margin: 0 0 0.75rem; }
  p { color: #8a9ab5; font-size: 0.95rem; margin: 0.5rem 0; }
  code { background: rgba(255,255,255,0.08); padding: 0.1rem 0.4rem;
         border-radius: 4px; font-size: 0.88rem; }
</style>
</head>
<body>
  <div class="box">
    <h1>Web UI is not available</h1>
    <p>The front-end bundle (<code>frontend/dist/</code>) was not found in this
       installation.</p>
    <p>The API is still running - only the packaged Web UI assets are missing.
       Reinstall or rebuild the front-end to restore the interface.</p>
  </div>
</body>
</html>
"""


def mount_static_assets(
    app: FastAPI,
    *,
    data_root: Path,
    repo_root: Path,
    strict: bool = False,
) -> None:
    """Mount chat-image + App Builder file roots then the SPA bundle.

    Order matters: the static mounts and the SPA catch-all must be
    registered AFTER all API/WS routers (done by the caller) so FastAPI
    matches ``/api/*`` etc. first; and every static mount must precede the
    SPA catch-all so the real bytes are served directly, not shadowed by
    the SPA fallback HTML.

    ``strict`` (F-2(a)): when ``True`` (production) a missing
    ``frontend/dist/`` bundle mounts a 503 maintenance page at ``/`` rather
    than silently skipping; ``/api/*`` etc. still return 404. When ``False``
    (dev default) the mount is skipped with a warning as before.
    """
    _mount_images(app, data_root=data_root)
    _mount_app_builder_files(app, data_root=data_root)
    _mount_spa(app, repo_root=repo_root, strict=strict)


def _mount_images(app: FastAPI, *, data_root: Path) -> None:
    """Mount the chat-image blob dir at ``/api/images/files`` (V1 parity).

    V1 ``backend/main.py:842``:
        app.mount("/api/images/files", StaticFiles(directory=str(_IMAGES_DIR)))

    The :class:`qai.chat.adapters.image_upload_store.FileSystemImageUploadStore`
    writes uploaded chat images to the chat-context blob dir
    (``data/blobs/chat`` via ``DataPaths.blob_dir("chat")``; ARCH-2,
    2026-06-09 — previously the ad-hoc ``data/images``) and returns URLs with
    the ``/api/images/files/`` prefix. The URL prefix is a V1-locked contract
    and stays unchanged; only the physical directory moved under ``blobs/``.
    Path resolution goes through the same ``DataPaths`` port as
    ``apps/api/_chat_di.py`` so the write side and the static mount can never
    drift. Without this mount the browser gets 404 displaying chat images.
    """
    images_dir = DataPaths(data_root).blob_dir("chat")
    images_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/api/images/files",
        StaticFiles(directory=str(images_dir)),
        name="chat-images",
    )


def _mount_app_builder_files(app: FastAPI, *, data_root: Path) -> None:
    """Mount the App Builder OUTPUT + audio-upload dirs (V1 parity).

    V1 ``backend/main.py:848-861``::

        app.mount("/api/appbuilder/files/outputs",
                  StaticFiles(directory=str(DATA_DIR / "outputs")))
        app.mount("/api/appbuilder/files/uploads/audio",
                  StaticFiles(directory=str(DATA_DIR / "uploads" / "audio")))

    The Pack runners write their OUTPUT artifacts to the *flat*
    ``<repo_root>/data/outputs/`` directory and emit a result path
    relative to ``repo_root`` (e.g. MeloTTS ``audio_path`` =
    ``data/outputs/tts-<run_id>.wav``; super-resolution / segmentation
    ``image_path`` = ``data/outputs/sr-<run_id>.png``). In production
    ``data_dir`` is anchored at ``<repo_root>/data`` (``main.py``), so
    ``data_root / "outputs"`` is exactly where the runner wrote the file.

    Without this mount the front-end (which rewrites a ``data/outputs/…``
    path onto ``/api/appbuilder/files/outputs/…`` — see
    ``frontend/src/utils/appBuilderAssetUrl.ts``) would 404 on every
    generated audio/image (the bug this fix addresses).

    The ``uploads/audio`` mount mirrors the V1-locked contract for the
    same flat layout. Note V2 *input* audio uploads live under the
    date-partitioned ``data/blobs/uploads/audio/<date>/`` and are resolved
    physically by ``input_artifact_resolver``; this mount serves the flat
    ``data/uploads/audio/`` tree a runner may reference, preserving V1
    behaviour without disturbing the input-resolution path.

    The ``/api/appbuilder/files/...`` URL prefix is a V1-locked contract
    (§3.1 — new mount, additive, does not touch ``/api/images/files``).
    Both directories are created on first call so a fresh ``data/`` (no
    runs yet) does not break the mount.
    """
    outputs_dir = data_root / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/api/appbuilder/files/outputs",
        StaticFiles(directory=str(outputs_dir)),
        name="appbuilder-outputs",
    )

    audio_uploads_dir = data_root / "uploads" / "audio"
    audio_uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/api/appbuilder/files/uploads/audio",
        StaticFiles(directory=str(audio_uploads_dir)),
        name="appbuilder-audio-uploads",
    )


def _mount_spa(app: FastAPI, *, repo_root: Path, strict: bool = False) -> None:
    """Wire the Vite-built SPA bundle into ``app`` (PR-074).

    Layout served:
    - ``GET /assets/<hashed-file>``  -> ``frontend/dist/assets/<hashed-file>``
    - ``GET /``                       -> ``frontend/dist/index.html``
    - ``GET /<any-non-api-path>``    -> same SPA entry HTML (client router)

    When ``frontend/dist/`` is absent (e.g. dev environment that has not
    run ``pnpm -C frontend build`` yet) the behaviour depends on ``strict``:

    - ``strict=False`` (dev default): the SPA mount is skipped with a
      warning; the API surface remains fully functional.
    - ``strict=True`` (production, F-2(a)): a 503 maintenance page is
      mounted at ``/`` and any client-router path so a packaged release
      with a missing bundle gives a clear error instead of a blank 404;
      ``/api/*``, ``/v1/*``, ``/openapi.json``, ``/docs``, ``/ws/*`` still
      return 404 so the API stays usable. Startup never crashes either way.

    Tests that need a deterministic SPA presence build the dist tree in a
    tmp dir and pass a custom ``repo_root``.
    """
    dist = repo_root / "frontend" / "dist"
    if not dist.is_dir():
        if strict:
            _LOGGER.warning(
                "spa.mount.unavailable",
                reason="frontend_dist_absent",
                path=str(dist),
                hint="production build missing — serving 503 maintenance page",
            )
            _mount_spa_unavailable(app)
            return
        _LOGGER.warning(
            "spa.mount.skipped",
            reason="frontend_dist_absent",
            path=str(dist),
            hint="run `pnpm -C frontend build` to generate the SPA bundle",
        )
        return

    entry: Path | None = None
    for candidate in _SPA_ENTRY_CANDIDATES:
        if (dist / candidate).is_file():
            entry = dist / candidate
            break
    if entry is None:
        _LOGGER.warning(
            "spa.mount.skipped",
            reason="spa_entry_html_missing",
            path=str(dist),
            looked_for=list(_SPA_ENTRY_CANDIDATES),
        )
        return

    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        # name="spa-assets" so it cannot accidentally collide with any
        # router-named route in the OpenAPI graph.
        app.mount(
            "/assets",
            _FixedMimeStaticFiles(directory=str(assets_dir)),
            name="spa-assets",
        )

    # Cache the resolved entry so the closure does not stat the disk on
    # every request.
    entry_path = entry

    # Cache headers for the SPA entry HTML (index.html).
    # ``no-cache`` does NOT mean "never cache" — it means "always validate
    # with the server before reusing". The browser may keep the cached copy
    # but MUST send a conditional GET; the server returns 304 if unchanged
    # or the new file otherwise. This is the correct policy for an
    # immutable-asset SPA: hashed JS/CSS chunks under /assets/ can be
    # cached forever, but index.html (which references them by name) MUST
    # NOT be cached. Without this, a previously-loaded index.html lingers
    # in the browser's memory module graph and keeps importing chunk names
    # that have been replaced by newer Build.bat output → 404 on every
    # lazy-loaded route.
    _SPA_HTML_HEADERS = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/", include_in_schema=False)
    async def _spa_root() -> FileResponse:  # pragma: no cover - thin wrapper
        return FileResponse(entry_path, media_type="text/html", headers=_SPA_HTML_HEADERS)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str) -> Response:
        # Never shadow API/WS surface — those routes were registered
        # earlier and FastAPI's matcher tries them first; the catch-all
        # only fires when nothing else claimed the path. We still guard
        # the obvious prefixes defensively so a future router rename
        # cannot silently route /api/foo to index.html.
        for guarded in ("api/", "v1/", "openapi.json", "docs", "redoc", "ws/"):
            if full_path == guarded.rstrip("/") or full_path.startswith(guarded):
                return Response(status_code=404)
        # Direct hits for files that exist verbatim under dist/ (e.g.
        # /favicon.ico, /robots.txt, /sw.js) take precedence over the
        # SPA fallback so the browser gets the real asset, not the HTML.
        direct = (repo_root / "frontend" / "dist" / full_path).resolve()
        try:
            direct.relative_to((repo_root / "frontend" / "dist").resolve())
        except ValueError:
            return Response(status_code=404)
        if direct.is_file():
            return FileResponse(direct, media_type=_MIME_OVERRIDES.get(direct.suffix.lower()))
        # Anything else is a client-router path — return the SPA entry
        # so vue-router can handle it. Same no-cache headers as `/`.
        return FileResponse(entry_path, media_type="text/html", headers=_SPA_HTML_HEADERS)


def _mount_spa_unavailable(app: FastAPI) -> None:
    """Mount a 503 maintenance page for the strict, dist-missing case (F-2(a)).

    Registered as the trailing catch-all (same shape as the normal SPA
    mount) so it only fires for paths no API/WS router claimed. The
    ``/api/*``, ``/v1/*``, ``/openapi.json``, ``/docs``, ``/redoc`` and
    ``/ws/*`` surfaces are guarded to return 404 — exactly like the normal
    fallback — so the running API stays usable while the Web UI is absent.
    """

    # Same no-cache headers as the normal mount: the user might run
    # `Build.bat` and refresh, and we want them to get the real SPA
    # immediately rather than a cached 503 page.
    _UNAVAILABLE_HEADERS = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/", include_in_schema=False)
    async def _spa_root_unavailable() -> HTMLResponse:
        return HTMLResponse(
            content=_DIST_MISSING_HTML,
            status_code=503,
            headers=_UNAVAILABLE_HEADERS,
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback_unavailable(full_path: str) -> Response:
        for guarded in ("api/", "v1/", "openapi.json", "docs", "redoc", "ws/"):
            if full_path == guarded.rstrip("/") or full_path.startswith(guarded):
                return Response(status_code=404)
        return HTMLResponse(
            content=_DIST_MISSING_HTML,
            status_code=503,
            headers=_UNAVAILABLE_HEADERS,
        )

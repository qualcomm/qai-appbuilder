# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
App Builder WebUI — generic FastAPI backbone  (copy, do NOT modify in place)
============================================================================

HOW TO USE
----------
1. Copy this file to  <app_id>/backend/main.py
2. Replace every  <<CHANGE: ...>>  placeholder with app-specific values.
3. Uncomment the /api/infer/upload block if the app accepts file uploads
   (image drag-and-drop, audio recorder, etc.).
4. Do NOT change the boilerplate sections marked  # [BOILERPLATE — keep as-is].

What this file provides out-of-the-box (no editing needed):
  • FastAPI lifespan: pre-loads the model in a background thread at startup
    so the first user request is never blocked for 10-30 s.
  • GET /health          — host readiness probe (must return 200 always).
  • GET /api/model-status — frontend polls this; returns {ready, error}.
  • POST /api/infer       — JSON-body inference, thread-safe via _INFER_LOCK.
  • Static mount + SPA   — serves frontend/ at /.
  • _INFER_LOCK           — serializes QNN calls (qai_appbuilder is NOT
                            thread-safe; concurrent requests corrupt context).
  • Startup env-var logging — logs APP_ROOT / MODEL_ROOT / PACK_ROOT so
    path problems are immediately visible in the host console.

Known pitfalls already handled here (do not re-introduce):
  • Lazy model loading  → first request hangs.  Fixed: lifespan pre-load.
  • Missing _INFER_LOCK → concurrent requests corrupt QNN context silently.
  • bool Form fields    → "false" coerces to True.  See /upload block comment.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# [CHANGE] Import your app's inference module and schemas.
from backend import inference
from backend.schemas import InferRequest, InferResponse   # <<CHANGE: match your schema>>

# ── Logging ───────────────────────────────────────────────────────────────────
# [BOILERPLATE — keep as-is]
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Host-injected env vars ────────────────────────────────────────────────────
# [BOILERPLATE — keep as-is]
# The host sets these when it manages the app. They are EMPTY when the app runs
# standalone (manual run.bat, or the PACKAGED zip on another machine) — that is
# EXPECTED and fine: inference.load_model() must resolve the model/pack dirs via
# a walk-up from its own __file__ that finds the BUNDLED copies inside the
# package (<pkg>/models/<id>/ and <pkg>/pack/<id>/). See the _resolve_dir()
# helper in inference.py. Do NOT "fix" an empty MODEL_ROOT/PACK_ROOT by hard-
# coding an install path — that breaks the packaged app on other machines.
#
# P4 双根（user-imported packs）：APP_BUILDER_USER_MODEL_ROOT /
# APP_BUILDER_USER_PACK_ROOT 是 host 为用户导入的 pack 注入的额外锚点。运行
# 时打包的 zip 不需要它们（zip 内文件通过 ancestor walk 兜底），但开发时的
# host 必须传下去，否则用户 pack 的权重会找不到。inference.load_model 会
# 把 built-in + user 两对 env 都塞给 _resolve_dir 的 4-tier fallback。
APP_ROOT         = os.environ.get("APP_ROOT", "")
APP_PROJECT_ROOT = Path(
    os.environ.get("APP_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent))
)
MODEL_ROOT = os.environ.get("APP_BUILDER_MODEL_ROOT", "")
PACK_ROOT  = os.environ.get("APP_BUILDER_PACK_ROOT",  "")
# P4 user anchors (empty in packaged zip; populated by host for dev-time run).
USER_MODEL_ROOT = os.environ.get("APP_BUILDER_USER_MODEL_ROOT", "")
USER_PACK_ROOT  = os.environ.get("APP_BUILDER_USER_PACK_ROOT",  "")

FRONTEND_DIR = APP_PROJECT_ROOT / "frontend"

# ── Model singleton ───────────────────────────────────────────────────────────
# [BOILERPLATE — keep as-is]
# Loaded ONCE at startup via lifespan; never reloaded per request.
_MODEL       = None
_MODEL_ERROR: str | None = None

# QNN serialization lock.
# qai_appbuilder QNN contexts are NOT thread-safe.  FastAPI runs async route
# handlers in a thread pool (run_in_executor); without this lock, concurrent
# requests corrupt the context and produce silent wrong results or native
# crashes (0xC0000005 access violation, no Python traceback).
# Acquire inside the run_in_executor callback — NOT in the async route body.
_INFER_LOCK = threading.Lock()


# ── Lifespan: pre-load at startup ─────────────────────────────────────────────
# [BOILERPLATE — keep as-is]
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load QNN contexts in a background thread at server startup.

    Using run_in_executor keeps the event loop unblocked so /health responds
    immediately (host readiness probe) while the model is still loading.
    The frontend polls /api/model-status and shows a banner until ready=true.
    """
    global _MODEL, _MODEL_ERROR
    log.info("[startup] APP_ROOT         = %s", APP_ROOT or "(not set)")
    log.info("[startup] MODEL_ROOT       = %s", MODEL_ROOT or "(not set)")
    log.info("[startup] PACK_ROOT        = %s", PACK_ROOT or "(not set)")
    log.info("[startup] USER_MODEL_ROOT  = %s", USER_MODEL_ROOT or "(not set)")
    log.info("[startup] USER_PACK_ROOT   = %s", USER_PACK_ROOT or "(not set)")
    log.info("[startup] APP_PROJECT_ROOT = %s", APP_PROJECT_ROOT)
    log.info("[startup] Loading model …")
    t0 = time.perf_counter()
    loop = asyncio.get_running_loop()   # safe: called inside async context
    try:
        _MODEL = await loop.run_in_executor(
            None,
            lambda: inference.load_model(
                model_root=MODEL_ROOT,
                pack_root=PACK_ROOT,
                user_model_root=USER_MODEL_ROOT,
                user_pack_root=USER_PACK_ROOT,
            ),
        )
        log.info("[startup] Model ready in %.0f ms.", (time.perf_counter() - t0) * 1000)
    except Exception as exc:  # noqa: BLE001
        _MODEL_ERROR = str(exc)
        log.error("[startup] Model load FAILED: %s", exc, exc_info=True)
        # Do NOT re-raise: /health must still return 200 so the host does not
        # loop-restart; /api/infer will surface a clear 503.
    yield
    log.info("[shutdown] App stopping.")


app = FastAPI(
    title="<<CHANGE: your app title>>",   # e.g. "PP-OCRv4 OCR Reader"
    lifespan=lifespan,
)


# ── Internal helpers ──────────────────────────────────────────────────────────
# [BOILERPLATE — keep as-is]
def _get_model():
    """Return the loaded model or raise an appropriate HTTP error."""
    if _MODEL_ERROR:
        raise HTTPException(
            status_code=503,
            detail=f"Model failed to load at startup: {_MODEL_ERROR}",
        )
    if _MODEL is None:
        raise HTTPException(
            status_code=503,
            detail="Model is still loading — please retry in a moment.",
        )
    return _MODEL


# ── Routes ────────────────────────────────────────────────────────────────────

# [BOILERPLATE — keep as-is]
@app.get("/health")
def health():
    """Host readiness probe. Always returns 200; model_ready is advisory."""
    return {"status": "ok", "model_ready": _MODEL is not None, "model_error": _MODEL_ERROR}


# [BOILERPLATE — keep as-is]
@app.get("/api/model-status")
def model_status():
    """Frontend polls this every 1.5 s until ready=true, then stops."""
    return {"ready": _MODEL is not None, "error": _MODEL_ERROR}


# [CHANGE] Adjust InferRequest / InferResponse to match your schemas.py.
@app.post("/api/infer", response_model=InferResponse)
async def infer(req: InferRequest):
    """JSON-body inference endpoint.

    Runs the blocking QNN call in a thread-pool executor so the event loop
    stays unblocked.  _INFER_LOCK serializes concurrent requests.
    """
    model = _get_model()

    def _run():
        with _INFER_LOCK:                       # serialize QNN calls
            return inference.run_inference(model, req)

    try:
        loop = asyncio.get_running_loop()   # safe: called from async route handler
        result = await loop.run_in_executor(None, _run)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:                    # noqa: BLE001
        log.error("[infer] error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


# ── Optional: multipart file-upload endpoint ─────────────────────────────────
# Uncomment and adapt when the frontend sends images/audio via drag-and-drop
# or <input type="file"> (multipart/form-data) instead of base64 JSON.
#
# PITFALL — bool Form fields:
#   HTML forms always send "true"/"false" as plain strings.
#   FastAPI's `bool` coercion only handles "1"/"0"/"on"/"off"; "false" is a
#   non-empty string and coerces to True.
#   Always declare bool-like Form params as `str` and parse manually:
#       rotate: str = Form("true")
#       rotate_bool = rotate.lower() not in ("false", "0", "no", "off")
#
# import base64
# from fastapi import File, Form, UploadFile
#
# @app.post("/api/infer/upload")
# async def infer_upload(
#     file: UploadFile = File(...),
#     language: str = Form("auto"),
#     rotate: str = Form("true"),          # str, NOT bool — see pitfall above
#     det_threshold: float = Form(0.3),
#     rec_threshold: float = Form(0.5),
# ):
#     rotate_bool = rotate.lower() not in ("false", "0", "no", "off")
#     data = await file.read()
#     b64  = base64.b64encode(data).decode("ascii")
#     req  = InferRequest(                 # <<CHANGE: match your InferRequest>>
#         image_b64=b64,
#         language=language,
#         rotate=rotate_bool,
#         det_threshold=det_threshold,
#         rec_threshold=rec_threshold,
#     )
#     model = _get_model()
#     def _run():
#         with _INFER_LOCK:
#             return inference.run_inference(model, req)
#     loop = asyncio.get_running_loop()
#     try:
#         return await loop.run_in_executor(None, _run)
#     except ValueError as exc:
#         raise HTTPException(status_code=400, detail=str(exc)) from exc
#     except Exception as exc:
#         raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── Static files + SPA ────────────────────────────────────────────────────────
# [BOILERPLATE — keep as-is]
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


# ── Manual-run convenience ────────────────────────────────────────────────────
# [BOILERPLATE — keep as-is]
# The host launches uvicorn externally; this block is only for run.bat / debug.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=os.environ.get("APP_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_PORT", "8000")),
        reload=False,
    )

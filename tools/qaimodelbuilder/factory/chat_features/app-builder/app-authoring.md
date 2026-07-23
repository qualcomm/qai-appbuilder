# App Builder · Fullstack App Authoring (reference template)

> **On-demand reference** (NOT auto-injected). Read with `${APP_ROOT}/factory/chat_features/app-builder/app-authoring.md` when generating an app. Copy templates below, adapt to model schema.

Goal: produce a **complete, runnable fullstack project** — **FastAPI backend + pure HTML/CSS/JS frontend** (no Vue, no build step) — at `${APP_ROOT}/data/app_builder/<app_id>/`. In-process inference via `qai_appbuilder`; does NOT call host `/api/app-builder/runs`. Host runs/previews/stops/packages.

> **Absolute-path rule.** Tool CWD = user WORKSPACE, not install dir. ALL paths MUST use `${APP_ROOT}/...`. Bare relative `data/app_builder/...` lands in wrong place.
> **Directory name.** `${APP_ROOT}/data/app_builder/<app_id>/` (WITH underscore). NEVER `data/appbuilder/` (dead).

---

## 1. Project directory structure

Write the following under `${APP_ROOT}/data/app_builder/<app_id>/`. Files marked
"optional" may be omitted; the rest are required.

```text
${APP_ROOT}/data/app_builder/<app_id>/
  app.yaml                    # app metadata (host entry point; see §2)
  README.md                   # how to run, models, I/O, limitations
  run.bat                     # Windows manual run entry (ASCII/English only)
  start.bat                   # Windows debug restart entry; kills old listener on port, then starts app
  run.ps1                     # optional; English-only content
  requirements.txt            # declared deps; the startup check installs any missing
  vendor/                     # optional; offline wheels (whl/) + per-pack runtime data
  backend/
    main.py                   # ← COPY from _webui/backend/main_base.py, then edit
    ensure_deps.py            # ← COPY from _webui/backend/ensure_deps_base.py as-is
    inference.py              # model load / preprocess / infer / postprocess
    model_refs.py             # model paths, weight paths, manifest-derived config
    schemas.py                # Pydantic request/response models
    utils/                    # copied/adapted helpers as needed
      qnn_helper.py           # adapt factory/chat_features/app-builder/shared/qnn_helper.py
      io_validator.py         # adapt; prevents native crashes
      audio_io.py             # audio models only
      image_io.py             # image models only
      telemetry.py            # optional perf metrics
  frontend/
    index.html
    app.js
    styles.css                # ← COPY from _webui/frontend/base.css, then extend
    model_poll.js             # ← COPY from _webui/frontend/model_poll.js as-is
  static/                     # optional runtime static assets (sample images)
  uploads/                    # optional; the app's own upload dir
  outputs/                    # optional; the app's own output dir
  logs/                       # optional; server.log
```

`app_id` rules: lowercase letters/digits/dash/underscore only, must start with
an alphanumeric, length 2–64, derived from the user request + primary model
(e.g. `melotts-tts-demo`). If the dir already exists, MODIFY it in place — do
not create `-copy` / `-new` unless the user explicitly asks.

### Pre-built shared components (COPY, do not rewrite)

| Source | Copy to | Action |
|---|---|---|
| `_webui/backend/main_base.py` | `backend/main.py` | Replace `<<CHANGE>>` placeholders; uncomment upload block if needed |
| `_webui/backend/ensure_deps_base.py` | `backend/ensure_deps.py` | As-is (no edits) |
| `_webui/frontend/base.css` | `frontend/styles.css` | Append app rules below `── App-specific styles ──` marker |
| `_webui/frontend/model_poll.js` | `frontend/model_poll.js` | As-is (no edits) |

---

## 2. `app.yaml` (required — host entry point)

Host treats a dir as an App Builder app **only** if it has a valid `app.yaml` (used for listing/running/packaging). Minimum schema:

```yaml
schema_version: 1
id: melotts-tts-demo              # MUST equal the dir name; regex ^[a-z0-9][a-z0-9_-]{1,63}$
name: MeloTTS Chinese TTS WebUI
description: Enter Chinese text and play the synthesized speech.
created_at: "2026-07-08T20:00:00+08:00"
updated_at: "2026-07-08T20:00:00+08:00"
models:
  # Case 1 — BUILT-IN pack (factory/chat_features/app-builder/models/<id>/):
  - id: melotts-zh
    title: MeloTTS (Chinese)
    builtin: true
    pack_dir: "${APP_ROOT}/factory/chat_features/app-builder/models/melotts-zh"
    model_dir: "${APP_ROOT}/models/melotts-zh"
  # Case 2 — USER-IMPORTED pack: use user roots; note extra models/ layer in model_dir.
  - id: inception-v3
    title: Inception v3
    builtin: false
    pack_dir: "${APP_ROOT}/data/app_builder/user_models/inception-v3"
    model_dir: "${APP_ROOT}/data/app_builder/user_model_weights/models/inception-v3"
entry:
  app_module: backend.main:app
  health_path: /health
  frontend_path: /
runtime:
  python: current_venv
  host: 127.0.0.1
  preferred_port: null
package:
  include_models: true
  include_outputs: false
```

Rules:

- `id` MUST equal directory name (host keys off it).
- `models[].pack_dir` / `model_dir` — absolute `${APP_ROOT}/...`. Pick layout by origin:
  - `builtin: true` → `factory/chat_features/app-builder/models/<id>/` + `models/<id>/`
  - `builtin: false` → `data/app_builder/user_models/<id>/` + `data/app_builder/user_model_weights/models/<id>/` (extra `models/` layer)
- `entry.app_module`: `backend.main:app`; `health_path`: `/health`; `frontend_path`: `/`.
- `runtime.python: current_venv`; `preferred_port: null` (host allocates).
- **On edit: update `updated_at`** (+ changed `models[]`).

---

## 3. `backend/main.py` (FastAPI backbone)

**Copy `${APP_ROOT}/factory/chat_features/app-builder/_webui/backend/main_base.py` → `backend/main.py`**, then ONLY:
1. Replace `<<CHANGE>>` import lines with your `InferRequest`/`InferResponse`.
2. Set `title=` in `FastAPI(...)`.
3. If file-upload needed: uncomment `/api/infer/upload` block + adjust constructor.

Everything else (`lifespan`, `_INFER_LOCK`, `/health`, `/api/model-status`, `run_in_executor`, static mount, env-var logging) is correct — do NOT rewrite.

Env vars the host injects at launch:

- `APP_ROOT` — the install directory.
- `APP_PROJECT_ROOT` — `${APP_ROOT}/data/app_builder/<app_id>`.
- `APP_BUILDER_MODEL_ROOT` — `${APP_ROOT}/models` (built-in pack weights).
- `APP_BUILDER_PACK_ROOT` — `${APP_ROOT}/factory/chat_features/app-builder/models` (built-in pack manifests / assets).
- `APP_BUILDER_USER_MODEL_ROOT` — `${APP_ROOT}/data/app_builder/user_model_weights` (P4; user-imported pack weights). **Note the extra `models/` layer**: real files live at `${APP_BUILDER_USER_MODEL_ROOT}/models/<id>/<bin>`.
- `APP_BUILDER_USER_PACK_ROOT` — `${APP_ROOT}/data/app_builder/user_models` (P4; user-imported pack manifests / assets). No extra layer: `${APP_BUILDER_USER_PACK_ROOT}/<id>/manifest.json`.
- `PYTHONPATH` includes the app dir and `${APP_ROOT}/src`.

A given pack lives in **exactly one** anchor pair (built-in OR user), never both. Your `_resolve_dir()` helper (§4) probes both so the app works regardless of pack origin.

---

## 4. `backend/inference.py` (load / preprocess / infer / postprocess)

Structure: `load_model()` (once, at startup) + `run_inference()` (per request). Import `qai_appbuilder` from the current venv.

- **Built-in models**: copy/adapt pack's `runner.py` + shared helpers (`qnn_helper.py`, `io_validator.py`, `audio_io.py`, `image_io.py`) into `backend/utils/`. Comment the source. Do NOT modify originals.
- **Custom models**: read the pack's `manifest.json`, `runner.py`, `io_contract`, `assets/`, `weights/` → implement real pre/post yourself. Use `io_validator` to validate shapes/dtypes (avoids native crashes). If the runner is a generic fallback, state the limitation in README — never fake a result.

```python
"""In-process inference for the app. Adapts the pack runner + shared helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

# NEVER import qai_appbuilder directly in the WebUI backend.
# Always use qnn_helper.QnnContext which wraps the lazy import, ensures
# QNNConfig.Config() is called only once per process, and gives clear
# errors when the on-device QNN venv is not active.
# Runtime backend varies per host arch: ARM64 (WoS) uses HTP/NPU; x64
# (Intel/AMD) falls back to CPU/DLC — no NPU acceleration. See
# docs/85-tasks/x64-windows-support-plan.md §9.
from backend.schemas import InferRequest, InferResponse
from backend.utils import qnn_helper          # adapt shared/qnn_helper.py
from backend.utils.qnn_helper import QnnContext


@dataclass
class LoadedModel:
    ctx: object            # QNNContext / QnnContext wrapper (loaded once)
    model_dir: Path


# ── Model resolution: P4 dual-root + packaged distribution ──────────────
# Built-in: MODEL_ROOT/<id>/  + PACK_ROOT/<id>/
# User:     USER_MODEL_ROOT/models/<id>/  + USER_PACK_ROOT/<id>/  (note extra "models/" layer)
# _resolve_dir: 4-tier resolution for model/pack dirs.
# Tier 1a: $BUILTIN_ENV/<id>  (dev host, built-in)
# Tier 1b: $USER_ENV/<user_extra>/<id>  (dev host, user-imported)
# Tier 2:  Walk up from __file__ looking for <arcname_sub>/<id> (packaged zip)
# Tier 3:  Last-resort path for clear FileNotFoundError
# After packaging, both flavours bundle as <pkg>/models/<id>/ + <pkg>/pack/<id>/
# so tier-2 walk-up is uniform. Copy this helper verbatim.
    builtin_env: str,
    user_env: str,
    model_id: str,
    user_extra: str,   # "models" for weights, "" for pack
    arcname_sub: str,  # "models" or "pack" (matches packager arcnames)
) -> Path:
    """Resolve a model or pack directory — probes built-in env, user env,
    then the bundled zip layout via ancestor walk."""
    # Tier 1a: built-in host env (e.g. APP_BUILDER_MODEL_ROOT/<id>).
    if builtin_env:
        p = Path(builtin_env) / model_id
        if p.is_dir():
            return p
    # Tier 1b: user env (extra "models/" layer for weights).
    if user_env:
        p = Path(user_env)
        if user_extra:
            p = p / user_extra
        p = p / model_id
        if p.is_dir():
            return p
    # Tier 2: ancestor walk (finds bundled zip layout).
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / arcname_sub / model_id
        if candidate.is_dir():
            return candidate
    # Tier 3: last-resort for meaningful FileNotFoundError path.
    if builtin_env:
        return Path(builtin_env) / model_id
    if user_env:
        p = Path(user_env)
        if user_extra:
            p = p / user_extra
        return p / model_id
    return Path(".") / model_id
def load_model(
    *,
    model_root: str = "",
    pack_root: str = "",
    user_model_root: str = "",
    user_pack_root: str = "",
) -> LoadedModel:
    """Load QNN contexts ONCE at startup. Pass all 4 env vars via os.environ.get();
    _resolve_dir picks the correct anchor."""
    # <<CHANGE: replace "melotts-zh" with your model_id; the "models"/"pack"
    #  and user_extra literals below are FIXED and match the packager output.>>
    model_dir = _resolve_dir(
        builtin_env=model_root,
        user_env=user_model_root,
        model_id="melotts-zh",
        user_extra="models",   # weights: user layout adds "models/" layer
        arcname_sub="models",  # packaged zip uses <pkg>/models/<id>/
    )
    pack_dir = _resolve_dir(
        builtin_env=pack_root,
        user_env=user_pack_root,
        model_id="melotts-zh",
        user_extra="",         # pack: user layout has no extra layer
        arcname_sub="pack",    # packaged zip uses <pkg>/pack/<id>/
    )
    # QNNConfig.Config is process-global; call once (see qnn_helper for the
    # guarded pattern). Load each .bin as a QNNContext.
    ctx = qnn_helper.QnnContext.load(model_dir / "encoder.bin", runtime="Htp")
    return LoadedModel(ctx=ctx, model_dir=model_dir)


def run_inference(model: LoadedModel, req: InferRequest) -> InferResponse:
    """Preprocess -> infer on NPU -> postprocess. Raise ValueError on bad input."""
    text = (req.text or "").strip()
    if not text:
        raise ValueError("Input text is required.")
    # 1. Preprocess (e.g. G2P / tokenization / image resize). Validate with
    #    io_validator to keep tensors within the model's contract.
    # 2. Run NPU inference via the loaded context(s).
    # 3. Postprocess to a final artifact (write WAV/PNG under outputs/, or
    #    return decoded text).
    # Return a response the frontend can render.
    return InferResponse(ok=True, text="...", audio_path=None, metrics={})
```

---

## 5. `backend/schemas.py` (Pydantic models)

```python
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class InferRequest(BaseModel):
    text: Optional[str] = None
    # Add fields matching the model schema, e.g.:
    # image_b64: Optional[str] = None
    speed: float = 1.0


class InferResponse(BaseModel):
    ok: bool
    text: Optional[str] = None
    audio_path: Optional[str] = None   # path served by the app's own static/outputs
    image_path: Optional[str] = None
    metrics: dict[str, Any] = {}
```

---

## 6. Pure HTML/JS frontend (same-origin, calls OWN backend at `/api/infer`)

Dark card-based layout: input panel + output panel + perf area.

### `frontend/styles.css`

**Copy** `_webui/frontend/base.css` → `frontend/styles.css`. Append app-specific rules below the `── App-specific styles ──` marker. Do NOT remove/override boilerplate above it (especially `[hidden] { display: none !important; }` and `.img-wrap`).


### `frontend/model_poll.js`

**Copy** `_webui/frontend/model_poll.js` → `frontend/model_poll.js` as-is (no edits). Load **before** `app.js` in `index.html`:
```html
<script src="/static/model_poll.js"></script>
<script src="/static/app.js"></script>
```

Use `_modelReady` (from `model_poll.js`) to guard the Run button:
```javascript
} finally {
  btn.disabled = !_modelReady;   // re-enable only if model is ready
}
```

### `frontend/index.html`

Write fresh per app. Required elements for `model_poll.js`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title><<App title>></title>
<link rel="stylesheet" href="/static/styles.css" />
</head>
<body>
  <div class="wrap">
    <!-- model_poll.js shows/hides this banner automatically -->
    <div id="modelBanner" class="model-banner" hidden>
      <span class="banner-spinner"></span>
      <span id="modelBannerText">Model loading, please wait (~10-30 s on first start)…</span>
    </div>

    <h1><<App title>></h1>
    <div class="grid">
      <section class="card">
        <h2>Input</h2>
        <!-- app-specific input controls here -->
        <div class="row">
          <!-- disabled initially; model_poll.js enables it when ready -->
          <button id="runBtn" disabled>Run</button>
          <span id="status" class="status"></span>
        </div>
      </section>
      <section class="card">
        <h2>Output</h2>
        <div id="output" class="out">No result yet.</div>
        <div id="media"></div>
        <div id="perf" class="perf"></div>
      </section>
    </div>
  </div>
  <script src="/static/model_poll.js"></script>
  <script src="/static/app.js"></script>
</body>
</html>
```

### `frontend/app.js`

Write this file fresh for each app. It handles user interaction and calls
`/api/infer`. Minimal skeleton:

```javascript
const $ = (id) => document.getElementById(id);

function setStatus(msg, kind) {
  const s = $('status');
  s.textContent = msg || '';
  s.className = 'status' + (kind ? ' ' + kind : '');
}

async function run() {
  const btn = $('runBtn');
  btn.disabled = true;
  $('output').textContent = '';
  $('media').innerHTML = '';
  $('perf').textContent = '';
  setStatus('Running…');
  try {
    const res = await fetch('/api/infer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ /* <<CHANGE: your request fields>> */ }),
    });
    if (!res.ok) {
      let detail = 'Request failed (' + res.status + ')';
      try { const j = await res.json(); if (j.detail) detail = j.detail; } catch (_) {}
      throw new Error(detail);
    }
    const data = await res.json();
    renderResult(data);
    setStatus('Done', 'ok');
  } catch (e) {
    setStatus(e.message, 'err');
    $('output').textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = !_modelReady;   // _modelReady is set by model_poll.js
  }
}

function renderResult(data) {
  if (data.metrics) $('perf').textContent = JSON.stringify(data.metrics);

  if (data.audio_path) {
    const el = document.createElement('audio');
    el.controls = true;
    el.src = data.audio_path;
    $('media').appendChild(el);
  }

  if (data.image_path) {
    // Always wrap result images in .img-wrap (defined in base.css).
    // Do NOT use bare <img max-width:100%> — it clips in flex/grid containers.
    const wrap = document.createElement('div');
    wrap.className = 'img-wrap';
    const el = document.createElement('img');
    el.src = data.image_path;
    el.alt = 'result';
    wrap.appendChild(el);
    $('media').appendChild(wrap);
  }

  $('output').textContent = data.text || JSON.stringify(data, null, 2);
}

$('runBtn').addEventListener('click', run);
```

---

## 7. `run.bat`, `start.bat`, and `README.md`

### `run.bat` (Windows manual run — ASCII/English ONLY)

Keep `.bat` content **ASCII / English-only** (Chinese corrupts under OEM encoding).

**CRITICAL — P4 env wiring.** Host UI injects `APP_BUILDER_MODEL_ROOT` / `PACK_ROOT` / `USER_MODEL_ROOT` / `USER_PACK_ROOT`. Manual double-click → env empty → `_resolve_dir` tier-1 skips. Built-in packs: tier-2 walk-up finds `<repo>/models/<id>/` — works. User-imported packs: weights at `<repo>/data/app_builder/user_model_weights/models/<id>/` (extra `models/` layer, NOT reachable by walk-up). **Fix**: `run.bat` derives repo root from `%~dp0` (3 levels up) and exports all 4 anchors with `if exist` guards (on foreign machine dirs absent → env empty → tier-2 walk-up finds bundled layout).


```bat
@echo off
REM Manual run for this App Builder app. Prefer the host UI (Apps menu).
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Guarded exports: on dev host these exist (tier-1a/1b hit); on foreign
REM machine they don't, env stays empty, tier-2 walk-up finds bundled layout.
set "REPO_ROOT=%~dp0..\..\.."
if exist "%REPO_ROOT%\models\" set "APP_BUILDER_MODEL_ROOT=%REPO_ROOT%\models"
if exist "%REPO_ROOT%\factory\chat_features\app-builder\models\" set "APP_BUILDER_PACK_ROOT=%REPO_ROOT%\factory\chat_features\app-builder\models"
if exist "%REPO_ROOT%\data\app_builder\user_model_weights\" set "APP_BUILDER_USER_MODEL_ROOT=%REPO_ROOT%\data\app_builder\user_model_weights"
if exist "%REPO_ROOT%\data\app_builder\user_models\" set "APP_BUILDER_USER_PACK_ROOT=%REPO_ROOT%\data\app_builder\user_models"

REM Prefer QAI ModelBuilder host-arch venv; fall back to PATH python.
REM Priority: 1) data\config\host_arch file  2) PROCESSOR_ARCHITECTURE detection
REM ensure_deps.py self-heals missing packages.
set "VENV_NAME=.venv_arm64_313"
if exist "%REPO_ROOT%\data\config\host_arch" (
    set /p _HA=<"%REPO_ROOT%\data\config\host_arch"
    if /i "!_HA!"=="x64" set "VENV_NAME=.venv_x64_313"
) else (
    if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64" set "VENV_NAME=.venv_x64_313"
    if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64" set "VENV_NAME=.venv_x64_313"
)
set "PY=%LOCALAPPDATA%\QAIModelBuilder\envs\%VENV_NAME%\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM Startup dep check — abort on failure rather than starting broken server.
"%PY%" -m backend.ensure_deps
if errorlevel 1 (
    echo.
    echo [run] Dependency check failed. See the message above. Aborting.
    exit /b 1
)

"%PY%" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### `start.bat` (Windows debug restart — ASCII/English ONLY)

MUST include. Accepts optional port arg, kills existing listener on that port, prints steps, starts uvicorn. ASCII/English-only. Derive `APP_ROOT` from `%~dp0..\..\..` (never hard-code).

### `run.ps1` (optional PowerShell equivalent)

Same anchor logic as `run.bat`. **No `Resolve-Path`** (throws on non-existent/cross-volume paths). No `$ErrorActionPreference='Stop'` for probes (missing anchor = normal fallback). Guard exports with `-PathType Container`:


```powershell
Set-Location -LiteralPath $PSScriptRoot
# Join without canonicalising — Resolve-Path would throw on foreign-machine
# unpacked layouts where the ancestor path does not exist / crosses volumes.
$RepoRoot = Join-Path $PSScriptRoot '..\..\..'
$anchors = @{
    APP_BUILDER_MODEL_ROOT       = Join-Path $RepoRoot 'models'
    APP_BUILDER_PACK_ROOT        = Join-Path $RepoRoot 'factory\chat_features\app-builder\models'
    APP_BUILDER_USER_MODEL_ROOT  = Join-Path $RepoRoot 'data\app_builder\user_model_weights'
    APP_BUILDER_USER_PACK_ROOT   = Join-Path $RepoRoot 'data\app_builder\user_models'
}
foreach ($k in $anchors.Keys) {
    if (Test-Path -LiteralPath $anchors[$k] -PathType Container) {
        Set-Item -Path "env:$k" -Value $anchors[$k]
    }
}
# Prefer host-arch runtime venv (.venv_arm64_313 or .venv_x64_313);
# Priority: 1) data\config\host_arch file  2) PROCESSOR_ARCHITECTURE detection.
# fall back to PATH python. ensure_deps.py self-heals missing packages.
$HostArchFile = Join-Path $RepoRoot 'data\config\host_arch'
if (Test-Path -LiteralPath $HostArchFile -PathType Leaf) {
    $ha = (Get-Content -LiteralPath $HostArchFile -First 1).Trim().ToLower()
    $VenvName = if ($ha -eq 'x64') { '.venv_x64_313' } else { '.venv_arm64_313' }
} else {
    $VenvName = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { '.venv_arm64_313' } else { '.venv_x64_313' }
}
$Py = "$env:LOCALAPPDATA\QAIModelBuilder\envs\$VenvName\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { $Py = "python" }
# Startup dependency check (reads requirements.txt, installs missing, runs pack
# predeploy hooks). Abort on failure with the message it printed.
& $Py -m backend.ensure_deps
if ($LASTEXITCODE -ne 0) { Write-Host "[run] Dependency check failed. Aborting."; exit 1 }
& $Py -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### `run.sh` (optional POSIX equivalent — for Linux/macOS reference distributions)

```sh
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
# Do NOT `set -e` around the anchor probes — a missing anchor dir is a
# normal fallback, not a fatal error. `cd ../../..` never fails on POSIX
# (`/..` resolves to `/`), so this stays inside `set -e` safely.
REPO_ROOT="$(cd ../../.. && pwd)"
[ -d "$REPO_ROOT/models" ] && export APP_BUILDER_MODEL_ROOT="$REPO_ROOT/models"
[ -d "$REPO_ROOT/factory/chat_features/app-builder/models" ] && export APP_BUILDER_PACK_ROOT="$REPO_ROOT/factory/chat_features/app-builder/models"
[ -d "$REPO_ROOT/data/app_builder/user_model_weights" ] && export APP_BUILDER_USER_MODEL_ROOT="$REPO_ROOT/data/app_builder/user_model_weights"
[ -d "$REPO_ROOT/data/app_builder/user_models" ] && export APP_BUILDER_USER_PACK_ROOT="$REPO_ROOT/data/app_builder/user_models"
# Startup dependency check: reads requirements.txt, installs anything missing
# (preferring vendor/whl/ wheels), runs each pack's predeploy hook. Abort on
# failure rather than starting a server that would fail on first request.
python3 -m backend.ensure_deps || { echo "[run] Dependency check failed. Aborting."; exit 1; }
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### `README.md`

Write a README in the app dir covering: how to run (host UI or manual `run.bat`), required model IDs + weight paths, input/output format, known limitations, and a **packaging note**: the zip is NOT fully offline — target needs a QAI ModelBuilder Python env (Python + `qai_appbuilder` + QNN runtime; zip does not bundle them).

---

## 8. Constraints recap

- **`GET /health`** → `{"status": "ok"}` (host readiness probe; two consecutive successes = ready).
- **Must NOT `webbrowser.open()`** — host opens browser after readiness.
- **Singleton model, pre-loaded at startup.** Use `lifespan` + `run_in_executor` background thread (NOT lazy on first request — blocks 10-30 s). Expose `GET /api/model-status` for frontend poll + loading banner.
- **Do NOT modify pack originals** (`factory/chat_features/app-builder/models/<id>/` or `models/<id>/`). Copy/adapt into `backend/utils/`.
- **Frontend calls its OWN backend** (same origin `/api/infer`), NOT host `/api/app-builder/runs`.
- **Deps in `requirements.txt`; `ensure_deps.py` installs them.** Target machine may lack the QAI venv — list ALL runtime deps (incl. `qai_appbuilder`, `numpy`, G2P/audio libs). `ensure_deps.py` (copied from `_webui/backend/ensure_deps_base.py`) merges app-level + each bundled `pack/<id>/requirements.txt` (app pins win), `find_spec`-checks, pip-installs missing (preferring `vendor/whl/`), writes sentinel for warm-start skip, then runs each pack's optional `predeploy.py`. Do NOT scatter `pip install` in app code. (Supersedes old "no auto pip" rule — packaged apps need it.)
- **Absolute paths only**: `${APP_ROOT}/...` form; app dir = `${APP_ROOT}/data/app_builder/<app_id>/` (underscore).
- **QNN not thread-safe — use `_INFER_LOCK`.** `threading.Lock()` around every `ctx.run()`. Acquire INSIDE `run_in_executor` callback, not outside.
- **`bool` Form params are strings.** `"false"` coerces to `True` in FastAPI `bool`. Receive as `str`, parse with `val.lower() not in ("false","0","no","off")`.
- **CSS `[hidden]` override (critical).** Add `[hidden] { display: none !important; }` — without it, `display:flex` etc. silently overrides HTML `hidden` attribute.
- **Poll timer order.** `_pollTimer = setInterval(...)` BEFORE calling `pollModelStatus()` — otherwise async early-resolve leaves interval alive, banner permanent.
- **Image containers.** `width:100%; height:auto` on `<img>`, wrapped in `.img-wrap` (`width:100%; overflow:hidden`). NOT `max-width:100%` (no effect without explicit container width → overflow/clip).

# App Builder · Fullstack App Authoring (reference template)

> **This file is a REFERENCE the Agent reads on demand** (via the `read` tool)
> while generating a standalone fullstack app in App Builder mode. It is NOT
> auto-injected into every prompt (only the top-level
> `factory/app_builder/SKILL.md` is). Read it — with the ABSOLUTE path
> `${APP_ROOT}/factory/app_builder/fullstack-authoring.SKILL.md` — when the user
> asks you to build/generate an app around a selected model. Copy the templates
> below and adapt them to the model's schema.

The goal: produce a **complete, runnable fullstack project** — a **FastAPI
(Python) backend + pure HTML/CSS/JS frontend** (no Vue, no build step) — written
to the ABSOLUTE path `${APP_ROOT}/data/app_builder/<app_id>/`. The app does its
own **on-device inference in-process** via `qai_appbuilder` and the project
shared helpers. It does **NOT** call the host `/api/app-builder/runs`. The host
(not the agent) runs, previews, stops, and packages the app.

> **Absolute-path rule (critical).** `${APP_ROOT}` and `${WORKSPACE}` are
> placeholders substituted at prompt-build time. Your tool working directory is
> the user **WORKSPACE**, not the install dir. Therefore **every path where you
> write app code** and **every reference file you read** MUST use the ABSOLUTE
> `${APP_ROOT}/...` form. A bare relative `data/app_builder/...` would land in
> the wrong place.
>
> **Directory name (critical).** The runtime app dir is
> `${APP_ROOT}/data/app_builder/<app_id>/` — WITH the underscore. NEVER
> `data/appbuilder/` (legacy dead directory).

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
  requirements.txt            # minimal deps; prefer the current venv (often empty)
  backend/
    main.py                   # ← COPY from _webui/backend/main_base.py, then edit
    inference.py              # model load / preprocess / infer / postprocess
    model_refs.py             # model paths, weight paths, manifest-derived config
    schemas.py                # Pydantic request/response models
    utils/                    # copied/adapted helpers as needed
      qnn_helper.py           # adapt factory/app_builder/shared/qnn_helper.py
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

### Pre-built shared components (copy, do not rewrite)

Three files under `${APP_ROOT}/factory/app_builder/_webui/` are ready-made and
contain all the boilerplate + known-pitfall fixes. **Copy them directly** instead
of regenerating the code from scratch — this saves tokens and avoids re-introducing
fixed bugs.

| Source file | Copy to | What to do after copying |
|---|---|---|
| `_webui/backend/main_base.py` | `backend/main.py` | Replace every `<<CHANGE: …>>` placeholder; uncomment the `/api/infer/upload` block if the app needs file upload |
| `_webui/frontend/base.css` | `frontend/styles.css` | Append app-specific rules below the `── App-specific styles ──` marker; do NOT remove the boilerplate above it |
| `_webui/frontend/model_poll.js` | `frontend/model_poll.js` | Copy as-is — no edits needed |

All three files contain inline comments explaining every pitfall they fix.
Refer to §8 Constraints recap for the summary.

---

## 2. `app.yaml` (required — the host entry point)

The host only treats a directory with a valid `app.yaml` as an App Builder app;
it reads this file for listing, running, and packaging (it does NOT guess the
directory contents). Minimum schema:

```yaml
schema_version: 1
id: melotts-tts-demo              # MUST equal the dir name; regex ^[a-z0-9][a-z0-9_-]{1,63}$
name: MeloTTS Chinese TTS WebUI
description: Enter Chinese text and play the synthesized speech.
created_at: "2026-07-08T20:00:00+08:00"
updated_at: "2026-07-08T20:00:00+08:00"
models:
  # Case 1 — BUILT-IN pack (factory/app_builder/models/<id>/):
  - id: melotts-zh
    title: MeloTTS (Chinese)
    builtin: true
    pack_dir: "${APP_ROOT}/factory/app_builder/models/melotts-zh"
    model_dir: "${APP_ROOT}/models/melotts-zh"
  # Case 2 — USER-IMPORTED pack (P4; imported via QAI ModelBuilder):
  # Use the user roots for BOTH pack_dir and model_dir. Note the extra
  # `models/` layer in model_dir (matches manifest `installPath` convention).
  # If unsure whether a pack is built-in or user, check whether
  # `${APP_ROOT}/factory/app_builder/models/<id>/manifest.json` exists on disk;
  # if not, use the user paths below.
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

- `id` MUST equal the directory name; all host APIs key off this id.
- `models[].pack_dir` and `models[].model_dir` use the ABSOLUTE `${APP_ROOT}/...`
  form. **Pick the right layout for the pack's origin** (see two cases above):
  - `builtin: true` → `factory/app_builder/models/<id>/` (pack) +
    `models/<id>/` (weights)
  - `builtin: false` (user-imported) → `data/app_builder/user_models/<id>/`
    (pack) + `data/app_builder/user_model_weights/models/<id>/` (weights)
    — note the extra `models/` layer in model_dir.
  The packager has a defensive fallback that will attempt the user layout
  if a built-in path is missing, but writing the correct layout up-front
  produces cleaner package_manifest.json and avoids warnings.
- `entry.app_module` is the import path uvicorn launches (`backend.main:app`);
  `health_path` must match the `/health` route; `frontend_path` is where the UI
  is served (`/`).
- `runtime.python: current_venv` means the host runs the app in the current QAI
  ModelBuilder venv; `preferred_port: null` lets the host allocate a port.
- **Whenever you EDIT an existing app, update `updated_at`** (and any changed
  metadata such as `models[]`).

---

## 3. `backend/main.py` (FastAPI backbone)

**Copy `${APP_ROOT}/factory/app_builder/_webui/backend/main_base.py` to
`backend/main.py`**, then make only these targeted edits:

1. Replace the two import lines marked `<<CHANGE>>` with your app's actual
   `InferRequest` / `InferResponse` schema names.
2. Set the `title=` string in `FastAPI(...)` to your app name.
3. If the app accepts file uploads (image drag-and-drop, audio recorder),
   uncomment the `/api/infer/upload` block and adjust the `InferRequest`
   constructor call inside it.

Everything else — `lifespan` pre-load, `_INFER_LOCK`, `/health`,
`/api/model-status`, `run_in_executor`, static mount, env-var logging — is
already correct and must **not** be rewritten.

Env vars the host injects at launch:

- `APP_ROOT` — the install directory.
- `APP_PROJECT_ROOT` — `${APP_ROOT}/data/app_builder/<app_id>`.
- `APP_BUILDER_MODEL_ROOT` — `${APP_ROOT}/models` (built-in pack weights).
- `APP_BUILDER_PACK_ROOT` — `${APP_ROOT}/factory/app_builder/models` (built-in pack manifests / assets).
- `APP_BUILDER_USER_MODEL_ROOT` — `${APP_ROOT}/data/app_builder/user_model_weights` (P4; user-imported pack weights). **Note the extra `models/` layer**: real files live at `${APP_BUILDER_USER_MODEL_ROOT}/models/<id>/<bin>`.
- `APP_BUILDER_USER_PACK_ROOT` — `${APP_ROOT}/data/app_builder/user_models` (P4; user-imported pack manifests / assets). No extra layer: `${APP_BUILDER_USER_PACK_ROOT}/<id>/manifest.json`.
- `PYTHONPATH` includes the app dir and `${APP_ROOT}/src`.

A given pack lives in **exactly one** anchor pair (built-in OR user), never both. Your `_resolve_dir()` helper (§4) probes both so the app works regardless of pack origin.

---

## 4. `backend/inference.py` (load / preprocess / infer / postprocess)

Structure the inference into `load_model()` (once) and `run_inference()` (per
request). Import `qai_appbuilder` from the current venv.

**Built-in models.** You MAY copy/adapt the pack's `runner.py` logic and the
shared helpers (`qnn_helper.py`, `io_validator.py`, `audio_io.py`,
`image_io.py`) into the app's `backend/utils/`. Keep a comment noting the
source, e.g. `# adapted from factory/app_builder/models/melotts-zh/runner.py`.
Do NOT modify the pack originals.

**Custom models.** You MUST read the pack's `manifest.json`, `runner.py`,
`io_contract`, `assets/`, and `weights/` and implement preprocessing /
postprocessing yourself. Use `io_validator` (or equivalent) to validate tensor
shapes/dtypes and avoid native crashes. If the pack's runner is a generic
fallback (returns only tensor stats, or lacks a tokenizer / postprocessing),
implement real pre/post yourself and **state the limitation honestly** in the
README — never fake a "working" result.

```python
"""In-process inference for the app. Adapts the pack runner + shared helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

# NEVER import qai_appbuilder directly in the WebUI backend.
# Always use qnn_helper.QnnContext which wraps the lazy import, ensures
# QNNConfig.Config() is called only once per process, and gives clear errors
# when the ARM64 QNN venv is not active.
from backend.schemas import InferRequest, InferResponse
from backend.utils import qnn_helper          # adapt shared/qnn_helper.py
from backend.utils.qnn_helper import QnnContext


@dataclass
class LoadedModel:
    ctx: object            # QNNContext / QnnContext wrapper (loaded once)
    model_dir: Path


# ── CRITICAL: self-contained model resolution (P4 双根 + 打包分发) ────────────
# Packs may originate from EITHER anchor pair (a given pack lives in exactly one):
#
#   built-in :  APP_BUILDER_MODEL_ROOT/<id>/<bin>          (weights)
#               APP_BUILDER_PACK_ROOT/<id>/manifest.json   (pack meta)
#   user     :  APP_BUILDER_USER_MODEL_ROOT/models/<id>/<bin>  ← extra "models/" layer!
#               APP_BUILDER_USER_PACK_ROOT/<id>/manifest.json
#
# After packaging, both flavours bundle under the SAME arcnames inside the zip:
#   <pkg>/models/<id>/<bin>   +   <pkg>/pack/<id>/manifest.json
# so the ancestor-walk fallback (§tier 3 below) is uniform.
#
# When the app runs standalone (no host env), APP_BUILDER_*_ROOT vars are NOT set,
# so you MUST NOT use a bare Path(env_root) / "<id>" — that resolves to "" / "<id>"
# which is a relative path from the process CWD, not the package dir.
#
# Use the 4-tier _resolve_dir() helper below (copy it verbatim). The helper takes
# BOTH env-var names for a category (built-in + user), plus the "user extra" layer
# name (either "models" for weights, or "" for pack), plus the arcname sub-dir the
# packager uses ("models" or "pack"):
#
#   1a. Built-in env root       : $BUILTIN_ENV/<id>                        (dev host, built-in pack)
#   1b. User env root           : $USER_ENV/<user_extra>/<id>              (dev host, user-imported pack)
#   2.  Walk up from __file__ looking for <arcname_sub>/<id>               (packaged zip on any machine)
#   3.  Last-resort relative path so callers get a clear FileNotFoundError
#
# This makes the zip self-contained AND makes the dev-host binding work for both
# built-in and user-imported packs — one helper, no per-pack conditionals.
def _resolve_dir(
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
    # Tier 1b: user host env (P4). Weights need the extra "models/" layer to
    # match manifest installPath convention; pack does not.
    if user_env:
        p = Path(user_env)
        if user_extra:
            p = p / user_extra
        p = p / model_id
        if p.is_dir():
            return p
    # Tier 2: walk up from backend/inference.py → backend/ → <pkg>/ → …
    # Finds <pkg>/models/<model_id> or <pkg>/pack/<model_id> inside a packaged
    # zip that was unpacked anywhere. Uniform for both built-in and user packs
    # because the packager writes them under the same arcnames.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / arcname_sub / model_id
        if candidate.is_dir():
            return candidate
    # Tier 3: last-resort — return the anchor candidate that best matches
    # what we know about the pack, so the caller's subsequent .is_dir()
    # check / open() call produces a clear error against a MEANINGFUL path
    # (not a bogus "./id" relative to CWD).
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
    """Load QNN context binaries ONCE. Called at startup; cached process-wide.

    ``model_root`` / ``pack_root`` come from ``APP_BUILDER_MODEL_ROOT`` /
    ``APP_BUILDER_PACK_ROOT`` (built-in anchors). ``user_model_root`` /
    ``user_pack_root`` come from ``APP_BUILDER_USER_MODEL_ROOT`` /
    ``APP_BUILDER_USER_PACK_ROOT`` (P4 user anchors). Pass every var
    ``os.environ.get()`` returns; the resolver decides which anchor holds
    this pack.
    """
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

## 6. Pure HTML/JS frontend (same-origin, calls the app's OWN backend)

The frontend calls its OWN backend at `/api/infer` (same origin) — NOT the host
API. Dark, card-based: input panel + output panel + a small perf area.

### `frontend/styles.css`

**Copy `${APP_ROOT}/factory/app_builder/_webui/frontend/base.css` to
`frontend/styles.css`**, then append app-specific rules below the
`── App-specific styles ──` marker at the bottom of the file.

Do NOT remove or override the boilerplate above that marker — especially the
`[hidden] { display: none !important; }` rule and the `.img-wrap` block.

### `frontend/model_poll.js`

**Copy `${APP_ROOT}/factory/app_builder/_webui/frontend/model_poll.js` to
`frontend/model_poll.js` as-is.** No edits needed.

In `index.html`, load it **before** `app.js`:
```html
<script src="/static/model_poll.js"></script>
<script src="/static/app.js"></script>
```

In `app.js`, use `_modelReady` (exported by `model_poll.js`) to guard the
Run button in the `finally` block:
```javascript
} finally {
  btn.disabled = !_modelReady;   // re-enable only if model is ready
}
```

### `frontend/index.html`

Write this file fresh for each app (it is short and fully app-specific).
Required elements that `model_poll.js` expects:

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

`run.bat` is generated content (data). Keep it **ASCII / English-only** — Chinese
comments in a `.bat` are risky under PowerShell/OEM encoding and can corrupt or
break the script.

**CRITICAL — env var wiring for user-imported (P4) packs.** When the host UI
launches the app (Apps menu), it injects
`APP_BUILDER_MODEL_ROOT` / `APP_BUILDER_PACK_ROOT` /
`APP_BUILDER_USER_MODEL_ROOT` / `APP_BUILDER_USER_PACK_ROOT`. But when the user
double-clicks `run.bat` directly (no host UI), env vars are empty and
`_resolve_dir`'s tier-1a/1b skip. For a **built-in** pack, tier-2 walk-up finds
`<repo>/models/<id>/` from the app's own path — works. For a **user-imported**
pack, real weights live at `<repo>/data/app_builder/user_model_weights/models/<id>/`
(extra `models/` layer, NOT reachable via `<ancestor>/models/<id>` walk-up).

Fix: `run.bat` itself derives the repo root from `%~dp0` (app dir is
`<repo>/data/app_builder/<app_id>/` → three levels up = `<repo>`) and exports
all four anchors. Each `if exist` guard means: after unzip on a foreign machine
the anchor dirs don't exist so env stays empty and `_resolve_dir` tier-2
walk-up finds the bundled `models/<id>/` next to `backend/` — no branch needed.

```bat
@echo off
REM Manual run for this App Builder app. Prefer the host UI (Apps menu).
setlocal
cd /d "%~dp0"

REM Derive repo root: <repo>/data/app_builder/<app_id>/run.bat -> up 3 levels.
REM Guarded exports: on a foreign machine (unpacked zip) these dirs do not
REM exist, env stays empty, and inference.py tier-2 walk-up finds bundled
REM models/<id>/ + pack/<id>/ next to backend/. On the dev host they DO
REM exist and tier-1a/1b hit directly, matching host-UI-launch behaviour.
REM ``if exist "...\"`` (trailing backslash) restricts the check to a real
REM directory — prevents a same-named FILE from being mistaken for a dir.
set "REPO_ROOT=%~dp0..\..\.."
if exist "%REPO_ROOT%\models\" set "APP_BUILDER_MODEL_ROOT=%REPO_ROOT%\models"
if exist "%REPO_ROOT%\factory\app_builder\models\" set "APP_BUILDER_PACK_ROOT=%REPO_ROOT%\factory\app_builder\models"
if exist "%REPO_ROOT%\data\app_builder\user_model_weights\" set "APP_BUILDER_USER_MODEL_ROOT=%REPO_ROOT%\data\app_builder\user_model_weights"
if exist "%REPO_ROOT%\data\app_builder\user_models\" set "APP_BUILDER_USER_PACK_ROOT=%REPO_ROOT%\data\app_builder\user_models"

"%LOCALAPPDATA%\QAIModelBuilder\envs\.venv_arm64_313\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### `start.bat` (Windows debug restart — ASCII/English ONLY)

Every generated WebUI app MUST also include `start.bat`. It is a manual debug
launcher that accepts an optional port argument, kills any existing LISTENING
process on that port, prints each step, pauses before commands when debugging is
needed, and starts uvicorn. Keep it ASCII / English-only. Resolve `APP_ROOT`
from `%~dp0..\..\..` instead of hard-coding a local user path.

### `run.ps1` (optional PowerShell equivalent)

Same anchor-derivation logic as run.bat. **Deliberately unrolled** — no
`Resolve-Path` (throws on non-existent / cross-volume paths, would crash the
script when unpacked in a shallow location) and no
`$ErrorActionPreference = 'Stop'` for the probe (a missing anchor dir is a
normal fallback path, not a fatal error). We only `-PathType Container`-guard
the exports so a same-named file is not mistaken for a directory:

```powershell
Set-Location -LiteralPath $PSScriptRoot
# Join without canonicalising — Resolve-Path would throw on foreign-machine
# unpacked layouts where the ancestor path does not exist / crosses volumes.
$RepoRoot = Join-Path $PSScriptRoot '..\..\..'
$anchors = @{
    APP_BUILDER_MODEL_ROOT       = Join-Path $RepoRoot 'models'
    APP_BUILDER_PACK_ROOT        = Join-Path $RepoRoot 'factory\app_builder\models'
    APP_BUILDER_USER_MODEL_ROOT  = Join-Path $RepoRoot 'data\app_builder\user_model_weights'
    APP_BUILDER_USER_PACK_ROOT   = Join-Path $RepoRoot 'data\app_builder\user_models'
}
foreach ($k in $anchors.Keys) {
    if (Test-Path -LiteralPath $anchors[$k] -PathType Container) {
        Set-Item -Path "env:$k" -Value $anchors[$k]
    }
}
& "$env:LOCALAPPDATA\QAIModelBuilder\envs\.venv_arm64_313\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
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
[ -d "$REPO_ROOT/factory/app_builder/models" ] && export APP_BUILDER_PACK_ROOT="$REPO_ROOT/factory/app_builder/models"
[ -d "$REPO_ROOT/data/app_builder/user_model_weights" ] && export APP_BUILDER_USER_MODEL_ROOT="$REPO_ROOT/data/app_builder/user_model_weights"
[ -d "$REPO_ROOT/data/app_builder/user_models" ] && export APP_BUILDER_USER_PACK_ROOT="$REPO_ROOT/data/app_builder/user_models"
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### `README.md`

Write the README in the app dir. It must cover:

- **How to run** — via the host App Builder UI (Apps menu: run / stop / package),
  or manually with `run.bat` (or the uvicorn command).
- **Required models** — which model id(s) and where the weights live
  (`${APP_ROOT}/models/<id>/`).
- **Input / output format** — what the app accepts and produces.
- **Known limitations** — especially if a custom model lacks full pre/post.
- **Explicit packaging note** — the produced zip is **NOT a fully offline
  package**: the target machine needs a **QAI ModelBuilder Python environment**
  (Python interpreter + `qai_appbuilder` + QNN runtime). The zip does not bundle
  Python or the QNN SDK.

---

## 8. Constraints recap

- **Must provide `GET /health`** returning `{"status": "ok"}` (host readiness
  probe; two consecutive successes mark ready).
- **Must NOT `webbrowser.open()`** — the host opens the browser after readiness.
- **Load the model as a process-wide singleton** — never reload per request.
- **Pre-load at startup via `lifespan`, NOT lazily on first request.** Lazy
  loading blocks the first user request for 10-30 s while the NPU binary
  compiles. Use FastAPI `lifespan` + `loop.run_in_executor` to load in a
  background thread at startup. Expose `GET /api/model-status` so the frontend
  can poll and show a loading banner until ready.
- **Do NOT modify pack originals** under
  `${APP_ROOT}/factory/app_builder/models/<id>/` or weights under
  `${APP_ROOT}/models/<id>/`. Copy/adapt into the app's `backend/utils/`.
- **The frontend calls the app's OWN backend** (same origin), NOT the host
  `/api/app-builder/runs`.
- **First version must NOT auto `pip install`.** Prefer deps already in the
  current venv + project helpers. If extra deps are truly required, list them in
  `requirements.txt` + README and note them for the user; do not install at run
  time.
- **All app-code write paths and reference-read paths use the ABSOLUTE
  `${APP_ROOT}/...` form**; the app dir is `${APP_ROOT}/data/app_builder/<app_id>/`
  (with underscore).
- **QNN is not thread-safe — always use `_INFER_LOCK`.** FastAPI runs sync
  routes in a thread pool; without a `threading.Lock()` around every
  `ctx.run()` call, concurrent requests corrupt the QNN context and cause
  silent wrong results or native crashes. Acquire the lock inside the
  `run_in_executor` callback, not outside it.
- **`bool` Form parameters are always strings.** HTML forms send `"true"`/
  `"false"` as plain strings. FastAPI's `bool` coercion only recognises
  `"1"`/`"0"`/`"on"`/`"off"`; `"false"` is a non-empty string and coerces
  to `True`. Always receive bool-like Form fields as `str` and parse with
  `val.lower() not in ("false", "0", "no", "off")`. The upload block in
  `main_base.py` already applies this pattern.
- **CSS `[hidden]` override (critical).** Always add
  `[hidden] { display: none !important; }` to your stylesheet. Without it,
  any CSS `display:` rule (e.g. `display:flex`, `display:inline-block`) silently
  overrides the HTML `hidden` attribute, so `element.hidden = true` in JS has
  no visual effect. This affects loading banners, spinners, and any element that
  uses both a CSS `display:` rule and the `hidden` attribute.
- **Model-status poll timer order.** Set `_pollTimer = setInterval(...)` BEFORE
  calling `pollModelStatus()`. The poll function is async; if the model is
  already ready, it resolves before the `setInterval` line runs and calls
  `clearInterval(null)` (no-op), leaving the interval alive and the banner
  permanently visible.
- **Image result containers.** Use `width:100%; height:auto` on result `<img>`
  elements, NOT `max-width:100%`. Wrap the image in a `<div class="img-wrap">`
  with `width:100%; overflow:hidden`. Without an explicit container width,
  `max-width:100%` has no effect and the image overflows its flex/grid parent,
  getting clipped on the left/top by `overflow:hidden`.

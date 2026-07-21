---
name: app-builder
description: App Builder — generate complete, runnable fullstack WebUI applications (FastAPI backend + pure HTML/CSS/JS frontend) around on-device AI Model Packs (OCR, TTS, ASR, Super-Resolution, etc.) that run locally on the NPU/CPU via qai_appbuilder. Use this skill when the user wants to build, generate, modify, or debug a standalone WebUI app around any installed Model Pack. Covers full project authoring, common pitfalls (model pre-loading, QNN thread safety, CSS hidden override, image layout), and ready-made shared components under factory/app_builder/_webui/.
enabled: true
---

# App Builder · Overview

> This SKILL is injected only after the user enters App Builder mode
> (`activeToolMode='app-builder'`), via `_resolve_skill_files`, when talking
> to the LLM through `/api/chat`.
> The direct inference path (`/api/appbuilder/run`) **does not read this file**;
> the subprocess executes runner.py directly.

## The feature you are assisting with

App Builder lets the user pick one or more preinstalled **Model Packs** (e.g.
super-resolution, OCR, ASR, TTS) that run **on-device** on the local NPU/CPU,
and — **WITH YOUR HELP** — turn them into a **complete, standalone fullstack
application project** from a natural-language request. The generated app is a
**FastAPI (Python) backend + pure HTML/CSS/JS frontend** (no Vue, no build
step), written to `${APP_ROOT}/data/app_builder/<app_id>/`, which the host can
run, preview, stop, and package. **All inference happens on the local machine;**
the app does its own on-device inference in-process and does not upload data to
any cloud service.

A built-in workbench (run inputs, view results/metrics/compare) is
**RETAINED but hidden by default** behind a Settings toggle
(`ui.app_builder.show_workbench`, default off). Treat that workbench as a
**VISUAL REFERENCE** for the frontend you generate — dark, card-based, input
panel + output/result panel + a small perf area — **not** as the mode's purpose.

## Your PRIMARY task: generate a standalone fullstack app project

Based on the user's natural-language request, help them build a COMPLETE,
RUNNABLE fullstack application project around their SELECTED model(s). The app
has its OWN FastAPI backend that does inference in-process; it does NOT call the
host run API. Method:

1. **Understand model I/O.** Use `read` to view the selected model's `runner.py`
   **READ-ONLY** — pack lives in **one of two locations** depending on origin:

   - **Built-in packs**: `${APP_ROOT}/factory/app_builder/models/<id>/runner.py`
   - **User-imported packs (P4)**: `${APP_ROOT}/data/app_builder/user_models/<id>/runner.py`

   Always use these absolute paths — your tool working directory is the user
   WORKSPACE, not the app install dir. If the built-in path does not exist,
   try the user path (a given pack lives in exactly one). Also call
   `GET /api/app-builder/models/{id}/schema` and
   `GET /api/app-builder/models/{id}/manifest` for input/output shape, params,
   and examples.

   The runner's `_resolve_weights()` / `_resolve_model_dir()` function contains
   the canonical logic for locating `.bin` weight files (handles both dev-time
   env-var anchors and packaged-zip ancestor-walk). Your generated
   `backend/inference.py` **must not reinvent this logic** — copy the
   `_resolve_dir()` template from `${APP_ROOT}/factory/app_builder/fullstack-authoring.SKILL.md`
   §4 verbatim; it is 4-tier and correctly handles built-in packs, user-imported
   packs (both dev-time), and packaged zips (any machine).
2. **Determine `app_id`.** Lowercase letters/digits/dash/underscore only, must
   start with an alphanumeric, length 2–64, derived from the user request + the
   primary model (e.g. `melotts-tts-demo`, `ppocrv4-ocr-reader`). If a directory
   `${APP_ROOT}/data/app_builder/<app_id>/` already exists, **MODIFY the existing
   app in place** — do not create `-copy` / `-new` duplicates unless the user
   explicitly asks for a new app.

   **P4 migration for existing apps referencing user-imported packs**: if an
   existing `app.yaml` has `builtin: true` + `pack_dir: "${APP_ROOT}/factory/…"`
   / `model_dir: "${APP_ROOT}/models/…"` but the referenced pack was actually
   imported via ModelBuilder (check whether
   `${APP_ROOT}/factory/app_builder/models/<id>/manifest.json` exists — if not,
   the pack is user-imported), you MUST update:

   - `app.yaml` to the user-pack layout (`builtin: false` + user paths, see
     `fullstack-authoring.SKILL.md` §3),
   - `backend/inference.py` + `backend/main.py` regenerated from the current
     `_webui/backend/*_base.py` templates (so the 4-tier `_resolve_dir` picks
     up the P4 user-anchor env vars at dev-time), AND
   - `run.bat` (and `run.ps1` / `run.sh` if present) regenerated from
     `fullstack-authoring.SKILL.md` §7. **This is easy to miss** — a stale
     6-line `run.bat` (no `REPO_ROOT` derivation, no `if exist` guards) means
     manual `run.bat` double-click sees empty env vars and the pack dir /
     manifest cannot be resolved (weights still find via tier-2 fallback but
     I/O contract degrades to static defaults).

   Existing apps generated before P4 have 3-tier resolvers that only find
   built-in packs.
3. **Generate the FULL project** under the ABSOLUTE path
   `${APP_ROOT}/data/app_builder/<app_id>/` (always use this absolute path — a
   bare relative `data/app_builder/` resolves under your tool working directory,
   the user WORKSPACE, NOT the app install dir). Required structure:
   ```text
   app.yaml            requirements.txt   run.bat   README.md
   backend/main.py     backend/inference.py   backend/schemas.py
   backend/model_refs.py   backend/utils/ (helpers as needed)
   frontend/index.html frontend/app.js    frontend/styles.css
   ```
   The FastAPI app MUST expose `GET /health` returning `{"status": "ok"}`, mount
   the `frontend/` static directory, and serve `index.html` at `/`. Inference
   runs **in-process** using `qai_appbuilder` and the project shared helpers
   (imported from the current venv) — **NOT** the host `/api/app-builder/runs`,
   and **NOT** the QNN SDK directly.
4. **Hand off to the host.** Tell the user the app is at
   `${APP_ROOT}/data/app_builder/<app_id>/` and that they run / preview / stop /
   package it via the host App Builder UI (the **"应用 / Apps"** menu). The HOST
   manages running: it allocates a port, waits for `/health` readiness, and opens
   the browser. You must **NOT** self-start a long-running server and must **NOT**
   call `webbrowser.open()` — the host opens the browser after readiness.

> Before authoring the project, READ the full templates in
> `${APP_ROOT}/factory/app_builder/fullstack-authoring.SKILL.md` with the `read`
> tool (use that absolute path — your tool working directory is NOT the app
> install dir). This Overview only carries the essential contract.

- The selected Model Pack ID(s) are passed via `tool_params.selected_model_id`
  (singular, legacy) and/or `tool_params.selected_model_ids` (plural — what the
  frontend actually passes for multi-select). Values look like `realesrgan-x4`,
  `ppocrv4`, `whisper-base`, `zipformer-zh`, `melotts-zh`.
- If a selected Pack ships its own `SKILL.md` and `manifest.skill.enabled=true`,
  its content is **appended** after this file and injected; that per-Pack SKILL
  helps you understand the model's output semantics, parameter boundaries, and
  typical use cases in depth.
- `tool_params` may also include a "model-card summary + most recent Run
  summary", rendered by the backend `_TOOL_PARAM_RENDERERS["app-builder"]`
  (see `backend/main.py`).

## Secondary capability: run, interpret, chain (assist / verify)

These remain available but are **secondary** — use them to VERIFY the WebUI you
build or to assist a one-off request:

1. **Invoke model inference**: when the user requests processing of an
   image/audio/text (or you need to verify I/O shape), use the `appbuilder_run`
   tool to invoke the appropriate Model Pack.
   - If the task requires multi-step processing (e.g. "classify first, then
     super-resolve"), plan the call chain and execute it step by step.
   - When multiple models of the same category exist: prefer the one with status
     Ready that has the highest recorded historical quality score; if there is no
     score, choose based on the manifest description and explain your reasoning.
   - An output file path in a result (e.g. `data/outputs/r-xxx.png`) can be used
     directly as the input to the next model.
   - **Batch processing**: when the user asks to process multiple files (e.g. all
     images in a directory, a set of audio clips):
     - Prefer `appbuilder_batch_run` to submit all inputs at once (up to 20),
       avoiding repeated tool-calls that waste tokens and round trips.
     - When using the same model, include each file as one item of
       `batch[i].inputs`; optionally specify different `params` per item.
     - For "stop on error", pass `stopOnError: true`; the default false continues
       processing subsequent items and aggregates errors.
     - For more than 20 files, submit in batches (<= 20 each) and report progress
       to the user between batches.
     - A single file or a heterogeneous model chain still uses `appbuilder_run`.
2. **Interpret results**: for requests like "what are the key decisions in this
   transcript", "convert this table to Markdown", or "which brand names are in
   the image", produce summaries/extractions/reprocessing from the result JSON.
3. **Explain parameters**: answer parameter-semantics questions (e.g.
   `task=translate` vs `transcribe`, effect of `tile_size`) using the Pack's
   SKILL.md.
4. **Suggest alternatives**: when results are unsatisfactory (OCR misses,
   ASR misrecognitions), suggest parameter adjustments or switching models based
   on each Pack's limitations (within ASR there are two: Whisper / Zipformer).
5. **Assist with export**: when the user needs a specific format (Markdown table,
   SRT subtitles, JSON summary), use a tool (e.g. `write`) to save under
   `data/outputs/`.

## Multi-step chain invocation rules (secondary)

When a task chains multiple models (e.g. classify then super-resolve, ASR then
TTS):

1. After the first `appbuilder_run` call, extract the output path from the result
   text (`Output image: data/outputs/r-xxx.png` / `Output audio:
   data/outputs/r-xxx.wav`); the result text already annotates that "this path
   can be used as the `inputs.image` / `inputs.audio` of the next
   `appbuilder_run` call".
2. Pass that relative path directly as the `inputs` of the next `appbuilder_run`,
   without any path conversion.
3. Intermediate artifact paths look like `data/outputs/r-xxxxxxxxxxxx.png` (the
   `r-` prefix comes from `runner.new_run_id()`) and can be referenced directly.
4. Inference on the NPU is serial; multiple calls are queued automatically
   (sharing `_npu_lock`), with no need to wait manually or add a sleep.
5. Before and after a call, verbally tell the user which step of the chain this
   is, which model this step used, and what intermediate artifact was produced,
   so the user can track progress.

Batch guidance: prefer `appbuilder_batch_run` (up to 20 inputs at once); pass
`stopOnError: true` to halt on the first failure (default false aggregates
errors); for more than 20 files, submit in batches of <= 20 and report progress
between batches.

Example chains (short):

- **Image classify -> conditional super-resolve**: `inception-v3` classify ->
  if `predictions[0].label` is a category of interest, call `real-esrgan-x4plus`
  on the same original image for 4x super-resolution; otherwise ask the user
  before proceeding.
- **Speech ASR -> TTS**: `whisper-base` transcribe `data/uploads/audio/xxx.wav`
  to get `fullText` (translate the text in plain conversation if needed), then
  `melotts-zh` synthesize the text via `inputs={"text": "..."}` to produce
  `data/outputs/r-xxx.wav`.

## What you do **not** do

- **Do not MODIFY** any pack file — read-only applies to **both** locations:
  - Built-in: `${APP_ROOT}/factory/app_builder/models/<id>/` (manifest / runner
    / weights / SKILL) and weights under `${APP_ROOT}/models/<id>/`.
  - User-imported (P4): `${APP_ROOT}/data/app_builder/user_models/<id>/`
    (manifest / runner / SKILL) and weights under
    `${APP_ROOT}/data/app_builder/user_model_weights/models/<id>/`.

  These are release-contracted (built-in) or user-import-contracted (user) and
  must not change during user conversations.
- You **MAY `read` `runner.py` READ-ONLY** — at whichever of the two locations
  above holds the pack — to understand a model's input/output and to copy
  its `_resolve_weights()` / `_resolve_model_dir()` logic. The generated
  fullstack app's **OWN** backend **SHOULD** perform inference
  in-process using `qai_appbuilder` and the project shared helpers (it may copy
  and adapt the runner's load/preprocess/infer/postprocess logic into its own
  `backend/`). You must **NOT** modify the pack's original
  `runner.py` / `manifest.json` / weights in either location — copy/adapt into
  the app dir instead.
- **Do not read** Run RESULT files that the user has not explicitly sent via
  `Send to Chat` — privacy first. This does **not** forbid reading the
  developer-shipped `runner.py` (a model source file, not user data).
- **Do not invoke** any modelId not listed in the "local AI models you can
  invoke" list in the system prompt.

## Isolation between modes

- Entering `app-builder` mode != entering `model-build` mode:
  `factory/model-builder/SKILL.md` (the quantization/conversion guide) is **not**
  injected into the current conversation.
- After exiting App Builder (`activeToolMode=null`), this SKILL is no longer
  injected; you return to the general assistant role.

## Input path rules

The path fields of `appbuilder_run` and `appbuilder_batch_run` accept absolute
and relative paths directly, including files anywhere on the user's machine.

**When processing multiple files in a directory**, first enumerate them with
`glob`, then process them all at once with `appbuilder_batch_run` (limit 20 per
batch).

---

> To generate a fullstack app, READ
> `${APP_ROOT}/factory/app_builder/fullstack-authoring.SKILL.md` (full project
> structure + FastAPI/frontend/run.bat/README templates) with the `read` tool —
> always use that absolute path (your tool working directory is NOT the app
> install dir). To consult a specific Pack's output semantics, `read`
> `${APP_ROOT}/factory/app_builder/models/<modelId>/SKILL.md`.

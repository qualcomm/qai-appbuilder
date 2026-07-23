---
name: app-builder
description: App Builder — generate complete, runnable fullstack WebUI applications (FastAPI backend + pure HTML/CSS/JS frontend) around on-device AI Model Packs (OCR, TTS, ASR, Super-Resolution, etc.) that run locally on the NPU/CPU via qai_appbuilder. Use this skill when the user wants to build, generate, modify, or debug a standalone WebUI app around any installed Model Pack. Covers full project authoring, common pitfalls (model pre-loading, QNN thread safety, CSS hidden override, image layout), and ready-made shared components under factory/chat_features/app-builder/_webui/.
enabled: true
---

# App Builder · Overview

> **Injection scope**: this SKILL is injected only when the user is in App Builder mode (`activeToolMode='app-builder'`, chat path via `/api/chat`). The direct inference path `/api/appbuilder/run` does NOT read this file — it executes `runner.py` directly.

## The feature you are assisting with

App Builder lets the user pick one or more preinstalled **Model Packs** (OCR / TTS / ASR / super-resolution / ...) that run **on-device** on the local NPU/CPU, and — **WITH YOUR HELP** — turn them into a **complete standalone fullstack app project** from a natural-language request. The generated app is a **FastAPI backend + pure HTML/CSS/JS frontend** (no Vue, no build step), written to `${APP_ROOT}/data/app_builder/<app_id>/`, which the host can run / preview / stop / package. **All inference runs locally in-process; no cloud upload.**

> **Platform note**: Model Packs run on-device on either ARM64 (WoS,
> NPU-accelerated via HTP) **or** x64 (Intel/AMD, no NPU — inference falls
> back to the CPU / DLC backend via `qai_appbuilder`). The generated app
> code is architecture-neutral; the runtime venv (arm64 vs x64) is picked
> per host by the `run.bat` / `run.ps1` templates below. See
> `docs/85-tasks/x64-windows-support-plan.md` §9 for how to force a
> specific architecture via `Setup.bat --arch <arm64|x64>` +
> `data/config/host_arch`.

A built-in workbench (inputs / results / metrics / compare) is **RETAINED but HIDDEN** behind Settings toggle `ui.app_builder.show_workbench` (default off). Treat it as a **visual reference** for the frontend you generate (dark, card-based, input + output/result + small perf panel) — **not** as the mode's purpose.

## Your PRIMARY task: generate a standalone fullstack app project

Based on the user's natural-language request, help them build a COMPLETE,
RUNNABLE fullstack application project around their SELECTED model(s). The app
has its OWN FastAPI backend that does inference in-process; it does NOT call the
host run API. Method:

1. **Understand model I/O.** Use `read` to view the selected model's `runner.py`
   **READ-ONLY** — pack lives in **one of two locations** depending on origin:

   - **Built-in packs**: `${APP_ROOT}/factory/chat_features/app-builder/models/<id>/runner.py`
   - **User-imported packs (P4)**: `${APP_ROOT}/data/app_builder/user_models/<id>/runner.py`

   Use absolute paths — your tool CWD is the user WORKSPACE, not the app install dir. A given pack lives in exactly one location; if built-in path is absent, try the user path. Also call `GET /api/app-builder/models/{id}/schema` and `GET /api/app-builder/models/{id}/manifest` for I/O shape, params, and examples.

   The runner's `_resolve_weights()` / `_resolve_model_dir()` holds the canonical `.bin` weight-locating logic (handles dev-time env-var anchors AND packaged-zip ancestor-walk). Your generated `backend/inference.py` **must not reinvent this** — copy the 4-tier `_resolve_dir()` template from `${APP_ROOT}/factory/chat_features/app-builder/app-authoring.md` §4 verbatim (correctly handles built-in packs, user-imported packs, and packaged zips).
2. **Determine `app_id`.** Lowercase letters/digits/dash/underscore only, must
   start with an alphanumeric, length 2–64, derived from the user request + the
   primary model (e.g. `melotts-tts-demo`, `ppocrv4-ocr-reader`). If a directory
   `${APP_ROOT}/data/app_builder/<app_id>/` already exists, **MODIFY the existing
   app in place** — do not create `-copy` / `-new` duplicates unless the user
   explicitly asks for a new app.

   **P4 migration for existing apps referencing user-imported packs**: if an existing `app.yaml` has `builtin: true` + built-in-style `pack_dir`/`model_dir` paths, but the pack is actually user-imported (check: if `${APP_ROOT}/factory/chat_features/app-builder/models/<id>/manifest.json` doesn't exist → user-imported), you MUST update:

   - `app.yaml` → user-pack layout (`builtin: false` + user paths; see `app-authoring.md` §3)
   - `backend/inference.py` + `backend/main.py` → regenerate from current `_webui/backend/*_base.py` templates (so the 4-tier `_resolve_dir` picks up P4 user-anchor env vars at dev-time)
   - `run.bat` (and `run.ps1`/`run.sh` if present) → regenerate from `app-authoring.md` §7. **Easy to miss** — a stale 6-line `run.bat` (no `REPO_ROOT` derivation, no `if exist` guards) → manual double-click sees empty env vars → pack/manifest unresolved → I/O contract degrades to static defaults.

   Apps generated before P4 have 3-tier resolvers that only find built-in packs.
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
> `${APP_ROOT}/factory/chat_features/app-builder/app-authoring.md` with the `read`
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

For **one-off requests** (a single Run, interpretation, chain, or batch — not authoring a full app), read `${APP_ROOT}/factory/chat_features/app-builder/references/secondary-capabilities.md`. It covers: `appbuilder_run` / `appbuilder_batch_run` usage (batch limit 20, `stopOnError`), multi-step chain rules (path passthrough, NPU serial queue, progress narration), result interpretation, parameter explanation, alternative suggestions, and export helpers. Keep this capability **secondary** — the PRIMARY task above is to author a standalone fullstack app.

## What you do **not** do

- **Do NOT MODIFY any pack file** (manifest / runner / weights / SKILL) — read-only in both locations, which are release/user-import contracted and must not change mid-conversation:
  - Built-in: `${APP_ROOT}/factory/chat_features/app-builder/models/<id>/` + weights `${APP_ROOT}/models/<id>/`.
  - User-imported (P4): `${APP_ROOT}/data/app_builder/user_models/<id>/` + weights `${APP_ROOT}/data/app_builder/user_model_weights/models/<id>/`.
- **You MAY `read` `runner.py` READ-ONLY** at whichever location holds the pack, to understand I/O and to **copy** its `_resolve_weights()` / `_resolve_model_dir()` logic into the generated app's own `backend/`. The generated app performs inference in-process via `qai_appbuilder` + shared helpers — never modify the original `runner.py`/`manifest.json`/weights, always copy/adapt into the app dir.
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

> **To author**: read `${APP_ROOT}/factory/chat_features/app-builder/app-authoring.md` (project structure + FastAPI/frontend/run.bat/README templates). **For a specific Pack's semantics**: read `${APP_ROOT}/factory/chat_features/app-builder/models/<modelId>/SKILL.md`. Always use absolute paths — your tool CWD is NOT the app install dir.

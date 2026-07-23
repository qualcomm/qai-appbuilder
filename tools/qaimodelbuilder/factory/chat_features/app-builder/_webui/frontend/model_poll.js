/* ---------------------------------------------------------------------
 * Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
 * SPDX-License-Identifier: BSD-3-Clause
 * --------------------------------------------------------------------- */
/* =============================================================================
   App Builder WebUI — model-status poller  (copy to frontend/model_poll.js)
   =============================================================================
   HOW TO USE
   ----------
   1. Copy this file to  <app_id>/frontend/model_poll.js
   2. In index.html, load it BEFORE app.js:
          <script src="/static/model_poll.js"></script>
          <script src="/static/app.js"></script>
   3. In app.js, guard the Run button with  _modelReady:
          btn.disabled = !_modelReady;   // in the finally block of run()

   What this file provides:
   • Polls GET /api/model-status every 1.5 s.
   • Shows #modelBanner while loading; hides it when ready.
   • Disables #runBtn while loading; enables it when ready.
   • Displays the error message in the banner if load failed.
   • Exposes  _modelReady (bool)  for app.js to read.

   Required HTML elements (must exist before this script runs):
     <div   id="modelBanner" class="model-banner" hidden> … </div>
     <span  id="modelBannerText"> … </span>   (inside modelBanner)
     <button id="runBtn" disabled> … </button>

   Required CSS:
     [hidden] { display: none !important; }
     (included in base.css — without this rule, .hidden = true has no effect
      because .model-banner { display: flex } overrides the browser default)

   PITFALL — poll timer order (already handled here, do not change):
     _pollTimer must be assigned BEFORE the first pollModelStatus() call.
     pollModelStatus() is async; if the model is already ready on a warm
     restart, it resolves before setInterval() runs and calls
     clearInterval(null) — a no-op — leaving the interval alive forever.
     The code below sets the timer first, then fires immediately.
   ============================================================================= */

/* exported _modelReady */
var _modelReady = false;   // read by app.js to guard the Run button
var _pollTimer  = null;

async function pollModelStatus() {
  try {
    const res  = await fetch('/api/model-status');
    if (!res.ok) return;                    // server not yet ready — keep polling
    const data = await res.json();

    if (data.ready) {
      // ── Model is ready ────────────────────────────────────────────────────
      _modelReady = true;
      const banner = document.getElementById('modelBanner');
      const btn    = document.getElementById('runBtn');
      if (banner) banner.hidden = true;     // hides banner (needs [hidden] CSS fix)
      if (btn)    btn.disabled  = false;    // unlock Run button
      clearInterval(_pollTimer);            // stop polling

    } else if (data.error) {
      // ── Model load failed ─────────────────────────────────────────────────
      const banner     = document.getElementById('modelBanner');
      const bannerText = document.getElementById('modelBannerText');
      if (banner) {
        banner.hidden = false;
        banner.classList.add('error');
      }
      if (bannerText) bannerText.textContent = 'Model failed to load: ' + data.error;
      clearInterval(_pollTimer);            // no point retrying a hard failure

    } else {
      // ── Still loading ─────────────────────────────────────────────────────
      const banner = document.getElementById('modelBanner');
      const btn    = document.getElementById('runBtn');
      if (banner) banner.hidden = false;    // show loading banner
      if (btn)    btn.disabled  = true;     // keep Run button locked
    }

  } catch (_) {
    // Server not yet up (uvicorn still starting) — keep polling silently.
  }
}

// ── Start polling ─────────────────────────────────────────────────────────────
// IMPORTANT: assign _pollTimer BEFORE calling pollModelStatus() — see pitfall
// note in the file header.
_pollTimer = setInterval(pollModelStatus, 1500);
pollModelStatus();                          // fire immediately without waiting 1.5 s

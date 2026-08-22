// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * ANSI SGR → HTML converter for service-log colourisation.
 *
 * Behaviour source (V1 = validated): `frontend/js/utils.js:110-157`
 * (`ansiToHtml`). The legacy implementation lived as a free function in a
 * 600-line global `utils.js`; this is the typed V2 re-implementation kept as
 * a focused, side-effect-free pure module so the Service log panel (and any
 * future log surface) can colourise streamed output the same way V1 did.
 *
 * Supported SGR codes (matching V1 exactly):
 *   - `0` (or empty `ESC[m`)  → reset: close all open spans
 *   - `1`                     → bold (`font-weight:bold`)
 *   - `30..37` / `90..97`     → foreground colour (GitHub dark palette)
 *   - `40..47` / `100..107`   → background colour
 * Unsupported codes (256-colour `38;5;n`, truecolor `38;2;r;g;b`, underline,
 * italic, …) are silently ignored — identical to V1.
 *
 * The output is safe to feed to `v-html`: the input text is HTML-escaped
 * BEFORE any span insertion, and the only markup we emit is
 * `<span style="…">` / `</span>` with a machine-built inline style string.
 */

/** Foreground SGR code → CSS colour (GitHub dark palette, mirrors V1). */
const FG: Readonly<Record<number, string>> = {
  30: "#4e4e4e", 31: "#f85149", 32: "#3fb950", 33: "#e3b341",
  34: "#58a6ff", 35: "#bc8cff", 36: "#39c5cf", 37: "#c9d1d9",
  90: "#8b949e", 91: "#ff7b72", 92: "#56d364", 93: "#f0c000",
  94: "#79c0ff", 95: "#d2a8ff", 96: "#76e3ea", 97: "#ffffff",
};

/** Background SGR code → CSS colour (mirrors V1). */
const BG: Readonly<Record<number, string>> = {
  40: "#4e4e4e", 41: "#f85149", 42: "#3fb950", 43: "#e3b341",
  44: "#58a6ff", 45: "#bc8cff", 46: "#39c5cf", 47: "#c9d1d9",
  100: "#8b949e", 101: "#ff7b72", 102: "#56d364", 103: "#f0c000",
  104: "#79c0ff", 105: "#d2a8ff", 106: "#76e3ea", 107: "#ffffff",
};

/** ESC[ … m sequence splitter (capturing the numeric/`;` payload). */
// eslint-disable-next-line no-control-regex
const SGR_SPLIT = /\x1b\[([0-9;]*)m/;

/** HTML-escape the three characters that could break out of text content. */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/**
 * Convert a log line that may contain ANSI SGR escape sequences into safe
 * HTML with inline-styled `<span>`s. Plain text (no escapes) round-trips to
 * its HTML-escaped form unchanged.
 */
export function ansiToHtml(text: string): string {
  const escaped = escapeHtml(text);
  // Even indices = literal text; odd indices = the captured SGR payload.
  const parts = escaped.split(SGR_SPLIT);

  let result = "";
  let openSpans = 0;

  for (let i = 0; i < parts.length; i++) {
    const part = parts[i] ?? "";
    if (i % 2 === 0) {
      result += part;
      continue;
    }
    const codes = part === "" ? [0] : part.split(";").map(Number);
    if (codes.includes(0)) {
      // Reset closes every span opened so far.
      result += "</span>".repeat(openSpans);
      openSpans = 0;
      continue;
    }
    let style = "";
    let bold = false;
    for (const code of codes) {
      if (code === 1) bold = true;
      else if (FG[code] !== undefined) style += `color:${FG[code]};`;
      else if (BG[code] !== undefined) style += `background:${BG[code]};`;
    }
    if (bold) style += "font-weight:bold;";
    if (style !== "") {
      result += `<span style="${style}">`;
      openSpans++;
    }
  }

  // Close any spans left open at end-of-line (defensive — mirrors V1).
  result += "</span>".repeat(openSpans);
  return result;
}

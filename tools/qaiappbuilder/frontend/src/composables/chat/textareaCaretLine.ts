// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * textareaCaretLine — decide whether the caret sits on the first or last
 * VISUAL line of a `<textarea>`.
 *
 * Why the DOM alone can't answer this
 * ------------------------------------
 * `selectionStart` gives the character offset; `scrollTop` gives the scroll
 * position; there is no `caretRect()` on a textarea. Users write both hard
 * newlines AND long paragraphs that soft-wrap, so a naive "no `\n` before the
 * caret" test (the shape the original ↑/↓ recall patch used) treats a 400-char
 * single-paragraph draft as one line and hijacks in-draft caret movement.
 *
 * The mirror-div technique used here
 * -----------------------------------
 * Render an off-screen `<div>` that faithfully copies every layout-affecting
 * style of the textarea (font, padding, border, width, wrap, letter-spacing),
 * fill it with `value.slice(0, caret)` followed by a zero-width marker span,
 * then read the marker's `offsetTop` — that IS the caret's Y offset inside
 * the content, in the same coordinate space as `scrollTop`. Compare against
 * `lineHeight` (top edge) and `scrollHeight - lineHeight` (bottom edge) to
 * classify.
 *
 * This is the same recipe used by libraries like `textarea-caret-position`;
 * we inline a compact version because we need only the two Y booleans, and
 * because the project is offline-first with a curated dependency list.
 *
 * Correctness caveats
 * -------------------
 *   * Runs on demand from a keydown handler — no per-keystroke reflow cost
 *     accumulates. The mirror is created, measured, removed in one call.
 *   * Uses `getComputedStyle` (not the inline `style`), so tokens resolved
 *     against user font scaling / accessibility zoom apply automatically.
 *   * Trailing `\n` is treated as an extra empty line by the browser but not
 *     by `offsetTop` — we mirror this by appending a non-empty marker so the
 *     empty final line still contributes height. See `atLastLine` below.
 */

/** The visual-line classification for a textarea caret. */
export interface CaretLinePosition {
  /** The caret sits on the first VISIBLE line (top of content, offset ≈ 0). */
  atFirstLine: boolean;
  /** The caret sits on the last VISIBLE line (bottom of content). */
  atLastLine: boolean;
}

/** Style properties the mirror MUST copy so its wrap matches the textarea. */
const MIRRORED_PROPS = [
  "boxSizing",
  "width",
  "paddingTop",
  "paddingRight",
  "paddingBottom",
  "paddingLeft",
  "borderTopWidth",
  "borderRightWidth",
  "borderBottomWidth",
  "borderLeftWidth",
  "borderTopStyle",
  "borderRightStyle",
  "borderBottomStyle",
  "borderLeftStyle",
  "fontFamily",
  "fontSize",
  "fontWeight",
  "fontStyle",
  "fontVariant",
  "letterSpacing",
  "wordSpacing",
  "lineHeight",
  "textIndent",
  "textTransform",
  "whiteSpace",
  "overflowWrap",
  "wordBreak",
  "tabSize",
] as const;

/**
 * Classify the caret's visual-line position inside `ta`.
 *
 * Returns `{ atFirstLine: true, atLastLine: true }` when the field is empty or
 * has a single visual line — either arrow key should trigger recall then.
 */
export function textareaCaretLine(ta: HTMLTextAreaElement, caret: number): CaretLinePosition {
  const doc = ta.ownerDocument;
  const cs = doc.defaultView?.getComputedStyle(ta);
  if (cs === undefined) {
    // No view (detached DOM in tests) — degrade to "both edges" so the caller
    // gets recall on either arrow rather than getting stuck.
    return { atFirstLine: true, atLastLine: true };
  }

  const mirror = doc.createElement("div");
  const s = mirror.style;
  // Off-screen but laid out (visibility:hidden would still allocate space; we
  // want it out of the flow entirely).
  s.position = "absolute";
  s.top = "0";
  s.left = "0";
  s.visibility = "hidden";
  s.pointerEvents = "none";
  // A textarea wraps its content and preserves whitespace: match with
  // `white-space: pre-wrap` unless the textarea overrides `whiteSpace`.
  s.whiteSpace = "pre-wrap";
  s.overflowWrap = "break-word";
  for (const prop of MIRRORED_PROPS) {
    // Both `getPropertyValue` and `setProperty` on CSSStyleDeclaration REQUIRE
    // kebab-case; camelCase is silently ignored for hyphenated properties
    // (`white-space`, `box-sizing`, etc.) which would leave the mirror at its
    // `<div>` defaults and mis-measure caret position. This was the bug in the
    // first cut of this helper — the `.style.whiteSpace = "pre-wrap"` above
    // stuck, but everything else silently reverted to defaults.
    const kebab = prop.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`);
    const v = cs.getPropertyValue(kebab);
    if (v !== "") s.setProperty(kebab, v);
  }

  const value = ta.value;
  const before = value.slice(0, caret);
  // A text node for the "before caret" content, then a marker span whose
  // offsetTop is the caret's Y offset. The marker carries a non-empty
  // character so a trailing newline in `before` still gets a fresh line box
  // (browsers collapse otherwise).
  mirror.textContent = before;
  const marker = doc.createElement("span");
  marker.textContent = "\u200b"; // zero-width space — invisible but positioned
  mirror.appendChild(marker);

  doc.body.appendChild(mirror);
  const lineHeightPx = parseFloat(cs.lineHeight);
  const caretTop = marker.offsetTop;
  doc.body.removeChild(mirror);

  // Content height comes from the TEXTAREA itself, not the mirror. The mirror
  // only holds the pre-caret slice (necessary to position the marker); using
  // its `scrollHeight` for the total would misreport "last visual line" as
  // true whenever the caret is near the top of a long entry — the exact bug
  // that made ↓-inside-a-recalled-multiline-prompt hop to newer instead of
  // moving the caret down. `ta.scrollHeight` reports the FULL text-flow
  // height and is exactly what we want to compare `caretTop` against.
  const contentHeight = ta.scrollHeight;

  // Guard against pathological `line-height: normal` (returns "normal" as a
  // string). Fall back to the font size, which browsers use as a floor.
  const lh = Number.isFinite(lineHeightPx) ? lineHeightPx : parseFloat(cs.fontSize) || 16;

  // "First visual line" = caret's line box starts within one line-height of the
  // content top. `padding-top` is baked into `offsetTop` via the mirror's box.
  const paddingTop = parseFloat(cs.paddingTop) || 0;
  const atFirstLine = caretTop - paddingTop < lh * 0.5;

  // "Last visual line" = one line-height or less remains below the caret. Half
  // a line-height tolerance absorbs sub-pixel rounding.
  const paddingBottom = parseFloat(cs.paddingBottom) || 0;
  const atLastLine = contentHeight - paddingBottom - caretTop < lh * 1.5;

  return { atFirstLine, atLastLine };
}

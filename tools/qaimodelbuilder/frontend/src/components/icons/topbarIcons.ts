// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Shared topbar icon set — a single, consistent family of monochrome line
 * icons for the global header action buttons (`HeaderAction.iconSvg`).
 *
 * Why this module exists
 * ----------------------
 * The topbar action buttons used to mix two rendering styles: chat hosted
 * inline line-SVGs (folder / wrench / chevron) while Service / Settings /
 * Skills used coloured emoji (⚙️ 🔄 📄 🗑). Emoji carry their own multi-colour
 * glyphs (e.g. 🔄 renders blue), don't follow `currentColor`, and render
 * differently per-OS — so placing them next to the monochrome line icons made
 * the toolbar look visually inconsistent.
 *
 * This module is the single source of truth for topbar icons: every icon is a
 * 16×16, `stroke="currentColor"`, 1.6px line glyph in the same visual family
 * (Feather/Lucide style). Views import these constants and pass them as
 * `iconSvg` so the whole topbar shares one consistent look and follows the
 * theme accent / text colour automatically.
 *
 * Safety: these are static literal strings (NOT user-supplied data); they are
 * emitted via `v-html` through the documented `HeaderAction.iconSvg`
 * escape-hatch (see `stores/headerActions.ts`).
 */

/** Wrap a viewBox-24 line path into a uniform 16×16 stroke icon. */
function lineIcon(inner: string): string {
  return (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
    'stroke-linejoin="round" style="flex-shrink:0" aria-hidden="true">' +
    inner +
    "</svg>"
  );
}

/** Workspace folder (chat). */
export const ICON_FOLDER = lineIcon(
  '<path d="M5 4h4l3 3h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/>',
);

/** Wrench / tool calls toggle (chat). */
export const ICON_WRENCH = lineIcon(
  '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
);

/** Chevron down (expand). */
export const ICON_CHEVRON_DOWN = lineIcon('<polyline points="6 9 12 15 18 9"/>');

/** Chevron up (collapse). */
export const ICON_CHEVRON_UP = lineIcon('<polyline points="18 15 12 9 6 15"/>');

/** Download / export. */
export const ICON_DOWNLOAD = lineIcon(
  '<path d="M12 3v12m0 0l-4-4m4 4l4-4M5 19h14"/>',
);

/** Trash / clear. */
export const ICON_TRASH = lineIcon(
  '<path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m2 0v12a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V7"/>',
);

/** Settings gear. */
export const ICON_SETTINGS = lineIcon(
  '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
);

/** Refresh / reload (circular arrows). */
export const ICON_REFRESH = lineIcon(
  '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v5h-5"/>',
);

/** Document / copy logs (text lines on a page). */
export const ICON_DOCUMENT = lineIcon(
  '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/>',
);

/** Help / usage guide (question mark in a circle). */
export const ICON_HELP = lineIcon(
  '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 4.5 1.5c0 1.5-2 1.75-2 3"/><path d="M12 17h.01"/>',
);

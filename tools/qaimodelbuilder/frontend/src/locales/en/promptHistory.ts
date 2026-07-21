// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — manually maintained, UTF-8 (no BOM).
//
// English is the schema source: en.ts is assembled and `typeof`-derived into
// MessageSchema (see ./schema.ts); zh-CN / zh-TW must mirror this key structure
// exactly (enforced by the locale parity test + tsc).
// =============================================================================

const promptHistory = {
  /** Button title / aria-label (the clock icon in the composer toolbar). */
  buttonTitle: "Prompt history & favorites",
  /** Popover header. */
  title: "Prompts",
  /** Search box placeholder. */
  searchPlaceholder: "Search prompts...",
  /** "Favorites" group header. */
  favorites: "Favorites",
  /** "Recent" group header. */
  recent: "Recent",
  /** Shown in the Favorites group when nothing is pinned yet. */
  favEmpty: "Star a prompt below to keep it here for quick reuse.",
  /** Shown when there is no history at all. */
  empty: "No prompts yet. The prompts you send will show up here.",
  /** Shown when a search yields no matches. */
  noResults: "No matching prompts.",
  /** Per-row star button (not yet favorited). */
  fav: "Add to favorites",
  /** Per-row star button (already favorited). */
  unfav: "Remove from favorites",
  /** Per-row delete button (recent only). */
  removeRecent: "Remove from history",
  /** Clear-history action. */
  clear: "Clear history",
  /** Confirm dialog title for clearing history. */
  clearTitle: "Clear prompt history?",
  /** Confirm dialog body for clearing history. */
  clearConfirm:
    "This removes all recent prompts. Your favorites are kept.",
  /** Tooltip on a row hinting that clicking fills the input. */
  fillTitle: "Click to fill the input",
  /** "Show all favorites" toggle when more than the default are pinned. */
  showAllFavorites: "Show all favorites",
  /** Collapse back to the default few favorites. */
  showFewerFavorites: "Show fewer",
};

export default promptHistory;

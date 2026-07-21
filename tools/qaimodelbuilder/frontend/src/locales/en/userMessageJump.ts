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

const userMessageJump = {
  /** Button title / aria-label (the bubble + lines icon in the composer toolbar). */
  buttonTitle: "Jump to a message I sent",
  /** Popover header. */
  title: "Jump to my messages",
  /** Shown when the active conversation has no user messages yet. */
  empty: "You haven't sent any messages in this conversation yet.",
};

export default userMessageJump;

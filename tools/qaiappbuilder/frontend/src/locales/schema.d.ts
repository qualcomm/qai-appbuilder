// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * vue-i18n module augmentation for type-safe message keys.
 *
 * Lets `t('nav.chat')` be statically checked against the canonical
 * `MessageSchema`. New keys must be added to `schema.ts` first; each
 * locale file is typed as `MessageSchema` and the compiler enforces
 * parity.
 */
import "vue-i18n";
import type { MessageSchema } from "./schema";

declare module "vue-i18n" {
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type
  export interface DefineLocaleMessage extends MessageSchema {}
}

// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// Edition stub module.
//
// Used by vite.config.ts `resolve.alias` in EXTERNAL builds as the resolution
// target for internal-only composables / component modules that have been
// physically removed from the open-source source tree. It provides inert
// named + default exports so that any residual import specifier resolves
// cleanly. None of these are ever actually invoked in an external build — the
// shared call sites guard on IS_INTERNAL before touching them; this module
// exists purely as a defence-in-depth resolution fallback.
// =============================================================================

const noop = () => ({});

// Default export doubles as an empty Vue component (rendered `null`) for any
// `.vue` specifier that happens to route here.
export default { render: () => null };

// Named exports mirror the internal composables so a stray `import { … }`
// resolves. All return empty objects and are never called externally.
export const useGomasterConnection = noop;
export const useGomasterOptimize = noop;
export const useProConnection = noop;

// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// Build-time edition seam.
//
// `__EDITION__` is injected by vite.config.ts `define` from the QAI_EDITION
// build env (see scripts/release/build.py). It is either "internal" (dev /
// full feature set) or "external" (packaged open-source release).
//
// Anything gated on `IS_INTERNAL` collapses to a constant at build time:
//   - internal build → `IS_INTERNAL === true`  → guarded code kept
//   - external build → `IS_INTERNAL === false` → guarded code (and any
//     dynamic import() inside it) is dead-code-eliminated by Rollup, so the
//     internal-only feature modules are never emitted into the bundle.
//
// Internal-only features use IS_INTERNAL. VLA also exposes a dedicated flag so
// this external source tree can opt into VLA without compiling absent Pro or
// GoMaster modules.
// =============================================================================

export const EDITION: "internal" | "external" =
  (typeof __EDITION__ === "string" && __EDITION__ === "external"
    ? "external"
    : "internal");

export const IS_INTERNAL = EDITION === "internal";

/** VLA-only opt-in for external source trees that omit other internal modules. */
export const IS_VLA_SIMULATION_ENABLED =
  IS_INTERNAL || __VLA_SIMULATION_ENABLED__;

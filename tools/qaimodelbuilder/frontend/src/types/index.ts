// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Public type entry point.
 *
 * Re-exports OpenAPI-derived types (generated via
 * `pnpm gen:types` → `api.ts`) and frontend-only contract types that
 * cannot be derived from the OpenAPI snapshot (chat WebSocket frames,
 * SSE envelopes — api-contract.md §3 §4).
 */

export type * from "./api";
export * from "./streaming";

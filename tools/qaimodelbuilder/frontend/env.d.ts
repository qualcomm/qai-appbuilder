// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/// <reference types="vite/client" />

declare module "*.vue" {
  import type { DefineComponent } from "vue";
  const component: DefineComponent<
    Record<string, unknown>,
    Record<string, unknown>,
    unknown
  >;
  export default component;
}

interface ImportMetaEnv {
  /** Optional override for the API base URL. When unset (the default in
   *  dev), requests use a relative path and the Vite proxy forwards them
   *  to the backend. */
  readonly VITE_API_BASE_URL?: string;
  /** Optional override for the WebSocket base URL. When unset, the page
   *  origin is used (which works through the Vite proxy in dev). */
  readonly VITE_WS_BASE_URL?: string;
  readonly VITE_APP_VERSION?: string;
  /** Dev-only override consumed by vite.config.ts when the backend
   *  is not on 127.0.0.1:8899. */
  readonly QAI_DEV_BACKEND_HTTP?: string;
  readonly QAI_DEV_BACKEND_WS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/** Injected at build time by vite.config.ts `define` from package.json version. */
declare const __APP_VERSION__: string;

/** Injected at build time by vite.config.ts `define`. Either "internal"
 *  (dev / full feature set) or "external" (packaged open-source release with
 *  internal-only features physically removed). */
declare const __EDITION__: string;

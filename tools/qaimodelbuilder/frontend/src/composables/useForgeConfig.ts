// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useForgeConfig` — composable for reading/saving forge config + the
 * data-driven chat-input toolbar module list.
 *
 * P4-A T2.7-A: extended to expose `toolbarModules` (the
 * `ui.toolbar_modules` nested dict on the legacy forge_config doc) so
 * `ChatComposer.vue` can render mode buttons by enabled+order, and
 * `AppConfigPanel.vue` can toggle them in Settings → App Config.
 *
 * Endpoint truth (verified 2026-06-01 via running backend):
 *   GET  /api/forge-config   → { config: {...} }
 *   POST /api/forge-config   → { config: {...} }   (shallow-merge)
 *
 * Defaults mirror V1 `frontend/js/config-defaults.js` (5 modes, order
 * 10/20/30/40/50). Align with V1: model_builder/app_builder/code are
 * enabled by default; translate/ppt are hidden by default (enabled:
 * false) and can be turned on in Settings → App Config. The mode code
 * stays present so no functionality is lost.
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";

import { apiJson, ApiError } from "@/api";
import { useToastStore } from "@/stores/toast";

// ─── Types ───────────────────────────────────────────────────────────────────

/** Forge config shape — generic record; concrete fields vary by section. */
export type ForgeConfig = Record<string, unknown>;

interface ForgeConfigResponse {
  config: ForgeConfig;
}

/** A single toolbar module entry stored under `ui.toolbar_modules.<key>`. */
export interface ToolbarModuleEntry {
  enabled: boolean;
  order: number;
  /** Value written to `activeToolMode` when clicked. */
  mode: string;
  /** i18n key for the button label (e.g. `index.modelBuilder`). */
  i18n: string;
  /** Icon name; ChatComposer uses `key` to pick the inline SVG. */
  icon: string;
  /**
   * Optional i18n key for a descriptive hover hint (e.g.
   * `index.appBuilderHint`). When set, the composer button tooltip shows
   * both the label and this hint. Only defined where a hint locale key
   * actually exists (do NOT fabricate keys).
   */
  hint?: string;
  /**
   * GoMaster only: which link is wired — "external" (one-click optimize task,
   * NOT chat), "agent" (conversational), or "both". The composer uses this to
   * decide whether a chat send routes to ``query::gomaster`` (agent/both only),
   * and the chat empty-state shows the GoMaster intro. Absent for other modules.
   */
  gomasterMode?: string;
}

export type ToolbarModulesMap = Record<string, ToolbarModuleEntry>;

/** Module entry as exposed to templates (key folded in). */
export interface VisibleToolbarModule extends ToolbarModuleEntry {
  key: string;
}

/** Default toolbar modules — align V1: translate/ppt hidden by default.
 *
 * Order values leave 10-unit gaps so future modules can slot in without
 * renumbering. The order reflects the intended user journey rather than
 * chronological addition: Model Builder → App Builder → Pro → GoMaster
 * (advanced / cloud-assisted model workflows) → Code → Translate → PPT
 * (general-purpose modes). See `forge_config.py` for the two internal-
 * only modules (pro / gomaster) that the backend injects with orders
 * 30 / 40 respectively — the two files intentionally share the same
 * order scheme so the toolbar looks the same on internal builds
 * regardless of whether a module comes from the frontend fallback or
 * the backend edition-gated injection. */
export const DEFAULT_TOOLBAR_MODULES: ToolbarModulesMap = {
  model_builder: {
    enabled: true,
    order: 30,
    mode: "model-build",
    i18n: "index.modelBuilder",
    icon: "cube",
  },
  // Model Hub — first-class mode (upgraded from the `aihub-model-run` skill):
  // download pre-compiled models from Qualcomm AI Hub and export them to App
  // Builder. Peer of model_builder / app_builder. `tool_mode` is the hyphenated
  // "model-hub" (backend system_prompt_builder already ships the display name /
  // feature prompt). Order 20 — the intended user journey is App Builder (10) →
  // Model Hub (20) → Model Builder (30): get a ready-made model first, build a
  // model only if you must convert your own.
  model_hub: {
    enabled: true,
    order: 20,
    mode: "model-hub",
    i18n: "index.modelHub",
    icon: "model_hub",
  },
  app_builder: {
    enabled: true,
    order: 10,
    mode: "app-builder",
    i18n: "index.appBuilder",
    icon: "apps",
    // Descriptive hover hint (already-shipped locale key). Only App Builder
    // currently has a matching `*Hint` locale key; peers omit `hint`.
    hint: "index.appBuilderHint",
  },
  // NOTE: order 40 / 50 are reserved for the internal-only `pro` and
  // `gomaster` modules that the backend injects (see forge_config.py
  // `pro.setdefault("order", 40)` / `gomaster.setdefault("order", 50)`).
  // The frontend has no fallback entries for those two — they only
  // render on internal editions where the backend ships them.
  code: {
    enabled: true,
    order: 60,
    mode: "code",
    i18n: "index.coding",
    icon: "code",
  },
  translate: {
    // V1 parity (config-defaults.js:274): translate mode is DISABLED by
    // default; the user opts in via Settings → App Config → Toolbar Modules.
    enabled: false,
    order: 70,
    mode: "translate",
    i18n: "index.translateMode",
    icon: "lang",
  },
  ppt: {
    // V1 parity (config-defaults.js:275): PPT generation mode is DISABLED by
    // default; the user opts in via Settings → App Config → Toolbar Modules.
    enabled: false,
    order: 80,
    mode: "ppt",
    i18n: "index.pptGen",
    icon: "slide",
  },
};

// ─── Module-level singleton state ────────────────────────────────────────────
//
// The composable is consumed by both `ChatComposer.vue` (read-only) and
// `AppConfigPanel.vue` (read+write). Both must observe the same reactive
// state so a Settings change immediately re-renders the chat toolbar.
// We keep the refs at module scope so multiple `useForgeConfig()` calls
// share state — typical Pinia-store style without registering a store.

const _config = ref<ForgeConfig>({});
const _loading = ref(false);
let _loadPromise: Promise<void> | null = null;

/**
 * Module-level, i18n-free read of the GoMaster link mode: "external" (one-click
 * optimize task, NOT chat), "agent" (conversational), "both", or null when
 * GoMaster is not wired (external edition / not injected / config not yet
 * loaded). Backend-driven via the gomaster toolbar module's ``gomaster_mode``.
 *
 * This lives at module scope — NOT inside `useForgeConfig()` — precisely so that
 * consumers who only need this reactive value (e.g. `useChatTransport`, which is
 * instantiated OUTSIDE a Vue `setup` context in tests and at some runtime call
 * sites) can import it without dragging in `useForgeConfig()`'s `useI18n()` call,
 * whose "must be called at the top of a `setup` function" constraint would
 * otherwise throw. It reads the same module-level `_config` singleton, so it stays
 * perfectly in sync with the composable's own `gomasterMode`.
 */
export const gomasterMode = computed<string | null>(
  () => readToolbarModules(_config.value)["gomaster"]?.gomasterMode ?? null,
);

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Legacy-order migration table (2026-07-20 rearrange).
 *
 * The toolbar-module order was rearranged to reflect the intended user
 * journey — get a ready-made model first, build your own only if needed:
 *
 *     app_builder    20 → 10
 *     model_hub      15 → 20
 *     model_builder  10 → 30
 *     pro            30 → 40   (backend-injected on internal editions)
 *     gomaster       40 → 50   (backend-injected on internal editions)
 *     code           50 → 60
 *     translate      60 → 70
 *     ppt            70 → 80
 *
 * `patchModule` persists the FULL toolbar-modules map (including `order`)
 * on any toggle change, so upgrading users whose `forge_config.json` was
 * populated by the PREVIOUS defaults still carry the old orders. We treat
 * "the exact previous-default value" as an implicit default (a user almost
 * never deliberately types 15 for model_hub or 50 for code — those numeric
 * constants only ever existed as defaults) and rewrite to the new order at
 * read time. Genuinely customised values (e.g. 33, 99) are preserved. This
 * makes a fresh install (Setup.bat → Start.bat) and any upgraded install
 * show the SAME order, per product requirement.
 *
 * The `from` values are the PREVIOUS defaults (the 2026-07-18 scheme).
 * Mirrored by the backend `forge_config.py` for pro / gomaster.
 */
const LEGACY_ORDER_MIGRATION: Record<string, { from: number; to: number }> = {
  app_builder: { from: 20, to: 10 },
  model_hub: { from: 15, to: 20 },
  model_builder: { from: 10, to: 30 },
  pro: { from: 30, to: 40 },
  gomaster: { from: 40, to: 50 },
  code: { from: 50, to: 60 },
  translate: { from: 60, to: 70 },
  ppt: { from: 70, to: 80 },
};

function migrateLegacyOrder(key: string, order: number): number {
  const rule = LEGACY_ORDER_MIGRATION[key];
  if (rule !== undefined && order === rule.from) {
    return rule.to;
  }
  return order;
}

function readToolbarModules(cfg: ForgeConfig | null | undefined): ToolbarModulesMap {
  if (cfg === null || cfg === undefined || typeof cfg !== "object") {
    return { ...DEFAULT_TOOLBAR_MODULES };
  }
  const ui = cfg["ui"];
  if (ui === null || ui === undefined || typeof ui !== "object") {
    return { ...DEFAULT_TOOLBAR_MODULES };
  }
  const tm = (ui as Record<string, unknown>)["toolbar_modules"];
  if (tm === null || tm === undefined || typeof tm !== "object") {
    return { ...DEFAULT_TOOLBAR_MODULES };
  }
  // Merge persisted entries onto defaults so newly-added modules
  // appear automatically (V2 follows V1's "additive defaults" pattern).
  const merged: ToolbarModulesMap = { ...DEFAULT_TOOLBAR_MODULES };
  for (const [key, raw] of Object.entries(tm as Record<string, unknown>)) {
    if (raw === null || typeof raw !== "object") continue;
    const r = raw as Record<string, unknown>;
    const fallback = DEFAULT_TOOLBAR_MODULES[key];
    merged[key] = {
      enabled: typeof r["enabled"] === "boolean"
        ? (r["enabled"] as boolean)
        : (fallback?.enabled ?? true),
      order: typeof r["order"] === "number"
        ? migrateLegacyOrder(key, r["order"] as number)
        : (fallback?.order ?? 999),
      mode: typeof r["mode"] === "string"
        ? (r["mode"] as string)
        : (fallback?.mode ?? key),
      i18n: typeof r["i18n"] === "string"
        ? (r["i18n"] as string)
        : (fallback?.i18n ?? `index.${key}`),
      icon: typeof r["icon"] === "string"
        ? (r["icon"] as string)
        : (fallback?.icon ?? "box"),
      // `hint` is presentation-only and never persisted; carry it from the
      // matching default entry (undefined when the default has none).
      hint: typeof r["hint"] === "string"
        ? (r["hint"] as string)
        : fallback?.hint,
      // GoMaster link mode ("external"/"agent"/"both"); backend-driven only.
      gomasterMode: typeof r["gomaster_mode"] === "string"
        ? (r["gomaster_mode"] as string)
        : fallback?.gomasterMode,
    };
  }
  return merged;
}

// ─── Composable ──────────────────────────────────────────────────────────────

export function useForgeConfig() {
  const config = _config;
  const loading = _loading;
  const { t } = useI18n();

  function toastError(msg: string): void {
    useToastStore().push({
      id: crypto.randomUUID(),
      kind: "error",
      message: msg,
      timeoutMs: 5000,
    });
  }

  async function load(): Promise<void> {
    if (_loadPromise !== null) {
      await _loadPromise;
      return;
    }
    loading.value = true;
    _loadPromise = (async () => {
      try {
        const res = await apiJson<ForgeConfigResponse>(
          "GET",
          "/api/forge-config",
        );
        _config.value = res.config;
      } catch (e) {
        toastError(
          e instanceof ApiError ? e.message : t("forgeConfig.loadFailed"),
        );
      } finally {
        loading.value = false;
      }
    })();
    try {
      await _loadPromise;
    } finally {
      _loadPromise = null;
    }
  }

  async function save(newConfig?: ForgeConfig): Promise<boolean> {
    loading.value = true;
    const payload = newConfig ?? _config.value;
    try {
      const res = await apiJson<ForgeConfigResponse>(
        "POST",
        "/api/forge-config",
        { config: payload },
      );
      _config.value = res.config;
      useToastStore().push({
        id: crypto.randomUUID(),
        kind: "success",
        message: t("forgeConfig.saved"),
        timeoutMs: 3000,
      });
      return true;
    } catch (e) {
      toastError(
        e instanceof ApiError ? e.message : t("forgeConfig.saveFailed"),
      );
      return false;
    } finally {
      loading.value = false;
    }
  }

  // ─── Toolbar modules surface ───────────────────────────────────────────

  /** Reactive read of `ui.toolbar_modules` (defaults filled in). */
  const toolbarModules = computed<ToolbarModulesMap>(() =>
    readToolbarModules(_config.value),
  );

  /** Sorted, enabled-only module list for the chat input toolbar. */
  const visibleToolbarModules = computed<VisibleToolbarModule[]>(() => {
    const map = toolbarModules.value;
    const list: VisibleToolbarModule[] = [];
    for (const [key, entry] of Object.entries(map)) {
      if (!entry.enabled) continue;
      list.push({ key, ...entry });
    }
    list.sort((a, b) => a.order - b.order);
    return list;
  });

  // The GoMaster link mode is exposed from the module-level `gomasterMode`
  // computed (defined above, i18n-free) so consumers outside a Vue `setup`
  // context can read it without triggering `useI18n()`. Re-exported here so
  // `useForgeConfig()`'s return shape is unchanged for its component callers.

  /**
   * Patch a single module entry and persist. The merge is shallow on
   * the entry itself, then nested under `ui.toolbar_modules.<key>` and
   * POST-merged onto the forge-config doc (backend itself shallow-merges
   * the top-level `ui` key, so we send a full replacement of the
   * `toolbar_modules` map to avoid losing peer entries).
   */
  async function patchModule(
    key: string,
    partial: Partial<ToolbarModuleEntry>,
  ): Promise<boolean> {
    if (Object.keys(_config.value).length === 0) {
      // Cold-start guard: load the doc once before patching so we
      // never blow away the persisted shape with a partial write.
      await load();
    }
    const current = readToolbarModules(_config.value);
    const fallback = DEFAULT_TOOLBAR_MODULES[key];
    const existing = current[key] ?? fallback ?? {
      enabled: true,
      order: 999,
      mode: key,
      i18n: `index.${key}`,
      icon: "box",
    };
    const next: ToolbarModuleEntry = { ...existing, ...partial };
    const nextMap: ToolbarModulesMap = { ...current, [key]: next };

    // Optimistically update local state so the UI flips immediately.
    const prevUi =
      typeof _config.value["ui"] === "object" && _config.value["ui"] !== null
        ? (_config.value["ui"] as Record<string, unknown>)
        : {};
    _config.value = {
      ..._config.value,
      ui: { ...prevUi, toolbar_modules: nextMap },
    };

    return save({ ui: { toolbar_modules: nextMap } });
  }

  // ─── AI Coding pills surface (T2.7-B) ──────────────────────────────────
  //
  // These gate the Claude Code / Open Code pills in the chat input toolbar.
  // V1 stored the flags as `forge_config.claude_code.enabled` /
  // `forge_config.opencode.enabled`, both shipping `true` in the factory
  // `forge_config.json` so a fresh install shows both pills out of the box
  // (see V1 `app.js:1932-1937`).
  // V2 stores the equivalent gate under `ai_coding.{cc,oc}.enabled`. The
  // backend GET `/api/forge-config` `setdefault`s both to `true` (V1 parity),
  // so the authoritative factory default lives server-side. The `false`
  // fallback below is purely defensive (config not yet loaded / malformed
  // node) and is not the intended factory state.

  type SubProvider = "cc" | "oc";

  function readAiCodingSub(
    cfg: ForgeConfig | null | undefined,
    sub: SubProvider,
  ): { enabled: boolean } {
    // Read the V2 pill gate `ai_coding.{cc,oc}.enabled`. The backend GET
    // already `setdefault`s both to `true` (V1 factory parity), so a loaded
    // config normally carries an explicit boolean. The `false` returned when
    // the config / node is missing is a defensive fallback for the
    // not-yet-loaded / malformed case, NOT the factory default.
    if (cfg === null || cfg === undefined || typeof cfg !== "object") {
      return { enabled: false };
    }
    const ai = cfg["ai_coding"];
    if (ai === null || ai === undefined || typeof ai !== "object") {
      return { enabled: false };
    }
    const node = (ai as Record<string, unknown>)[sub];
    if (node === null || node === undefined || typeof node !== "object") {
      return { enabled: false };
    }
    const enabled = (node as Record<string, unknown>)["enabled"];
    return { enabled: typeof enabled === "boolean" ? enabled : false };
  }

  /** Reactive `ai_coding.cc.enabled` (backend defaults to `true`, V1 parity). */
  const ccEnabled = computed<boolean>(
    () => readAiCodingSub(_config.value, "cc").enabled,
  );

  /** Reactive `ai_coding.oc.enabled` (backend defaults to `true`, V1 parity). */
  const ocEnabled = computed<boolean>(
    () => readAiCodingSub(_config.value, "oc").enabled,
  );

  // ─── App Builder workbench gate ────────────────────────────────────────
  //
  // The heavy App Builder model workbench (`AppBuilderWorkbenchOverlay.vue`)
  // is retained in full but HIDDEN BY DEFAULT — entering App Builder mode no
  // longer pops the heavy three-column console. The user opts in via
  // Settings → App Config. Stored under `ui.app_builder.show_workbench`
  // (default `false`). The code/functionality is untouched; this is purely a
  // visibility gate.

  function readShowWorkbench(cfg: ForgeConfig | null | undefined): boolean {
    if (cfg === null || cfg === undefined || typeof cfg !== "object") {
      return false;
    }
    const ui = cfg["ui"];
    if (ui === null || ui === undefined || typeof ui !== "object") {
      return false;
    }
    const ab = (ui as Record<string, unknown>)["app_builder"];
    if (ab === null || ab === undefined || typeof ab !== "object") {
      return false;
    }
    const v = (ab as Record<string, unknown>)["show_workbench"];
    return typeof v === "boolean" ? v : false;
  }

  /** Reactive `ui.app_builder.show_workbench` (default `false`). */
  const appBuilderShowWorkbench = computed<boolean>(() =>
    readShowWorkbench(_config.value),
  );

  /**
   * Patch `ui.app_builder.show_workbench` and persist. Preserves the full
   * `ui` sub-tree (backend shallow-merges the top-level `ui` key, so we
   * must resend `toolbar_modules` and any other peers to avoid stomping
   * them).
   */
  async function patchAppBuilderShowWorkbench(
    show: boolean,
  ): Promise<boolean> {
    if (Object.keys(_config.value).length === 0) {
      await load();
    }
    const prevUi =
      typeof _config.value["ui"] === "object" && _config.value["ui"] !== null
        ? (_config.value["ui"] as Record<string, unknown>)
        : {};
    const prevAb =
      typeof prevUi["app_builder"] === "object"
        && prevUi["app_builder"] !== null
        ? (prevUi["app_builder"] as Record<string, unknown>)
        : {};
    const nextAb = { ...prevAb, show_workbench: show };
    const nextUi: Record<string, unknown> = {
      ...prevUi,
      app_builder: nextAb,
    };

    // Optimistic local mutation so the UI reacts instantly.
    _config.value = { ..._config.value, ui: nextUi };

    return save({ ui: nextUi });
  }

  /**
   * Patch `ai_coding.{cc|oc}.enabled` and persist. Same pattern as
   * `patchModule`: cold-start load → optimistic local mutation →
   * `save({ ai_coding: {...} })`. Backend shallow-merges the
   * `ai_coding` key so we send the full sub-tree to avoid stomping
   * peer providers.
   */
  async function patchAiCoding(
    sub: SubProvider,
    partial: { enabled?: boolean },
  ): Promise<boolean> {
    if (Object.keys(_config.value).length === 0) {
      await load();
    }
    const prevAi =
      typeof _config.value["ai_coding"] === "object"
        && _config.value["ai_coding"] !== null
        ? (_config.value["ai_coding"] as Record<string, unknown>)
        : {};
    const prevSub =
      typeof prevAi[sub] === "object" && prevAi[sub] !== null
        ? (prevAi[sub] as Record<string, unknown>)
        : {};
    const nextSub = { ...prevSub, ...partial };
    const nextAi: Record<string, unknown> = { ...prevAi, [sub]: nextSub };

    // Optimistic local mutation so the pill flips instantly.
    _config.value = {
      ..._config.value,
      ai_coding: nextAi,
    };

    return save({ ai_coding: nextAi });
  }

  return {
    // Generic forge-config surface (back-compat — pr702 spec).
    config,
    loading,
    save,
    load,
    // Toolbar-module surface (T2.7-A).
    toolbarModules,
    visibleToolbarModules,
    gomasterMode,
    patchModule,
    // AI coding pills surface (T2.7-B).
    ccEnabled,
    ocEnabled,
    patchAiCoding,
    // App Builder workbench visibility gate (retained-but-hidden-by-default).
    appBuilderShowWorkbench,
    patchAppBuilderShowWorkbench,
  };
}

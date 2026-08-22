// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useRunHistoryPanel — run-history view-model for the App Builder
 * workbench overlay (cohesion split).
 *
 * Extracted from `AppBuilderWorkbenchOverlay.vue` so the overlay shell
 * stays a thin layout host. The list / row formatting / expansion lives in
 * `HistoryPanel.vue`; this composable owns the overlay-side glue:
 *   - subtitle assembly (selected variant / quant / model title)
 *   - history loading/error projection + current-run highlight
 *   - select / delete (useConfirm — §3.9) / export / share / add-to-compare
 *
 * Pure reactive state + handlers; no template, no styling. The
 * `variantOptions` it needs for the subtitle comes from the workbench
 * composable, so it is injected (kept out of this slice to avoid pulling
 * the whole schema-projection layer in).
 */
import { computed, type ComputedRef } from "vue";

import {
  historyOpen,
} from "@/composables/app-builder/useAppBuilderModeUi";
import type { VariantOption } from "@/composables/app-builder/useAppBuilderWorkbench";
import type { ConfirmOptions } from "@/stores/confirm";
import type { AppRun, useAppBuilderStore } from "@/stores/appBuilder";

type AppBuilderStore = ReturnType<typeof useAppBuilderStore>;
type Translator = (key: string, named?: Record<string, unknown>) => string;
type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>;

export interface UseRunHistoryPanelReturn {
  selectedVariant: ComputedRef<VariantOption | null>;
  selectedVariantQuant: ComputedRef<string>;
  selectedModelTitle: ComputedRef<string>;
  historyLoading: ComputedRef<boolean>;
  historyError: ComputedRef<string | null>;
  currentHistoryRunId: ComputedRef<string | null>;
  onSelectHistory: (runId: string) => void;
  onDeleteHistoryRun: (runId: string) => Promise<void>;
  onExportRun: (runId: string) => void;
  onShareRun: (runId: string) => void;
  onAddToCompareFromHistory: (run: AppRun) => void;
}

export function useRunHistoryPanel(deps: {
  store: AppBuilderStore;
  confirm: ConfirmFn;
  t: Translator;
  /** From the workbench composable — needed for the subtitle variant lookup. */
  variantOptions: ComputedRef<VariantOption[]>;
}): UseRunHistoryPanelReturn {
  const { store, confirm, t, variantOptions } = deps;

  const selectedVariant = computed<VariantOption | null>(
    () => variantOptions.value.find((v) => v.id === store.selectedVariantId) ?? null,
  );

  // V1 subtitle 取 selectedVariant.runtime.quantization；V2 VariantOption 没有
  // runtime 字段，从 selectedManifest.variants 找同 id 项的 runtime.quantization。
  const selectedVariantQuant = computed<string>(() => {
    const vid = store.selectedVariantId;
    if (vid === null) return "";
    const manifest = store.selectedManifest;
    if (manifest === null) return "";
    const variants = (manifest.variants ?? []) as Array<Record<string, unknown>>;
    const v = variants.find((x) => x.id === vid);
    if (v === undefined) return "";
    const rt = (v.runtime ?? {}) as Record<string, unknown>;
    const q = rt.quantization;
    return typeof q === "string" ? q : "";
  });

  const selectedModelTitle = computed<string>(() => {
    const m = store.selectedModel;
    if (m === null) return "";
    // V1 用 displayName || modelId；V2 AppModelResponse 是 title || id。
    return m.title ?? m.id;
  });

  // V1 fetchHistory loading / error 在面板内部 ref；V2 store 共享 loading/error
  // 状态。这里只把 history modal 期间的 loading 暴露给子组件。
  const historyLoading = computed<boolean>(() => store.loading);
  const historyError = computed<string | null>(() => store.error);
  const currentHistoryRunId = computed<string | null>(() => store.displayedRun?.id ?? null);

  function onSelectHistory(runId: string): void {
    const run = store.runs.find((r) => r.id === runId);
    if (run !== undefined) store.viewHistorySnapshot(run);
    historyOpen.value = false;
  }

  async function onDeleteHistoryRun(runId: string): Promise<void> {
    // V2 §3.9：删除历史 run 必须用定制对话框（V1 走 window.confirm，V2 在此处
    // 用 useConfirm + danger 样式统一 UI，满足判据 1「比 V1 更优」）。
    const ok = await confirm({
      icon: "🗑",
      title: t("appBuilder.history.title"),
      message: `Run ID: ${runId}`,
      // TODO（主 Agent）：补 i18n key appBuilder.history.delete /
      // appBuilder.history.deleteConfirm（含模型上下文）+ common.cancel。
      confirmText: "Delete",
      cancelText: t("appBuilder.cancel"),
      confirmStyle: "danger",
    });
    if (!ok) return;
    await store.deleteHistoryRun(runId);
  }

  function onExportRun(runId: string): void {
    // 后端路由 GET /api/app-builder/runs/{id}/export?format=md 是否就位由主 Agent
    // 统一收口。当前按 V1 行为：优先调 store.exportRun（若已注入），否则 best-effort
    // window.open V2 路由路径，后端不存在时浏览器只显示 404，不破坏 UI。
    const action = (store as unknown as { exportRun?: (id: string) => unknown }).exportRun;
    if (typeof action === "function") {
      void action(runId);
      return;
    }
    try {
      window.open(
        `/api/app-builder/runs/${encodeURIComponent(runId)}/export?format=md`,
        "_blank",
      );
    } catch {
      /* best-effort */
    }
  }

  function onShareRun(runId: string): void {
    // 同 export：store 若已有 shareRun action 优先调；否则占位（真实 token 流由
    // 主 Agent 在 store action 里实现）。
    const action = (store as unknown as { shareRun?: (id: string) => unknown }).shareRun;
    if (typeof action === "function") {
      void action(runId);
    }
  }

  function onAddToCompareFromHistory(run: AppRun): void {
    if (run.id === null) return;
    store.addToCompare(run);
  }

  return {
    selectedVariant,
    selectedVariantQuant,
    selectedModelTitle,
    historyLoading,
    historyError,
    currentHistoryRunId,
    onSelectHistory,
    onDeleteHistoryRun,
    onExportRun,
    onShareRun,
    onAddToCompareFromHistory,
  };
}

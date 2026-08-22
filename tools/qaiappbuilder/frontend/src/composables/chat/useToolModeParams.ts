// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useToolModeParams` — per-tab tool-mode parameter state for the chat
 * composer's mode sub-toolbars (V1 `app.js:1238-1266` translate auto-detect
 * + per-mode param wiring + `index.html:1313-1351` CC effort dropdown).
 *
 * Extracted from `ChatComposer.vue` (F1⑤ cohesion split). Owns:
 *   - code mode params  (speed / persona / file_path / repo_url)
 *   - translate params  (target_lang + UI-only auto-detect toggle)
 *   - ppt params        (length)
 *   - translate auto-detect (`detectTargetLang` + the two watchers that
 *     run detection while in translate mode and reset auto on mode entry)
 *   - CC effort dropdown (current effort / label / options / picker)
 *
 * All persisted params read/write through the active tab's `toolParams`
 * (V1 `_toolParamsComputed` parity) so the transport keeps aggregating
 * them into the outgoing `tool_mode` / `tool_params`. The behaviour is
 * byte-for-byte the composer's previous inline logic; this only relocates
 * it into a single-responsibility composable.
 *
 * Per-call (not a singleton): `translateAuto` and the effort-dropdown open
 * flag are local per-composer UI state, exactly as before.
 */
import { ref, computed, watch, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore } from "@/stores/chatTabs";
import type { ToolMode } from "@/stores/ui";
import type { useClaudeCode } from "@/composables/useClaudeCode";

type ClaudeCode = ReturnType<typeof useClaudeCode>;

export type CodeSpeed = "fast" | "think" | "expert";
export type TranslateLang = "zh-CN" | "en" | "zh-TW";
export type PptLength = "smart" | "short" | "medium" | "long";
export type CcEffort = "low" | "medium" | "high" | "max" | null;

export interface UseToolModeParams {
  codeSpeed: ComputedRef<CodeSpeed>;
  codePersona: ComputedRef<string | null>;
  codeFilePath: ComputedRef<string>;
  codeRepoUrl: ComputedRef<string>;
  translateLang: ComputedRef<TranslateLang>;
  pptLength: ComputedRef<PptLength>;
  translateAuto: Ref<boolean>;
  updateCodeSpeed: (v: CodeSpeed) => void;
  updateCodePersona: (v: string | null) => void;
  updateCodeFilePath: (v: string) => void;
  updateCodeRepoUrl: (v: string) => void;
  updateTranslateLang: (v: TranslateLang) => void;
  updatePptLength: (v: PptLength) => void;
  // CC effort dropdown
  ccCurrentEffort: ComputedRef<CcEffort>;
  ccEffortLabel: ComputedRef<string>;
  ccEffortOptions: ReadonlyArray<{ value: CcEffort; labelKey: string }>;
  pickCcEffort: (value: CcEffort) => void;
}

/**
 * V1 `_detectLang` (app.js L1238-1266): pick the translate target by
 * inspecting the dominant script of the input. CJK-dominant → English;
 * otherwise → Simplified Chinese. Only used while auto-detect is on; a
 * manual language pick flips `translateAuto` off (handled in the frame).
 */
export function detectTargetLang(textValue: string): TranslateLang | null {
  const stripped = textValue.replace(/\s/g, "");
  if (stripped.length === 0) return null;
  const cjk = stripped.match(/[\u4e00-\u9fff\u3400-\u4dbf]/g);
  const cjkCount = cjk ? cjk.length : 0;
  const ratio = cjkCount / stripped.length;
  // CJK-dominant input → translate to English; else → Simplified Chinese.
  return ratio >= 0.3 ? "en" : "zh-CN";
}

/**
 * @param textRef         The composer textarea value (translate auto-detect).
 * @param effectiveMode   Reactive effective tool mode (ChatTab.activeMode
 *                        widened with the ppt UI-only mode).
 * @param claudeCode      The shared `useClaudeCode()` instance (CC effort).
 * @param effortDropdownOpen  The composer's effort-dropdown open ref, closed
 *                        after picking (kept in the composer because the ESC
 *                        fan-out also toggles it).
 */
export function useToolModeParams(
  textRef: Ref<string>,
  effectiveMode: ComputedRef<ToolMode>,
  claudeCode: ClaudeCode,
  effortDropdownOpen: Ref<boolean>,
): UseToolModeParams {
  const store = useChatTabsStore();
  const { t } = useI18n();

  // These read/write through the active tab's `toolParams` so the
  // transport (useChatTransport) can aggregate them into the outgoing
  // `tool_mode` + `tool_params` (V1 `_toolParamsComputed` parity).
  const codeSpeed = computed<CodeSpeed>(
    () => (store.activeTab?.toolParams.speed ?? "fast") as CodeSpeed,
  );
  const codePersona = computed<string | null>(
    () => store.activeTab?.toolParams.persona ?? null,
  );
  const codeFilePath = computed<string>(
    () => store.activeTab?.toolParams.file_path ?? "",
  );
  const codeRepoUrl = computed<string>(
    () => store.activeTab?.toolParams.repo_url ?? "",
  );
  const translateLang = computed<TranslateLang>(
    () => (store.activeTab?.toolParams.target_lang ?? "zh-CN") as TranslateLang,
  );
  const pptLength = computed<PptLength>(
    () => (store.activeTab?.toolParams.length ?? "smart") as PptLength,
  );
  // Auto-detect is a UI-only toggle (not sent to the backend). Kept local
  // per-composer; defaults on (V1 parity).
  const translateAuto = ref<boolean>(true);

  function updateCodeSpeed(v: CodeSpeed): void {
    const tab = store.activeTab;
    if (tab !== null) store.setToolParams(tab.id, { speed: v });
  }
  function updateCodePersona(v: string | null): void {
    const tab = store.activeTab;
    if (tab !== null) store.setToolParams(tab.id, { persona: v ?? undefined });
  }
  function updateCodeFilePath(v: string): void {
    const tab = store.activeTab;
    if (tab !== null) store.setToolParams(tab.id, { file_path: v });
  }
  function updateCodeRepoUrl(v: string): void {
    const tab = store.activeTab;
    if (tab !== null) store.setToolParams(tab.id, { repo_url: v });
  }
  function updateTranslateLang(v: TranslateLang): void {
    const tab = store.activeTab;
    if (tab !== null) store.setToolParams(tab.id, { target_lang: v });
  }
  function updatePptLength(v: PptLength): void {
    const tab = store.activeTab;
    if (tab !== null) store.setToolParams(tab.id, { length: v });
  }

  // Run auto-detection while in translate mode with auto-detect enabled.
  watch([textRef, translateAuto, effectiveMode], () => {
    if (effectiveMode.value !== "translate" || !translateAuto.value) return;
    const detected = detectTargetLang(textRef.value);
    if (detected !== null && detected !== translateLang.value) {
      updateTranslateLang(detected);
    }
  });

  // V1 parity (app.js:1258-1261): when activeToolMode switches to
  // "translate", reset translateAuto to true (auto-detect on).
  watch(effectiveMode, (newMode, oldMode) => {
    if (newMode === "translate" && oldMode !== "translate") {
      translateAuto.value = true;
    }
  });

  // ─── CC effort dropdown (V1 index.html:1313-1351) ──────────────────────
  // Only visible when CC mode is on AND a session is active. 5 options:
  // default(null) / low / medium / high / max. Click → claudeCode.setEffort
  // scoped to the active session id; trigger label shows the current effort
  // in Chinese + 🧠 emoji. Disabled while CC is streaming (V1 parity).
  const ccCurrentEffort = computed<CcEffort>(() => {
    const sid = claudeCode.activeSessionId.value;
    if (sid === null) return null;
    const s = claudeCode.sessions.value.find((x) => x.session_id === sid);
    const e = s?.effort ?? null;
    if (e === "low" || e === "medium" || e === "high" || e === "max") return e;
    return null;
  });

  const ccEffortLabel = computed<string>(() => {
    const e = ccCurrentEffort.value;
    if (e === "low") return t("index.effortLow");
    if (e === "medium") return t("index.effortMedium");
    if (e === "high") return t("index.effortHigh");
    if (e === "max") return t("index.effortMax");
    return t("index.effortDefault");
  });

  const ccEffortOptions: ReadonlyArray<{ value: CcEffort; labelKey: string }> = [
    { value: null, labelKey: "index.effortDefaultEmoji" },
    { value: "low", labelKey: "index.effortLow" },
    { value: "medium", labelKey: "index.effortMedium" },
    { value: "high", labelKey: "index.effortHigh" },
    { value: "max", labelKey: "index.effortMax" },
  ];

  function pickCcEffort(value: CcEffort): void {
    const sid = claudeCode.activeSessionId.value;
    if (sid !== null) {
      void claudeCode.setEffort(sid, value);
    }
    effortDropdownOpen.value = false;
  }

  return {
    codeSpeed,
    codePersona,
    codeFilePath,
    codeRepoUrl,
    translateLang,
    pptLength,
    translateAuto,
    updateCodeSpeed,
    updateCodePersona,
    updateCodeFilePath,
    updateCodeRepoUrl,
    updateTranslateLang,
    updatePptLength,
    ccCurrentEffort,
    ccEffortLabel,
    ccEffortOptions,
    pickCcEffort,
  };
}

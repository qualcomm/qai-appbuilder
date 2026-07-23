<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModeFrameAppBuilder — chat-input sub-toolbar for `app-builder` mode.
 *
 * Block-4: mirrors V1 `index.html:1513-1594` — exit badge + the three
 * App-Builder workflow controls (Send to Chat / Edit Prompt / Compare)
 * plus a workbench toggle. The workbench overlay itself
 * (`AppBuilderWorkbenchOverlay.vue`) renders above the message list and
 * shares state via the `appBuilder` Pinia store + the module-level UI
 * toggles in `useAppBuilderModeUi`.
 *
 *   - Send to Chat → `useAppBuilderChatBridge.sendToChat()` injects the
 *     current run result into the active chat tab (disabled until a run
 *     has output).
 *   - Edit Prompt  → toggles the send-to-chat prompt dialog (rendered by
 *     the workbench overlay).
 *   - Compare      → toggles the compare tray (disabled when empty).
 *   - Workbench    → show/hide the workbench overlay.
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useAppBuilderStore } from "@/stores/appBuilder";
import type { AppModelResponse, AppEntry, AppRunState, PackageState } from "@/stores/appBuilder";
import { useAppBuilderChatBridge } from "@/composables/chat/useAppBuilderChatBridge";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useForgeConfig } from "@/composables/useForgeConfig";
import { useToast } from "@/composables/useToast";
import { useConfirm } from "@/composables/useConfirm";
import { formatSpeed, formatEta, formatBytes } from "@/composables/downloads/format";
import {
  togglePromptDialog,
} from "@/composables/app-builder/useAppBuilderModeUi";
import { useModeFrameTriggers } from "@/composables/useModeFrameTriggers";
import { extractModelWorkdirFromMessages } from "@/utils/modelWorkdir";
import PromoteToAppBuilderCard from "@/components/app-builder/model-builder/PromoteToAppBuilderCard.vue";

const { t } = useI18n();
const store = useAppBuilderStore();
const bridge = useAppBuilderChatBridge();
const tabs = useChatTabsStore();
const toast = useToast();
const { confirm } = useConfirm();

// Workbench visibility gate (retained-but-hidden-by-default). The three
// run/prompt/compare controls only make sense when the workbench overlay is
// enabled in Settings; when it is off (default) they are permanently inert,
// so we hide them entirely (see #6).
// Also read the raw forge config so the Promote popover can derive its
// workspace root (mirrors ModeFrameModelBuilder — parity).
const { appBuilderShowWorkbench, config: forgeConfig } = useForgeConfig();

// Load the imported-model registry when this toolbar mounts (entering
// app-builder mode). Previously the heavy workbench overlay owned this
// fetch; now that the workbench is hidden by default, the toolbar must
// populate `store.models` itself so the model-select menu isn't empty.
onMounted(() => {
  if (store.models.length === 0) void store.fetchModels();
});

const emit = defineEmits<{
  exit: [];
}>();

const hasRunOutput = computed(
  () => store.currentRun !== null && store.currentRun.output !== null,
);
const compareCount = computed(() => store.compareItems.length);

// ── Imported-model multi-select (popup menu) ────────────────────────────────
// The selection is stored on the appBuilder store (single source of truth)
// and projected into the active chat tab's tool_params via the bridge so the
// next message carries `selected_model_ids` for the backend to inject each
// selected model's SKILL + runner.py path into the system prompt.
const modelMenuOpen = ref(false);
const models = computed(() => store.models);
const selectedIds = computed(() => store.selectedModelIds);
const selectedCount = computed(() => selectedIds.value.length);

function isModelSelected(id: string): boolean {
  return selectedIds.value.includes(id);
}

/** Last taxonomy segment as a small task badge (best-effort). */
function kindOf(taxonomy: string[] | undefined): string | null {
  if (!Array.isArray(taxonomy) || taxonomy.length === 0) return null;
  return taxonomy[taxonomy.length - 1] ?? null;
}

/**
 * Install-state hint for a not-yet-ready model (Phase 4, P4.1). A built-in
 * pack whose runner auto-downloads weights on the first Run shows
 * "auto-downloads on first run"; anything else that is NotInstalled needs a
 * manual conversion/import step. Ready models show no hint. Returns the
 * localized string, or null when the model is ready.
 */
function downloadHintOf(m: AppModelResponse): string | null {
  const notReady = m.status === "NotInstalled" || m.enabled === false;
  if (!notReady) return null;
  return m.auto_download
    ? t("appBuilder.modelStrip.autoDownloadHint")
    : t("appBuilder.modelStrip.needsConversionHint");
}

function toggleModelMenu(): void {
  modelMenuOpen.value = !modelMenuOpen.value;
}

/**
 * Discrete UI state for a model row's right-side affordance. Drives the
 * one-of: ✓ ready icon / Download button / live progress bar / error+retry.
 *
 *   - "downloading" / "extracting" / "error" come straight from the store's
 *     per-id download slot (an in-flight or failed download always wins so the
 *     row keeps showing progress even if `status` momentarily lags).
 *   - "ready" = installed weights present AND runnable (Ready + enabled).
 *   - "download" = not installed, weights fetchable (auto_download built-in).
 *   - "hint" = not installed and NOT fetchable → keep the "needs conversion"
 *     hint (no Download button that would 404).
 */
type RowState =
  | "ready"
  | "download"
  | "downloading"
  | "extracting"
  | "error"
  | "hint";

function dlOf(id: string) {
  return store.downloadStateOf(id);
}

function rowState(m: AppModelResponse): RowState {
  const dl = dlOf(m.id);
  if (dl.status === "downloading") return "downloading";
  if (dl.status === "extracting") return "extracting";
  if (dl.status === "error") return "error";
  const ready = m.status !== "NotInstalled" && m.enabled !== false;
  if (ready) return "ready";
  if (m.auto_download === true) return "download";
  return "hint";
}

/** Progress bar width % (0..100). Null percent → 0 (indeterminate handled in CSS). */
function progressPercent(id: string): number {
  const p = dlOf(id).percent;
  if (p === null || !Number.isFinite(p)) return 0;
  return Math.max(0, Math.min(100, Math.round(p)));
}

/** Whether the bar should render indeterminate (unknown total → null percent). */
function isIndeterminate(id: string): boolean {
  return dlOf(id).percent === null;
}

/** Human display label for the percent chip ("NN%" or "…" when unknown). */
function percentLabel(id: string): string {
  const p = dlOf(id).percent;
  return p === null ? "…" : `${Math.round(p)}%`;
}

/**
 * Secondary "speed · ETA" line under the bar. Reuses the Download Center's
 * verified `formatSpeed` / `formatEta` helpers (1024-base units, ceil ETA).
 * Falls back to empty when speed is 0 / eta is null so we don't show noise.
 */
function speedEtaLabel(id: string): string {
  const dl = dlOf(id);
  const speed = formatSpeed(dl.speedBps);
  const eta = formatEta(dl.etaSeconds);
  const etaText = eta !== "" ? t("appBuilder.modelStrip.etaLeft", { eta }) : "";
  if (speed !== "" && etaText !== "") return `${speed} · ${etaText}`;
  return speed !== "" ? speed : etaText;
}

/** Start (or retry) a weight download for a model. */
function onDownload(id: string): void {
  void store.startWeightDownload(id);
}

/** Cancel an in-flight weight download for a model. */
function onCancel(id: string): void {
  void store.cancelWeightDownload(id);
}

function onToggleModel(id: string): void {
  store.toggleSelectedModelId(id);
  // Project the updated multi-select onto the active tab so the next message
  // carries `selected_model_ids` (bridge also ensures app-builder mode).
  bridge.applyToolParams();
}

function onExit(): void {
  emit("exit");
}

/**
 * Jump the active tab into Model Builder mode so the user can import a model
 * (surfaced from the "no models imported" empty state — #5). Mirrors how the
 * chat bridge switches modes (`tabs.setActiveMode(tab.id, mode)`); the
 * composer's sub-toolbar selection is driven purely by the per-tab
 * `activeMode` (see ChatComposer `effectiveMode`), so no UI-store mirror is
 * needed. Also closes the model menu popup.
 */
function onOpenModelBuilder(): void {
  const tab = tabs.activeTab;
  if (tab === null) return;
  modelMenuOpen.value = false;
  tabs.setActiveMode(tab.id, "model-build");
}

function onSendToChat(): void {
  if (!hasRunOutput.value) return;
  bridge.sendToChat();
}

function onEditPrompt(): void {
  togglePromptDialog();
}

function onCompare(): void {
  if (compareCount.value === 0) return;
  store.toggleCompare();
}

// ── Generated-apps menu (Phase 4, plan §9.1) ────────────────────────────────
// A second popup menu next to the model menu that lists generated *app
// projects* (data/app_builder/<id>) and their managed-process state. Backed by
// the store's /api/app-builder/apps/* actions.
const appsMenuOpen = ref(false);
const apps = computed<AppEntry[]>(() => store.apps);
// App id whose logs panel is expanded inline (only one at a time).
const expandedLogsAppId = ref<string | null>(null);

function toggleAppsMenu(): void {
  appsMenuOpen.value = !appsMenuOpen.value;
  if (appsMenuOpen.value) void store.fetchApps();
}

/** The live run-state slot for an app (run/stop/logs snapshot), or null. */
function runStateOf(id: string): AppRunState | null {
  return store.appRunStateOf(id);
}

/**
 * Effective status for the row badge: the managed run-state (freshest, from
 * run/stop/logs) wins over the list snapshot's `status` (State-Truth-First).
 */
function statusOf(app: AppEntry): string {
  return runStateOf(app.id)?.status ?? app.status ?? "stopped";
}

/** Localized status label keyed off the (lowercased) status string. */
function statusLabel(app: AppEntry): string {
  const map: Record<string, string> = {
    stopped: t("appBuilder.apps.statusStopped"),
    starting: t("appBuilder.apps.statusStarting"),
    stopping: t("appBuilder.apps.statusStopping"),
    ready: t("appBuilder.apps.statusReady"),
    running: t("appBuilder.apps.statusRunning"),
    failed: t("appBuilder.apps.statusFailed"),
    packaging: t("appBuilder.apps.statusPackaging"),
  };
  return map[statusOf(app).toLowerCase()] ?? statusOf(app);
}

/** Whether the app is in a "reachable now" state (Open affordance enabled). */
function isReady(app: AppEntry): boolean {
  const s = statusOf(app).toLowerCase();
  const rs = runStateOf(app.id);
  return (s === "ready" || s === "running") && !!rs?.url;
}

/** Whether a stop is in flight (client-side optimistic transient status). */
function isStopping(app: AppEntry): boolean {
  return statusOf(app).toLowerCase() === "stopping";
}

/**
 * Whether Run should be disabled: the app already has a live/in-flight
 * managed process (starting/running/ready), OR a stop is in flight
 * (stopping) — re-running mid-stop would race the teardown.
 */
function isBusy(app: AppEntry): boolean {
  const s = statusOf(app).toLowerCase();
  return (
    s === "starting" || s === "running" || s === "ready" || s === "stopping"
  );
}

/**
 * Whether Stop should be enabled: only when the app has a live or in-flight
 * managed process AND a stop is not already in flight. A stopped/failed app
 * has nothing to stop; a "stopping" app already has a stop in progress, so
 * the button disables to give immediate click feedback + prevent repeat
 * clicks (issue: no feedback on click → users click repeatedly).
 */
function isStoppable(app: AppEntry): boolean {
  const s = statusOf(app).toLowerCase();
  return s === "starting" || s === "running" || s === "ready";
}

/** The loopback URL to open, or null when not running. */
function runUrlOf(app: AppEntry): string | null {
  return runStateOf(app.id)?.url ?? app.preview_url ?? null;
}

/** The manual command line for a failed/started app (copyable), or null. */
function manualCommandOf(app: AppEntry): string | null {
  return runStateOf(app.id)?.manualCommand ?? null;
}

/**
 * Open a backend-returned loopback URL in a new tab. Only accepts the
 * `http://127.0.0.1:<port>/` (or localhost) URL the backend returned in the
 * managed-run snapshot — never an LLM-authored/arbitrary URL (plan §5.8).
 */
function openUrl(url: string): void {
  if (!/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url)) {
    // Refuse anything that isn't a loopback URL from the backend.
    return;
  }
  window.open(url, "_blank", "noopener");
}

/** Map a store error code (app_builder.<suffix>) to localized toast text. */
function localizedError(code: string | null): string {
  const suffix = (code ?? "").replace(/^app_builder\./, "");
  const known = [
    "app_not_found",
    "app_invalid",
    "app_already_running",
    "port_in_use",
    "no_bindable_port",
    "app_start_failed",
    "app_not_running",
    "package_failed",
    "delete_failed",
  ];
  const key = known.includes(suffix) ? suffix : "unknown";
  return t(`appBuilder.apps.errors.${key}`);
}

/** Extract a stable error code from a thrown ApiError-like value (best-effort). */
function _codeOf(e: unknown): string | null {
  if (e && typeof e === "object" && "code" in e) {
    const c = (e as { code?: unknown }).code;
    return typeof c === "string" ? c : null;
  }
  return null;
}

async function onRunApp(app: AppEntry): Promise<void> {
  // Re-entry guard: the button is :disabled when isBusy(), but Vue applies
  // that attribute on the next flush (a microtask after the click), so two
  // clicks dispatched in the same task can both land here before the DOM
  // disables. Bail if a launch/run is already in flight so a fast double-tap
  // cannot fire a second POST /run (which the backend would answer with
  // "already running" but the user perceives as an error).
  if (isBusy(app)) return;
  try {
    const res = await store.runApp(app.id);
    if (typeof res.url === "string" && res.url !== "") {
      openUrl(res.url);
    }
  } catch {
    // The store recorded the error code on the run-state slot; toast it.
    toast.error(localizedError(runStateOf(app.id)?.error ?? null));
  }
}

async function onStopApp(app: AppEntry): Promise<void> {
  try {
    await store.stopApp(app.id);
  } catch {
    toast.error(localizedError(runStateOf(app.id)?.error ?? null));
  }
}

function onOpenApp(app: AppEntry): void {
  const url = runUrlOf(app);
  if (url !== null) openUrl(url);
}

async function onShowLogs(app: AppEntry): Promise<void> {
  if (expandedLogsAppId.value === app.id) {
    expandedLogsAppId.value = null;
    return;
  }
  expandedLogsAppId.value = app.id;
  await store.fetchAppLogs(app.id);
}

function logsOf(id: string): string {
  return store.appLogs[id] ?? "";
}

/** Manually refresh the open log panel (re-fetch logs + reconcile status). */
async function onRefreshLogs(app: AppEntry): Promise<void> {
  await store.fetchAppLogs(app.id);
}

/** Copy the app's current logs to the clipboard (one-click, no prompt). */
async function onCopyLogs(app: AppEntry): Promise<void> {
  const text = logsOf(app.id);
  if (text === "") return;
  try {
    await navigator.clipboard.writeText(text);
    toast.success(t("appBuilder.apps.copied"));
  } catch {
    toast.error(t("appBuilder.apps.copyFailed"));
  }
}

async function onCopyCommand(app: AppEntry): Promise<void> {
  const cmd = manualCommandOf(app);
  if (cmd === null || cmd === "") return;
  try {
    await navigator.clipboard.writeText(cmd);
    toast.success(t("appBuilder.apps.copied"));
  } catch {
    toast.error(t("appBuilder.apps.copyFailed"));
  }
}

// ── Packaging (Phase 5, plan §9.3) ──────────────────────────────────────────
/** The per-app packaging slot, or undefined when never started. */
function packageStateOf(id: string): PackageState | undefined {
  return store.packageStateOf(id);
}

/** Whether a packaging job is in flight (drives the progress bar + disable). */
function isPackaging(id: string): boolean {
  return packageStateOf(id)?.running === true;
}

/** Progress-bar width % (0..100) for the packaging job. */
function packagePercent(id: string): number {
  const p = packageStateOf(id)?.percent ?? 0;
  return Math.max(0, Math.min(100, p));
}

/** Percent chip label ("NN%") for the packaging job. */
function packagePercentLabel(id: string): string {
  return `${Math.round(packagePercent(id))}%`;
}

/** Localized phase/message line under the packaging bar. */
function packageStatusLabel(id: string): string {
  const st = packageStateOf(id);
  if (st === undefined) return "";
  if (st.message !== "") return st.message;
  return t("appBuilder.apps.packageProgress", {
    percent: Math.round(packagePercent(id)),
  });
}

/**
 * Package an app to a distributable ZIP. On success (State-Truth-First: only
 * after the SSE stream resolves on `event: done`) toast the zip path + a
 * human-readable size. On failure toast the localized error keyed off the
 * server code (plan §5.7) recorded on the package slot.
 */
async function onPackageApp(app: AppEntry): Promise<void> {
  if (isPackaging(app.id)) return;
  try {
    await store.packageApp(app.id);
    const st = packageStateOf(app.id);
    if (st !== undefined && st.isComplete) {
      toast.success(
        t("appBuilder.apps.packageDone", {
          path: st.zipPath ?? "",
          size: formatBytes(st.sizeBytes),
        }),
      );
    }
  } catch {
    toast.error(localizedError(packageStateOf(app.id)?.error ?? null));
  }
}

/** Cancel an in-flight packaging job. */
function onCancelPackage(app: AppEntry): void {
  void store.cancelPackage(app.id);
}

/**
 * Whether the app has a completed package result to display persistently.
 * The transient toast is easy to miss (packaging takes seconds/minutes and the
 * toast dismisses quickly), so we surface the produced zip path directly on
 * the app row until dismissed. Distinct from ``isPackaging`` (which is the
 * live progress bar).
 */
function hasPackageResult(id: string): boolean {
  const st = packageStateOf(id);
  return (
    st !== undefined
    && st.isComplete === true
    && st.running === false
    && !!st.zipPath
  );
}

/** The completed zip path for an app (empty string when none). */
function packagedZipPath(id: string): string {
  return packageStateOf(id)?.zipPath ?? "";
}

/** Human-readable size for the completed zip ("326.1 MB"). */
function packagedSize(id: string): string {
  return formatBytes(packageStateOf(id)?.sizeBytes);
}

/** Copy the packaged zip path to the clipboard so the user can paste it into
 *  Explorer / a terminal. Mirrors the manual-command copy pattern. */
async function onCopyPackagePath(app: AppEntry): Promise<void> {
  const path = packagedZipPath(app.id);
  if (!path) return;
  try {
    await navigator.clipboard.writeText(path);
    toast.success(t("appBuilder.apps.copied"));
  } catch {
    toast.error(t("appBuilder.apps.copyFailed"));
  }
}

/** Dismiss the completed-package panel (clears the result slot; the user
 *  can package again later — the row's Package button remains available). */
function onDismissPackageResult(app: AppEntry): void {
  store.clearPackageResult(app.id);
}

// ── Delete (destructive) ──────────────────────────────────────────────────────
// Deleting removes the on-disk dev project (data/app_builder/<id>/); the
// backend stops the managed process first if running. Packaged zips in the
// workspace are NOT affected. Guarded by a danger confirm dialog (useConfirm —
// NOT window.confirm, per PROJECT-RULES §3.9).
const _deletingIds = ref<Set<string>>(new Set());

function isDeleting(id: string): boolean {
  return _deletingIds.value.has(id);
}

async function onDeleteApp(app: AppEntry): Promise<void> {
  if (isDeleting(app.id) || isPackaging(app.id)) return;
  const ok = await confirm({
    title: t("appBuilder.apps.deleteConfirmTitle"),
    message: t("appBuilder.apps.deleteConfirmMessage", { name: app.name }),
    confirmText: t("appBuilder.apps.delete"),
    cancelText: t("appBuilder.apps.deleteCancel"),
    confirmStyle: "danger",
    icon: "\uD83D\uDDD1\uFE0F",
  });
  if (!ok) return;
  // Track deleting state (reactive Set → reassign for Vue reactivity).
  _deletingIds.value = new Set(_deletingIds.value).add(app.id);
  try {
    await store.deleteApp(app.id);
    toast.success(t("appBuilder.apps.deleteDone", { name: app.name }));
  } catch (e) {
    toast.error(localizedError(_codeOf(e)));
  } finally {
    const next = new Set(_deletingIds.value);
    next.delete(app.id);
    _deletingIds.value = next;
  }
}

// ── Inline model delete (user-imported models only) ─────────────────────────
// Provides a direct-in-menu path for the destructive "delete an imported
// model" action so the user does not have to open the App Builder workbench
// + drill into a per-model drawer just to remove one. Only visible when the
// row is a user-imported model (built-ins are protected server-side with
// HTTP 403 anyway, but the button also hides for those rows). Confirmation
// goes through the same global ConfirmDialog + toast as ``onDeleteApp``
// (per PROJECT-RULES §3.9 no ``window.confirm``); the store already
// re-fetches the authoritative list on success so gallery / taxonomy /
// count reflect the delete without local optimistic guessing.
type ModelRow = (typeof store.models)[number];
const _deletingModelIds = ref<Set<string>>(new Set());

function isDeletingModel(id: string): boolean {
  return _deletingModelIds.value.has(id);
}

async function onDeleteModel(m: ModelRow): Promise<void> {
  if (isDeletingModel(m.id)) return;
  // Defence in depth: never even PROMPT for a built-in. The template
  // already gates on ``m.user_imported === true`` so this branch is
  // structurally unreachable, but guarding here keeps the action safe
  // if the button is ever wired from another callsite.
  if (m.user_imported !== true) return;
  const displayName = m.title !== "" ? m.title : m.id;
  const ok = await confirm({
    title: t("appBuilder.modelStrip.deleteConfirmTitle"),
    message: t("appBuilder.modelStrip.deleteConfirmMessage", {
      name: displayName,
    }),
    confirmText: t("appBuilder.modelStrip.deleteConfirmBtn"),
    cancelText: t("appBuilder.modelStrip.deleteCancelBtn"),
    confirmStyle: "danger",
    icon: "\uD83D\uDDD1\uFE0F",
  });
  if (!ok) return;
  _deletingModelIds.value = new Set(_deletingModelIds.value).add(m.id);
  try {
    // ``store.deleteModel`` returns ``{ok, warnings}`` (缺陷 P4): a 200
    // response carries non-fatal warnings from file cleanup (e.g. an
    // AV-locked ``.bin`` file left on disk); a network / 4xx / 5xx failure
    // resolves ``{ok: false, warnings: []}`` and sets ``store.error``.
    const res = await store.deleteModel(m.id);
    if (!res.ok) {
      toast.error(
        t("appBuilder.modelStrip.deleteFailed", {
          name: displayName,
          msg: store.error ?? "Unknown error",
        }),
      );
      return;
    }
    if (res.warnings.length > 0) {
      toast.success(
        t("appBuilder.modelStrip.deleteDoneWithWarnings", {
          name: displayName,
          n: res.warnings.length,
        }),
      );
      // Also log full warning list so power users can inspect via the
      // browser devtools (toast is capped at one line for readability).
      // eslint-disable-next-line no-console
      console.warn(
        "[app-builder] model delete warnings for",
        m.id,
        res.warnings,
      );
    } else {
      toast.success(
        t("appBuilder.modelStrip.deleteDone", { name: displayName }),
      );
    }
  } catch (e) {
    // ``store.deleteModel`` swallows fetch errors internally and returns
    // ``ok=false``, but a hostile clock-skew / boundary case could still
    // throw at the store layer — surface as a toast.
    toast.error(
      t("appBuilder.modelStrip.deleteFailed", {
        name: displayName,
        msg: e instanceof Error ? e.message : String(e),
      }),
    );
  } finally {
    const next = new Set(_deletingModelIds.value);
    next.delete(m.id);
    _deletingModelIds.value = next;
  }
}

// ── Promote to App Builder (parity with Model Builder mode) ─────────────────
// Symmetric to ModeFrameModelBuilder's promote entry: exposes the same
// PromoteToAppBuilderCard as a popover in this mode's toolbar too, so users
// don't have to switch back to Model Builder to promote a converted model.
// The workdir is derived from the active chat's messages via
// `extractModelWorkdirFromMessages` — the SAME scan Model Builder mode uses
// as its fallback source, so promote works after a chat-driven conversion
// without any manual model-path upload.
const promotePanelOpen = ref(false);
const workspaceModelRoot = computed<string>(() => {
  const cfg = forgeConfig.value as Record<string, unknown> | null;
  if (cfg === null || typeof cfg !== "object") return "";
  const ws = cfg["workspace"];
  if (ws !== null && typeof ws === "object") {
    const root = (ws as Record<string, unknown>)["model_root"];
    if (typeof root === "string" && root.trim() !== "") return root;
  }
  return "";
});
const promoteWorkdir = computed<string>(() =>
  extractModelWorkdirFromMessages(
    tabs.activeTab?.messages,
    workspaceModelRoot.value || undefined,
  ),
);

// "Ready" badge on the Promote button — symmetric with
// ModeFrameModelBuilder's `promoteReady`. When the active conversation
// references a promote-able model workdir (either an uploaded model path
// or a `<root>\<model>` path scanned from messages), we draw a subtle
// 6-px accent dot on the button so users notice the affordance is now
// actionable. Purely cosmetic; the actual variant scan runs inside
// PromoteToAppBuilderCard on click, so a false-positive dot is harmless.
// Same rationale as `promoteReady` in ModeFrameModelBuilder — user
// feedback: "ModelBuilder shows a dot on Promote when a workdir is
// detected, AppBuilder does not; they should be consistent."
const promoteReady = computed<boolean>(() => promoteWorkdir.value !== "");
function togglePromotePanel(): void {
  promotePanelOpen.value = !promotePanelOpen.value;
}
function onPromoteImported(): void {
  promotePanelOpen.value = false;
  toast.success(t("modelBuilder.promote.importSuccess"));
  // Refresh the App Builder model list so the freshly imported model shows
  // in this frame's model menu.
  void store.fetchModels();
}

// ── Cross-component triggers from ModeIntroCard chips ───────────────────────
// The ModeIntroCard's `open-my-apps` / `open-promote` chips route through
// `useModeFrameTriggers` — bump tokens that we watch here to open the
// corresponding local popover. This keeps the mode-frame's local `ref`
// state as the SINGLE source of truth for panel open/close (only reactive
// to its OWN watchers), while letting the intro card surface these panels
// without cross-component coupling.
//
// The `activeMode` gate mirrors `ModeFrameModelBuilder.vue` — even though
// mode-frames are currently mounted via `v-else-if` (mutually exclusive),
// the gate keeps the token contract symmetric so a future refactor that
// makes frames persistent (e.g. for KeepAlive optimisation) cannot cause
// two frames to both react to the same bump.
const { openMyAppsToken, openPromoteToken } = useModeFrameTriggers();
watch(openMyAppsToken, () => {
  if (tabs.activeTab?.activeMode !== "app-builder") return;
  appsMenuOpen.value = true;
  void store.fetchApps();
});
watch(openPromoteToken, () => {
  if (tabs.activeTab?.activeMode !== "app-builder") return;
  promotePanelOpen.value = true;
});
</script>

<template>
  <div
    class="rit-left"
    data-testid="mode-frame-app-builder"
  >
    <button
      type="button"
      class="rit-mode-badge"
      data-testid="mode-frame-exit"
      @click="onExit"
    >
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <rect
          x="3"
          y="3"
          width="7"
          height="7"
          rx="1"
        />
        <rect
          x="14"
          y="3"
          width="7"
          height="7"
          rx="1"
        />
        <rect
          x="3"
          y="14"
          width="7"
          height="7"
          rx="1"
        />
        <rect
          x="14"
          y="14"
          width="7"
          height="7"
          rx="1"
        />
      </svg>
      <span>{{ t("index.appBuilder") }}</span>
      <span class="rit-close">✕</span>
    </button>

    <span class="rit-sep"></span>

    <!-- Imported-model multi-select (popup menu, opens upward) -->
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        :class="{ 'rit-model-upload--active': modelMenuOpen || selectedCount > 0 }"
        :aria-expanded="modelMenuOpen"
        data-testid="app-builder-model-menu-toggle"
        :title="t('appBuilder.modelStrip.tip')"
        @click="toggleModelMenu"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><rect
          x="3"
          y="3"
          width="7"
          height="7"
          rx="1"
        /><rect
          x="14"
          y="3"
          width="7"
          height="7"
          rx="1"
        /><rect
          x="3"
          y="14"
          width="7"
          height="7"
          rx="1"
        /><rect
          x="14"
          y="14"
          width="7"
          height="7"
          rx="1"
        /></svg>
        <span>{{ t("appBuilder.modelStrip.label") }}</span>
        <span
          v-if="selectedCount > 0"
          class="ab-model-count"
        >{{ selectedCount }}</span>
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline
            v-if="modelMenuOpen"
            points="18 15 12 9 6 15"
          />
          <polyline
            v-else
            points="6 9 12 15 18 9"
          />
        </svg>
      </button>
      <div
        v-if="modelMenuOpen"
        class="rit-submenu ab-model-menu"
        role="menu"
        data-testid="app-builder-model-menu"
      >
        <div class="ab-model-menu-title">
          {{ t("appBuilder.modelStrip.header") }}
        </div>
        <div
          v-if="models.length === 0"
          class="ab-model-menu-empty"
          data-testid="app-builder-model-menu-empty"
        >
          {{ t("appBuilder.modelStrip.empty") }}
          <button
            type="button"
            class="ab-model-menu-open-mb"
            data-testid="app-builder-open-model-builder"
            @click="onOpenModelBuilder"
          >
            {{ t("appBuilder.modelStrip.openModelBuilder") }}
          </button>
        </div>
        <template v-else>
          <label
            v-for="m in models"
            :key="m.id"
            class="ab-model-row"
            :class="{ 'ab-model-row--on': isModelSelected(m.id) }"
            :data-testid="`app-builder-model-item-${m.id}`"
          >
            <input
              type="checkbox"
              class="ab-model-cb"
              :checked="isModelSelected(m.id)"
              @change="onToggleModel(m.id)"
            />
            <span
              class="ab-model-name"
              :title="m.title"
            >{{ m.title }}</span>
            <span
              v-if="kindOf(m.taxonomy)"
              class="ab-model-badge"
            >{{ kindOf(m.taxonomy) }}</span>

            <!-- Right-side affordance: one of ✓ ready / Download / progress /
                 error+retry, chosen by rowState(m). The "needs conversion" hint
                 is kept only for the "hint" state (not-installed + not
                 auto-downloadable). Row height stays stable across states. -->
            <span
              class="ab-dl-slot"
              @click.stop
            >
              <!-- Ready state renders nothing — installed weights are the
                   overwhelmingly common case (built-ins ship installed;
                   imports land installed), so a per-row ✓ badge added
                   visual noise without conveying new information. The
                   trailing column shows an affordance ONLY when the row
                   needs one: Download / progress / error / conversion
                   hint. When ``rowState(m) === 'ready'`` the slot is
                   simply empty and the row's flex layout tightens up. -->

              <!-- Not installed, fetchable: Download button -->
              <button
                v-if="rowState(m) === 'download'"
                type="button"
                class="ab-dl-btn"
                :data-testid="`app-builder-model-download-${m.id}`"
                :title="t('appBuilder.modelStrip.download')"
                @click.stop="onDownload(m.id)"
              >
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2.2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                ><path d="M12 3v12" /><polyline points="7 10 12 15 17 10" /><path d="M5 21h14" /></svg>
                <span>{{ t("appBuilder.modelStrip.download") }}</span>
              </button>

              <!-- Downloading / extracting: live progress bar + cancel ✕ -->
              <span
                v-else-if="rowState(m) === 'downloading' || rowState(m) === 'extracting'"
                class="ab-dl-progress"
                :data-testid="`app-builder-model-progress-${m.id}`"
                :title="
                  rowState(m) === 'extracting'
                    ? t('appBuilder.modelStrip.extracting')
                    : t('appBuilder.modelStrip.downloading')
                "
              >
                <span class="ab-dl-progress-main">
                  <span
                    class="ab-dl-track"
                    :class="{ 'ab-dl-track--indeterminate': isIndeterminate(m.id) }"
                  >
                    <span
                      class="ab-dl-fill"
                      :style="
                        isIndeterminate(m.id)
                          ? undefined
                          : { width: progressPercent(m.id) + '%' }
                      "
                    ></span>
                  </span>
                  <span class="ab-dl-pct">{{ percentLabel(m.id) }}</span>
                  <button
                    type="button"
                    class="ab-dl-cancel"
                    :data-testid="`app-builder-model-cancel-${m.id}`"
                    :title="t('appBuilder.modelStrip.cancel')"
                    @click.stop="onCancel(m.id)"
                  >✕</button>
                </span>
                <span
                  v-if="speedEtaLabel(m.id) !== ''"
                  class="ab-dl-meta"
                >{{ speedEtaLabel(m.id) }}</span>
              </span>

              <!-- Error: message + retry -->
              <span
                v-else-if="rowState(m) === 'error'"
                class="ab-dl-error"
                :data-testid="`app-builder-model-dl-error-${m.id}`"
                :title="dlOf(m.id).error ?? t('appBuilder.modelStrip.downloadFailed')"
              >
                <span class="ab-dl-error-text">{{ t("appBuilder.modelStrip.downloadFailed") }}</span>
                <button
                  type="button"
                  class="ab-dl-retry"
                  @click.stop="onDownload(m.id)"
                >{{ t("appBuilder.modelStrip.retry") }}</button>
              </span>

              <!-- Hint: not installed, not fetchable (needs conversion) -->
              <span
                v-else-if="downloadHintOf(m)"
                class="ab-model-dl-hint"
                :data-testid="`app-builder-model-dl-hint-${m.id}`"
              >{{ downloadHintOf(m) }}</span>
            </span>

            <!-- Inline delete button (user-imported models only) — placed at
                 the TRAILING edge of the row, AFTER ``.ab-dl-slot``. This is
                 the last flex item, so nothing sits between it and the row's
                 right edge. Previously the button lived BEFORE ``.ab-dl-slot``
                 which itself has ``flex: 1 1 auto`` — the slot absorbed the
                 leftover row space and pushed the button leftward against
                 the taxonomy badge (the "not right-aligned" symptom the user
                 reported). Placing the button AFTER the slot puts it at the
                 physical right end; the slot then naturally shrinks to hug
                 the button's left edge. Built-in rows render nothing here
                 (v-if gate + backend 403). Hover-reveal keeps the resting
                 state clean; click stops label propagation so hitting ×
                 does not also toggle the row checkbox. -->
            <button
              v-if="m.user_imported === true"
              type="button"
              class="ab-model-del"
              :class="{ 'ab-model-del--busy': isDeletingModel(m.id) }"
              :disabled="isDeletingModel(m.id)"
              :aria-label="t('appBuilder.modelStrip.deleteBtnAria')"
              :title="t('appBuilder.modelStrip.deleteBtnTitle')"
              :data-testid="`app-builder-model-delete-${m.id}`"
              @click.stop.prevent="onDeleteModel(m)"
            >
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2.2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              ><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          </label>
          <div class="ab-model-menu-footer">
            {{ t("appBuilder.modelStrip.selectedCount", { n: selectedCount }) }}
          </div>
        </template>
      </div>
      <div
        v-if="modelMenuOpen"
        class="dropdown-overlay"
        @click="modelMenuOpen = false"
      ></div>
    </div>

    <span class="rit-sep"></span>

    <!-- Generated-apps menu (Phase 4, plan §9.1). Not gated behind the
         workbench toggle — it is part of the new standalone-app flow. -->
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        :class="{ 'rit-model-upload--active': appsMenuOpen }"
        :aria-expanded="appsMenuOpen"
        :aria-label="t('appBuilder.apps.menuAria')"
        data-testid="app-builder-apps-menu-toggle"
        :title="t('appBuilder.apps.menuAria')"
        @click="toggleAppsMenu"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><rect
          x="2"
          y="3"
          width="20"
          height="14"
          rx="2"
        /><line
          x1="8"
          y1="21"
          x2="16"
          y2="21"
        /><line
          x1="12"
          y1="17"
          x2="12"
          y2="21"
        /></svg>
        <span>{{ t("appBuilder.apps.menuLabel") }}</span>
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline
            v-if="appsMenuOpen"
            points="18 15 12 9 6 15"
          />
          <polyline
            v-else
            points="6 9 12 15 18 9"
          />
        </svg>
      </button>
      <div
        v-if="appsMenuOpen"
        class="rit-submenu ab-apps-menu"
        :class="{ 'ab-apps-menu--logs-open': expandedLogsAppId !== null }"
        role="menu"
        data-testid="app-builder-apps-menu"
      >
        <div class="ab-model-menu-title">
          {{ t("appBuilder.apps.menuLabel") }}
        </div>
        <div
          v-if="apps.length === 0"
          class="ab-model-menu-empty"
          data-testid="app-builder-apps-menu-empty"
        >
          {{ t("appBuilder.apps.empty") }}
        </div>
        <template v-else>
          <div
            v-for="app in apps"
            :key="app.id"
            class="ab-app-row"
            :data-testid="`app-builder-app-item-${app.id}`"
          >
            <div class="ab-app-head">
              <span
                class="ab-app-name"
                :title="app.name"
              >{{ app.name }}</span>
              <span
                class="ab-app-status"
                :class="`ab-app-status--${statusOf(app).toLowerCase()}`"
                :data-testid="`app-builder-app-status-${app.id}`"
              >{{ statusLabel(app) }}</span>
            </div>

            <div
              v-if="runUrlOf(app) !== null || runStateOf(app.id)?.port"
              class="ab-app-meta"
            >
              <span
                v-if="runStateOf(app.id)?.port"
                class="ab-app-port"
              >{{ t("appBuilder.apps.port", { port: runStateOf(app.id)?.port }) }}</span>
              <button
                v-if="runUrlOf(app) !== null"
                type="button"
                class="ab-app-url-link"
                :title="t('appBuilder.apps.openUrlTitle', { url: runUrlOf(app) ?? '' })"
                :aria-label="t('appBuilder.apps.openAria')"
                :data-testid="`app-builder-app-url-${app.id}`"
                @click="onOpenApp(app)"
              >{{ runUrlOf(app) }}</button>
            </div>

            <div class="ab-app-actions">
              <button
                type="button"
                class="ab-app-btn"
                :disabled="isBusy(app)"
                :aria-label="t('appBuilder.apps.runAria')"
                :data-testid="`app-builder-app-run-${app.id}`"
                @click="onRunApp(app)"
              >{{ t("appBuilder.apps.run") }}</button>
              <button
                type="button"
                class="ab-app-btn"
                :disabled="!isReady(app)"
                :aria-label="t('appBuilder.apps.openAria')"
                :data-testid="`app-builder-app-open-${app.id}`"
                @click="onOpenApp(app)"
              >{{ t("appBuilder.apps.open") }}</button>
              <button
                type="button"
                class="ab-app-btn"
                :disabled="!isStoppable(app)"
                :aria-label="t('appBuilder.apps.stopAria')"
                :data-testid="`app-builder-app-stop-${app.id}`"
                @click="onStopApp(app)"
              >{{ t("appBuilder.apps.stop") }}</button>
              <button
                type="button"
                class="ab-app-btn"
                :aria-label="t('appBuilder.apps.logsAria')"
                :data-testid="`app-builder-app-logs-${app.id}`"
                @click="onShowLogs(app)"
              >{{ t("appBuilder.apps.logs") }}</button>
              <button
                type="button"
                class="ab-app-btn"
                :disabled="isPackaging(app.id)"
                :aria-label="t('appBuilder.apps.packageAria')"
                :data-testid="`app-builder-app-package-${app.id}`"
                @click="onPackageApp(app)"
              >{{ t("appBuilder.apps.package") }}</button>
              <button
                type="button"
                class="ab-app-btn ab-app-btn--danger"
                :disabled="isPackaging(app.id) || isDeleting(app.id)"
                :aria-label="t('appBuilder.apps.deleteAria')"
                :data-testid="`app-builder-app-delete-${app.id}`"
                @click="onDeleteApp(app)"
              >{{ t("appBuilder.apps.delete") }}</button>
            </div>

            <!-- Packaging: live progress bar + percent + phase/message + cancel -->
            <div
              v-if="isPackaging(app.id)"
              class="ab-dl-progress ab-app-package-progress"
              :data-testid="`app-builder-app-package-progress-${app.id}`"
              :title="t('appBuilder.apps.packaging')"
            >
              <span class="ab-dl-progress-main">
                <span class="ab-dl-track">
                  <span
                    class="ab-dl-fill"
                    :style="{ width: packagePercent(app.id) + '%' }"
                  ></span>
                </span>
                <span class="ab-dl-pct">{{ packagePercentLabel(app.id) }}</span>
                <button
                  type="button"
                  class="ab-dl-cancel"
                  :data-testid="`app-builder-app-package-cancel-${app.id}`"
                  :aria-label="t('appBuilder.apps.packageCancel')"
                  :title="t('appBuilder.apps.packageCancel')"
                  @click.stop="onCancelPackage(app)"
                >✕</button>
              </span>
              <span class="ab-dl-meta">{{ packageStatusLabel(app.id) }}</span>
            </div>

            <!-- Packaged result: persistent panel with the produced zip path.
                 Rendered when a package job finished successfully (the toast
                 alone was easy to miss). Shows path + size, Copy-path button
                 (paste into Explorer/terminal), and a dismiss ✕ so the row
                 returns to its normal state when the user is done with it. -->
            <div
              v-if="hasPackageResult(app.id)"
              class="ab-app-package-result"
              :data-testid="`app-builder-app-package-result-${app.id}`"
            >
              <div class="ab-app-package-result-head">
                <span class="ab-app-package-result-icon" aria-hidden="true">✓</span>
                <span class="ab-app-package-result-title">
                  {{ t("appBuilder.apps.packagedTitle", { size: packagedSize(app.id) }) }}
                </span>
                <button
                  type="button"
                  class="ab-app-package-result-dismiss"
                  :aria-label="t('appBuilder.apps.packageDismissAria')"
                  :title="t('appBuilder.apps.packageDismissAria')"
                  :data-testid="`app-builder-app-package-dismiss-${app.id}`"
                  @click="onDismissPackageResult(app)"
                >✕</button>
              </div>
              <div class="ab-app-package-result-body">
                <code
                  class="ab-app-package-result-path"
                  :title="packagedZipPath(app.id)"
                  :data-testid="`app-builder-app-package-path-${app.id}`"
                >{{ packagedZipPath(app.id) }}</code>
                <button
                  type="button"
                  class="ab-app-copy"
                  :aria-label="t('appBuilder.apps.copyPath')"
                  :title="t('appBuilder.apps.copyPath')"
                  :data-testid="`app-builder-app-package-copy-${app.id}`"
                  @click="onCopyPackagePath(app)"
                >{{ t("appBuilder.apps.copyPath") }}</button>
              </div>
            </div>

            <!-- Manual command (copyable, no window.prompt) -->
            <div
              v-if="manualCommandOf(app) !== null"
              class="ab-app-manual"
            >
              <code
                class="ab-app-manual-cmd"
                :title="manualCommandOf(app) ?? ''"
              >{{ manualCommandOf(app) }}</code>
              <button
                type="button"
                class="ab-app-copy"
                :aria-label="t('appBuilder.apps.copyCommand')"
                :title="t('appBuilder.apps.copyCommand')"
                :data-testid="`app-builder-app-copy-${app.id}`"
                @click="onCopyCommand(app)"
              >{{ t("appBuilder.apps.copyCommand") }}</button>
            </div>

            <!-- Inline logs panel -->
            <div
              v-if="expandedLogsAppId === app.id"
              class="ab-app-logs"
              :data-testid="`app-builder-app-logs-panel-${app.id}`"
            >
              <div class="ab-app-logs-head">
                <span class="ab-app-logs-title">
                  {{ t("appBuilder.apps.logsTitle") }}
                </span>
                <span class="ab-app-logs-actions">
                  <button
                    type="button"
                    class="ab-app-logs-btn"
                    :aria-label="t('appBuilder.apps.logsRefresh')"
                    :title="t('appBuilder.apps.logsRefresh')"
                    :data-testid="`app-builder-app-logs-refresh-${app.id}`"
                    @click="onRefreshLogs(app)"
                  >{{ t("appBuilder.apps.logsRefresh") }}</button>
                  <button
                    type="button"
                    class="ab-app-logs-btn"
                    :disabled="logsOf(app.id) === ''"
                    :aria-label="t('appBuilder.apps.copyLogs')"
                    :title="t('appBuilder.apps.copyLogs')"
                    :data-testid="`app-builder-app-logs-copy-${app.id}`"
                    @click="onCopyLogs(app)"
                  >{{ t("appBuilder.apps.copyLogs") }}</button>
                </span>
              </div>
              <pre
                v-if="logsOf(app.id) !== ''"
                class="ab-app-logs-pre"
              >{{ logsOf(app.id) }}</pre>
              <div
                v-else
                class="ab-app-logs-empty"
              >{{ t("appBuilder.apps.logsEmpty") }}</div>
            </div>
          </div>
        </template>
      </div>
      <div
        v-if="appsMenuOpen"
        class="dropdown-overlay"
        @click="appsMenuOpen = false"
      ></div>
    </div>

    <!-- Promote to App Builder (parity with ModeFrameModelBuilder). Symmetric
         entry so users don't have to bounce back to Model Builder mode to
         promote a converted model. Same PromoteToAppBuilderCard; workdir is
         scanned from the active chat's messages (extractModelWorkdirFromMessages).
         Hidden until the user clicks (popover) — does NOT compete for space
         with the always-visible Model + Apps menus. -->
    <span class="rit-sep"></span>
    <div class="rit-submenu-wrap">
      <button
        type="button"
        class="rit-btn"
        :class="{
          'rit-model-upload--active': promotePanelOpen,
          'ab-promote-btn--ready': promoteReady,
        }"
        :title="t('modelBuilder.promote.title')"
        data-testid="ab-toggle-promote"
        @click="togglePromotePanel"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><path d="M4 14h6v6H4z" /><path d="M14 4h6v6h-6z" /><path d="M7 14V7h7" /><polyline points="14 10 7 10" /></svg>
        <span>{{ t("modelBuilder.promote.title") }}</span>
        <!-- Ready dot (§14 UX) — symmetric with ModeFrameModelBuilder's
             `mb-promote-ready-dot`. A subtle 6px accent dot when a
             promote-able model workdir has been detected. Draws the eye
             without shouting; purely cosmetic. -->
        <span
          v-if="promoteReady"
          class="ab-promote-ready-dot"
          role="status"
          :aria-label="t('modelBuilder.promote.readyBadgeAria')"
        ></span>
      </button>
      <div
        v-if="promotePanelOpen"
        class="rit-submenu rit-submenu--wide"
        style="min-width: 400px; max-height: 500px; overflow-y: auto"
        data-testid="ab-promote-panel"
      >
        <PromoteToAppBuilderCard
          :session-model-workdir="promoteWorkdir"
          @imported="onPromoteImported"
        />
      </div>
      <div
        v-if="promotePanelOpen"
        class="dropdown-overlay"
        @click="promotePanelOpen = false"
      ></div>
    </div>

    <!--
      Workbench-only controls (#6): Send to Chat / Edit Prompt / Compare all
      produce or consume workbench overlay state. With the workbench hidden
      (default) there is no way to produce a run, so they would be permanently
      inert. Render them only when the workbench is enabled in Settings.
    -->
    <template v-if="appBuilderShowWorkbench">
      <span class="rit-sep"></span>

      <!-- Send to Chat（V1 index.html:1524-1529） -->
      <button
        type="button"
        class="rit-btn"
        data-testid="app-builder-send-to-chat"
        :disabled="!hasRunOutput"
        :title="t('appBuilder.sendToChatTip')"
        @click="onSendToChat"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><line
          x1="22"
          y1="2"
          x2="11"
          y2="13"
        /><polygon points="22 2 15 22 11 13 2 9 22 2" /></svg>
        <span>{{ t("appBuilder.sendToChat") }}</span>
      </button>

      <!-- Edit Prompt（V1 index.html:1531-1537） -->
      <button
        type="button"
        class="rit-btn"
        data-testid="app-builder-edit-prompt"
        :title="t('appBuilder.editPromptTip')"
        @click="onEditPrompt"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></svg>
        <span>{{ t("appBuilder.editPrompt") }}</span>
      </button>

      <!-- Compare（V1 index.html:1542-1554） -->
      <button
        type="button"
        class="rit-btn rit-btn--compare"
        :class="{
          'rit-btn--active': store.compareOpen && compareCount > 0,
          'rit-btn--disabled': compareCount === 0,
        }"
        data-testid="app-builder-compare"
        :disabled="compareCount === 0"
        :title="
          compareCount === 0
            ? t('appBuilder.compare.emptyTip')
            : t('appBuilder.compare.toggleTip')
        "
        @click="onCompare"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        ><rect
          x="3"
          y="3"
          width="7"
          height="18"
          rx="1"
        /><rect
          x="14"
          y="3"
          width="7"
          height="18"
          rx="1"
        /></svg>
        <span>{{ t("appBuilder.compare.title") }} ({{ compareCount }})</span>
      </button>
    </template>
  </div>
</template>

<style scoped>
/* Selected-count pill on the "模型" toolbar button. */
.ab-model-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 5px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
  color: #fff;
  background: var(--accent, #6d5efc);
}

/* Popup menu container. */
.ab-model-menu {
  min-width: 260px;
  max-width: 320px;
  padding: 6px;
}
.ab-model-menu-title {
  padding: 6px 8px 8px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--text-muted, rgba(255, 255, 255, 0.55));
}
.ab-model-menu-empty {
  padding: 14px 10px;
  text-align: center;
  font-size: var(--text-sm, 12px);
  color: var(--text-muted, rgba(255, 255, 255, 0.5));
  font-style: italic;
}

/* Actionable "jump to Model Builder" button in the empty state (#5). Small,
   subtle accent, consistent with the dark menu. */
.ab-model-menu-open-mb {
  display: inline-flex;
  align-items: center;
  margin-top: 10px;
  padding: 5px 12px;
  border: 1px solid color-mix(in srgb, var(--accent, #6d5efc) 45%, transparent);
  border-radius: 7px;
  font-size: 12px;
  font-weight: 600;
  font-style: normal;
  line-height: 1.2;
  color: var(--accent, #6d5efc);
  background: color-mix(in srgb, var(--accent, #6d5efc) 12%, transparent);
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease;
}
.ab-model-menu-open-mb:hover {
  background: color-mix(in srgb, var(--accent, #6d5efc) 22%, transparent);
  border-color: color-mix(in srgb, var(--accent, #6d5efc) 65%, transparent);
}

/* One selectable model row (whole row is a <label>, fully clickable). */
.ab-model-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px 10px 8px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.12s ease;
}
.ab-model-row::before {
  content: "";
  position: absolute;
  left: 4px;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 0;
  border-radius: 2px;
  background: var(--accent, #6d5efc);
  transition: height 0.12s ease;
}
.ab-model-row:hover {
  background: var(--hover-bg, rgba(255, 255, 255, 0.05));
}
.ab-model-row--on {
  background: color-mix(in srgb, var(--accent, #6d5efc) 12%, transparent);
}
.ab-model-row--on::before {
  height: 60%;
}

/* Checkbox tinted to the accent. */
.ab-model-cb {
  flex: 0 0 auto;
  width: 15px;
  height: 15px;
  margin: 0;
  cursor: pointer;
  accent-color: var(--accent, #6d5efc);
}

/* Model name takes the remaining width, truncates instead of wrapping. */
.ab-model-name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary, #e8e8ef);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ab-model-row--on .ab-model-name {
  color: var(--accent, #6d5efc);
}

/* Compact task pill, single line, never grows. */
.ab-model-badge {
  flex: 0 0 auto;
  max-width: 40%;
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 10px;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text-muted, rgba(255, 255, 255, 0.6));
  background: var(--border-light, rgba(255, 255, 255, 0.08));
}

/* Install-state hint for a not-yet-ready model that is NOT auto-downloadable
   (needs manual conversion). Muted; only shown in the "hint" row state now
   that auto-downloadable packs render a Download button / progress bar. */
.ab-model-dl-hint {
  min-width: 0;
  text-align: right;
  font-size: 10px;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text-muted, rgba(255, 255, 255, 0.5));
}

/* ── Inline delete button (user-imported models only) ────────────────────────
   Rendered as the LAST flex item in ``.ab-model-row`` (after
   ``.ab-dl-slot``), so it lands at the row's right edge without any tricks:
   because nothing sits to its right, the button naturally hugs the trailing
   edge. Earlier attempts to place the button before ``.ab-dl-slot`` and rely
   on ``margin-left: auto`` failed because the slot itself is ``flex: 1 1
   auto`` — it swallowed the leftover space and pinned the button against
   the taxonomy badge. Now the slot shrinks to fit whatever content it
   carries (Download / progress / hint) and the button owns the trailing
   edge. Hover-reveal keeps the resting state visually calm. */
.ab-model-del {
  flex: 0 0 auto;
  width: 22px;
  height: 22px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--text-muted, rgba(255, 255, 255, 0.5));
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s ease, background 0.12s ease, color 0.12s ease,
    border-color 0.12s ease;
}
.ab-model-row:hover .ab-model-del,
.ab-model-del:focus-visible {
  /* On row hover OR keyboard focus (a11y), reveal the button. */
  opacity: 1;
}
.ab-model-del:hover {
  background: rgba(239, 68, 68, 0.14);
  border-color: rgba(239, 68, 68, 0.35);
  color: var(--danger, #ef4444);
}
.ab-model-del:focus-visible {
  outline: 2px solid var(--focus, rgba(109, 94, 252, 0.6));
  outline-offset: 1px;
}
.ab-model-del:disabled,
.ab-model-del--busy {
  cursor: wait;
  opacity: 0.5;
}
.ab-model-del:disabled:hover,
.ab-model-del--busy:hover {
  /* Don't paint the danger hover state when busy — the click is a no-op. */
  background: transparent;
  border-color: transparent;
  color: var(--text-muted, rgba(255, 255, 255, 0.5));
}

/* ── Right-side download affordance slot ─────────────────────────────────────
   Fixed-ish right column that holds exactly one of: ✓ ready / Download button
   / live progress bar / error+retry / conversion hint. Flex-grows to fill the
   row so the bar has room, min-height keeps the row height stable across the
   button↔bar swap (no vertical jump). */
.ab-dl-slot {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  min-height: 24px;
}

/* Green ✓ ready icon: rule intentionally removed (2026-07-15). The
   per-row ready badge was dropped from the template because "installed"
   is the near-universal state; keeping the class here as dead CSS would
   just be noise for a future reader. If the badge ever comes back,
   restore both the ``<span class="ab-dl-ready">`` markup and the block
   below (see git history). */

/* Compact Download button. */
.ab-dl-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border: 1px solid color-mix(in srgb, var(--accent, #6d5efc) 45%, transparent);
  border-radius: 7px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--accent, #6d5efc);
  background: color-mix(in srgb, var(--accent, #6d5efc) 12%, transparent);
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.12s ease, border-color 0.12s ease;
}
.ab-dl-btn:hover {
  background: color-mix(in srgb, var(--accent, #6d5efc) 22%, transparent);
  border-color: color-mix(in srgb, var(--accent, #6d5efc) 65%, transparent);
}

/* Live progress: bar + percent + cancel on the first line, speed·ETA below. */
.ab-dl-progress {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: stretch;
}
.ab-dl-progress-main {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ab-dl-track {
  position: relative;
  flex: 1 1 auto;
  min-width: 60px;
  height: 6px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--border-light, rgba(255, 255, 255, 0.1));
}
.ab-dl-fill {
  position: absolute;
  inset: 0 auto 0 0;
  height: 100%;
  width: 0;
  border-radius: 999px;
  background: var(--accent, #6d5efc);
  transition: width 0.25s ease;
}
/* Indeterminate: a pulsing slice sweeps across the track (unknown total). */
.ab-dl-track--indeterminate .ab-dl-fill {
  width: 40%;
  animation: ab-dl-indeterminate 1.1s ease-in-out infinite;
}
@keyframes ab-dl-indeterminate {
  0% {
    left: -40%;
  }
  100% {
    left: 100%;
  }
}
.ab-dl-pct {
  flex: 0 0 auto;
  min-width: 30px;
  text-align: right;
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--text-primary, #e8e8ef);
}
.ab-dl-cancel {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  padding: 0;
  border: none;
  border-radius: 999px;
  font-size: 10px;
  line-height: 1;
  color: var(--text-muted, rgba(255, 255, 255, 0.55));
  background: transparent;
  cursor: pointer;
  transition: color 0.12s ease, background 0.12s ease;
}
.ab-dl-cancel:hover {
  color: var(--text-primary, #e8e8ef);
  background: var(--hover-bg, rgba(255, 255, 255, 0.08));
}
.ab-dl-meta {
  font-size: 10px;
  line-height: 1.3;
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted, rgba(255, 255, 255, 0.5));
}

/* Error + retry. */
.ab-dl-error {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.ab-dl-error-text {
  font-size: 10px;
  line-height: 1.3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--danger, #f87171);
}
.ab-dl-retry {
  flex: 0 0 auto;
  padding: 3px 8px;
  border: 1px solid color-mix(in srgb, var(--accent, #6d5efc) 45%, transparent);
  border-radius: 6px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--accent, #6d5efc);
  background: color-mix(in srgb, var(--accent, #6d5efc) 12%, transparent);
  cursor: pointer;
  transition: background 0.12s ease;
}
.ab-dl-retry:hover {
  background: color-mix(in srgb, var(--accent, #6d5efc) 22%, transparent);
}

/* Footer showing the running selected count. */
.ab-model-menu-footer {
  margin-top: 4px;
  padding: 7px 10px 4px;
  border-top: 1px solid var(--border-light, rgba(255, 255, 255, 0.08));
  font-size: 11px;
  color: var(--text-muted, rgba(255, 255, 255, 0.5));
}

/* ── Generated-apps menu (Phase 4) ───────────────────────────────────────── */
.ab-apps-menu {
  /* Width sized to comfortably fit the single-row action bar (six buttons:
     run/open/stop/logs/package/delete) so they never wrap, while long app
     names ellipsize inside the row rather than inflating the menu. Capped to
     the viewport so it never overflows the screen. */
  width: 460px;
  max-width: 92vw;
  max-height: min(520px, 78vh);
  overflow-y: auto;
  padding: 6px;
  transition: width 0.12s ease, max-width 0.12s ease;
}
/* When a log panel is open, widen + heighten the menu so logs are easy to
   read/scroll (issue #1). Reverts automatically when logs are collapsed
   (the modifier class is bound to `expandedLogsAppId !== null`). Caps to the
   viewport so it never overflows the screen. */
.ab-apps-menu--logs-open {
  width: min(720px, 92vw);
  max-width: min(720px, 92vw);
  max-height: min(640px, 82vh);
}
/* When a log panel is open, widen + heighten the menu so logs are easy to
   read/scroll (issue #1). Reverts automatically when logs are collapsed
   (the modifier class is bound to `expandedLogsAppId !== null`). Caps to the
   viewport so it never overflows the screen. */
.ab-apps-menu--logs-open {
  width: min(720px, 92vw);
  min-width: min(720px, 92vw);
  max-width: min(720px, 92vw);
  max-height: min(640px, 82vh);
}

/* One app project row (card-style, dark, consistent with model rows). */
.ab-app-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--hover-bg, rgba(255, 255, 255, 0.03));
  border: 1px solid var(--border-light, rgba(255, 255, 255, 0.06));
}
.ab-app-row + .ab-app-row {
  margin-top: 6px;
}
.ab-app-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ab-app-name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, #e8e8ef);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ab-app-status {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.4;
  color: var(--text-muted, rgba(255, 255, 255, 0.6));
  background: var(--border-light, rgba(255, 255, 255, 0.08));
}
.ab-app-status--ready,
.ab-app-status--running {
  color: var(--success, #34d399);
  background: color-mix(in srgb, var(--success, #34d399) 16%, transparent);
}
.ab-app-status--starting,
.ab-app-status--stopping,
.ab-app-status--packaging {
  color: var(--accent, #6d5efc);
  background: color-mix(in srgb, var(--accent, #6d5efc) 16%, transparent);
}
.ab-app-status--failed {
  color: var(--danger, #f87171);
  background: color-mix(in srgb, var(--danger, #f87171) 16%, transparent);
}
.ab-app-meta {
  display: flex;
  gap: 10px;
  align-items: center;
  font-size: 11px;
  color: var(--text-muted, rgba(255, 255, 255, 0.55));
}
.ab-app-url {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #7fa7ff;
}
/* Clickable preview URL — opens the loopback app in a new tab (via the
   same-origin-guarded openUrl), so the user never has to copy/type it. */
.ab-app-url-link {
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  color: #7fa7ff;
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}
.ab-app-url-link:hover {
  color: #a9c4ff;
}
.ab-app-url-link:focus-visible {
  outline: 2px solid #7fa7ff;
  outline-offset: 2px;
  border-radius: 2px;
}
.ab-app-actions {
  display: flex;
  flex-wrap: nowrap;      /* keep all action buttons on a single row */
  gap: 6px;
  white-space: nowrap;
}
.ab-app-btn {
  flex: 0 0 auto;         /* buttons size to their label, never shrink-wrap */
  padding: 4px 10px;
  border: 1px solid color-mix(in srgb, var(--accent, #6d5efc) 45%, transparent);
  border-radius: 7px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.2;
  white-space: nowrap;
  color: var(--accent, #6d5efc);
  background: color-mix(in srgb, var(--accent, #6d5efc) 12%, transparent);
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease;
}
.ab-app-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent, #6d5efc) 22%, transparent);
  border-color: color-mix(in srgb, var(--accent, #6d5efc) 65%, transparent);
}
.ab-app-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
/* Destructive Delete button — red accent so it reads as dangerous. */
.ab-app-btn--danger {
  color: var(--err, #f85149);
  border-color: color-mix(in srgb, var(--err, #f85149) 45%, transparent);
}
.ab-app-btn--danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--err, #f85149) 16%, transparent);
  border-color: color-mix(in srgb, var(--err, #f85149) 70%, transparent);
}
.ab-app-manual {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* Persistent packaged-result panel — shown after a successful package job so
   the user can see and copy the zip path without relying on the transient
   toast. Styled like the manual-command block but with a success accent. */
.ab-app-package-result {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(63, 185, 80, 0.07);
  border: 1px solid rgba(63, 185, 80, 0.25);
}
.ab-app-package-result-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ab-app-package-result-icon {
  color: var(--ok, #3fb950);
  font-size: 12px;
  flex-shrink: 0;
}
.ab-app-package-result-title {
  flex: 1;
  font-size: 12px;
  font-weight: 600;
  color: var(--ok, #3fb950);
}
.ab-app-package-result-dismiss {
  flex-shrink: 0;
  background: none;
  border: none;
  color: var(--text-muted, rgba(255,255,255,0.4));
  font-size: 11px;
  cursor: pointer;
  padding: 0 2px;
  line-height: 1;
}
.ab-app-package-result-dismiss:hover {
  color: var(--text-primary, #fff);
}
.ab-app-package-result-body {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.ab-app-package-result-path {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11px;
  color: var(--text-secondary, rgba(255,255,255,0.75));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  /* Show the TAIL of the path (filename) rather than the leading dirs, since
     the filename is the most informative part. direction:rtl + unicode-bidi
     achieves right-to-left text overflow so the end is always visible. */
  direction: rtl;
  unicode-bidi: plaintext;
}
.ab-app-manual-cmd {
  flex: 1 1 auto;
  min-width: 0;
  padding: 4px 8px;
  border-radius: 6px;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 10.5px;
  color: var(--text-primary, #e8e8ef);
  background: rgba(0, 0, 0, 0.3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ab-app-copy {
  flex: 0 0 auto;
  padding: 3px 8px;
  border: 1px solid var(--border-light, rgba(255, 255, 255, 0.12));
  border-radius: 6px;
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted, rgba(255, 255, 255, 0.7));
  background: transparent;
  cursor: pointer;
  transition: background 0.12s ease;
}
.ab-app-copy:hover {
  background: var(--hover-bg, rgba(255, 255, 255, 0.08));
}
.ab-app-logs {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ab-app-logs-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.ab-app-logs-title {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--text-muted, rgba(255, 255, 255, 0.5));
}
.ab-app-logs-actions {
  display: flex;
  gap: 6px;
}
.ab-app-logs-btn {
  padding: 2px 8px;
  border-radius: 5px;
  border: 1px solid var(--border-subtle, rgba(255, 255, 255, 0.12));
  background: rgba(255, 255, 255, 0.04);
  color: var(--text-secondary, rgba(255, 255, 255, 0.75));
  font-size: 10px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.12s ease, color 0.12s ease;
}
.ab-app-logs-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.1);
  color: var(--text-primary, #fff);
}
.ab-app-logs-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
.ab-app-logs-pre {
  margin: 0;
  /* Roomier panel so logs are easy to read + scroll (issue #2). Grows with
     available height but caps so the menu stays usable. */
  min-height: 200px;
  max-height: 42vh;
  resize: vertical;
  overflow: auto;
  padding: 10px;
  border-radius: 6px;
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11px;
  line-height: 1.5;
  white-space: pre;
  word-break: normal;
  color: var(--text-primary, #d8d8e0);
  background: rgba(0, 0, 0, 0.4);
}
.ab-app-logs-empty {
  padding: 8px;
  font-size: 11px;
  font-style: italic;
  color: var(--text-muted, rgba(255, 255, 255, 0.4));
}

/* "Ready" dot on the Promote button — symmetric with
 * `.mb-promote-ready-dot` in ModeFrameModelBuilder. Accent-colored 6px
 * pill that sits inline after the label so it inherits button padding.
 * Purely CSS + theme tokens; no i18n text (aria-label on the dot itself
 * conveys meaning to assistive tech). Kept as its own class name
 * (`ab-` prefix) so future divergence between the two frames is easy. */
.ab-promote-ready-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent, #6d5efc);
  box-shadow: 0 0 0 2px var(--bg-secondary, #1c1c22);
  margin-left: 2px;
  flex: 0 0 auto;
}
</style>

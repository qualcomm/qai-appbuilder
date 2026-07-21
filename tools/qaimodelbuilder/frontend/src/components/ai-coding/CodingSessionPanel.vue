<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * CodingSessionPanel — floating Claude Code / Open Code session-management
 * panel (V1 `.cc-panel-float` + AiCodingPanel.js parity).
 *
 * V1 reference: frontend/index.html:846-862 (`.cc-panel-float`) wrapping
 * `<ai-coding-panel>` (frontend/js/ai-coding/AiCodingPanel.js).
 *
 * This panel floats over the lower-right of the chat surface and does NOT
 * render conversation messages — those stream into the unified chat message
 * area (see ChatViewClaudeCode / ChatViewOpenCode). The panel owns:
 *   - header (provider icon + name + New / collapse(—) / exit(✕))
 *   - Active Sessions / History tabs
 *   - provider-availability hint row
 *   - Source filter (All / WebChat / WeChat / Feishu)
 *   - session list (context badge, rename, change-dir, close)
 *   - new-session inline form
 *   - footer status bar (connection + Active N · Total M + Refresh)
 *
 * `kind` prop selects the provider; both CC and OC reuse this one component
 * (V1 single component + `provider` prop, AiCodingPanel.js:41-50).
 *
 * Status-bar fields are limited to what `/api/{cc,oc}/health` + the session
 * list actually return (V2 has no SDK-version / auth-configured fields, so
 * we show real connection state + real Active/Total counts — never invented
 * values, per the project's "no fabricated fields" rule).
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useCodingSession, type CodingSession } from "@/composables/useCodingSession";
import { useConfirm } from "@/composables/useConfirm";
import { fetchCcHealth, fetchOcHealth } from "@/api/aiCodingHealth";

const props = defineProps<{ kind: "cc" | "oc" }>();

const { t } = useI18n();
const { confirm } = useConfirm();
const core = useCodingSession(props.kind);

const meta = computed(() =>
  props.kind === "cc"
    ? {
        icon: "🤖",
        name: t("index.claudeCodeMode"),
        accent: "#7eb8f7", // V1 AiCodingPanel.js providerColor
        accentBg: "rgba(126,184,247,0.08)", // V1 active-tab tint
        accentBgItem: "rgba(126,184,247,0.12)", // V1 active session item tint
      }
    : {
        icon: "🔷",
        name: t("index.openCodeMode"),
        accent: "#63b3ed",
        accentBg: "rgba(99,179,237,0.08)",
        accentBgItem: "rgba(99,179,237,0.12)",
      },
);

// ── Tabs / filters ──────────────────────────────────────────────────────────
const activeTab = ref<"active" | "history">("active");
const sourceFilter = ref<"all" | "webchat" | "wechat" | "feishu">("all");

/**
 * Source-pill labels (V1 parity: AiCodingPanel.js:952 — `[{v:'all',l:$t('common.all')},
 * {v:'webchat',l:'WebChat'},{v:'wechat',l:$t('aiCoding.panel.srcWechat')},
 * {v:'feishu',l:$t('aiCoding.panel.srcFeishu')}]`).
 * "WebChat" is a hard-coded literal in V1; the others use i18n.
 */
function sourceLabel(src: "all" | "webchat" | "wechat" | "feishu"): string {
  if (src === "all") return t("common.all", "All");
  if (src === "webchat") return "WebChat";
  if (src === "wechat") return t("aiCoding.panel.srcWechat", "WeChat");
  return t("aiCoding.panel.srcFeishu", "Feishu");
}

// ── Health (connection / auth readiness) ─────────────────────────────────────
// V1 parity (AiCodingPanel.js:128-136): Claude Code checks SDK availability +
// auth; OpenCode checks HTTP service connectivity. `envReady` gates session
// creation so users can never spawn a session (and silently hit the scripted
// fallback) against an un-configured / un-connected provider.
const available = ref<boolean | null>(null);
const sdkAvailable = ref(false);
const authConfigured = ref(false);
const sdkVersion = ref("");
const healthLoading = ref(false);

// V1 AiCodingPanel.js:128-131 — CC reads sdk.available, OC reads health.available
const isSdkAvailable = computed(() =>
  props.kind === "oc" ? available.value === true : sdkAvailable.value,
);
// V1 AiCodingPanel.js:132-135 — CC reads auth_configured, OC reads health.available
const isAuthConfigured = computed(() =>
  props.kind === "oc" ? available.value === true : authConfigured.value,
);
// V1 AiCodingPanel.js:136 — both must hold before creating sessions
const envReady = computed(() => isSdkAvailable.value && isAuthConfigured.value);

async function loadHealth(): Promise<void> {
  healthLoading.value = true;
  try {
    const res =
      props.kind === "cc" ? await fetchCcHealth() : await fetchOcHealth();
    available.value = res.available === true;
    sdkAvailable.value = res.sdk_available === true;
    authConfigured.value = res.auth_configured === true;
    sdkVersion.value = res.sdk_version ?? "";
  } catch {
    available.value = false;
    sdkAvailable.value = false;
    authConfigured.value = false;
    sdkVersion.value = "";
  } finally {
    healthLoading.value = false;
  }
}

// ── Session list (real Active/Total counts) ─────────────────────────────────────
const sessions = computed<CodingSession[]>(() => core.sessions.value);
const activeCount = computed(
  () => sessions.value.filter((s) => s.status === "active").length,
);
const totalCount = computed(() => sessions.value.length);

// ── New-session form ──────────────────────────────────────────────────────────
const showCreateForm = ref(false);
const newWorkspace = ref("");
const newTitle = ref("");
const formError = ref("");

async function onCreate(): Promise<void> {
  const ws = newWorkspace.value.trim();
  if (ws === "") {
    formError.value = t("claudeCode.workspaceRequired", "Workspace is required");
    return;
  }
  formError.value = "";
  const title = newTitle.value.trim();
  const created = await core.startSession(ws, title === "" ? undefined : title);
  if (created !== null) {
    newWorkspace.value = "";
    newTitle.value = "";
    showCreateForm.value = false;
  }
}

// ── Inline rename / change working dir ──────────────────────────────────────────
const renamingId = ref<string | null>(null);
const renameValue = ref("");
const changingDirId = ref<string | null>(null);
const changeDirValue = ref("");

function startRename(id: string, current: string | null): void {
  renamingId.value = id;
  renameValue.value = current ?? "";
}
async function commitRename(): Promise<void> {
  if (renamingId.value === null) return;
  const v = renameValue.value.trim();
  if (v !== "") await core.renameSession(renamingId.value, v);
  renamingId.value = null;
}
function startChangeDir(id: string, current: string): void {
  changingDirId.value = id;
  changeDirValue.value = current;
}
async function commitChangeDir(): Promise<void> {
  if (changingDirId.value === null) return;
  const v = changeDirValue.value.trim();
  if (v !== "") await core.changeWorkingDir(changingDirId.value, v);
  changingDirId.value = null;
}

function onSelect(id: string): void {
  core.setActive(id);
  void core.refreshContextUsage(id);
}
async function onClose(id: string): Promise<void> {
  await core.stopSession(id);
}

/**
 * Fork the active session as a new branch (V1 AiCodingPanel.js:626-634
 * handleForkActiveSession). Calls restoreSession(id, true) — when the
 * session is already in memory the backend just sets the fork flag so the
 * next send produces a new claude_session_id; on cold restore it reloads
 * the persisted history first then sets the flag.
 */
async function onForkActive(id: string): Promise<void> {
  await core.restoreSession(id, true);
}

/**
 * Toggle WeChat dual-channel notifications (V1 AiCodingPanel.js:451-481
 * handleToggleWechatNotify). When already bound → unbind (null); when
 * unbound → bind to the session owner (which is the WeChat user id for
 * wechat-source sessions). Webui-owned sessions cannot bind: we surface
 * a definite alert via useConfirm (cancelText="" → single OK button)
 * mirroring V1's "cannot bind" dialog.
 */
async function onToggleWechatNotify(s: CodingSession): Promise<void> {
  const bound = s.wechat_notify_user_id !== undefined && s.wechat_notify_user_id !== null;
  if (bound) {
    await core.setWechatNotify(s.session_id, null);
    return;
  }
  const owner = s.owner ?? "";
  if (owner === "" || owner === "webui") {
    await confirm({
      icon: "⚠️",
      title: t("aiCoding.panel.cannotBindTitle"),
      message: t("aiCoding.panel.cannotBindWechat"),
      confirmText: t("common.gotIt", "Got it"),
      cancelText: "",
      confirmStyle: "primary",
    });
    return;
  }
  await core.setWechatNotify(s.session_id, owner);
}

/**
 * Toggle Feishu dual-channel notifications (V1 AiCodingPanel.js:483-512).
 * Mirrors `onToggleWechatNotify`. Note `setFeishuNotify` mutates the
 * session list optimistically so the badge / button color flips without
 * a full refresh.
 */
async function onToggleFeishuNotify(s: CodingSession): Promise<void> {
  const bound = s.feishu_notify_user_id !== undefined && s.feishu_notify_user_id !== null;
  if (bound) {
    await core.setFeishuNotify(s.session_id, null);
    return;
  }
  const owner = s.owner ?? "";
  if (owner === "" || owner === "webui") {
    await confirm({
      icon: "⚠️",
      title: t("aiCoding.panel.cannotBindTitle"),
      message: t("aiCoding.panel.cannotBindFeishu"),
      confirmText: t("common.gotIt", "Got it"),
      cancelText: "",
      confirmStyle: "primary",
    });
    return;
  }
  await core.setFeishuNotify(s.session_id, owner);
}

// ── Context badge (V1 token pill) ──────────────────────────────────────────────
function contextBadge(s: CodingSession): string | null {
  if (s.context_max_tokens === undefined || s.context_max_tokens === 0) {
    return null;
  }
  const total = s.context_total_tokens ?? 0;
  const pct =
    s.context_percentage !== undefined
      ? Math.round(s.context_percentage)
      : Math.round((total / s.context_max_tokens) * 100);
  const k = total >= 1000 ? `${(total / 1000).toFixed(1)}K` : `${total}`;
  return `~${k} / ${pct}%`;
}
function contextBadgeClass(s: CodingSession): string {
  const pct = s.context_percentage ?? 0;
  if (pct >= 80) return "cc-ctx-badge--danger";
  if (pct >= 60) return "cc-ctx-badge--warn";
  return "";
}

/**
 * Inline style for ctx badge (V1 parity: AiCodingPanel.js:1055-1064 — three
 * thresholds, danger/warn/ok with matching bg + border + color). V2 has the
 * same 80% / 60% / safe boundaries as V1.
 */
function ctxBadgeStyle(s: CodingSession): string {
  const pct = s.context_percentage ?? 0;
  const base =
    "display:inline-flex;align-items:center;padding:0 5px;height:14px;border-radius:7px;font-size:var(--text-xs);font-weight:600;flex-shrink:0;white-space:nowrap";
  if (pct >= 80) {
    return `${base};background:rgba(248,113,113,0.15);color:#f87171;border:1px solid rgba(248,113,113,0.3)`;
  }
  if (pct >= 60) {
    return `${base};background:rgba(251,191,36,0.15);color:#fbbf24;border:1px solid rgba(251,191,36,0.3)`;
  }
  return `${base};background:rgba(74,222,128,0.12);color:#4ade80;border:1px solid rgba(74,222,128,0.25)`;
}

/**
 * Last segment of a working dir path (V1 parity: AiCodingPanel.js getDirName helper).
 * Handles both "/" and "\" separators; falls back to the full string if none.
 */
function dirShortName(path: string): string {
  if (typeof path !== "string" || path === "") return "";
  const last = path.split(/[/\\]/).filter((s) => s !== "").pop();
  return last ?? path;
}

// ── V1 session-list badges (AiCodingPanel.js:1021-1064) ──────────────────────

/** Status icon (V1 claude-code-utils.js formatSessionStatus:98-111). */
function statusIcon(status: string): string {
  switch (status) {
    case "idle":
    case "active":
      return "🟢";
    case "running":
      return "🔄";
    case "error":
      return "🔴";
    case "closed":
    case "terminated":
      return "⚫";
    default:
      return "❓";
  }
}

/** Source-badge text/color (V1 AiCodingPanel.js:1027-1029 + getSourceColor:528). */
function sourceBadge(s: CodingSession): { label: string; color: string } | null {
  if (s.source === "wechat") {
    return { label: t("aiCoding.panel.srcWechat", "WeChat"), color: "#4ade80" };
  }
  if (s.source === "feishu") {
    return { label: t("aiCoding.panel.srcFeishu", "Feishu"), color: "#f97316" };
  }
  return null;
}

/** Dual-sync 🔔 badge when a notify channel is bound (V1:1029). */
function hasDualSync(s: CodingSession): boolean {
  return (
    (s.wechat_notify_user_id !== undefined && s.wechat_notify_user_id !== null) ||
    (s.feishu_notify_user_id !== undefined && s.feishu_notify_user_id !== null)
  );
}

/** Turns-badge color thresholds (V1 AiCodingPanel.js:1034-1042). */
function turnsColor(n: number): string {
  if (n >= 30) return "#f87171";
  if (n >= 20) return "#fbbf24";
  return "#7eb8f7";
}

/** Tool-calls-badge color thresholds (V1 AiCodingPanel.js:1043-1052). */
function toolsColor(n: number): string {
  if (n >= 50) return "#f87171";
  if (n >= 20) return "#fbbf24";
  return "#7eb8f7";
}

/**
 * Hover-tint behaviour (V1 parity: AiCodingPanel.js:1016-1017 — different tints
 * for active vs inactive items). Mirrors V1's inline @mouseenter/@mouseleave.
 */
function onItemHover(ev: MouseEvent, sessionId: string, entering: boolean): void {
  const el = ev.currentTarget as HTMLElement | null;
  if (el === null) return;
  const isActive = sessionId === core.activeSessionId.value;
  if (entering) {
    el.style.background = isActive
      ? "rgba(126,184,247,0.18)"
      : "var(--bg-hover)";
  } else {
    el.style.background = isActive
      ? meta.value.accentBgItem
      : "";
  }
}

// History tab uses the real persisted-history list (V1 historySessions,
// GET /sessions/history/all). Loaded on demand when the tab is opened.
const historySessions = computed(() => core.historySessions.value);

async function loadHistory(): Promise<void> {
  await core.loadHistorySessions(true, sourceFilter.value);
}

async function onRestore(sessionId: string, fork: boolean): Promise<void> {
  await core.restoreSession(sessionId, fork);
  activeTab.value = "active";
}

/**
 * Permanently delete a history-tab session record (V1 AiCodingPanel.js:351-367
 * handleDeleteSession). Asks for danger-style confirmation via the project
 * custom dialog (§3.9), then calls deleteSession (which routes to V2's
 * `DELETE {prefix}/sessions/{id}/permanent`). Refreshes both lists on
 * success so the row disappears from history.
 */
async function onDeleteHistory(sessionId: string): Promise<void> {
  const ok = await confirm({
    icon: "🗑️",
    title: t("aiCoding.panel.deleteSessionTitle"),
    message: t("aiCoding.panel.deleteSessionMessage"),
    confirmText: t("common.delete", "Delete"),
    cancelText: t("common.cancel", "Cancel"),
    confirmStyle: "danger",
  });
  if (!ok) return;
  await core.deleteSession(sessionId);
  await loadHistory();
}

async function onRefresh(): Promise<void> {
  await Promise.all([core.fetchSessions(), loadHealth()]);
  if (activeTab.value === "history") await loadHistory();
}

// Re-fetch history whenever the History tab opens or the source filter
// changes while it is open (V1 loadHistorySessions on tab/filter change).
watch(
  [activeTab, sourceFilter],
  ([tab]) => {
    if (tab === "history") void loadHistory();
  },
);

function onCollapse(): void {
  core.collapsePanel();
}
function onExit(): void {
  core.exitMode();
}

onMounted(() => {
  void loadHealth();
});
</script>

<template>
  <div
    class="cc-panel-float"
    :data-testid="`coding-panel-${kind}`"
  >
    <!-- Header (V1 parity: AiCodingPanel.js:843-881)
         - inline padding 8px 10px + border-bottom (V1 row container)
         - title font-weight:600 / font-size:base / flex:1
         - "New / Cancel" button = global .btn .btn-ghost .btn-sm with V1 inline
           font-size:var(--text-xs); padding:2px 8px (NOT a green pill!)
         - "—" collapse  = .btn .btn-icon .panel-header-collapse-btn
         - "✕" exit-mode = .btn .btn-icon .panel-header-exit-btn (red #f87171)
         CSS for the two header buttons lives in chat.css:2466-2477. -->
    <header
      style="display:flex;align-items:center;padding:8px 10px;border-bottom:1px solid var(--border);flex-shrink:0"
    >
      <span
        style="font-weight:600;font-size:var(--text-base);flex:1;display:flex;align-items:center;gap:4px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
      >
        <span aria-hidden="true">{{ meta.icon }}</span>
        {{ meta.name }}
      </span>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        style="font-size:var(--text-xs);padding:2px 8px"
        :title="showCreateForm
          ? t('common.cancel', 'Cancel')
          : t('aiCoding.panel.newCustomTitle', 'New session (custom working directory)')"
        :disabled="!showCreateForm && !envReady"
        :data-testid="`coding-panel-new-${kind}`"
        @click="showCreateForm = !showCreateForm"
      >
        {{ showCreateForm ? t("common.cancel", "Cancel") : t("aiCoding.panel.new", "New") }}
      </button>
      <button
        type="button"
        class="btn btn-icon panel-header-collapse-btn"
        style="font-size:var(--text-base);margin-left:4px;padding:0 6px"
        :title="t('aiCoding.panel.collapsePanelTitle', { name: meta.name }, `Collapse panel (${meta.name} mode stays active)`)"
        :aria-label="t('common.collapse')"
        :data-testid="`coding-panel-collapse-${kind}`"
        @click="onCollapse"
      >
        —
      </button>
      <button
        type="button"
        class="btn btn-icon panel-header-exit-btn"
        style="font-size:var(--text-base);margin-left:2px;padding:0 6px;color:#f87171"
        :title="t('aiCoding.panel.exitModeTitle', { name: meta.name }, `Exit ${meta.name} mode`)"
        :aria-label="t('aiCoding.panel.exitModeTitle', { name: meta.name }, `Exit ${meta.name} mode`)"
        :data-testid="`coding-panel-exit-${kind}`"
        @click="onExit"
      >
        ✕
      </button>
    </header>

    <!-- Tabs (V1 parity: AiCodingPanel.js:884-899)
         border-bottom:1px solid var(--border) on the row, each tab uses inline
         padding:5px 0 / font-size:var(--text-xs) / no border / 2px bottom
         accent + active background tint (provider color for Active, #a78bfa for
         History). -->
    <div
      style="display:flex;border-bottom:1px solid var(--border);flex-shrink:0"
    >
      <button
        type="button"
        :style="activeTab === 'active'
          ? `flex:1;padding:5px 0;font-size:var(--text-xs);border:none;cursor:pointer;transition:background 0.15s;border-bottom:2px solid ${meta.accent};color:${meta.accent};background:${meta.accentBg}`
          : 'flex:1;padding:5px 0;font-size:var(--text-xs);border:none;cursor:pointer;transition:background 0.15s;border-bottom:2px solid transparent;color:var(--text-muted);background:transparent'"
        @click="activeTab = 'active'"
      >
        {{ t("aiCoding.panel.activeSessions", "Active Sessions") }}
      </button>
      <button
        type="button"
        :style="activeTab === 'history'
          ? 'flex:1;padding:5px 0;font-size:var(--text-xs);border:none;cursor:pointer;transition:background 0.15s;border-bottom:2px solid #a78bfa;color:#a78bfa;background:rgba(167,139,250,0.06)'
          : 'flex:1;padding:5px 0;font-size:var(--text-xs);border:none;cursor:pointer;transition:background 0.15s;border-bottom:2px solid transparent;color:var(--text-muted);background:transparent'"
        @click="activeTab = 'history'"
      >
        {{ t("aiCoding.panel.history", "History") }}
      </button>
    </div>

    <!-- Availability hint (V1 parity: AiCodingPanel.js:902-911 — env-not-ready row)
         padding 8px 10px / font-size:xs / red text on red-tinted bg, with an
         underlined "Recheck" affordance and a border-bottom separator. -->
    <div
      v-if="activeTab === 'active' && !envReady && !healthLoading"
      style="padding:8px 10px;font-size:var(--text-xs);color:#f87171;background:rgba(248,113,113,0.08);border-bottom:1px solid var(--border);flex-shrink:0"
      :data-testid="`coding-panel-hint-${kind}`"
    >
      <span v-if="kind === 'oc'">{{ t("aiCoding.panel.ocNotConnected", "OpenCode service not connected — start it in Settings > AI Coding") }}</span>
      <span v-else-if="!isSdkAvailable">{{ t("aiCoding.panel.sdkNotInstalled", "Claude Code SDK not installed — see Settings > AI Coding") }}</span>
      <span v-else-if="!isAuthConfigured">{{ t("aiCoding.panel.authNotConfigured", "Authentication not configured (set API Key in Settings > AI Coding)") }}</span>
      <span
        style="margin-left:6px;cursor:pointer;text-decoration:underline"
        @click="loadHealth"
      >{{ t("aiCoding.panel.recheck", "Recheck") }}</span>
    </div>

    <!-- New session form (V1 parity: AiCodingPanel.js:913-944 — inline create form
         shown below the header when "New" is toggled). padding 8px 10px /
         border-bottom / vertical stack / two text inputs + submit button. -->
    <form
      v-if="showCreateForm"
      style="padding:8px 10px;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--bg-secondary);display:flex;flex-direction:column;gap:6px"
      :data-testid="`coding-panel-form-${kind}`"
      @submit.prevent="onCreate"
    >
      <input
        v-model="newTitle"
        type="text"
        class="cc-input"
        style="font-size:var(--text-sm);width:100%;box-sizing:border-box"
        :placeholder="t('aiCoding.panel.sessionNamePlaceholder', 'Session name (optional)')"
        :data-testid="`coding-panel-title-${kind}`"
      />
      <input
        v-model="newWorkspace"
        type="text"
        class="cc-input cc-input--mono"
        style="font-size:var(--text-sm);width:100%;box-sizing:border-box"
        :placeholder="t('aiCoding.panel.workingDirPlaceholder', 'Working directory path…')"
        :data-testid="`coding-panel-workspace-${kind}`"
        required
      />
      <div
        v-if="formError !== ''"
        style="font-size:var(--text-xs);color:#f87171"
      >
        {{ formError }}
      </div>
      <button
        type="submit"
        class="btn btn-primary btn-sm"
        style="width:100%;font-size:var(--text-sm)"
        :data-testid="`coding-panel-create-${kind}`"
      >
        {{ t("aiCoding.panel.createSession", "Create Session") }}
      </button>
    </form>

    <!-- Source filter (V1 parity: AiCodingPanel.js:949-960)
         row inline padding 5px 10px + gap:4px; pills inline
         font-size:var(--text-xs);padding:1px 7px;border-radius:10px;
         active = filled with provider accent (text on accent: #0d1b2a). -->
    <div
      style="display:flex;align-items:center;padding:5px 10px;border-bottom:1px solid var(--border);flex-shrink:0;gap:4px"
    >
      <span
        style="font-size:var(--text-xs);color:var(--text-muted);flex-shrink:0"
      >{{ t("aiCoding.panel.source", "Source:") }}</span>
      <button
        v-for="src in (['all', 'webchat', 'wechat', 'feishu'] as const)"
        :key="src"
        type="button"
        :style="sourceFilter === src
          ? `font-size:var(--text-xs);padding:1px 7px;border-radius:10px;border:1px solid ${meta.accent};cursor:pointer;transition:all 0.15s;background:${meta.accent};color:#0d1b2a`
          : 'font-size:var(--text-xs);padding:1px 7px;border-radius:10px;border:1px solid var(--border);cursor:pointer;transition:all 0.15s;background:transparent;color:var(--text-muted)'"
        @click="sourceFilter = src"
      >
        {{ sourceLabel(src) }}
      </button>
    </div>

    <!-- Session list (V1 parity: AiCodingPanel.js:993-1088)
         outer wrapper: flex:1;overflow-y:auto;padding:4px 0
         each item: padding:5px 8px / cursor:pointer /
                    border-left:2px solid (transparent | provider color when active) /
                    border-bottom:1px solid rgba(255,255,255,0.04)
                    hover background tint (handled via inline mouseenter/leave like V1).
         Three-row layout:
           row 1: status icon + name (provider color when active)
           row 2: 📁 dir-name + turns badge + tools badge + ctx badge
           row 3: action buttons (Rename / Dir / Close) — V1 .btn .btn-ghost .btn-sm
         V2 has fewer fields than V1 (no source/turns/tool counts in API yet),
         so we render what we have without inventing values; layout & geometry
         match V1 row-for-row. -->
    <div
      style="flex:1;overflow-y:auto;padding:4px 0"
      :data-testid="`coding-panel-list-${kind}`"
    >
      <div
        v-if="(activeTab === 'active' ? sessions.filter((x) => x.status === 'active').length : historySessions.length) === 0"
        style="padding:12px 10px;font-size:var(--text-sm);color:var(--text-muted);text-align:center"
      >
        {{ activeTab === 'active'
          ? t("aiCoding.panel.noActiveSessions", 'No active sessions, click "+ New" to start')
          : t("aiCoding.panel.noHistorySessions", "No history sessions") }}
      </div>
      <template v-else>
        <div
          v-for="s in (activeTab === 'active' ? sessions.filter((x) => x.status === 'active') : historySessions)"
          :key="s.session_id"
          :style="s.session_id === core.activeSessionId.value
            ? `padding:5px 8px;cursor:pointer;border-left:2px solid ${meta.accent};background:${meta.accentBgItem};transition:background 0.15s;border-bottom:1px solid var(--border)`
            : 'padding:5px 8px;cursor:pointer;border-left:2px solid transparent;transition:background 0.15s;border-bottom:1px solid var(--border)'"
          @click="onSelect(s.session_id)"
          @mouseenter="onItemHover($event, s.session_id, true)"
          @mouseleave="onItemHover($event, s.session_id, false)"
        >
          <!-- Inline rename input -->
          <template v-if="renamingId === s.session_id">
            <input
              v-model="renameValue"
              class="cc-input"
              style="width:100%;font-size:var(--text-sm);box-sizing:border-box"
              :data-testid="`coding-panel-rename-input-${kind}`"
              @click.stop
              @keydown.enter.prevent="commitRename"
              @keydown.esc.prevent="renamingId = null"
              @blur="commitRename"
            />
          </template>
          <!-- Inline change-dir input -->
          <template v-else-if="changingDirId === s.session_id">
            <input
              v-model="changeDirValue"
              class="cc-input cc-input--mono"
              style="width:100%;font-size:var(--text-sm);box-sizing:border-box"
              :data-testid="`coding-panel-change-dir-input-${kind}`"
              @click.stop
              @keydown.enter.prevent="commitChangeDir"
              @keydown.esc.prevent="changingDirId = null"
              @blur="commitChangeDir"
            />
          </template>
          <template v-else>
            <!-- Row 1: status icon + name + source badge + dual-sync (V1:1021-1033) -->
            <div style="display:flex;align-items:center;gap:4px;min-width:0">
              <span
                aria-hidden="true"
                style="flex-shrink:0;font-size:var(--text-xs)"
              >{{ statusIcon(s.status) }}</span>
              <span
                :style="s.session_id === core.activeSessionId.value
                  ? `font-size:var(--text-sm);font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:${meta.accent}`
                  : 'font-size:var(--text-sm);font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-primary)'"
                :title="s.title ?? s.session_id"
              >
                {{ s.title ?? s.session_id }}
              </span>
              <span
                v-if="sourceBadge(s) !== null"
                :style="`flex-shrink:0;font-size:var(--text-xs);padding:0 5px;border-radius:7px;color:${sourceBadge(s)!.color};border:1px solid ${sourceBadge(s)!.color};background:transparent`"
                :title="s.owner ?? ''"
              >{{ sourceBadge(s)!.label }}</span>
              <span
                v-if="hasDualSync(s)"
                style="flex-shrink:0;color:#fbbf24;font-size:var(--text-xs)"
                :title="t('aiCoding.panel.dualSyncOn', 'Dual-channel sync enabled')"
              >🔔</span>
            </div>
            <!-- Row 2: 📁 working dir + turns + tools + ctx badge (V1:1034-1064) -->
            <div style="display:flex;align-items:center;gap:4px;margin-top:2px;padding-left:0;overflow:hidden">
              <span
                style="font-size:var(--text-xs);color:var(--text-muted);flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0"
                :title="s.workspace"
              >📁 {{ dirShortName(s.workspace) }}</span>
              <span
                v-if="(s.turn_count ?? 0) > 0"
                :style="`flex-shrink:0;font-size:var(--text-xs);color:${turnsColor(s.turn_count ?? 0)};white-space:nowrap`"
                :title="t('aiCoding.panel.totalTurns', { n: s.turn_count ?? 0 }, `${s.turn_count ?? 0} turns total`)"
              >{{ t("aiCoding.panel.turnsSuffix", { n: s.turn_count ?? 0 }, `${s.turn_count ?? 0} turns`) }}</span>
              <span
                v-if="(s.total_tool_calls ?? 0) > 0"
                :style="`flex-shrink:0;font-size:var(--text-xs);color:${toolsColor(s.total_tool_calls ?? 0)};white-space:nowrap`"
                :title="t('aiCoding.panel.totalToolCalls', { n: s.total_tool_calls ?? 0 }, `${s.total_tool_calls ?? 0} total tool calls`)"
              >🔧{{ s.total_tool_calls ?? 0 }}</span>
              <span
                v-if="contextBadge(s) !== null"
                :style="ctxBadgeStyle(s)"
                :title="t('aiCoding.panel.ctxLast', { current: String(s.context_total_tokens ?? 0) }, `Context ~${s.context_total_tokens ?? 0} tokens at last send`)"
              >{{ contextBadge(s) }}</span>
            </div>
            <!-- Row 3: action buttons (V1 .btn .btn-ghost .btn-sm with tight inline) -->
            <div style="display:flex;gap:3px;flex-wrap:wrap;margin-top:3px">
              <!-- History-tab actions: Restore + New-branch fork + Delete (V1:1231-1282) -->
              <template v-if="activeTab === 'history'">
                <button
                  type="button"
                  class="btn btn-ghost btn-sm"
                  style="font-size:var(--text-xs);padding:1px 6px;color:#4ade80;border:1px solid rgba(74,222,128,0.4);white-space:nowrap"
                  :title="t('aiCoding.panel.restoreSessionTitle', 'Restore session')"
                  :data-testid="`coding-panel-restore-${kind}`"
                  @click.stop="onRestore(s.session_id, false)"
                >
                  {{ t("aiCoding.panel.restore", "Restore") }}
                </button>
                <button
                  v-if="kind === 'cc' && s.claude_session_id"
                  type="button"
                  class="btn btn-ghost btn-sm"
                  style="font-size:var(--text-xs);padding:1px 6px;color:#a78bfa;border:1px solid rgba(167,139,250,0.4);white-space:nowrap"
                  :title="t('aiCoding.panel.restoreForkTitle', 'Restore and fork a new branch')"
                  :data-testid="`coding-panel-fork-${kind}`"
                  @click.stop="onRestore(s.session_id, true)"
                >
                  {{ t("aiCoding.panel.newBranch", "New Branch") }}
                </button>
                <!-- Permanent-delete (V1 AiCodingPanel.js:1276-1282).
                     useConfirm danger style (§3.9 compliant). -->
                <button
                  type="button"
                  class="btn btn-ghost btn-sm"
                  style="font-size:var(--text-xs);padding:1px 6px;color:#f87171;border:1px solid rgba(248,113,113,0.3);white-space:nowrap"
                  :title="t('aiCoding.panel.deletePermanentTitle')"
                  :data-testid="`coding-panel-delete-${kind}`"
                  @click.stop="onDeleteHistory(s.session_id)"
                >
                  {{ t("common.delete", "Delete") }}
                </button>
              </template>
              <!-- Active-tab actions: Rename / Dir / Fork active / Close -->
              <template v-else>
                <button
                  type="button"
                  class="btn btn-ghost btn-sm"
                  style="font-size:var(--text-xs);padding:1px 6px;color:var(--text-muted);border:1px solid var(--border);white-space:nowrap"
                  :title="t('aiCoding.panel.renameTitle', 'Rename this session')"
                  @click.stop="startRename(s.session_id, s.title)"
                >
                  ✏️ {{ t("common.rename", "Rename") }}
                </button>
                <button
                  type="button"
                  class="btn btn-ghost btn-sm"
                  style="font-size:var(--text-xs);padding:1px 6px;color:var(--text-muted);border:1px solid var(--border);white-space:nowrap"
                  :title="t('aiCoding.panel.changeDirTitle', 'Change working directory')"
                  @click.stop="startChangeDir(s.session_id, s.workspace)"
                >
                  📁 {{ t("aiCoding.panel.directory", "Dir") }}
                </button>
                <!-- Fork active session as a new branch (V1 AiCodingPanel.js:1077-1083).
                     Shown only on CC active sessions that have a claude_session_id
                     (i.e. at least one successful turn so the SDK conversation id
                     exists and forking is meaningful). Calls restoreSession(id, true)
                     under the hood — V2 already supports the fork flag. -->
                <button
                  v-if="kind === 'cc' && s.claude_session_id"
                  type="button"
                  class="btn btn-ghost btn-sm"
                  style="font-size:var(--text-xs);padding:1px 6px;color:#a78bfa;border:1px solid rgba(167,139,250,0.4);white-space:nowrap"
                  :title="t('aiCoding.panel.forkActiveTitle')"
                  :data-testid="`coding-panel-fork-active-${kind}`"
                  @click.stop="onForkActive(s.session_id)"
                >
                  {{ t("aiCoding.panel.newBranch", "New Branch") }}
                </button>
                <!-- WeChat dual-sync toggle (V1 AiCodingPanel.js:1254-1264).
                     Visible only on wechat-source active sessions; flips between
                     bound (🔔 同步) and unbound (🔕 同步) states. -->
                <button
                  v-if="s.source === 'wechat'"
                  type="button"
                  class="btn btn-ghost btn-sm"
                  :style="(s.wechat_notify_user_id !== undefined && s.wechat_notify_user_id !== null)
                    ? 'font-size:var(--text-xs);padding:1px 6px;color:#fbbf24;border:1px solid rgba(251,191,36,0.5);white-space:nowrap'
                    : 'font-size:var(--text-xs);padding:1px 6px;color:var(--text-muted);border:1px solid var(--border);white-space:nowrap'"
                  :title="(s.wechat_notify_user_id !== undefined && s.wechat_notify_user_id !== null)
                    ? t('aiCoding.panel.wechatSyncOff')
                    : t('aiCoding.panel.wechatSyncOn')"
                  :data-testid="`coding-panel-wechat-toggle-${kind}`"
                  @click.stop="onToggleWechatNotify(s)"
                >
                  {{ (s.wechat_notify_user_id !== undefined && s.wechat_notify_user_id !== null)
                    ? t("aiCoding.panel.syncOnLabel")
                    : t("aiCoding.panel.syncOffLabel") }}
                </button>
                <!-- Feishu dual-sync toggle (V1 AiCodingPanel.js:1266-1275). -->
                <button
                  v-if="s.source === 'feishu'"
                  type="button"
                  class="btn btn-ghost btn-sm"
                  :style="(s.feishu_notify_user_id !== undefined && s.feishu_notify_user_id !== null)
                    ? 'font-size:var(--text-xs);padding:1px 6px;color:#fbbf24;border:1px solid rgba(251,191,36,0.5);white-space:nowrap'
                    : 'font-size:var(--text-xs);padding:1px 6px;color:var(--text-muted);border:1px solid var(--border);white-space:nowrap'"
                  :title="(s.feishu_notify_user_id !== undefined && s.feishu_notify_user_id !== null)
                    ? t('aiCoding.panel.feishuSyncOff')
                    : t('aiCoding.panel.feishuSyncOn')"
                  :data-testid="`coding-panel-feishu-toggle-${kind}`"
                  @click.stop="onToggleFeishuNotify(s)"
                >
                  {{ (s.feishu_notify_user_id !== undefined && s.feishu_notify_user_id !== null)
                    ? t("aiCoding.panel.syncOnLabel")
                    : t("aiCoding.panel.syncOffLabel") }}
                </button>
                <button
                  type="button"
                  class="btn btn-ghost btn-sm"
                  style="font-size:var(--text-xs);padding:1px 6px;color:#f87171;border:1px solid rgba(248,113,113,0.4);white-space:nowrap"
                  :title="t('aiCoding.panel.closeSessionTitle', 'Close session')"
                  @click.stop="onClose(s.session_id)"
                >
                  {{ t("common.close", "Close") }}
                </button>
              </template>
            </div>
          </template>
        </div>
      </template>
    </div>

    <!-- Footer status bar (V1 parity: AiCodingPanel.js:1350-1388)
         Outer wrapper: border-top + flex-shrink:0
         Inner row: padding 4px 10px / font-size:xs / display flex / gap 8px
         Connection state colored #4ade80 (ok) / #f87171 (off).
         "Active N · Total M" text uses --text-muted; refresh btn is .btn .btn-ghost .btn-sm.
         Pill text label is hidden by .ai-coding-statusbar-refresh-label media rules
         (components.css:1031-1040 — V1-faithful 280px panel hides the word). -->
    <div style="border-top:1px solid var(--border);flex-shrink:0">
      <footer
        class="ai-coding-statusbar"
        style="padding:4px 10px;font-size:var(--text-xs);color:var(--text-muted);display:flex;align-items:center;gap:8px"
      >
        <span
          v-if="healthLoading"
          class="spinner"
          style="width:11px;height:11px;border-width:1.5px;flex-shrink:0"
        ></span>
        <div
          class="ai-coding-statusbar-info"
          style="display:flex;align-items:center;gap:8px;min-width:0;flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis"
        >
          <!-- Claude Code: SDK version + auth status (V1 AiCodingPanel.js:1361-1368) -->
          <template v-if="kind === 'cc'">
            <span
              :style="sdkAvailable ? 'color:#4ade80;flex-shrink:0' : 'color:#f87171;flex-shrink:0'"
              :title="`SDK ${sdkVersion}`"
            >
              SDK {{ sdkAvailable ? "✅" : "❌" }}
              <span class="ai-coding-statusbar-detail">{{ sdkVersion }}</span>
            </span>
            <span
              :style="authConfigured ? 'color:#4ade80;flex-shrink:0' : 'color:#f87171;flex-shrink:0'"
              :title="t('aiCoding.panel.authStatusTitle', 'Authentication status')"
            >
              {{ t("aiCoding.panel.authLabel", "Auth") }} {{ authConfigured ? "✅" : "❌" }}
            </span>
          </template>
          <!-- OpenCode: HTTP service connection state (V1 AiCodingPanel.js:1370-1374) -->
          <template v-else>
            <span
              :style="available
                ? 'color:#4ade80;flex-shrink:0'
                : 'color:#f87171;flex-shrink:0'"
              :title="t('aiCoding.panel.httpStatusTitle', 'HTTP service connection status')"
            >
              {{ t("aiCoding.panel.serviceLabel", "Service") }}
              {{ available
                ? t("aiCoding.panel.connected", "Connected")
                : t("aiCoding.panel.notConnected", "Not connected") }}
            </span>
          </template>
          <span
            class="ai-coding-statusbar-sessions"
            style="overflow:hidden;text-overflow:ellipsis;min-width:0"
            :title="t('aiCoding.panel.sessionsCountTitle', 'Active sessions / total sessions')"
          >
            {{ t("aiCoding.panel.activeShort", "Active") }} {{ activeCount }} ·
            {{ t("aiCoding.panel.totalShort", "Total") }} {{ totalCount }}
          </span>
        </div>
        <button
          type="button"
          class="btn btn-ghost btn-sm ai-coding-statusbar-refresh"
          style="font-size:var(--text-xs);padding:1px 6px;flex-shrink:0;display:flex;align-items:center;gap:3px"
          :title="t('aiCoding.panel.refreshStatusTitle', 'Refresh status')"
          :data-testid="`coding-panel-refresh-${kind}`"
          @click="onRefresh"
        >
          🔄
          <span class="ai-coding-statusbar-refresh-label">{{ t("aiCoding.panel.refreshStatus", "Refresh") }}</span>
        </button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
/* `.cc-panel-float` container positioning lives in components.css (shared
 * with V1 — width:280px, max-height:580px, position:absolute, right:0,
 * bg/border/shadow/radius). Header / tabs / source pills / session items /
 * footer all use V1 inline styles applied directly in the template, mirroring
 * AiCodingPanel.js so colors, paddings and font-sizes match V1 1:1.
 *
 * Two header-button class hooks live in chat.css and are referenced by name
 * (.panel-header-collapse-btn / .panel-header-exit-btn) for hover behavior.
 *
 * Only a few utility selectors remain scoped here:
 *   - `.cc-input` / `.cc-input--mono` — inline text inputs (rename / change-dir)
 *   - `.cc-btn`   / `.cc-btn--primary` — form submit button
 *   - `.cc-ctx-badge*` — fallback alt class for callers that prefer class-based
 *                       theming (the live styling is computed inline via
 *                       ctxBadgeStyle so the three thresholds match V1).
 */
.cc-input {
  padding: 6px 8px;
  border: 1px solid var(--border, #2a2750);
  border-radius: 4px;
  background: var(--bg-primary, #0f0d24);
  color: var(--text-primary, #e9e7ff);
  font-size: var(--text-xs, 12px);
  font-family: inherit;
}
.cc-input--mono {
  font-family: var(--font-mono, monospace);
}
.cc-btn {
  padding: 5px 12px;
  border: 1px solid var(--border, #2a2750);
  border-radius: 4px;
  background: transparent;
  color: var(--text-primary, #e9e7ff);
  cursor: pointer;
  font-size: var(--text-xs, 12px);
}
.cc-btn--primary {
  background: var(--accent, #7c6cf0);
  color: #fff;
  border-color: var(--accent, #7c6cf0);
}
.cc-ctx-badge {
  font-size: 10px;
  color: var(--text-muted, #6b7280);
  margin-top: 2px;
}
.cc-ctx-badge--warn {
  color: var(--warning, #f59e0b);
}
.cc-ctx-badge--danger {
  color: var(--error, #ef4444);
}
</style>

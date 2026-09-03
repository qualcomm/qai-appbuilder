<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModeFrameSsh — SSH remote server management panel.
 *
 * Floating panel (Teleport to body). Always mounted; hidden when
 * `visible` is false. Clicking the backdrop or ✕ emits `close`.
 *
 * Style: reuses the project-wide rename-dialog-* shell + config-input
 * + rename-dialog-btn tokens so it matches the app theme (dark / light).
 *
 * Features:
 *   - Multi-server list: add / remove / collapse-expand per server card
 *   - Per-server: Test Connection + Install + Start (SSE progress + log)
 *   - Running instances bar: Open + Stop chips
 *   - Auth: password or private key + passphrase
 */
import { ref, watch, onMounted, nextTick } from "vue";
import { useI18n } from "vue-i18n";
import {
  useSshRemote,
  DEFAULT_REMOTE_PORT,
  OKTA_REGISTERED_PORTS,
  type SshDeployParams,
  type DeployHooks,
} from "@/composables/useSshRemote";

const props = defineProps<{ visible: boolean }>();
const emit = defineEmits<{ close: [] }>();

const { t } = useI18n();

const {
  testConnect,
  install,
  start,
  instances,
  loadInstances,
  stopInstance,
  openRemoteUrl,
  blockedOpenUrl,
} = useSshRemote();

// ── Server draft ──────────────────────────────────────────────────────────────
interface ServerDraft {
  id: string;
  name: string;
  host: string;
  username: string;
  authMethod: "password" | "private_key";
  authRef: string;
  keyPath: string;
  sshPort: number;
  remotePort: number;
  enableSso: boolean;
  showLog: boolean;
  expanded: boolean;
  saved: boolean;
}

const servers = ref<ServerDraft[]>([]);
const activeDeployId = ref<string | null>(null);

// ── Per-server state maps (BUG 10/11 fix) ────────────────────────────────────
// Shared composable refs (connectSuccess, connectError, deployLog, etc.) are
// single values — showing result for server A would overwrite server B's card.
// Instead, key all feedback by server.id so each card is independent.

interface ConnectResult {
  success: boolean;
  error: string | null;
  connecting: boolean;
}

interface DeployState {
  log: string[];
  percent: number;
  error: string | null;
  deploying: boolean;
  /**
   * Which button owns the run in flight. Both buttons are disabled while any
   * run is active (`deploying`), but only the one that started it may show its
   * progress label — a shared boolean made Install's run read as "Starting…"
   * on the Start button.
   */
  phase: "install" | "start" | null;
}

const connectResults = ref<Record<string, ConnectResult>>({});
const deployStates = ref<Record<string, DeployState>>({});

function getConnectResult(id: string): ConnectResult {
  if (!connectResults.value[id]) {
    connectResults.value[id] = { success: false, error: null, connecting: false };
  }
  return connectResults.value[id]!;
}

function getDeployState(id: string): DeployState {
  if (!deployStates.value[id]) {
    deployStates.value[id] = {
      log: [], percent: 0, error: null, deploying: false, phase: null,
    };
  }
  return deployStates.value[id]!;
}

function newDraft(): ServerDraft {
  return {
    id: crypto.randomUUID(),
    name: "",
    host: "",
    username: "",
    authMethod: "password",
    authRef: "",
    keyPath: "",
    sshPort: 22,
    remotePort: DEFAULT_REMOTE_PORT,
    enableSso: true,
    showLog: false,
    expanded: true,
    saved: false,
  };
}

function saveServer(s: ServerDraft): void {
  s.saved = true;
  // Persist to localStorage so servers survive page reload
  const stored = servers.value.map(({ authRef: _cred, ...rest }) => rest);
  try {
    localStorage.setItem("qai_ssh_servers", JSON.stringify(stored));
  } catch {
    // storage quota — non-fatal
  }
}

function loadSavedServers(): void {
  try {
    const raw = localStorage.getItem("qai_ssh_servers");
    if (!raw) return;
    const parsed: Array<Partial<ServerDraft>> = JSON.parse(raw);
    servers.value = parsed.map((p) => ({
      ...newDraft(),
      ...p,
      sshPort: Number(p.sshPort) || 22,
      remotePort: Number(p.remotePort) || DEFAULT_REMOTE_PORT,
      // Servers saved before the SSO switch existed have no flag; default on
      // so an upgrade keeps the login gate rather than silently dropping it.
      enableSso: p.enableSso ?? true,
      authRef: "",
      saved: true,
    }));
  } catch {
    // corrupt storage — ignore
  }
}

function addServer(): void {
  servers.value.push(newDraft());
}

function removeServer(id: string): void {
  servers.value = servers.value.filter((s) => s.id !== id);
  delete connectResults.value[id];
  delete deployStates.value[id];
}

function toggleExpand(s: ServerDraft): void {
  s.expanded = !s.expanded;
}

// ── Tunnel helpers ────────────────────────────────────────────────────────────
// The local tunnel port always equals the remote port. That is a hard
// requirement of the SSO flow, not a convenience: the board derives its Okta
// `redirect_uri` from its own bound port, so Okta sends the browser to
// `http://localhost:<remote port>/callback`, which only reaches the board if
// our listener owns that same number locally. The backend allocates it and
// reports it back as `local_url`, so this component never computes a port.

/** Just the URL fields these helpers read — keeps them usable with the
 *  deep-readonly instances the composable exposes. */
interface InstanceUrls {
  local_url?: string;
  remote_url: string;
}

function tunnelCommand(s: ServerDraft): string {
  const port = s.sshPort === 22 ? "" : ` -p ${s.sshPort}`;
  const key = s.authMethod === "private_key" && s.keyPath
    ? ` -i ${s.keyPath}`
    : "";
  return `ssh -N${port}${key} -L ${s.remotePort}:localhost:${s.remotePort} ${s.username}@${s.host}`;
}
function tunnelUrl(s: ServerDraft): string {
  return `http://localhost:${s.remotePort}/chat`;
}
function openUrl(url: string): void {
  window.open(url, "_blank", "noopener,noreferrer");
}
function instanceLabel(inst: InstanceUrls): string {
  const url = inst.local_url || inst.remote_url;
  return url.replace(/^https?:\/\//, "").replace(/\/chat$/, "");
}
function openInstance(inst: InstanceUrls): void {
  // Prefer the tunnel URL the backend actually bound; the direct remote URL is
  // only a fallback and cannot complete an SSO login (its origin is the board's
  // IP, which is not a registered redirect_uri).
  const url = inst.local_url || inst.remote_url;
  if (url) openUrl(url);
}

// ── Log auto-scroll ───────────────────────────────────────────────────────────
// The log box is short and fills fast. Without pinning it to the bottom the
// operator keeps staring at the first few lines while the real output — and any
// error — scrolls below the fold, which makes a slow-but-working install look
// identical to a stalled one. Plain (non-reactive) map: these are DOM nodes
// read imperatively, and wrapping them in a ref() would proxy them for nothing.
const logEls: Record<string, HTMLElement | null> = {};

function setLogEl(id: string, el: unknown): void {
  logEls[id] = (el as HTMLElement | null) ?? null;
}

function scrollLogToBottom(id: string): void {
  void nextTick(() => {
    const el = logEls[id];
    if (el) el.scrollTop = el.scrollHeight;
  });
}

// ── Actions ───────────────────────────────────────────────────────────────────
async function onTestConnect(s: ServerDraft): Promise<void> {
  const cr = getConnectResult(s.id);
  cr.connecting = true;
  cr.success = false;
  cr.error = null;
  try {
    const ok = await testConnect({
      host: s.host,
      ssh_port: s.sshPort,
      username: s.username,
      auth_method: s.authMethod,
      auth_ref: s.authRef,
      key_path: s.keyPath,
    });
    cr.success = ok;
    if (!ok) cr.error = t("index.sshConnectFailed");
  } catch (err) {
    cr.success = false;
    cr.error = err instanceof Error ? err.message : String(err);
  } finally {
    cr.connecting = false;
  }
}

// Install and Start drive an identical per-card lifecycle and differ only in
// which composable call they make, so they share one runner. `phase` records
// which of the two is in flight so only that button shows a progress label.
async function runPhase(
  s: ServerDraft,
  phase: "install" | "start",
  run: (params: SshDeployParams, hooks: DeployHooks) => Promise<unknown>,
): Promise<void> {
  activeDeployId.value = s.id;
  s.showLog = true;
  const ds = getDeployState(s.id);
  ds.log = [];
  ds.percent = 0;
  ds.error = null;
  ds.deploying = true;
  ds.phase = phase;
  try {
    await run(
      {
        host: s.host,
        ssh_port: s.sshPort,
        username: s.username,
        auth_method: s.authMethod,
        auth_ref: s.authRef,
        key_path: s.keyPath,
        remote_port: s.remotePort,
        enable_sso: s.enableSso,
      },
      {
        // Route each SSE frame into THIS card's state. The composable's own
        // deployLog/deployPercent refs are single-valued and would show
        // server A's output on server B's card.
        onLine(line, percent) {
          ds.log.push(line);
          ds.percent = percent;
          scrollLogToBottom(s.id);
        },
        onError(message) {
          // The SSE error frame does not throw, so without this the card's
          // error line would stay empty on a failed run.
          ds.error = message;
        },
      },
    );
  } catch (err) {
    ds.error = err instanceof Error ? err.message : String(err);
  } finally {
    ds.deploying = false;
    ds.phase = null;
    activeDeployId.value = null;
  }
}

/** Download the bundle and run setup.sh. Starts nothing, so nothing to open. */
function onInstall(s: ServerDraft): Promise<void> {
  return runPhase(s, "install", install);
}

/** Start an installed service. `start()` opens the tunnel URL itself once the
 *  tunnel is up, so there is nothing to do here afterwards. */
function onStart(s: ServerDraft): Promise<void> {
  return runPhase(s, "start", start);
}

// backdrop: only dismiss if the pointerdown also started on the overlay
const pointerDownOnOverlay = ref(false);
function onOverlayPointerDown(e: PointerEvent): void {
  pointerDownOnOverlay.value = e.target === e.currentTarget;
}
function onOverlayClick(e: MouseEvent): void {
  if (e.target === e.currentTarget && pointerDownOnOverlay.value) emit("close");
  pointerDownOnOverlay.value = false;
}

// ── Lifecycle ─────────────────────────────────────────────────────────────────
onMounted(() => {
  loadSavedServers();
  void loadInstances();
  if (servers.value.length === 0) addServer();
});

watch(
  () => props.visible,
  (v) => { if (v) void loadInstances(); },
);
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="rename-dialog-overlay ssh-overlay"
      data-testid="ssh-panel-backdrop"
      @pointerdown="onOverlayPointerDown"
      @click="onOverlayClick"
    >
      <div
        class="rename-dialog ssh-dialog"
        role="dialog"
        aria-modal="true"
        data-testid="ssh-panel"
        :aria-label="t('index.sshMode')"
      >
        <!-- ── Header ──────────────────────────────────────────────────────── -->
        <div class="rename-dialog-title ssh-header">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <rect x="2" y="3" width="20" height="15" rx="2" />
            <path d="M6 8l4 4-4 4" /><path d="M12 16h6" />
          </svg>
          <span>{{ t("index.sshMode") }}</span>
          <span
            v-if="instances.filter(i => i.state === 'running').length > 0"
            class="ssh-badge"
          >{{ instances.filter(i => i.state === "running").length }}</span>
          <button
            type="button"
            class="ssh-close-btn"
            :aria-label="t('index.close')"
            data-testid="ssh-panel-close"
            @click="emit('close')"
          >✕</button>
        </div>

        <!-- ── Running instances ──────────────────────────────────────────── -->
        <div v-if="instances.length > 0" class="ssh-instances">
          <p class="ssh-section-label">{{ t("index.sshInstances", { count: instances.length }) }}</p>
          <div class="ssh-chips">
            <div
              v-for="inst in instances"
              :key="inst.instance_id"
              class="ssh-chip"
              :class="`ssh-chip--${inst.state}`"
            >
              <span class="ssh-chip-dot"></span>
              <span class="ssh-chip-host">{{ instanceLabel(inst) }}</span>
              <button
                v-if="inst.state === 'running'"
                type="button"
                class="ssh-chip-action"
                @click="openInstance(inst)"
              >{{ t("index.sshOpen") }}</button>
              <button
                type="button"
                class="ssh-chip-action ssh-chip-action--danger"
                @click="stopInstance(inst.instance_id)"
              >{{ t("index.sshStop") }}</button>
            </div>
          </div>
        </div>

        <!-- ── Blocked auto-open ──────────────────────────────────────────── -->
        <!-- Start opens its tab only after the SSE stream ends, tens of seconds
             past the click, so the browser silently blocks it. This is the
             recovery path: a real click, which always succeeds. -->
        <div v-if="blockedOpenUrl" class="ssh-blocked" data-testid="ssh-open-blocked">
          <span class="ssh-blocked-text">{{ t("index.sshOpenBlocked") }}</span>
          <button
            type="button"
            class="rename-dialog-btn rename-dialog-btn--confirm"
            data-testid="ssh-open-blocked-action"
            @click="openRemoteUrl(blockedOpenUrl)"
          >
            {{ t("index.sshOpenBlockedAction") }}
          </button>
        </div>

        <!-- ── Server list ────────────────────────────────────────────────── -->
        <div class="ssh-servers">
          <div
            v-for="s in servers"
            :key="s.id"
            class="ssh-server"
            :data-testid="`ssh-server-${s.id}`"
          >
            <!-- Card header (click to collapse/expand) -->
            <button
              type="button"
              class="ssh-server-head"
              @click="toggleExpand(s)"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <rect x="2" y="3" width="20" height="15" rx="2" />
                <path d="M6 8l4 4-4 4" /><path d="M12 16h6" />
              </svg>
              <span class="ssh-server-label">
                {{ s.name || s.host || t("index.sshNewServer") }}<span v-if="!s.name && s.username"> · {{ s.username }}</span>
                <span v-if="s.saved" class="ssh-saved-dot" :title="t('index.sshSaved')">●</span>
              </span>
              <svg
                class="ssh-chevron"
                :class="{ 'ssh-chevron--open': s.expanded }"
                width="11" height="11" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" stroke-width="2.5"
                stroke-linecap="round" stroke-linejoin="round"
                aria-hidden="true"
              ><polyline points="6 9 12 15 18 9" /></svg>
              <button
                type="button"
                class="ssh-remove-btn"
                :aria-label="t('index.sshRemoveServer')"
                @click.stop="removeServer(s.id)"
              >✕</button>
            </button>

            <!-- Card body -->
            <div v-if="s.expanded" class="ssh-server-body">
              <!-- Server name -->
              <div class="ssh-field">
                <label class="ssh-label" :for="`ssh-name-${s.id}`">{{ t("index.sshServerName") }}</label>
                <input
                  :id="`ssh-name-${s.id}`"
                  v-model="s.name"
                  type="text"
                  class="config-input"
                  :placeholder="t('index.sshServerNamePlaceholder')"
                  :data-testid="`ssh-name-${s.id}`"
                />
              </div>

              <!-- Row 1: host -->
              <div class="ssh-field">
                <label class="ssh-label" :for="`ssh-host-${s.id}`">{{ t("index.sshHost") }}</label>
                <input
                  :id="`ssh-host-${s.id}`"
                  v-model="s.host"
                  type="text"
                  class="config-input"
                  placeholder="192.168.1.100"
                  :data-testid="`ssh-host-${s.id}`"
                />
              </div>

              <div class="ssh-row">
                <div class="ssh-field">
                  <label class="ssh-label" :for="`ssh-port-${s.id}`">{{ t("index.sshPort") }}</label>
                  <input
                    :id="`ssh-port-${s.id}`"
                    v-model.number="s.sshPort"
                    type="number"
                    min="1"
                    max="65535"
                    class="config-input"
                  />
                </div>
                <div class="ssh-field">
                  <label class="ssh-label" :for="`ssh-remote-port-${s.id}`">{{ t("index.sshRemotePort") }}</label>
                  <select
                    :id="`ssh-remote-port-${s.id}`"
                    v-model.number="s.remotePort"
                    class="config-input"
                    :data-testid="`ssh-remote-port-${s.id}`"
                  >
                    <option v-for="p in OKTA_REGISTERED_PORTS" :key="p" :value="p">{{ p }}</option>
                  </select>
                </div>
              </div>

              <!-- SSO gate. Only the Okta-registered loopback ports above can
                   carry a login, which is why the port is a fixed choice and
                   not a free-form number. -->
              <div class="ssh-field ssh-field--check">
                <label class="ssh-check">
                  <input
                    v-model="s.enableSso"
                    type="checkbox"
                    :data-testid="`ssh-sso-${s.id}`"
                  />
                  <span>{{ t("index.sshEnableSso") }}</span>
                </label>
                <p class="ssh-hint">{{ t("index.sshEnableSsoHint") }}</p>
              </div>

              <!-- Row 2: username / auth method -->
              <div class="ssh-row">
                <div class="ssh-field">
                  <label class="ssh-label" :for="`ssh-user-${s.id}`">{{ t("index.sshUsername") }}</label>
                  <input
                    :id="`ssh-user-${s.id}`"
                    v-model="s.username"
                    type="text"
                    class="config-input"
                    placeholder="ubuntu"
                    :data-testid="`ssh-username-${s.id}`"
                  />
                </div>
                <div class="ssh-field">
                  <label class="ssh-label" :for="`ssh-auth-${s.id}`">{{ t("index.sshAuthMethod") }}</label>
                  <select
                    :id="`ssh-auth-${s.id}`"
                    v-model="s.authMethod"
                    class="config-input"
                  >
                    <option value="password">{{ t("index.sshAuthPassword") }}</option>
                    <option value="private_key">{{ t("index.sshAuthPrivateKey") }}</option>
                  </select>
                </div>
              </div>

              <!-- Password / passphrase -->
              <div class="ssh-field">
                <label class="ssh-label" :for="`ssh-cred-${s.id}`">
                  {{ s.authMethod === "private_key" ? t("index.sshPassphrase") : t("index.sshPassword") }}
                </label>
                <input
                  :id="`ssh-cred-${s.id}`"
                  v-model="s.authRef"
                  type="password"
                  class="config-input"
                  autocomplete="current-password"
                  :data-testid="`ssh-authref-${s.id}`"
                />
              </div>

              <!-- Private key path (only when private_key) -->
              <div v-if="s.authMethod === 'private_key'" class="ssh-field">
                <label class="ssh-label" :for="`ssh-key-${s.id}`">{{ t("index.sshKeyPath") }}</label>
                <input
                  :id="`ssh-key-${s.id}`"
                  v-model="s.keyPath"
                  type="text"
                  class="config-input mono"
                  placeholder="~/.ssh/id_rsa"
                />
              </div>

              <!-- SSH tunnel information. The command is intentionally shown
                   rather than executed by the browser: browsers cannot open
                   a local SSH process. The operator runs it in PowerShell /
                   Terminal, then opens the localhost URL below. -->
              <div class="ssh-tunnel-box">
                <p class="ssh-tunnel-title">{{ t("index.sshTunnelTitle") }}</p>
                <p class="ssh-tunnel-hint">
                  {{ t("index.sshTunnelHint", { port: s.remotePort }) }}
                </p>
                <code class="ssh-tunnel-command">{{ tunnelCommand(s) }}</code>
                <button
                  type="button"
                  class="rename-dialog-btn rename-dialog-btn--cancel ssh-tunnel-open"
                  @click="openUrl(tunnelUrl(s))"
                >
                  {{ t("index.sshOpenTunnel", { port: s.remotePort }) }}
                </button>
              </div>

              <!-- Actions -->
              <div class="rename-dialog-footer ssh-actions">
                <button
                  type="button"
                  class="rename-dialog-btn rename-dialog-btn--cancel"
                  :disabled="getConnectResult(s.id).connecting || !s.host || !s.username"
                  :data-testid="`ssh-connect-${s.id}`"
                  @click="onTestConnect(s)"
                >
                  {{ getConnectResult(s.id).connecting ? t("index.sshConnecting") : t("index.sshTestConnect") }}
                </button>
                <button
                  type="button"
                  class="rename-dialog-btn rename-dialog-btn--save"
                  :disabled="!s.host || !s.username"
                  :data-testid="`ssh-save-${s.id}`"
                  @click="saveServer(s)"
                >
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" stroke-width="2.5"
                       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
                    <polyline points="17 21 17 13 7 13 7 21" />
                    <polyline points="7 3 7 8 15 8" />
                  </svg>
                  {{ s.saved ? t("index.sshSaved") : t("index.sshSave") }}
                </button>
                <button
                  type="button"
                  class="rename-dialog-btn rename-dialog-btn--confirm"
                  :disabled="getDeployState(s.id).deploying || !s.host || !s.username"
                  :data-testid="`ssh-install-${s.id}`"
                  @click="onInstall(s)"
                >
                  {{ getDeployState(s.id).phase === "install" ? t("index.sshInstalling") : t("index.sshInstall") }}
                </button>
                <button
                  type="button"
                  class="rename-dialog-btn rename-dialog-btn--confirm"
                  :disabled="getDeployState(s.id).deploying || !s.host || !s.username"
                  :data-testid="`ssh-start-${s.id}`"
                  @click="onStart(s)"
                >
                  {{ getDeployState(s.id).phase === "start" ? t("index.sshStarting") : t("index.sshStart") }}
                </button>
              </div>

              <!-- Connect feedback (per-server) -->
              <p v-if="getConnectResult(s.id).success" class="ssh-feedback ssh-feedback--ok">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
                     stroke="currentColor" stroke-width="2.5"
                     stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
                {{ t("index.sshConnectOk") }}
              </p>
              <p v-else-if="getConnectResult(s.id).error" class="ssh-feedback ssh-feedback--err">
                {{ getConnectResult(s.id).error }}
              </p>

              <!-- Deploy progress (per-server) -->
              <div v-if="s.showLog" class="ssh-log">
                <div class="ssh-log-bar">
                  <div class="ssh-log-fill" :style="{ width: getDeployState(s.id).percent + '%' }"></div>
                </div>
                <pre
                  :ref="(el) => setLogEl(s.id, el)"
                  class="ssh-log-lines"
                  :data-testid="`ssh-log-${s.id}`"
                >{{ getDeployState(s.id).log.join("\n") }}</pre>
                <p v-if="getDeployState(s.id).error" class="ssh-feedback ssh-feedback--err">{{ getDeployState(s.id).error }}</p>
              </div>
            </div>
          </div>

          <!-- Add server -->
          <button
            type="button"
            class="ssh-add-btn"
            data-testid="ssh-add-server"
            @click="addServer"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" stroke-width="2.5"
                 stroke-linecap="round" aria-hidden="true">
              <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            {{ t("index.sshAddServer") }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* ── Overlay position override (center → bottom-anchored) ────────────────── */
/* rename-dialog-overlay already provides the backdrop + blur; we only        */
/* override alignment so the panel sits above the composer bar.               */
.ssh-overlay {
  align-items: flex-end;
  padding-bottom: 72px;
}

/* ── Dialog size override ────────────────────────────────────────────────── */
.ssh-dialog {
  width: min(500px, 94vw);
  max-height: 76vh;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

/* ── Header ──────────────────────────────────────────────────────────────── */
.ssh-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: 0;           /* override rename-dialog-title margin */
}
.ssh-badge {
  background: var(--success);
  color: #fff;
  font-size: var(--text-xs);
  font-weight: 700;
  border-radius: var(--radius-full);
  padding: 1px 6px;
  min-width: 18px;
  text-align: center;
  line-height: 1.6;
}
.ssh-close-btn {
  margin-left: auto;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-muted);
  font-size: var(--text-base);
  padding: 2px var(--space-1);
  border-radius: var(--radius-xs);
  transition: background var(--transition), color var(--transition);
}
.ssh-close-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* ── Section label ───────────────────────────────────────────────────────── */
.ssh-section-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
  margin: 0 0 var(--space-2);
  text-transform: uppercase;
  letter-spacing: var(--tracking-wide);
}

/* ── Running instances ───────────────────────────────────────────────────── */
.ssh-instances {
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}
.ssh-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}
.ssh-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: 3px var(--space-2);
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  font-weight: 500;
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  color: var(--text-secondary);
}
.ssh-chip-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
}
.ssh-chip--running .ssh-chip-dot { background: var(--success); }
.ssh-chip--failed  .ssh-chip-dot { background: var(--error); }
.ssh-chip-host {
  font-family: var(--font-mono);
}
.ssh-chip-action {
  background: none;
  border: none;
  cursor: pointer;
  font-size: var(--text-xs);
  color: var(--accent);
  padding: 0 2px;
  transition: color var(--transition);
}
.ssh-chip-action:hover { color: var(--accent-hover); }
.ssh-chip-action--danger { color: var(--error); }
.ssh-chip-action--danger:hover { opacity: 0.8; }

/* ── Blocked auto-open banner ────────────────────────────────────────────── */
.ssh-blocked {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  background: var(--accent-muted);
}
.ssh-blocked-text {
  flex: 1;
  font-size: var(--text-xs);
  color: var(--text-primary);
  line-height: 1.45;
}

/* ── Server list ─────────────────────────────────────────────────────────── */
.ssh-servers {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  border-top: 1px solid var(--border);
  padding-top: var(--space-3);
}
.ssh-server {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

/* Card header */
.ssh-server-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  background: var(--bg-tertiary);
  border: none;
  cursor: pointer;
  text-align: left;
  color: var(--text-secondary);
  transition: background var(--transition);
}
.ssh-server-head:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.ssh-server-label {
  flex: 1;
  font-size: var(--text-sm);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ssh-chevron {
  transition: transform var(--duration-fast) var(--ease-out);
  flex-shrink: 0;
  color: var(--text-muted);
}
.ssh-chevron--open { transform: rotate(180deg); }
.ssh-remove-btn {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-muted);
  font-size: var(--text-xs);
  padding: 2px var(--space-1);
  border-radius: var(--radius-xs);
  flex-shrink: 0;
  transition: color var(--transition), background var(--transition);
}
.ssh-remove-btn:hover {
  color: var(--error);
  background: var(--banner-error-bg);
}

/* Card body */
.ssh-server-body {
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  background: var(--bg-secondary);
}

/* ── Form layout ─────────────────────────────────────────────────────────── */
.ssh-row {
  display: flex;
  gap: var(--space-2);
}
.ssh-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  flex: 1;
  min-width: 0;
}
.ssh-field--sm { flex: 0 0 88px; }
.ssh-field--check { margin-top: var(--space-2); }
.ssh-check {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-xs);
  color: var(--text-primary);
  font-weight: var(--weight-medium);
  cursor: pointer;
}
.ssh-check input { cursor: pointer; }
.ssh-hint {
  margin: var(--space-1) 0 0;
  font-size: var(--text-xs);
  color: var(--text-secondary);
  line-height: 1.45;
}
.ssh-label {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  font-weight: var(--weight-medium);
}

/* ── Local SSH tunnel instructions ──────────────────────────────────────── */
.ssh-tunnel-box {
  margin: var(--space-3) 0 0;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 8px);
  background: var(--bg-secondary);
}
.ssh-tunnel-title {
  margin: 0 0 var(--space-1);
  font-weight: var(--weight-semibold, 600);
}
.ssh-tunnel-hint {
  margin: 0 0 var(--space-2);
  color: var(--text-secondary);
  font-size: var(--text-sm, 13px);
}
.ssh-tunnel-command {
  display: block;
  overflow-x: auto;
  padding: var(--space-2);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: var(--text-xs, 12px);
  user-select: all;
}
.ssh-tunnel-open {
  margin-top: var(--space-2);
}

/* ── Actions row ─────────────────────────────────────────────────────────── */
.ssh-actions {
  margin-top: var(--space-1);
}

/* ── Feedback ────────────────────────────────────────────────────────────── */
.ssh-feedback {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--text-xs);
  margin: 0;
  line-height: 1.4;
}
.ssh-feedback--ok  { color: var(--success); }
.ssh-feedback--err { color: var(--error); }

/* ── Deploy log ──────────────────────────────────────────────────────────── */
.ssh-log {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.ssh-log-bar {
  height: 3px;
  border-radius: var(--radius-full);
  background: var(--border);
  overflow: hidden;
}
.ssh-log-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s var(--ease-out);
}
.ssh-log-lines {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  max-height: 220px;
  overflow-y: auto;
  background: var(--bg-code);
  color: var(--code-text);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: var(--leading-normal);
}

/* ── Save button variant ─────────────────────────────────────────────────── */
.rename-dialog-btn--save {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  border: 1px solid var(--border);
}
.rename-dialog-btn--save:hover:not(:disabled) {
  background: var(--accent-muted);
  color: var(--accent);
  border-color: var(--accent);
}
.rename-dialog-btn--save:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.ssh-saved-dot {
  font-size: 8px;
  color: var(--success);
  margin-left: var(--space-1);
  vertical-align: middle;
}

/* ── Add server button ───────────────────────────────────────────────────── */
.ssh-add-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-1);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  border: 1px dashed var(--border-light);
  border-radius: var(--radius-sm);
  background: none;
  color: var(--text-muted);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background var(--transition), color var(--transition), border-color var(--transition);
}
.ssh-add-btn:hover {
  background: var(--accent-muted);
  color: var(--accent);
  border-color: var(--accent);
}
</style>

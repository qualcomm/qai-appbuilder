<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * SecurityDialog — file-access / command-execution authorization dialog.
 *
 * V1 parity (`js/security/SecurityDialog.js`): a non-blocking toast-style
 * overlay pinned top-right, showing the head of the permission-request queue
 * with five grant tiers (deny / once / session / process / permanent), the
 * operation + path/command + caller + session fields, bulk actions when the
 * queue holds more than one request, and an "ignore" footer that lets the
 * backend time out.
 *
 * Unlike V1 (where the parent passed composable state in as props), V2 drives
 * the dialog directly from the {@link usePermissionDialog} module-level
 * singleton — App.vue's single `/api/events` SSE handler feeds the same queue
 * via `enqueue` / `fetchPending`, so no prop-drilling is needed. The dialog
 * owns its own presentation + grant wiring; the composable owns the queue +
 * API calls.
 *
 * Esc minimizes (badge to restore); Enter approves the head for the session
 * (V1 default). All confirmation is in-component — no native window.* dialogs
 * (AGENTS.md §3.9.2).
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  usePermissionDialog,
  type GrantScope,
  type GrantRange,
} from "@/composables/security/usePermissionDialog";

const { t, te } = useI18n();

const {
  currentRequest,
  queueCount,
  isVisible,
  minimized,
  pendingRequests,
  respond,
  respondAll,
  ignoreCurrent,
  minimize,
  restore,
  cancel,
  cancelAllForPid,
  cancelAll,
} = usePermissionDialog();

const op = computed(() => currentRequest.value?.op ?? "");
const rawPath = computed(() => currentRequest.value?.path ?? "");
const caller = computed(() => currentRequest.value?.caller ?? "");
const channel = computed(() => currentRequest.value?.channel ?? "");
const sessionId = computed(() => currentRequest.value?.session_id ?? "");
const isExec = computed(() => op.value === "exec");
/**
 * P-11B (2026-07-07): grant *range* selection — an orthogonal dimension to
 * the scope tiers (once/session/process/permanent). The user explicitly
 * chooses whether an approval covers only THIS file or the WHOLE parent
 * directory. Only meaningful for file-path requests; exec-command requests
 * never authorise a directory (the backend ignores range for exec), so the
 * range selector is hidden for them via `showRangeSelector`.
 *
 * Defaults to "file" (the pre-existing behaviour). Reset back to "file" each
 * time the shared dialog advances to a different queued request so a
 * "directory" choice on request N never leaks onto request N+1.
 */
const selectedRange = ref<GrantRange>("file");
/** Only file-path requests may be widened to a directory (never exec). */
const showRangeSelector = computed(() => !isExec.value);
/**
 * 2026-07-08: exec-command requests get their OWN range choice — "this
 * command only" (file, exact command string) vs "permanently allow this whole
 * program" (program → the backend stores the normalized binary token so any
 * future command with the same binary is auto-allowed). This is what lets a
 * user stop being asked for every powershell/git/... invocation. Only shown
 * for exec; file-path requests use `showRangeSelector` (file/directory).
 */
const showProgramSelector = computed(() => isExec.value);
/**
 * P-11B: purely-presentational dirname of the current path so the user can
 * see WHICH directory a "whole directory" grant would cover. The backend is
 * the source of truth (it derives + depth-checks the parent); this is a hint
 * only. Handles both `\` and `/` separators; empty when no dirname.
 */
const parentDir = computed(() => {
  const p = rawPath.value;
  if (!p) return "";
  const idx = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  return idx > 0 ? p.slice(0, idx) : "";
});
/**
 * P-EXEC (2026-07-06): the exec-broker dangerous-command ASK rationale
 * ("why does this need confirmation?"). Empty / absent for the plain
 * FileGuard path ASK → the reason banner is not rendered.
 *
 * i18n contract (A2): the backend sends a locale-free `reason_code` (+ optional
 * `reason_args`); we render the localized text from the frontend locale catalog
 * (`security.askReason.<code>`) so switching the UI language re-localizes with
 * NO backend round-trip. Fallback order: known `reason_code` → localized text;
 * unknown/absent code → the verbatim `reason` string (operator-custom rule text
 * or a legacy frame). This keeps ALL user-visible wording in the locale files.
 */
const reason = computed(() => {
  const req = currentRequest.value;
  if (!req) return "";
  const code = req.reason_code;
  if (code) {
    const key = `security.askReason.${code}`;
    if (te(key)) {
      return t(key, (req.reason_args ?? {}) as Record<string, unknown>);
    }
  }
  return req.reason ?? "";
});
/**
 * Phase 2: the current request's pid (if the backend supplied it via the
 * native-hook bridge). Used to decide whether to show the "Cancel all from
 * this process" affordance.
 */
const currentPid = computed(() => {
  const p = currentRequest.value?.pid;
  return typeof p === "number" && Number.isFinite(p) && p > 0 ? p : null;
});
/** Phase 2: is the currently-displayed request a pre-restart orphan? */
const currentIsOrphan = computed(
  () => currentRequest.value?.is_orphan === true,
);
/**
 * P-11 (2026-07-06): a native-subprocess file event always carries an empty
 * `scope_conversation_id` on the backend, so a "session"-scope grant can never
 * match it. Rather than hide the session button (which would shift layout as
 * the queue advances between native / in-process requests), we GRAY IT OUT.
 * Computed over `currentRequest` so it re-evaluates reactively each time the
 * shared dialog advances to the next queued request (native ⇒ disabled,
 * in-process ⇒ enabled). Missing / undefined field ⇒ false (enabled).
 */
const disableSession = computed(
  () => currentRequest.value?.is_native_subprocess === true,
);
/**
 * Phase 2: how many pending requests share the currently-displayed pid.
 * When > 1 we surface a compact "Cancel all N from process XXX" button (per
 * plan §2.4 — a queue-level batch action, NOT a separate process view).
 */
const sameProcessQueueCount = computed(() => {
  const pid = currentPid.value;
  if (pid === null) return 0;
  return pendingRequests.value.filter((r) => r.pid === pid).length;
});
/** Phase 2: any orphan request pending → show the batch-clear affordance. */
const hasAnyOrphan = computed(() =>
  pendingRequests.value.some((r) => r.is_orphan === true),
);

const opIcon = computed(() => {
  if (op.value === "read") return "📖";
  if (op.value === "write") return "✏️";
  if (op.value === "exec") return "⚡";
  return "🔐";
});

const opLabel = computed(() => {
  const map: Record<string, string> = {
    read: t("security.op.read"),
    write: t("security.op.write"),
    exec: t("security.op.exec"),
  };
  return map[op.value] ?? op.value ?? t("security.op.access");
});

/** Path > 80 chars → head 40 + tail 36 (full text in `title`). */
const displayPath = computed(() => {
  const p = rawPath.value;
  if (p.length <= 80) return p;
  return `${p.slice(0, 40)} … ${p.slice(-36)}`;
});

/** `1/N` queue badge when more than one request is pending. */
const queueLabel = computed(() =>
  queueCount.value > 1 ? `1 / ${queueCount.value}` : "",
);

const channelClass = computed(
  () => `qai-sec-dlg__channel qai-sec-dlg__channel--${channel.value || "web"}`,
);

/** A restore badge is shown while minimized with a non-empty queue. */
const showRestoreBadge = computed(
  () => minimized.value && pendingRequests.value.length > 0,
);

function onGrant(grant: GrantScope): void {
  const req = currentRequest.value;
  if (!req) return;
  // P-11: a "session" grant is meaningless for a native subprocess event
  // (backend scope_conversation_id is ""). The button is disabled in the
  // template, but guard here too so the keyboard/programmatic path can't
  // slip a session grant through for a native request.
  if (grant === "session" && disableSession.value) return;
  // Forward the user-selected range. Two orthogonal selectors:
  //  * file-path requests (showRangeSelector): "file" | "directory"
  //  * exec-command requests (showProgramSelector): "file" | "program"
  // Guard defensively so an exec grant never carries "directory" and a
  // file grant never carries "program", even if state somehow drifted.
  let range: GrantRange = "file";
  if (showRangeSelector.value && selectedRange.value === "directory") {
    range = "directory";
  } else if (showProgramSelector.value && selectedRange.value === "program") {
    range = "program";
  }
  void respond(req.id, grant, range);
}

function onGrantAll(grant: GrantScope): void {
  void respondAll(grant);
}

/**
 * Phase 2 (plan §2.4 / §2.6): user "cancel" — distinct from `deny`. Native
 * hook still returns False, but audit records `user_cancelled` (not
 * `user_denied`) so operators can distinguish "actively refused" from
 * "just not this one". Cancel dequeues the CURRENT request only.
 */
function onCancel(): void {
  const req = currentRequest.value;
  if (!req) return;
  void cancel(req.id);
}

/** Cancel every pending request originating from the current pid. */
function onCancelAllForPid(): void {
  const pid = currentPid.value;
  if (pid === null) return;
  void cancelAllForPid(pid);
}

/** Emergency: cancel every pending request across all processes. */
function onCancelAll(): void {
  void cancelAll();
}

// Global keyboard: Esc minimizes (regardless of focus); Enter approves the
// head for the session (skip when typing in an input/textarea). V1 parity.
function onKeydown(e: KeyboardEvent): void {
  if (!isVisible.value) return;
  if (e.key === "Escape") {
    e.preventDefault();
    minimize();
    return;
  }
  const tgt = e.target as HTMLElement | null;
  const tag = tgt?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  if (e.key === "Enter") {
    e.preventDefault();
    // P-11: the Enter shortcut maps to a session grant, which is invalid for
    // a native subprocess request — skip it (the session button is also
    // disabled). onGrant guards this too, but returning early keeps the
    // preventDefault behaviour explicit for the disabled case.
    if (disableSession.value) return;
    onGrant("session");
  }
}

// P-11B: reset the grant-range selection to "file" whenever the shared
// dialog advances to a *different* queued request (id change), so a
// "directory" choice made on one request never leaks onto the next. Keyed on
// the request id (not the object) so an in-place mutation — e.g. the orphan
// re-flag in updateBootId — does not needlessly reset the user's choice.
watch(
  () => currentRequest.value?.id ?? null,
  () => {
    selectedRange.value = "file";
  },
);

onMounted(() => {
  window.addEventListener("keydown", onKeydown);
});
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <Teleport to="body">
    <!-- Restore badge (minimized but queue non-empty) -->
    <div
      v-if="showRestoreBadge"
      class="qai-sec-restore-badge"
      data-testid="security-dialog-restore"
      :title="t('security.restoreBadge')"
      role="button"
      tabindex="0"
      @click="restore()"
      @keydown.enter.prevent="restore()"
      @keydown.space.prevent="restore()"
    >
      <span aria-hidden="true">🔒</span>
      <span v-if="queueCount > 1">{{ queueCount }}</span>
    </div>

    <div
      v-if="isVisible && currentRequest"
      class="qai-sec-dlg__overlay"
      role="dialog"
      aria-modal="false"
      :aria-label="t('security.dialogTitle')"
      data-testid="security-dialog"
    >
      <div class="qai-sec-dlg__card">
        <!-- Header -->
        <div class="qai-sec-dlg__header">
          <span class="qai-sec-dlg__icon" aria-hidden="true">{{ opIcon }}</span>
          <span class="qai-sec-dlg__title">{{ t("security.dialogTitle") }}</span>
          <span
            v-if="queueLabel"
            class="qai-sec-dlg__badge"
            :title="queueCount + ' ' + t('security.pendingRequests')"
          >{{ queueLabel }}</span>
          <!-- Phase 2: pre-restart orphan badge (plan §P3). -->
          <span
            v-if="currentIsOrphan"
            class="qai-sec-dlg__badge qai-sec-dlg__badge--orphan"
            data-testid="security-dialog-orphan-badge"
            :title="t('security.permission.orphan_hint')"
          >{{ t("security.permission.orphan_badge") }}</span>
          <span
            v-if="channel"
            :class="channelClass"
          >{{ channel }}</span>
          <button
            type="button"
            class="qai-sec-dlg__close"
            data-testid="security-dialog-minimize"
            :title="t('security.btn.minimize')"
            :aria-label="t('security.btn.minimizeLabel')"
            @click="minimize()"
          >
            &times;
          </button>
        </div>

        <!-- Body -->
        <div class="qai-sec-dlg__body">
          <div class="qai-sec-dlg__row">
            <span class="qai-sec-dlg__row-label">{{ t("security.rowLabel.op") }}</span>
            <span class="qai-sec-dlg__row-value">{{ opLabel }}（{{ op || "?" }}）</span>
          </div>
          <div class="qai-sec-dlg__row">
            <span class="qai-sec-dlg__row-label">{{
              isExec ? t("security.rowLabel.command") : t("security.rowLabel.path")
            }}</span>
            <span
              :class="[
                'qai-sec-dlg__row-value',
                isExec ? 'qai-sec-dlg__cmd' : 'qai-sec-dlg__path',
              ]"
              :title="rawPath"
            >{{ displayPath }}</span>
          </div>
          <!--
            P-EXEC (2026-07-06): dangerous-command rationale banner. Rendered
            only when the backend supplied a non-empty `reason` (exec-broker
            ASK path). Styled prominently (warning accent) so the user sees
            *why* confirmation is required before granting. Absent for the
            plain FileGuard path ASK → no banner, layout unchanged.
          -->
          <div
            v-if="reason"
            class="qai-sec-dlg__reason"
            role="note"
          >
            <span class="qai-sec-dlg__reason-icon">⚠️</span>
            <span class="qai-sec-dlg__reason-body">
              <span class="qai-sec-dlg__reason-label">{{
                t("security.rowLabel.reason")
              }}</span>
              <span class="qai-sec-dlg__reason-text">{{ reason }}</span>
            </span>
          </div>
          <div
            v-if="caller"
            class="qai-sec-dlg__row"
          >
            <span class="qai-sec-dlg__row-label">{{ t("security.rowLabel.caller") }}</span>
            <span class="qai-sec-dlg__row-value qai-sec-dlg__muted">{{ caller }}</span>
          </div>
          <div
            v-if="sessionId"
            class="qai-sec-dlg__row"
          >
            <span class="qai-sec-dlg__row-label">{{ t("security.rowLabel.session") }}</span>
            <span class="qai-sec-dlg__row-value qai-sec-dlg__muted">{{ sessionId }}</span>
          </div>
          <!--
            Phase 2 (plan §2.4): subprocess metadata rows — pid /
            process_path / command_line — so the user can see "which Agent
            wants to do what" at a glance. Only rendered when the backend
            actually populated them (native-hook bridge). Kept muted so
            they don't overwhelm the primary op/path readout.
          -->
          <div
            v-if="currentRequest?.pid"
            class="qai-sec-dlg__row"
          >
            <span class="qai-sec-dlg__row-label">{{ t("security.permission.pid_label") }}</span>
            <span class="qai-sec-dlg__row-value qai-sec-dlg__muted">{{ currentRequest.pid }}</span>
          </div>
          <div
            v-if="currentRequest?.process_path"
            class="qai-sec-dlg__row"
          >
            <span class="qai-sec-dlg__row-label">{{ t("security.permission.process_label") }}</span>
            <span
              class="qai-sec-dlg__row-value qai-sec-dlg__muted qai-sec-dlg__path"
              :title="currentRequest.process_path"
            >{{ currentRequest.process_path }}</span>
          </div>
          <div
            v-if="currentRequest?.command_line"
            class="qai-sec-dlg__row"
          >
            <span class="qai-sec-dlg__row-label">{{ t("security.permission.cmdline_label") }}</span>
            <span
              class="qai-sec-dlg__row-value qai-sec-dlg__muted qai-sec-dlg__cmd"
              :title="currentRequest.command_line"
            >{{ currentRequest.command_line }}</span>
          </div>
          <!--
            P-11B (2026-07-07): grant *range* selector — orthogonal to the
            scope tiers below. Lets the user decide whether an approval covers
            only THIS file or the WHOLE parent directory. Shown ONLY for
            file-path requests (`showRangeSelector`); exec-command requests
            never grant a directory (the backend ignores range for exec), so
            it is hidden for them and `selectedRange` stays "file". Defaults to
            "file"; the "directory" option surfaces the (presentational)
            parent dir it would cover — the backend is the real source of
            truth for the derived + depth-checked directory.
          -->
          <div
            v-if="showRangeSelector"
            class="qai-sec-dlg__range"
            role="radiogroup"
            :aria-label="t('security.grantRange.label')"
            data-testid="security-dialog-range"
          >
            <span class="qai-sec-dlg__range-label">{{ t("security.grantRange.label") }}</span>
            <div class="qai-sec-dlg__range-opts">
              <label class="qai-sec-dlg__range-opt">
                <input
                  type="radio"
                  name="qai-sec-grant-range"
                  value="file"
                  :checked="selectedRange === 'file'"
                  data-testid="security-dialog-range-file"
                  @change="selectedRange = 'file'"
                />
                <span>{{ t("security.grantRange.file") }}</span>
              </label>
              <label class="qai-sec-dlg__range-opt">
                <input
                  type="radio"
                  name="qai-sec-grant-range"
                  value="directory"
                  :checked="selectedRange === 'directory'"
                  data-testid="security-dialog-range-directory"
                  @change="selectedRange = 'directory'"
                />
                <span>{{ t("security.grantRange.directory") }}</span>
              </label>
            </div>
            <div
              v-if="selectedRange === 'directory' && parentDir"
              class="qai-sec-dlg__range-hint"
              data-testid="security-dialog-range-hint"
              :title="parentDir"
            >
              {{ t("security.grantRange.dirHint", { dir: parentDir }) }}
            </div>
          </div>
          <!--
            2026-07-08: exec-command range selector — "this command only" vs
            "permanently allow this whole program". Shown ONLY for exec
            requests (`showProgramSelector`). Choosing "program" makes the
            backend store the normalized binary token (e.g. powershell) so any
            future command with the same binary is auto-allowed (asked once per
            program, not per command). Defaults to "file" (this command only).
          -->
          <div
            v-if="showProgramSelector"
            class="qai-sec-dlg__range"
            role="radiogroup"
            :aria-label="t('security.grantRange.programLabel')"
            data-testid="security-dialog-program-range"
          >
            <span class="qai-sec-dlg__range-label">{{ t("security.grantRange.programLabel") }}</span>
            <div class="qai-sec-dlg__range-opts">
              <label class="qai-sec-dlg__range-opt">
                <input
                  type="radio"
                  name="qai-sec-grant-program"
                  value="file"
                  :checked="selectedRange !== 'program'"
                  data-testid="security-dialog-program-this"
                  @change="selectedRange = 'file'"
                />
                <span>{{ t("security.grantRange.thisCommand") }}</span>
              </label>
              <label class="qai-sec-dlg__range-opt">
                <input
                  type="radio"
                  name="qai-sec-grant-program"
                  value="program"
                  :checked="selectedRange === 'program'"
                  data-testid="security-dialog-program-whole"
                  @change="selectedRange = 'program'"
                />
                <span>{{ t("security.grantRange.program") }}</span>
              </label>
            </div>
          </div>
        </div>

        <!-- Grant actions: deny / once / session / process / permanent -->
        <div class="qai-sec-dlg__actions">
          <button
            class="qai-sec-dlg__btn qai-sec-dlg__btn--deny"
            data-testid="security-dialog-deny"
            :title="t('security.btn.denyTitle')"
            @click="onGrant('deny')"
          >
            {{ t("security.btn.deny") }}
          </button>
          <button
            class="qai-sec-dlg__btn"
            data-testid="security-dialog-once"
            :title="t('security.btn.onceTitle')"
            @click="onGrant('once')"
          >
            {{ t("security.btn.once") }}
          </button>
          <button
            class="qai-sec-dlg__btn qai-sec-dlg__btn--default"
            data-testid="security-dialog-session"
            :disabled="disableSession"
            :title="disableSession
              ? t('security.btn.sessionNativeDisabledTitle')
              : t('security.btn.sessionTitle')"
            @click="onGrant('session')"
          >
            {{ t("security.btn.session") }}
          </button>
          <button
            class="qai-sec-dlg__btn"
            data-testid="security-dialog-process"
            :title="t('security.btn.processTitle')"
            @click="onGrant('process')"
          >
            {{ t("security.btn.process") }}
          </button>
          <button
            class="qai-sec-dlg__btn qai-sec-dlg__btn--warn"
            data-testid="security-dialog-permanent"
            :title="t('security.btn.permanentTitle')"
            @click="onGrant('permanent')"
          >
            {{ t("security.btn.permanent") }}
          </button>
        </div>

        <!--
          Phase 2 (plan §2.4 / §2.6): Cancel row — a SEPARATE row from the
          grant actions so it does not visually compete with Approve/Deny.
          `cancel` is semantically distinct from `deny`: the underlying op
          still fails but audit records `user_cancelled` (see §2.6). Also
          hosts the "cancel all from this process" affordance when the queue
          has >1 items sharing the current pid (compact, no separate view).
        -->
        <div class="qai-sec-dlg__cancel-row">
          <button
            class="qai-sec-dlg__link qai-sec-dlg__link--cancel"
            data-testid="security-dialog-cancel"
            :title="t('security.permission.cancel')"
            @click="onCancel()"
          >
            {{ t("security.permission.cancel") }}
          </button>
          <button
            v-if="currentPid !== null && sameProcessQueueCount > 1"
            class="qai-sec-dlg__link qai-sec-dlg__link--cancel"
            data-testid="security-dialog-cancel-for-pid"
            :title="t('security.permission.cancel_all_for_process')"
            @click="onCancelAllForPid()"
          >
            {{
              t("security.permission.cancel_all_for_process_n", {
                n: sameProcessQueueCount,
                pid: currentPid,
              })
            }}
          </button>
        </div>

        <!-- Bulk actions (queue holds more than one) -->
        <div
          v-if="queueCount > 1"
          class="qai-sec-dlg__bulk"
        >
          <span class="qai-sec-dlg__bulk-label">{{
            t("security.bulk.label", { count: queueCount })
          }}</span>
          <button
            class="qai-sec-dlg__btn qai-sec-dlg__btn--small"
            data-testid="security-dialog-allow-all"
            :title="t('security.bulk.allowAllTitle')"
            @click="onGrantAll('session')"
          >
            {{ t("security.bulk.allowAll") }}
          </button>
          <button
            class="qai-sec-dlg__btn qai-sec-dlg__btn--small qai-sec-dlg__btn--deny"
            data-testid="security-dialog-deny-all"
            :title="t('security.bulk.denyAllTitle')"
            @click="onGrantAll('deny')"
          >
            {{ t("security.bulk.denyAll") }}
          </button>
          <!--
            Phase 2 (plan §P5): emergency "cancel every pending request"
            batch action. Distinct from "deny all" (audit reason differs).
            Also shown alone when the queue is dominated by pre-restart
            orphans and the user wants a quick clean-slate reset.
          -->
          <button
            class="qai-sec-dlg__btn qai-sec-dlg__btn--small qai-sec-dlg__btn--cancel"
            data-testid="security-dialog-cancel-all"
            :title="hasAnyOrphan
              ? t('security.permission.cancel_all_orphans_hint')
              : t('security.permission.cancel_all')"
            @click="onCancelAll()"
          >
            {{ t("security.permission.cancel_all") }}
          </button>
        </div>

        <!-- Footer: ignore + hint -->
        <div class="qai-sec-dlg__footer">
          <button
            class="qai-sec-dlg__link"
            data-testid="security-dialog-ignore"
            :title="t('security.ignoreTitle')"
            @click="ignoreCurrent()"
          >
            {{ t("security.ignore") }}
          </button>
          <span class="qai-sec-dlg__hint">{{ t("security.hint") }}</span>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* Toast-style top-right overlay (non-blocking; V1 .sec-dlg-overlay). */
.qai-sec-dlg__overlay {
  position: fixed;
  top: 16px;
  right: 16px;
  z-index: 9998;
  pointer-events: none;
}

.qai-sec-dlg__card {
  pointer-events: auto;
  width: 460px;
  max-width: calc(100vw - 32px);
  background: var(--bg-elevated, var(--bg-secondary));
  color: var(--text, var(--text-primary));
  border: 1px solid var(--border);
  border-left: 4px solid var(--warning, #f59e0b);
  border-radius: 10px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
  font-size: var(--text-base, var(--text-sm));
  line-height: 1.5;
  animation: qai-sec-dlg-pop 160ms ease-out;
}

@keyframes qai-sec-dlg-pop {
  from {
    opacity: 0;
    transform: translateY(-8px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.qai-sec-dlg__header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
}

.qai-sec-dlg__icon {
  font-size: var(--text-lg);
}

.qai-sec-dlg__title {
  flex: 1;
}

.qai-sec-dlg__badge {
  font-size: var(--text-xs);
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(245, 158, 11, 0.18);
  color: var(--warning, #f59e0b);
  border: 1px solid rgba(245, 158, 11, 0.4);
}

.qai-sec-dlg__channel {
  font-size: var(--text-xs);
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(126, 184, 247, 0.15);
  color: #7eb8f7;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.qai-sec-dlg__channel--wechat {
  background: rgba(34, 197, 94, 0.15);
  color: #4ade80;
}

.qai-sec-dlg__channel--feishu {
  background: rgba(168, 85, 247, 0.15);
  color: #c084fc;
}

.qai-sec-dlg__close {
  background: transparent;
  border: 0;
  color: var(--text-muted);
  font-size: var(--text-lg);
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}

.qai-sec-dlg__close:hover {
  color: var(--text, var(--text-primary));
}

.qai-sec-dlg__body {
  padding: 10px 12px;
}

.qai-sec-dlg__row {
  display: flex;
  gap: var(--space-2);
  margin-bottom: 6px;
  align-items: baseline;
}

.qai-sec-dlg__row-label {
  flex-shrink: 0;
  width: 56px;
  color: var(--text-muted);
  font-size: var(--text-sm);
}

.qai-sec-dlg__row-value {
  flex: 1;
  word-break: break-all;
}

.qai-sec-dlg__path {
  font-family: var(--font-mono, monospace);
  font-size: var(--text-sm);
  background: rgba(0, 0, 0, 0.25);
  padding: 2px 6px;
  border-radius: 4px;
}

.qai-sec-dlg__cmd {
  font-family: var(--font-mono, monospace);
  font-size: var(--text-sm);
  color: #fbbf24;
  background: rgba(0, 0, 0, 0.35);
  padding: 4px 8px;
  border-radius: 4px;
  white-space: pre-wrap;
}

.qai-sec-dlg__muted {
  color: var(--text-muted);
  font-size: var(--text-sm);
}

/*
 * P-EXEC (2026-07-06): dangerous-command rationale banner. Amber warning
 * accent (matches the exec `__cmd` #fbbf24 highlight) with a left border so
 * it reads as "heads up, here's why we're asking" distinct from the muted
 * metadata rows. Only present when `reason` is non-empty.
 */
.qai-sec-dlg__reason {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  border-left: 3px solid #fbbf24;
  background: rgba(251, 191, 36, 0.12);
}

.qai-sec-dlg__reason-icon {
  flex-shrink: 0;
  line-height: 1.3;
}

.qai-sec-dlg__reason-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.qai-sec-dlg__reason-label {
  font-size: var(--text-sm);
  font-weight: 600;
  color: #fbbf24;
}

.qai-sec-dlg__reason-text {
  font-size: var(--text-sm);
  color: var(--text, var(--text-primary));
  word-break: break-word;
}

.qai-sec-dlg__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 12px;
  border-top: 1px solid var(--border);
}

/*
 * P-11B (2026-07-07): grant-range selector — sits at the bottom of the body,
 * directly above the scope buttons. Muted, compact radio group so it reads as
 * a secondary refinement of the primary approve/deny decision, not a competing
 * action. Hidden entirely for exec requests (see `showRangeSelector`).
 */
.qai-sec-dlg__range {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--border);
}

.qai-sec-dlg__range-label {
  font-size: var(--text-sm);
  color: var(--text-muted);
  font-weight: 600;
}

.qai-sec-dlg__range-opts {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.qai-sec-dlg__range-opt {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: var(--text-sm);
  cursor: pointer;
}

.qai-sec-dlg__range-opt input {
  cursor: pointer;
}

.qai-sec-dlg__range-hint {
  flex-basis: 100%;
  font-family: var(--font-mono, monospace);
  font-size: var(--text-xs);
  color: var(--text-muted);
  word-break: break-all;
}

.qai-sec-dlg__btn {
  flex: 1;
  min-width: 64px;
  padding: 6px 10px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg, var(--bg-secondary));
  color: var(--text, var(--text-primary));
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    background 120ms,
    border-color 120ms;
}

.qai-sec-dlg__btn:hover {
  background: rgba(255, 255, 255, 0.1);
  border-color: rgba(255, 255, 255, 0.25);
}

/*
 * P-11: disabled grant button (currently only the session button, when the
 * request is a native subprocess file event — see `disableSession`). Grayed
 * out + non-interactive so the layout stays fixed while the shared dialog
 * advances between native / in-process requests. `:disabled` also blocks the
 * hover state and native click, so the visual + interaction are consistent.
 */
.qai-sec-dlg__btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.qai-sec-dlg__btn:disabled:hover {
  background: var(--bg, var(--bg-secondary));
  border-color: var(--border);
}

.qai-sec-dlg__btn--default {
  background: rgba(126, 184, 247, 0.18);
  border-color: rgba(126, 184, 247, 0.55);
  color: #7eb8f7;
  font-weight: 600;
}

.qai-sec-dlg__btn--default:hover {
  background: rgba(126, 184, 247, 0.28);
}

.qai-sec-dlg__btn--deny {
  background: rgba(248, 113, 113, 0.12);
  border-color: rgba(248, 113, 113, 0.45);
  color: #f87171;
}

.qai-sec-dlg__btn--deny:hover {
  background: rgba(248, 113, 113, 0.22);
}

.qai-sec-dlg__btn--warn {
  background: rgba(245, 158, 11, 0.12);
  border-color: rgba(245, 158, 11, 0.45);
  color: var(--warning, #f59e0b);
}

.qai-sec-dlg__btn--warn:hover {
  background: rgba(245, 158, 11, 0.22);
}

/*
 * Phase 2: cancel button styling — subordinate to Approve/Deny (neither
 * primary green nor destructive red). Uses the muted "advisory" palette
 * so it reads as "close this without deciding".
 */
.qai-sec-dlg__btn--cancel {
  background: rgba(148, 163, 184, 0.12);
  border-color: rgba(148, 163, 184, 0.45);
  color: #94a3b8;
}

.qai-sec-dlg__btn--cancel:hover {
  background: rgba(148, 163, 184, 0.22);
}

.qai-sec-dlg__cancel-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 4px 12px 6px;
  font-size: var(--text-xs);
}

.qai-sec-dlg__link--cancel {
  color: #94a3b8;
}

.qai-sec-dlg__link--cancel:hover {
  color: var(--text, var(--text-primary));
}

/* Phase 2: pre-restart orphan badge — distinct hue from the queue badge. */
.qai-sec-dlg__badge--orphan {
  background: rgba(148, 163, 184, 0.18);
  color: #94a3b8;
  border-color: rgba(148, 163, 184, 0.45);
}

.qai-sec-dlg__btn--small {
  flex: 0 0 auto;
  padding: 4px 10px;
  font-size: var(--text-xs);
}

.qai-sec-dlg__bulk {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 6px 12px;
  background: rgba(255, 255, 255, 0.02);
  border-top: 1px solid var(--border);
}

.qai-sec-dlg__bulk-label {
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.qai-sec-dlg__footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 12px 10px;
  font-size: var(--text-xs);
  color: var(--text-muted);
}

.qai-sec-dlg__link {
  background: transparent;
  border: 0;
  color: var(--text-muted);
  font-size: var(--text-xs);
  cursor: pointer;
  text-decoration: underline;
  padding: 0;
}

.qai-sec-dlg__link:hover {
  color: var(--text, var(--text-primary));
}

.qai-sec-dlg__hint {
  font-size: var(--text-xs);
  opacity: 0.7;
}

.qai-sec-restore-badge {
  position: fixed;
  top: 16px;
  right: 16px;
  z-index: 9997;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  background: var(--bg-elevated, var(--bg-secondary));
  color: var(--warning, #f59e0b);
  border: 1px solid rgba(245, 158, 11, 0.55);
  border-left: 4px solid var(--warning, #f59e0b);
  border-radius: 8px;
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
  animation: qai-sec-dlg-pop 160ms ease-out;
  user-select: none;
}

.qai-sec-restore-badge:hover {
  background: rgba(245, 158, 11, 0.12);
  border-color: rgba(245, 158, 11, 0.8);
}
</style>

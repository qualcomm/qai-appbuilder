<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<!--
  BuilderEnvCard — install the model-conversion environment from the Downloads page.

  The environment is the x64 Python 3.10 converter venv + the QAIRT SDK (several GB).
  It is NOT installed with the app: the user starts it here, the backend downloads it in
  a child process, and this card only shows progress — nothing else in the app waits.

  States (see useBuilderEnv.phase):
    ready        green line + where the SDK / venv live
    missing      warning + size hint + mirror / proxy options + [Install]
    installing   progress bar, step name, size / speed / ETA, [Cancel]
    failed       error + reason + [Retry]            (cancelled: same, softer wording)
    unavailable  info: this build cannot install it  (no button)
  Hidden while the state is unknown, or when the server has no such feature.
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";

import type { BuilderEnvMirror } from "@/api/builderEnv";
import { formatBytes, formatEta, formatSpeed } from "@/composables/downloads/format";
import { useBuilderEnv } from "@/composables/useBuilderEnv";

const { t, te } = useI18n();
const env = useBuilderEnv();

onMounted(env.acquire);
onBeforeUnmount(env.release);

const mirror = ref<BuilderEnvMirror>("auto");
const proxy = ref("");
const showOptions = ref(false);
const showLog = ref(false);

const status = env.status;
const job = computed(() => status.value?.job ?? null);

const tone = computed(() => {
  switch (env.phase.value) {
    case "ready":
      return "success";
    case "installing":
      return "info";
    case "failed":
      return "error";
    case "cancelled":
    case "missing":
      return "warning";
    default:
      return "info";
  }
});

/** "Step 3 of 7: Python packages" style label for the step in flight. */
const stepLabel = computed(() => {
  const step = job.value?.step;
  if (!step) return "";
  const key = `builderEnv.step.${step}`;
  return te(key) ? t(key) : step;
});

/** Detail line under the bar: size / speed / ETA for byte steps, percent for unpack. */
const detail = computed(() => {
  const j = job.value;
  if (!j || j.state !== "running") return "";
  const parts: string[] = [];
  if (j.unit === "bytes" && j.total) {
    parts.push(`${formatBytes(j.done)} / ${formatBytes(j.total)}`);
  } else if (j.unit === "bytes" && j.done > 0) {
    parts.push(formatBytes(j.done));
  } else if (j.unit === "percent") {
    parts.push(`${Math.round(j.done)}%`);
  } else if (j.unit === "phase" && j.total) {
    parts.push(`${j.done}/${j.total}`);
  }
  const speed = formatSpeed(j.speed);
  if (speed) parts.push(speed);
  const eta = formatEta(j.eta);
  if (eta) parts.push(`${t("builderEnv.eta")} ${eta}`);
  return parts.join(" · ");
});

/** Human text for a problem / failure code; falls back to the raw message. */
const failureText = computed(() => {
  const j = job.value;
  if (!j) return "";
  const key = `builderEnv.error.${j.error_code}`;
  const known = te(key) ? t(key) : "";
  return known || j.error_message || j.error_code;
});

const missingText = computed(() => {
  const m = status.value?.missing ?? [];
  if (m.length === 0) return "";
  return m
    .map((k) => (te(`builderEnv.part.${k}`) ? t(`builderEnv.part.${k}`) : k))
    .join(" / ");
});

const proxyInvalid = computed(() => {
  const p = proxy.value.trim();
  return p !== "" && !/^(https?|socks5h?):\/\//i.test(p);
});

async function onInstall(): Promise<void> {
  if (proxyInvalid.value) return;
  await env.install({ mirror: mirror.value, proxy: proxy.value.trim() });
}

const visible = computed(() => env.phase.value !== "unknown");

/**
 * QAIRT version shown next to the title: the installed one once ready, otherwise the one an
 * install would fetch (they differ only while the config is stale).
 */
const sdkVersion = computed(() => {
  const s = status.value;
  if (!s) return "";
  if (env.phase.value === "ready") return s.installed_sdk_version || s.sdk_version || "";
  return s.sdk_version || "";
});
</script>

<template>
  <section
    v-if="visible"
    class="dc-info-banner builder-env-card"
    :class="tone"
    data-testid="builder-env-card"
    :data-phase="env.phase.value"
    role="status"
    aria-live="polite"
  >
    <div class="builder-env-card__body">
      <h2 class="builder-env-card__title">
        {{ t("builderEnv.title") }}
        <span
          v-if="sdkVersion"
          class="builder-env-card__version"
          data-testid="builder-env-version"
        >QAIRT SDK {{ sdkVersion }}</span>
      </h2>

      <!-- ready -->
      <template v-if="env.phase.value === 'ready'">
        <p class="builder-env-card__text">
          {{ t("builderEnv.ready") }}
        </p>
        <p
          v-if="status?.sdk_root"
          class="builder-env-card__path"
        >
          SDK: {{ status.sdk_root }}
        </p>
      </template>

      <!-- missing / unavailable -->
      <template v-else-if="env.phase.value === 'missing' || env.phase.value === 'unavailable'">
        <p class="builder-env-card__text">
          {{ t("builderEnv.missing") }}
        </p>
        <p
          v-if="status?.sdk_version && status?.installed_sdk_version && status.installed_sdk_version !== status.sdk_version"
          class="builder-env-card__hint"
          data-testid="builder-env-version-mismatch"
        >
          {{ t("builderEnv.versionMismatch", { installed: status.installed_sdk_version, expected: status.sdk_version }) }}
        </p>
        <p
          v-if="missingText"
          class="builder-env-card__path"
          data-testid="builder-env-missing"
        >
          {{ t("builderEnv.missingParts") }}: {{ missingText }}
        </p>
        <p class="builder-env-card__hint">
          {{ t("builderEnv.sizeHint") }}
        </p>
        <p
          v-if="env.phase.value === 'unavailable'"
          class="builder-env-card__hint"
        >
          {{ t("builderEnv.unavailable") }}
        </p>
      </template>

      <!-- installing -->
      <template v-else-if="env.phase.value === 'installing'">
        <p class="builder-env-card__text">
          {{ t("builderEnv.installing") }}<span v-if="stepLabel"> — {{ stepLabel }}</span>
        </p>
        <div
          class="builder-env-card__bar"
          role="progressbar"
          :aria-valuenow="env.percent.value"
          aria-valuemin="0"
          aria-valuemax="100"
          data-testid="builder-env-progress"
        >
          <div
            class="progress-bar"
            :style="{ width: env.percent.value + '%' }"
          />
        </div>
        <p class="builder-env-card__hint">
          {{ env.percent.value }}%<span v-if="detail"> · {{ detail }}</span>
        </p>
        <p class="builder-env-card__hint">
          {{ t("builderEnv.keepUsing") }}
        </p>
      </template>

      <!-- failed / cancelled -->
      <template v-else-if="env.phase.value === 'failed' || env.phase.value === 'cancelled'">
        <p
          v-if="env.phase.value === 'failed'"
          class="builder-env-card__text"
          data-testid="builder-env-error"
        >
          {{ t("builderEnv.failed") }}: {{ failureText }}
        </p>
        <p
          v-else
          class="builder-env-card__text"
        >
          {{ t("builderEnv.cancelled") }}
        </p>
        <p
          v-if="job?.failed_step && env.phase.value === 'failed'"
          class="builder-env-card__hint"
        >
          {{ t("builderEnv.failedAt") }}: {{ te(`builderEnv.step.${job.failed_step}`) ? t(`builderEnv.step.${job.failed_step}`) : job.failed_step }}
        </p>
        <p class="builder-env-card__hint">
          {{ t("builderEnv.resumeHint") }}
        </p>
      </template>

      <p
        v-if="env.actionError.value"
        class="builder-env-card__text builder-env-card__action-error"
        data-testid="builder-env-action-error"
      >
        {{ env.actionError.value }}
      </p>

      <!-- options (only before / between installs) -->
      <div
        v-if="['missing', 'failed', 'cancelled'].includes(env.phase.value) && status?.installable"
        class="builder-env-card__options"
      >
        <button
          type="button"
          class="builder-env-card__link"
          data-testid="builder-env-options-toggle"
          @click="showOptions = !showOptions"
        >
          {{ showOptions ? "▾" : "▸" }} {{ t("builderEnv.options") }}
        </button>
        <div
          v-if="showOptions"
          class="builder-env-card__fields"
        >
          <label class="builder-env-card__field">
            <span>{{ t("builderEnv.mirror") }}</span>
            <select
              v-model="mirror"
              data-testid="builder-env-mirror"
            >
              <option value="auto">{{ t("builderEnv.mirrorAuto") }}</option>
              <option value="official">{{ t("builderEnv.mirrorOfficial") }}</option>
              <option value="cn">{{ t("builderEnv.mirrorCn") }}</option>
            </select>
          </label>
          <label class="builder-env-card__field">
            <span>{{ t("builderEnv.proxy") }}</span>
            <input
              v-model="proxy"
              type="text"
              placeholder="http://127.0.0.1:7890"
              autocomplete="off"
              spellcheck="false"
              data-testid="builder-env-proxy"
            />
          </label>
          <p
            v-if="proxyInvalid"
            class="builder-env-card__hint builder-env-card__invalid"
            data-testid="builder-env-proxy-invalid"
          >
            {{ t("builderEnv.proxyInvalid") }}
          </p>
        </div>
      </div>

      <div
        v-if="job && job.log_tail.length && env.phase.value !== 'ready'"
        class="builder-env-card__logwrap"
      >
        <button
          type="button"
          class="builder-env-card__link"
          data-testid="builder-env-log-toggle"
          @click="showLog = !showLog"
        >
          {{ showLog ? "▾" : "▸" }} {{ t("builderEnv.log") }}
        </button>
        <pre
          v-if="showLog"
          class="builder-env-card__log"
          data-testid="builder-env-log"
        >{{ job.log_tail.join("\n") }}</pre>
      </div>
    </div>

    <div class="builder-env-card__actions">
      <button
        v-if="env.phase.value === 'installing'"
        type="button"
        class="btn btn-ghost btn-sm"
        :disabled="env.busy.value"
        data-testid="builder-env-cancel"
        @click="env.cancel()"
      >
        {{ t("builderEnv.cancel") }}
      </button>
      <button
        v-else-if="['missing', 'failed', 'cancelled'].includes(env.phase.value) && status?.installable"
        type="button"
        class="btn btn-primary btn-sm"
        :disabled="env.busy.value || proxyInvalid"
        data-testid="builder-env-install"
        @click="onInstall"
      >
        {{ env.phase.value === 'missing' ? t("builderEnv.install") : t("builderEnv.retry") }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.builder-env-card {
  align-items: flex-start;
}

.builder-env-card__body {
  flex: 1;
  min-width: 0;
}

.builder-env-card__title {
  margin: 0 0 var(--space-1);
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--text-primary);
}

.builder-env-card__version {
  display: inline-block;
  margin-left: var(--space-2);
  padding: 0 var(--space-2);
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: var(--text-xs);
  font-weight: 500;
  line-height: 1.6;
  color: var(--text-secondary);
  vertical-align: middle;
  white-space: nowrap;
}

.builder-env-card__text {
  margin: 0 0 var(--space-1);
  line-height: 1.5;
}

.builder-env-card__hint,
.builder-env-card__path {
  margin: 0 0 var(--space-1);
  font-size: var(--text-xs);
  opacity: 0.85;
  overflow-wrap: anywhere;
}

.builder-env-card__invalid,
.builder-env-card__action-error {
  color: var(--error);
}

.builder-env-card__bar {
  height: 6px;
  margin: var(--space-2) 0 var(--space-1);
  background: var(--bg-tertiary);
  border-radius: 3px;
  overflow: hidden;
}

.builder-env-card__actions {
  display: flex;
  flex-shrink: 0;
  gap: var(--space-2);
}

.builder-env-card__options,
.builder-env-card__logwrap {
  margin-top: var(--space-2);
}

.builder-env-card__link {
  padding: 0;
  background: none;
  border: none;
  color: inherit;
  font-size: var(--text-xs);
  text-decoration: underline;
  cursor: pointer;
}

.builder-env-card__fields {
  display: grid;
  gap: var(--space-2);
  margin-top: var(--space-2);
  max-width: 420px;
}

.builder-env-card__field {
  display: grid;
  gap: 2px;
  font-size: var(--text-xs);
}

.builder-env-card__field input,
.builder-env-card__field select {
  padding: var(--space-1) var(--space-2);
  background: var(--bg-secondary);
  border: 1px solid var(--border-subtle, rgb(255 255 255 / 16%));
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: var(--text-sm);
}

.builder-env-card__log {
  max-height: 160px;
  margin: var(--space-1) 0 0;
  padding: var(--space-2);
  overflow: auto;
  background: var(--bg-tertiary);
  border-radius: var(--radius-sm);
  font-size: var(--text-xs);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>

<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * QaiServiceGatewayCard — opt-in QAI Service (enterprise gateway) connection.
 *
 * QAI Service is DISABLED by default: no address, no models. It is switched on
 * only by the user typing the gateway address and confirming the dialog that
 * lists what will happen. There is deliberately NO checkbox / toggle — the
 * state is derived from the backend (an address is stored or it is not).
 *
 * Connect flow (the JWT can only be minted inside the Okta `/callback`):
 *   POST /api/qai-service/connect  → store the address (no network I/O)
 *   silentLogin({reexchange:true}) → re-run SSO so `/callback` mints the JWT
 *                                    (popup fallback inside a click gesture)
 *   POST /api/qai-service/sync     → pull the model roster
 * Disconnect: DELETE /api/qai-service (address, models and JWT are forgotten).
 */
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { popupLogin, silentLogin } from "@/api/auth";
import { useAuthStore } from "@/stores/auth";
import { useConfirm } from "@/composables/useConfirm";
import { useToast } from "@/composables/useToast";

interface GatewayStatus {
  configured: boolean;
  base_url: string;
  has_token: boolean;
  model_count: number;
}
interface SyncResult extends GatewayStatus {
  ok: boolean;
  skipped_reason: string | null;
}

const emit = defineEmits<{ (e: "changed"): void }>();

const { t } = useI18n();
const toast = useToast();
const { confirm } = useConfirm();
const auth = useAuthStore();

type Phase = "idle" | "saving" | "login" | "syncing";

const status = ref<GatewayStatus>({
  configured: false,
  base_url: "",
  has_token: false,
  model_count: 0,
});
const urlInput = ref("");
const phase = ref<Phase>("idle");
const errorKey = ref<string | null>(null);

const busy = computed(() => phase.value !== "idle");
const trimmed = computed(() => urlInput.value.trim());
const urlValid = computed(() => /^https?:\/\/[^\s/?#]+/i.test(trimmed.value));
/** Empty input while an address is stored means "disconnect". */
const isDisconnect = computed(() => trimmed.value === "" && status.value.configured);
const canSubmit = computed(
  () => !busy.value && (isDisconnect.value || urlValid.value),
);
const stateLabel = computed(() => {
  if (phase.value !== "idle") return t(`cloudModels.gateway.phase.${phase.value}`);
  if (!status.value.configured) return t("cloudModels.gateway.stateOff");
  if (!status.value.has_token) return t("cloudModels.gateway.stateNeedLogin");
  return t("cloudModels.gateway.stateOn", { n: status.value.model_count });
});

async function refreshStatus(): Promise<void> {
  try {
    status.value = await apiJson<GatewayStatus>("GET", "/api/qai-service");
    urlInput.value = status.value.base_url;
  } catch {
    /* status is cosmetic; leave the previous value */
  }
}

async function acquireTokenAndSync(): Promise<void> {
  phase.value = "login";
  auth.beginLogin();
  try {
    try {
      await silentLogin({ reexchange: true, timeoutMs: 15000 });
    } catch {
      // IdP session gone / iframe blocked: ask visibly (we are in a click).
      await popupLogin({ reexchange: true });
    }
  } finally {
    auth.endLogin();
  }
  phase.value = "syncing";
  const res = await apiJson<SyncResult>("POST", "/api/qai-service/sync");
  status.value = res;
  if (!res.has_token) {
    errorKey.value = "cloudModels.gateway.errNoToken";
  } else if (!res.ok || res.skipped_reason === "probe_failed") {
    errorKey.value = "cloudModels.gateway.errSyncFailed";
  } else {
    toast.success(t("cloudModels.gateway.connected", { n: res.model_count }));
  }
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
  errorKey.value = null;

  if (isDisconnect.value) {
    const ok = await confirm({
      title: t("cloudModels.gateway.disconnectTitle"),
      message: t("cloudModels.gateway.disconnectMsg"),
      confirmText: t("cloudModels.gateway.disconnectBtn"),
      confirmStyle: "danger",
    });
    if (!ok) return;
    phase.value = "saving";
    try {
      status.value = await apiJson<GatewayStatus>("DELETE", "/api/qai-service");
      urlInput.value = "";
      emit("changed");
    } catch {
      errorKey.value = "cloudModels.gateway.errSave";
    } finally {
      phase.value = "idle";
    }
    return;
  }

  const ok = await confirm({
    title: t("cloudModels.gateway.confirmTitle"),
    message: t("cloudModels.gateway.confirmMsg", { url: trimmed.value }),
    confirmText: t("cloudModels.gateway.confirmBtn"),
  });
  if (!ok) return;

  try {
    phase.value = "saving";
    status.value = await apiJson<GatewayStatus>(
      "POST",
      "/api/qai-service/connect",
      { url: trimmed.value },
    );
    urlInput.value = status.value.base_url;
  } catch {
    errorKey.value = "cloudModels.gateway.errSave";
    phase.value = "idle";
    return;
  }
  try {
    await acquireTokenAndSync();
  } catch {
    // Popup blocked / closed, or sync request failed. The address stays saved;
    // pressing the button again retries the sign-in + sync.
    errorKey.value = "cloudModels.gateway.errLogin";
  } finally {
    phase.value = "idle";
    emit("changed");
  }
}

onMounted(() => {
  void refreshStatus();
});
</script>

<template>
  <details
    class="gateway-card"
    :open="status.configured"
  >
    <summary class="gateway-summary">
      <span class="gateway-title">🏢 {{ t("cloudModels.gateway.title") }}</span>
      <span
        class="gateway-state"
        :class="{ 'gateway-state-on': status.configured && status.has_token }"
      >{{ stateLabel }}</span>
    </summary>

    <div class="gateway-body">
      <p class="gateway-desc">
        {{ t("cloudModels.gateway.desc") }}
      </p>
      <div class="gateway-row">
        <input
          v-model="urlInput"
          class="gateway-input"
          type="url"
          spellcheck="false"
          :disabled="busy"
          :placeholder="t('cloudModels.gateway.urlPlaceholder')"
          @keydown.enter.prevent="submit"
        />
        <button
          type="button"
          class="btn btn-primary btn-sm"
          :class="{ 'btn-danger': isDisconnect }"
          :disabled="!canSubmit"
          @click="submit"
        >
          {{
            isDisconnect
              ? t("cloudModels.gateway.disconnectBtn")
              : status.configured && !status.has_token
                ? t("cloudModels.gateway.retryBtn")
                : t("cloudModels.gateway.saveBtn")
          }}
        </button>
      </div>
      <p
        v-if="trimmed !== '' && !urlValid"
        class="gateway-hint gateway-hint-warn"
      >
        {{ t("cloudModels.gateway.urlInvalid") }}
      </p>
      <p class="gateway-hint">
        {{ t("cloudModels.gateway.emptyHint") }}
      </p>
      <p
        v-if="errorKey !== null"
        class="gateway-hint gateway-hint-warn"
      >
        {{ t(errorKey) }}
      </p>
    </div>
  </details>
</template>

<style scoped>
.gateway-card {
  border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
  border-radius: var(--radius-md, 8px);
  padding: var(--space-2, 8px) var(--space-3, 12px);
  margin-bottom: var(--space-3, 12px);
}
.gateway-summary {
  display: flex;
  align-items: center;
  gap: var(--space-3, 12px);
  cursor: pointer;
  user-select: none;
}
.gateway-title {
  font-weight: 600;
  color: var(--text-primary);
}
.gateway-state {
  font-size: var(--text-xs, 0.75rem);
  color: var(--text-secondary);
}
.gateway-state-on {
  color: var(--success, #3fb950);
}
.gateway-body {
  margin-top: var(--space-2, 8px);
}
.gateway-desc,
.gateway-hint {
  margin: var(--space-1, 4px) 0;
  font-size: var(--text-sm, 0.85rem);
  color: var(--text-secondary);
}
.gateway-hint-warn {
  color: var(--warning, #d29922);
}
.gateway-row {
  display: flex;
  gap: var(--space-2, 8px);
  align-items: center;
}
.gateway-input {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
}
</style>

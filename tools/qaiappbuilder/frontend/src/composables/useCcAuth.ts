// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useCcAuth — V1-parity Claude Code authentication scheme + credential
 * management for the Claude Code config panel.
 *
 * Extracted from `ClaudeCodeConfigPanel.vue` (cohesion split). Owns:
 *
 *   • The `AUTH_SCHEMES` table (5 schemes: Enterprise / Anthropic /
 *     Bedrock / Vertex / Azure Foundry) — types `AuthVar` / `AuthScheme`
 *     are exported for the consumer's template helpers.
 *   • Per-scheme localStorage persistence + collapsed/expanded list view.
 *   • Unified `varInputs` (secret + non-secret), `credStatus`, and the
 *     two predicates `credConfigured` / `credEnvOnly`.
 *   • `loadCredentials` / `saveCredentials` / `removeCredential` —
 *     non-secret values merge into `cfg.auth_env`; secret values go
 *     through the SecretStore credential endpoints. The host injects
 *     `cfg` (reactive) and `onAfterMutation` (typically `checkHealth`).
 *   • `hydrateAuthEnvDefaults()` — pure helper the host's `loadConfig`
 *     calls after backend config has merged into `cfg`, populating
 *     `varInputs` with current `cfg.auth_env` values + per-scheme
 *     non-secret defaults (V1 parity).
 *
 * No watch, no lifecycle hooks: the host keeps `onMounted(() =>
 * loadConfig().then(loadCredentials))` so timing is byte-for-byte
 * identical to the inline implementation.
 */
import { computed, reactive, ref, type ComputedRef, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  updateCcConfig,
  fetchCcCredentials,
  saveCcCredentials,
  deleteCcCredential,
} from "@/api";
import type { CredentialStatusEnvelope as CredentialStatus } from "@/types/aiCoding";

/** Single env-var slot inside an auth scheme (V1 parity). */
export interface AuthVar {
  name: string;
  envKey?: string;
  label: string;
  secret: boolean;
  placeholder?: string;
  defaultValue?: string;
  /** V1 parity: required/optional badge. Defaults to `secret` when absent. */
  required?: boolean;
  /** V1 parity: per-variable inline help text (i18n string). */
  hint?: string;
}

/** A named bundle of env-vars users pick between (Enterprise / Anthropic / …). */
export interface AuthScheme {
  id: string;
  label: string;
  desc: string;
  vars: AuthVar[];
}

const AUTH_SCHEME_KEY = "qai-cc-auth-scheme";

// V1 parity (ClaudeCodeConfigPanel.js:419-469): scheme list defaults to
// collapsed; show only the first N (=1) + the currently selected one,
// with a "Show more (N)" / "Show less" toggle. Mirrors the model-list
// collapse on the same panel.
const AUTH_SCHEMES_COLLAPSED_LIMIT = 1;

/**
 * Minimal slice of the host's `CcConfig` we read/write.
 * (Kept intentionally narrow so the composable can be tested with a
 *  toy reactive object instead of the full backend DTO.)
 */
export interface CcAuthConfigShape {
  auth_env: Record<string, string>;
}

export interface UseCcAuthOptions<T extends CcAuthConfigShape> {
  /** Reactive config object — composable reads `cfg.auth_env` and writes back. */
  cfg: T;
  /** Toast bus. The host already wraps `useToastStore()` into a typed pusher. */
  pushToast: (kind: "success" | "error" | "info", message: string) => void;
  /**
   * Side-effect run after a successful mutation (typically the panel's
   * `checkHealth` so the status badges refresh). Optional so tests can
   * pass a no-op.
   */
  onAfterMutation?: () => Promise<void>;
}

export interface UseCcAuthReturn {
  AUTH_SCHEMES: AuthScheme[];
  AUTH_SCHEMES_COLLAPSED_LIMIT: number;
  authScheme: Ref<string>;
  selectScheme: (id: string) => void;
  currentScheme: ComputedRef<AuthScheme>;
  authSchemesExpanded: Ref<boolean>;
  displayedAuthSchemes: ComputedRef<AuthScheme[]>;
  authSchemesHiddenCount: ComputedRef<number>;
  varInputs: Record<string, string>;
  credStatus: Record<string, CredentialStatus>;
  credConfigured: (name: string) => boolean;
  credEnvOnly: (name: string) => boolean;
  credSaving: Ref<boolean>;
  loadCredentials: () => Promise<void>;
  saveCredentials: () => Promise<void>;
  removeCredential: (name: string) => Promise<void>;
  hydrateAuthEnvDefaults: () => void;
}

export function useCcAuth<T extends CcAuthConfigShape>(
  opts: UseCcAuthOptions<T>,
): UseCcAuthReturn {
  const { t } = useI18n();
  const { cfg, pushToast } = opts;
  const onAfterMutation =
    opts.onAfterMutation ?? (async () => {
      /* no-op */
    });

  // ─── Authentication schemes (V1 parity) ─────────────────────────────────
  // secret=true → SecretStore via credentials endpoint; secret=false → auth_env.
  // `envKey` lets a logical input map to a shared real env var (Base URL).
  const AUTH_SCHEMES: AuthScheme[] = [
    {
      id: "enterprise",
      label: t("aiCoding.config.schemeEnterprise", "Cloud LLM Service"),
      desc: t("aiCoding.config.schemeEnterpriseDesc", "Self-hosted / proxy gateway with an auth token."),
      vars: [
        { name: "ANTHROPIC_AUTH_TOKEN", label: "Auth Token", secret: true, required: true, placeholder: "YOUR_API_KEY", hint: t("claudeCode.config.hintAuthToken", "Enterprise API Token, equivalent to ANTHROPIC_API_KEY") },
        { name: "_enterprise_base_url", envKey: "ANTHROPIC_BASE_URL", label: "Base URL", secret: false, required: true, placeholder: "https://api.enterprise.com/", hint: t("claudeCode.config.hintBaseUrl", "Enterprise internal API Gateway address") },
        { name: "DISABLE_TELEMETRY", label: t("aiCoding.config.fieldDisableTelemetry", "Disable Telemetry"), secret: false, placeholder: "1", hint: t("claudeCode.config.hintDisableTelemetry", "Set to 1 to disable telemetry (enterprise compliance)") },
        { name: "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", label: t("aiCoding.config.fieldDisableExtraTraffic", "Disable Extra Traffic"), secret: false, placeholder: "1", hint: t("claudeCode.config.hintDisableExtraTraffic", "Set to 1 to disable non-essential traffic (enterprise compliance)") },
        { name: "NODE_TLS_REJECT_UNAUTHORIZED", label: t("aiCoding.config.fieldSkipTlsVerify", "Skip TLS Verify"), secret: false, placeholder: "0", defaultValue: "0", hint: t("claudeCode.config.hintSkipTlsVerify", "Set to 0 to skip TLS certificate verification (only for enterprise self-signed cert environments)") },
      ],
    },
    {
      id: "anthropic",
      label: t("aiCoding.config.schemeAnthropic", "Anthropic API"),
      desc: t("aiCoding.config.schemeAnthropicDesc", "Direct Anthropic API key."),
      vars: [
        { name: "ANTHROPIC_API_KEY", label: "API Key", secret: true, required: true, placeholder: "sk-ant-api03-...", hint: t("claudeCode.config.hintAnthropicApiKey", "Get it from console.anthropic.com") },
        { name: "_anthropic_base_url", envKey: "ANTHROPIC_BASE_URL", label: "Base URL", secret: false, placeholder: "https://api.anthropic.com", hint: t("claudeCode.config.hintAnthropicBaseUrl", "Optional. Leave empty to use the official endpoint.") },
      ],
    },
    {
      id: "bedrock",
      label: t("aiCoding.config.schemeBedrock", "AWS Bedrock"),
      desc: t("aiCoding.config.schemeBedrockDesc", "AWS Bedrock credentials."),
      vars: [
        { name: "AWS_ACCESS_KEY_ID", label: "Access Key ID", secret: true, required: true, placeholder: "AKIAIOSFODNN7EXAMPLE", hint: t("claudeCode.config.hintAwsAccessKey", "AWS IAM access key ID") },
        { name: "AWS_SECRET_ACCESS_KEY", label: "Secret Access Key", secret: true, required: true, placeholder: "wJalrXUtnFEMI/...", hint: t("claudeCode.config.hintAwsSecretKey", "AWS IAM secret access key (sensitive)") },
        // V1 parity (ClaudeCodeConfigPanel.js:390): AWS_REGION is required.
        { name: "AWS_REGION", label: "Region", secret: false, required: true, placeholder: "us-east-1", hint: t("claudeCode.config.hintAwsRegion", "AWS region, e.g. us-east-1, ap-northeast-1") },
      ],
    },
    {
      id: "vertex",
      label: t("aiCoding.config.schemeVertex", "Google Vertex AI"),
      desc: t("aiCoding.config.schemeVertexDesc", "GCP service account."),
      vars: [
        { name: "GOOGLE_APPLICATION_CREDENTIALS", label: "Service Account JSON Path", secret: false, placeholder: "C:\\path\\to\\service-account.json", hint: t("claudeCode.config.hintGcpCredentials", "Absolute path to GCP service account JSON file") },
        { name: "GOOGLE_CLOUD_PROJECT", label: "GCP Project ID", secret: false, placeholder: "your-gcp-project-id", hint: t("claudeCode.config.hintGcpProject", "Google Cloud project ID") },
      ],
    },
    {
      id: "azure",
      label: t("aiCoding.config.schemeAzure", "Azure Foundry"),
      desc: t("aiCoding.config.schemeAzureDesc", "Azure AI Foundry endpoint."),
      vars: [
        { name: "CLAUDE_CODE_USE_FOUNDRY", label: "Enable Foundry", secret: false, required: true, placeholder: "1", hint: t("claudeCode.config.hintEnableFoundry", "Set to 1 to enable Azure Foundry mode") },
        { name: "ANTHROPIC_FOUNDRY_RESOURCE", label: "Resource Name", secret: false, placeholder: "your-azure-resource-name", hint: t("claudeCode.config.hintFoundryResource", "Azure AI Foundry resource name") },
        { name: "ANTHROPIC_FOUNDRY_BASE_URL", label: "Endpoint URL", secret: false, placeholder: "https://your-endpoint.openai.azure.com/", hint: t("claudeCode.config.hintFoundryEndpoint", "Azure AI Foundry endpoint URL") },
        // V1 parity (ClaudeCodeConfigPanel.js:410): Foundry API Key is OPTIONAL
        // (required: false). The previous V2 marker (required: true) was wrong.
        { name: "ANTHROPIC_FOUNDRY_API_KEY", label: "API Key", secret: true, required: false, placeholder: "your-azure-foundry-api-key", hint: t("claudeCode.config.hintFoundryApiKey", "Azure AI Foundry API Key (confidential)") },
      ],
    },
  ];

  function readScheme(): string {
    try {
      const v = localStorage.getItem(AUTH_SCHEME_KEY);
      if (v && AUTH_SCHEMES.some((s) => s.id === v)) return v;
    } catch {
      /* ignore */
    }
    return "enterprise";
  }
  const authScheme = ref(readScheme());
  function selectScheme(id: string): void {
    authScheme.value = id;
    try {
      localStorage.setItem(AUTH_SCHEME_KEY, id);
    } catch {
      /* ignore */
    }
  }
  const currentScheme = computed<AuthScheme>(
    () =>
      AUTH_SCHEMES.find((s) => s.id === authScheme.value) ??
      (AUTH_SCHEMES[0] as AuthScheme),
  );

  const authSchemesExpanded = ref(false);
  const displayedAuthSchemes = computed<AuthScheme[]>(() => {
    if (
      authSchemesExpanded.value ||
      AUTH_SCHEMES.length <= AUTH_SCHEMES_COLLAPSED_LIMIT
    ) {
      return AUTH_SCHEMES;
    }
    const head = AUTH_SCHEMES.slice(0, AUTH_SCHEMES_COLLAPSED_LIMIT);
    const currentId = authScheme.value;
    if (currentId && !head.some((s) => s.id === currentId)) {
      const sel = AUTH_SCHEMES.find((s) => s.id === currentId);
      if (sel) return [...head, sel];
    }
    return head;
  });
  const authSchemesHiddenCount = computed(() =>
    Math.max(0, AUTH_SCHEMES.length - displayedAuthSchemes.value.length),
  );

  // Credential var inputs (secret + non-secret unified) and status map.
  const varInputs = reactive<Record<string, string>>({});
  const credStatus = reactive<Record<string, CredentialStatus>>({});

  function credConfigured(name: string): boolean {
    return credStatus[name]?.configured ?? false;
  }
  function credEnvOnly(name: string): boolean {
    const s = credStatus[name];
    return !!s && s.in_env && !s.in_store;
  }

  const credSaving = ref(false);

  /**
   * Hydrate `varInputs` from the current `cfg.auth_env` snapshot, then
   * fill any non-secret slot that has a `defaultValue` and is still
   * empty (V1 parity for the ClaudeCodeConfigPanel.js loadConfig path).
   * The host calls this from its `loadConfig` after the backend payload
   * has merged into `cfg`.
   */
  function hydrateAuthEnvDefaults(): void {
    for (const [k, v] of Object.entries(cfg.auth_env)) {
      varInputs[k] = String(v ?? "");
    }
    for (const scheme of AUTH_SCHEMES) {
      for (const v of scheme.vars) {
        if (!v.secret && v.defaultValue !== undefined && !varInputs[v.name]) {
          varInputs[v.name] = v.defaultValue;
        }
      }
    }
  }

  async function loadCredentials(): Promise<void> {
    try {
      const res = await fetchCcCredentials();
      for (const [name, info] of Object.entries(res.credentials)) {
        credStatus[name] = info;
        if (info.in_store && !varInputs[name]) varInputs[name] = "****";
      }
    } catch {
      /* non-fatal */
    }
  }

  async function saveCredentials(): Promise<void> {
    credSaving.value = true;
    try {
      const scheme = currentScheme.value;
      const secrets: Record<string, string> = {};
      const nonSecret: Record<string, string> = {};
      for (const v of scheme.vars) {
        const val = varInputs[v.name] ?? "";
        if (v.secret) {
          secrets[v.name] = val;
        } else {
          nonSecret[v.name] = val;
          if (v.envKey && v.envKey !== v.name) nonSecret[v.envKey] = val;
        }
      }
      // 1) Non-secret → merge into auth_env via config PUT.
      if (Object.keys(nonSecret).length > 0) {
        const merged = { ...cfg.auth_env, ...nonSecret };
        cfg.auth_env = merged;
        await updateCcConfig({ auth_env: merged });
      }
      // 2) Secret → credentials endpoint (SecretStore).
      if (Object.keys(secrets).length > 0) {
        const res = await saveCcCredentials(secrets);
        for (const name of res.saved) {
          credStatus[name] = {
            in_store: true,
            in_env: credStatus[name]?.in_env ?? false,
            configured: true,
          };
          varInputs[name] = "****";
        }
        for (const name of res.deleted) {
          credStatus[name] = {
            in_store: false,
            in_env: credStatus[name]?.in_env ?? false,
            configured: credStatus[name]?.in_env ?? false,
          };
          varInputs[name] = "";
        }
        if (res.skipped.length > 0) {
          pushToast(
            "info",
            t(
              "aiCoding.config.credSkipped",
              "Some credentials were not stored (unsupported or masked): ",
            ) + res.skipped.join(", "),
          );
        }
      }
      pushToast(
        "success",
        t("aiCoding.config.authSaved", "Authentication saved"),
      );
      await onAfterMutation();
    } catch (e) {
      pushToast(
        "error",
        t("aiCoding.config.authSaveFailed", "Failed to save authentication: ") +
          (e as Error).message,
      );
    } finally {
      credSaving.value = false;
    }
  }

  async function removeCredential(name: string): Promise<void> {
    try {
      await deleteCcCredential(name);
      const prevEnv = credStatus[name]?.in_env ?? false;
      credStatus[name] = {
        in_store: false,
        in_env: prevEnv,
        configured: prevEnv,
      };
      varInputs[name] = "";
      pushToast(
        "info",
        t("aiCoding.config.credDeleted", "Credential deleted: ") + name,
      );
      await onAfterMutation();
    } catch (e) {
      pushToast(
        "error",
        t("aiCoding.config.deleteFailed", "Delete failed: ") +
          (e as Error).message,
      );
    }
  }

  return {
    AUTH_SCHEMES,
    AUTH_SCHEMES_COLLAPSED_LIMIT,
    authScheme,
    selectScheme,
    currentScheme,
    authSchemesExpanded,
    displayedAuthSchemes,
    authSchemesHiddenCount,
    varInputs,
    credStatus,
    credConfigured,
    credEnvOnly,
    credSaving,
    loadCredentials,
    saveCredentials,
    removeCredential,
    hydrateAuthEnvDefaults,
  };
}

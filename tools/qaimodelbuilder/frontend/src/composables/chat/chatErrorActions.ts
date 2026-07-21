// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `chatErrorActions` — declarative chat-error display registry.
 *
 * The SINGLE source of truth mapping a backend error `code` (the stable
 * contract value carried on the stream ERROR frame's `payload.code`) to how
 * the chat UI should render it: a localized title/message, up to two action
 * buttons, and whether a Retry affordance is offered.
 *
 * DESIGN — framework-free & unit-testable (AGENTS.md §2 判据 1):
 *   This module stores ONLY declarative data + stable action *IDs* (strings).
 *   It NEVER imports Vue / router / stores / i18n — so it can be unit-tested
 *   in isolation and stays the pure "what to show" layer. The "how to do it"
 *   layer (confirm dialogs, router.push, useRuntimeConfig, clipboard, retry)
 *   lives in the `useChatErrorActions` composable, keyed by the same action
 *   IDs. Rendering (ChatMessageList.vue) consumes `getChatErrorSpec(code)` and
 *   dispatches button clicks to the executor by `action.id`.
 *
 * i18n: `labelKey` / `titleKey` / `messageKey` are i18n paths resolved by the
 * caller's `t(...)`. All live under the `chatErrors.*` namespace (labels under
 * `chatErrors.actions.*`, messages under `chatErrors.messages.*`, the fallback
 * under `chatErrors.generic`). Keeping them here (not hard strings) keeps the
 * registry locale-agnostic.
 */

/** Stable action identifiers. The executor (`useChatErrorActions`) maps each
 *  to real behaviour; the registry only references them by string so it never
 *  couples to Vue. Exported as a const object (not a TS enum) so it tree-shakes
 *  and can be imported as values in both the executor and tests. */
export const CHAT_ERROR_ACTION = {
  /** Show a security-warning confirm, disable `ssl_verify`, then re-run the
   *  failed turn. USER-FIXABLE self-signed / untrusted-CA path only. */
  configureTlsAndRetry: "configure_tls_and_retry",
  /** Deep-link to Settings → Cloud Models (fix base_url / provider config). */
  openProviderSettings: "open_provider_settings",
  /** Open the existing edition-aware API-key flow. */
  openApiKeyFlow: "open_api_key_flow",
  /** Deep-link to the model picker (cloud-models settings). */
  selectModel: "select_model",
  /** Wire to the existing compact/compress action (prompt too long). */
  compressContext: "compress_context",
  /** Re-run the failed turn via the existing retry path. */
  retryRequest: "retry_request",
  /** Copy a sanitized diagnostics block to the clipboard. */
  copyDiagnostics: "copy_diagnostics",
} as const;

export type ChatErrorActionId =
  (typeof CHAT_ERROR_ACTION)[keyof typeof CHAT_ERROR_ACTION];

export interface ChatErrorAction {
  /** Stable id dispatched to the executor. */
  readonly id: ChatErrorActionId;
  /** i18n key for the button label. */
  readonly labelKey: string;
  /** Visual weight; renderer maps to a button class. Defaults to "ghost". */
  readonly style?: "primary" | "ghost" | "danger";
}

export interface ChatErrorSpec {
  /** The backend error `code` this spec renders (contract value). */
  readonly code: string;
  /** Optional i18n key for a short bold title above the message. */
  readonly titleKey?: string;
  /** i18n key for the concise, user-facing localized message. */
  readonly messageKey: string;
  /** Primary (emphasised) action button. */
  readonly primaryAction?: ChatErrorAction;
  /** Secondary (lower-weight) action button. */
  readonly secondaryAction?: ChatErrorAction;
  /** Whether to render the ↻ Retry affordance (re-run the failed turn). */
  readonly showRetry?: boolean;
}

// ── Reusable action descriptors (keeps the table below DRY) ────────────────
const A = CHAT_ERROR_ACTION;

const openProviderSettings: ChatErrorAction = {
  id: A.openProviderSettings,
  labelKey: "chatErrors.actions.openProviderSettings",
  style: "primary",
};
const openProviderSettingsGhost: ChatErrorAction = {
  id: A.openProviderSettings,
  labelKey: "chatErrors.actions.openProviderSettings",
  style: "ghost",
};
const configureTlsAndRetry: ChatErrorAction = {
  id: A.configureTlsAndRetry,
  labelKey: "chatErrors.actions.disableTlsAndRetry",
  style: "danger",
};
const openApiKeyFlow: ChatErrorAction = {
  id: A.openApiKeyFlow,
  labelKey: "chatErrors.actions.setApiKey",
  style: "primary",
};
const selectModel: ChatErrorAction = {
  id: A.selectModel,
  labelKey: "chatErrors.actions.selectModel",
  style: "primary",
};
/**
 * Ghost-styled `selectModel` — used as the SECONDARY affordance on the
 * `permission_denied` bubble (primary is "Open Cloud Model settings", the
 * long-term fix; secondary is "pick another model right now", the immediate
 * fix). Same action id → the executor's existing `selectModel` handler
 * (`useChatErrorActions.ts:152`) opens the same model-picker route; only
 * the label + visual weight differ.
 */
const selectModelGhost: ChatErrorAction = {
  id: A.selectModel,
  labelKey: "chatErrors.actions.switchModel",
  style: "ghost",
};
const compressContext: ChatErrorAction = {
  id: A.compressContext,
  labelKey: "chatErrors.actions.compressContext",
  style: "primary",
};

/**
 * The full code → spec table. Codes align 1:1 with the backend contract
 * (chat stream ERROR frame `payload.code`). Message keys live under
 * `chatErrors.messages.*`. `showRetry` is set for codes whose disposition is a
 * bounded/exhausted transient failure (re-running the turn is meaningful);
 * terminal-config errors (TLS / auth / model) steer the user to a fix instead.
 *
 * NOTE: this is the ONLY place codes are enumerated — ChatMessageList routes
 * ALL terminal errors (including the former apiKey / unsupported-param special
 * cases) through `getChatErrorSpec`, so there is a single rendering path.
 */
const SPECS: readonly ChatErrorSpec[] = [
  // ── TLS ────────────────────────────────────────────────────────────────
  {
    // Self-signed / untrusted CA — the one USER-FIXABLE TLS case: offer to
    // turn off verification (danger) with a security confirm, or fix config.
    code: "chat.llm.tls_cert_untrusted",
    messageKey: "chatErrors.messages.tlsCertUntrusted",
    primaryAction: configureTlsAndRetry,
    secondaryAction: openProviderSettingsGhost,
  },
  {
    // Cert doesn't match host → the fix is base_url, NOT disabling verify.
    code: "chat.llm.tls_hostname_mismatch",
    messageKey: "chatErrors.messages.tlsHostnameMismatch",
    primaryAction: openProviderSettings,
  },
  {
    code: "chat.llm.tls_cert_expired",
    messageKey: "chatErrors.messages.tlsCertExpired",
    primaryAction: openProviderSettings,
  },
  {
    code: "chat.llm.tls_handshake_failed",
    messageKey: "chatErrors.messages.tlsHandshakeFailed",
    primaryAction: openProviderSettings,
  },
  // ── Connectivity (bounded_fast → terminal after retries) ────────────────
  {
    code: "chat.llm.dns_error",
    messageKey: "chatErrors.messages.dnsError",
    primaryAction: openProviderSettings,
    showRetry: true,
  },
  {
    code: "chat.llm.connection_refused",
    messageKey: "chatErrors.messages.connectionRefused",
    primaryAction: openProviderSettings,
    showRetry: true,
  },
  {
    code: "chat.llm.host_unreachable",
    messageKey: "chatErrors.messages.hostUnreachable",
    primaryAction: openProviderSettings,
    showRetry: true,
  },
  // ── network_wait, now capped at 600s wall-clock → terminal ──────────────
  {
    code: "chat.llm.connect_error",
    messageKey: "chatErrors.messages.networkExhausted",
    primaryAction: openProviderSettingsGhost,
    showRetry: true,
  },
  {
    code: "chat.llm.timeout",
    messageKey: "chatErrors.messages.networkExhausted",
    showRetry: true,
  },
  {
    code: "chat.llm.read_error",
    messageKey: "chatErrors.messages.networkExhausted",
    showRetry: true,
  },
  {
    // was infinite, now terminal.
    code: "chat.llm.network_error",
    messageKey: "chatErrors.messages.networkExhausted",
    showRetry: true,
  },
  // ── Upstream server ─────────────────────────────────────────────────────
  {
    code: "chat.llm.server_error",
    messageKey: "chatErrors.messages.serverError",
    showRetry: true,
  },
  // ── Auth / authz / model availability (never — steer to a fix) ──────────
  {
    code: "chat.llm.auth_failed",
    messageKey: "chatErrors.messages.authFailed",
    primaryAction: openApiKeyFlow,
  },
  {
    code: "chat.llm.permission_denied",
    messageKey: "chatErrors.messages.permissionDenied",
    primaryAction: openProviderSettings,
    // Secondary: let the user pick a different model right now (deep-links
    // to Cloud Model settings where the picker lives — same route as the
    // primary, but a distinct label because the user's intent is different:
    // "I want to switch models" vs "I want to configure this provider").
    // The backend's ``permission_denied`` snapshot separately hides denied
    // models from the composer dropdown (see `stores/cloudModelPermissions.ts`
    // + `ModelDropdown.vue`), so any model the user picks from the settings
    // page will be one their key actually has access to.
    secondaryAction: selectModelGhost,
  },
  {
    code: "chat.llm.model_unavailable",
    messageKey: "chatErrors.messages.modelUnavailable",
    primaryAction: selectModel,
  },
  // ── Existing codes (folded into the single registry path) ───────────────
  {
    // Missing cloud API key — was the `isMissingApiKeyError` special case.
    code: "chat.llm.provider_api_key_missing",
    messageKey: "cloudModels.apiKeyError.message",
    primaryAction: openApiKeyFlow,
  },
  {
    // Unsupported sampling param — was the `isUnsupportedParamError` case.
    code: "chat.llm.unsupported_param",
    messageKey: "chatErrors.messages.unsupportedParam",
    primaryAction: openProviderSettings,
  },
  {
    code: "prompt_too_long",
    messageKey: "chatErrors.messages.promptTooLong",
    primaryAction: compressContext,
  },
  {
    code: "throttling",
    messageKey: "chatErrors.messages.throttling",
    showRetry: true,
  },
  {
    code: "content_filtered",
    messageKey: "chatErrors.messages.contentFiltered",
  },
];

/** O(1) lookup table built once at module load. */
const SPEC_BY_CODE: ReadonlyMap<string, ChatErrorSpec> = new Map(
  SPECS.map((s) => [s.code, s]),
);

/**
 * The generic fallback spec for codes with no dedicated entry. Retryable by
 * default (re-running is harmless and often works for transient/unknown
 * failures) and always offers Copy diagnostics via the renderer.
 */
export const GENERIC_CHAT_ERROR_SPEC: ChatErrorSpec = {
  code: "__generic__",
  messageKey: "chatErrors.generic",
  showRetry: true,
};

/**
 * Look up the display spec for a backend error `code`.
 * Returns `null` when the code is unknown; callers fall back to
 * `GENERIC_CHAT_ERROR_SPEC`. Kept separate (not auto-falling-back) so tests /
 * callers can distinguish "known code" from "generic".
 */
export function getChatErrorSpec(
  code: string | null | undefined,
): ChatErrorSpec | null {
  if (code === null || code === undefined || code === "") {
    return null;
  }
  return SPEC_BY_CODE.get(code) ?? null;
}

/**
 * Convenience: always returns a renderable spec — the dedicated one when the
 * code is known, otherwise a `__generic__`-coded clone carrying the real code
 * so diagnostics still report it. Used by the single rendering path.
 */
export function resolveChatErrorSpec(
  code: string | null | undefined,
): ChatErrorSpec {
  const found = getChatErrorSpec(code);
  if (found !== null) {
    return found;
  }
  return { ...GENERIC_CHAT_ERROR_SPEC, code: code ?? "__generic__" };
}

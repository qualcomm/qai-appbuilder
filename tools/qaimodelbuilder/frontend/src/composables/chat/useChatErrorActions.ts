// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useChatErrorActions` — chat-error action EXECUTOR.
 *
 * The "how to do it" half of the declarative error registry
 * (`chatErrorActions.ts` is the "what to show" half). Maps a stable
 * `ChatErrorActionId` to real behaviour that needs the Vue runtime — router
 * deep-links, the confirm dialog, `useRuntimeConfig().save`, toasts, clipboard
 * — so the registry itself stays framework-free & unit-testable.
 *
 * ChatMessageList renders a registry spec's buttons and dispatches clicks here
 * via `runChatErrorAction(actionId, ctx)`. The caller supplies a `ctx` with
 * the failed-turn identity + a `retry` callback (the existing re-run path) so
 * this composable never reaches into ChatMessageList internals.
 */
import { useRouter } from "vue-router";

import { useConfirm } from "@/composables/useConfirm";
import { useToast } from "@/composables/useToast";
import { useRuntimeConfig } from "@/composables/useRuntimeConfig";
import { useCloudModelStatus } from "@/composables/useCloudModelStatus";
import { useI18n } from "vue-i18n";

import { CHAT_ERROR_ACTION, type ChatErrorActionId } from "./chatErrorActions";

/**
 * Per-invocation context handed in by the renderer. Kept minimal + explicit so
 * the executor stays decoupled from the component: identity of the failed turn
 * (guard against stale/duplicate clicks) + the existing re-run path.
 */
export interface ChatErrorActionContext {
  /** The failed user message's id (anchor for re-running the turn). */
  readonly failedMessageId: string | null;
  /** The failed user message's content (re-submitted on retry). */
  readonly failedContent: string;
  /** True if this error is STILL the current failed turn. When it isn't (the
   *  user already retried / cleared / moved on), destructive/re-run actions are
   *  skipped so a stale button press can't fire an unexpected turn. */
  readonly isCurrent: () => boolean;
  /** Re-run the failed turn (the existing `emit("retry")` → ChatView path). */
  readonly retry: (messageId: string, content: string) => void;
  /** Sanitized diagnostics string for the Copy action (see `buildDiagnostics`). */
  readonly diagnostics: () => string;
}

export interface UseChatErrorActions {
  /** Execute the action for `actionId` against `ctx`. Async because the TLS
   *  path awaits a confirm + a config save. Resolves when done (never throws
   *  to the caller — surfaces failures via toast). */
  runChatErrorAction: (
    actionId: ChatErrorActionId,
    ctx: ChatErrorActionContext,
  ) => Promise<void>;
}

export function useChatErrorActions(): UseChatErrorActions {
  const router = useRouter();
  const { confirm } = useConfirm();
  const toast = useToast();
  const { save: saveRuntimeConfig } = useRuntimeConfig();
  const cloudModelStatus = useCloudModelStatus();
  const { t } = useI18n();

  /** Deep-link to Settings → Cloud Models (fix base_url / provider / model). */
  function goToCloudModelSettings(): void {
    void router.push({ path: "/settings", query: { tab: "cloud-models" } });
  }

  /**
   * `configure_tls_and_retry` — the USER-FIXABLE self-signed/untrusted-CA path.
   *   (a) show a SECURITY WARNING confirm (MITM risk);
   *   (b) on confirm, `save({ ssl_verify: false })` — hot-applied, no reboot;
   *   (c) toast success;
   *   (d) re-run the failed turn via the provided `ctx.retry`.
   *
   * STALE/DUPLICATE guard: we re-check `ctx.isCurrent()` right before the
   * config save AND before the retry (the confirm dialog is async — the world
   * may have moved on while it was open). The button itself is disabled on
   * click by the renderer; this is the belt-and-suspenders logical guard.
   */
  async function configureTlsAndRetry(
    ctx: ChatErrorActionContext,
  ): Promise<void> {
    const ok = await confirm({
      icon: "⚠️",
      title: t("chatErrors.tlsWarning.title"),
      message: t("chatErrors.tlsWarning.message"),
      confirmText: t("chatErrors.tlsWarning.confirm"),
      cancelText: t("chatErrors.tlsWarning.cancel"),
      confirmStyle: "danger",
    });
    if (!ok) {
      return;
    }
    // The turn may no longer be the current failed one (user retried/cleared
    // while the dialog was open) — do NOT silently disable TLS for a stale
    // error. Bail without changing config.
    if (!ctx.isCurrent()) {
      return;
    }
    await saveRuntimeConfig({ ssl_verify: false });
    toast.success(t("chatErrors.tlsWarning.disabledToast"));
    // Re-run the failed turn only if it is still the current one and we have a
    // valid anchor message.
    if (ctx.isCurrent() && ctx.failedMessageId !== null) {
      ctx.retry(ctx.failedMessageId, ctx.failedContent);
    }
  }

  /** `retry_request` — re-run the failed turn (guarded against stale clicks). */
  function retryRequest(ctx: ChatErrorActionContext): void {
    if (!ctx.isCurrent() || ctx.failedMessageId === null) {
      return;
    }
    ctx.retry(ctx.failedMessageId, ctx.failedContent);
  }

  /** `copy_diagnostics` — copy the SANITIZED diagnostics block to clipboard. */
  async function copyDiagnostics(ctx: ChatErrorActionContext): Promise<void> {
    const text = ctx.diagnostics();
    try {
      if (
        typeof navigator !== "undefined" &&
        navigator.clipboard !== undefined &&
        typeof navigator.clipboard.writeText === "function"
      ) {
        await navigator.clipboard.writeText(text);
        toast.success(t("chatErrors.diagnosticsCopied"));
        return;
      }
      throw new Error("clipboard unavailable");
    } catch {
      // Clipboard can fail (permissions / insecure context). Surface the text
      // so the user can still copy it manually rather than a silent dead click.
      toast.warning(t("chatErrors.diagnosticsCopyFailed"));
    }
  }

  async function runChatErrorAction(
    actionId: ChatErrorActionId,
    ctx: ChatErrorActionContext,
  ): Promise<void> {
    switch (actionId) {
      case CHAT_ERROR_ACTION.configureTlsAndRetry:
        await configureTlsAndRetry(ctx);
        return;
      case CHAT_ERROR_ACTION.openProviderSettings:
        goToCloudModelSettings();
        return;
      case CHAT_ERROR_ACTION.openApiKeyFlow:
        cloudModelStatus.openApiKeyFlow();
        return;
      case CHAT_ERROR_ACTION.selectModel:
        // No standalone model-picker route exists; the cloud-models settings
        // tab is where a model is chosen/added — same deep-link as provider
        // settings (mirrors the existing unsupported-param navigation).
        goToCloudModelSettings();
        return;
      case CHAT_ERROR_ACTION.compressContext:
        // No in-code compact action is exposed to call directly (compaction is
        // driven by the `/compact` slash command / backend). Point the user at
        // it via a toast rather than a dead button.
        toast.info(t("chatErrors.compressHint"));
        return;
      case CHAT_ERROR_ACTION.retryRequest:
        retryRequest(ctx);
        return;
      case CHAT_ERROR_ACTION.copyDiagnostics:
        await copyDiagnostics(ctx);
        return;
      default:
        return;
    }
  }

  return { runChatErrorAction };
}

// ── Diagnostics (task 4) ────────────────────────────────────────────────────

/** Input shape for `buildDiagnostics`. All fields optional/nullable — assemble
 *  from whatever the error envelope + store make available. */
export interface DiagnosticsInput {
  readonly code: string | null | undefined;
  readonly retryDisposition?: string | null;
  readonly httpStatus?: number | null;
  readonly requestId?: string | null;
  /** Model name/id the failed turn targeted. */
  readonly model?: string | null;
  /** Target base_url (userinfo/query/token are stripped to host[:port]). */
  readonly baseUrl?: string | null;
  /** Attempt count, when known. */
  readonly attempt?: number | null;
  /** First line of the backend English message (never the full body). */
  readonly messageFirstLine?: string | null;
  /** App version (build-time `__APP_VERSION__`). */
  readonly appVersion?: string | null;
}

/**
 * Strip a URL to a bare `host[:port]` — NO userinfo (user:pass@), NO path,
 * query or fragment (which can carry tokens). Returns null when the input has
 * no usable host. Falsy/garbage input → null (never leaks the raw string).
 */
export function sanitizeHost(
  raw: string | null | undefined,
): string | null {
  if (raw === null || raw === undefined || raw.trim() === "") {
    return null;
  }
  try {
    const u = new URL(raw);
    // `u.host` is host[:port] and NEVER includes userinfo — safe.
    return u.host !== "" ? u.host : null;
  } catch {
    // Not a full URL — extract a conservative host token: drop any scheme,
    // userinfo, path/query. Never return the raw string wholesale.
    const noScheme = raw.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
    const noUser = noScheme.includes("@")
      ? noScheme.slice(noScheme.lastIndexOf("@") + 1)
      : noScheme;
    const hostOnly = noUser.split(/[/?#]/)[0] ?? "";
    return hostOnly !== "" ? hostOnly : null;
  }
}

/**
 * Assemble a SANITIZED, human-readable diagnostics block for the "Copy
 * diagnostics" action. INCLUDES ONLY non-sensitive fields:
 *   error code · retry_disposition · HTTP status · request_id · model ·
 *   target host (host[:port] only) · attempt count · app version · timestamp.
 *
 * NEVER includes: Authorization header, API key, full prompt, raw response
 * body. The `messageFirstLine` is intentionally first-line-only (backend
 * English diagnostic sentence), not the full message/body.
 */
export function buildDiagnostics(input: DiagnosticsInput): string {
  const lines: string[] = [];
  const push = (label: string, value: string | number | null | undefined) => {
    if (value !== null && value !== undefined && String(value).trim() !== "") {
      lines.push(`${label}: ${String(value)}`);
    }
  };
  push("code", input.code ?? null);
  push("retry_disposition", input.retryDisposition ?? null);
  push("http_status", input.httpStatus ?? null);
  push("request_id", input.requestId ?? null);
  push("model", input.model ?? null);
  push("host", sanitizeHost(input.baseUrl));
  push("attempt", input.attempt ?? null);
  // First line only — guard against a multi-line/huge backend message.
  const firstLine =
    input.messageFirstLine != null
      ? input.messageFirstLine.split(/\r?\n/)[0]?.slice(0, 300) ?? null
      : null;
  push("message", firstLine);
  push("app_version", input.appVersion ?? null);
  push("timestamp", new Date().toISOString());
  return lines.join("\n");
}

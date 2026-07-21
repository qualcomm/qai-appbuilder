// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

// =============================================================================
// i18n locale sub-file — 手工维护，UTF-8（无 BOM）。
//
// chatErrors namespace — declarative chat-error registry strings (titles,
// concise user-facing messages, action button labels) + the TLS security
// warning dialog. Consumed by `chatErrorActions.ts` (message/label keys) and
// `useChatErrorActions.ts` (dialog + toast keys). Keep key structure identical
// across en / zh-CN / zh-TW (enforced by tsc + locale parity tests).
// =============================================================================

const chatErrors = {
  // Generic fallback for unknown error codes.
  generic: "The request failed. You can retry, or copy diagnostics for details.",
  // Localized, concise, user-facing messages keyed by intent (not code — the
  // registry maps code → key so several codes can share one message).
  messages: {
    tlsCertUntrusted:
      "Could not verify the model service's TLS certificate (it may be self-signed or from a corporate gateway). Turning off verification lowers security — only do so if you trust this service.",
    tlsHostnameMismatch:
      "The certificate does not match the service address — usually the base_url host is wrong.",
    tlsCertExpired:
      "The model service's TLS certificate has expired. Contact the service operator or fix the base_url.",
    tlsHandshakeFailed:
      "TLS handshake with the model service failed. Check the base_url and network.",
    dnsError:
      "Could not resolve the model service address. Check the base_url, VPN or network.",
    connectionRefused:
      "The model service refused the connection. Make sure it is running and the port / base_url are correct.",
    hostUnreachable:
      "The model service host is unreachable. Check the base_url and network.",
    networkExhausted:
      "The network did not recover in time; automatic reconnection has stopped.",
    serverError:
      "The model service is temporarily unavailable and still failed after several retries.",
    authFailed: "Authentication failed — the API key is invalid or expired.",
    permissionDenied:
      "No access to this model (it may be unauthorized or region-restricted).",
    modelUnavailable:
      "The model or endpoint was not found. Pick another model or check the configuration.",
    unsupportedParam:
      "The model does not support one of the sampling parameters. Turn it off in Cloud Model settings.",
    promptTooLong:
      "The prompt exceeds the model's context window. Compress the conversation and retry.",
    throttling: "Rate limited by the model service. Please retry shortly.",
    contentFiltered:
      "The request was blocked by the model's content filter.",
  },
  // Action button labels.
  actions: {
    disableTlsAndRetry: "Trust & disable verification, then retry",
    openProviderSettings: "Open Cloud Model settings",
    setApiKey: "Set API Key",
    selectModel: "Select model",
    switchModel: "Try another model",
    compressContext: "Compress context",
    copyDiagnostics: "Copy diagnostics",
  },
  // TLS security-warning confirm dialog.
  tlsWarning: {
    title: "Turn off TLS verification?",
    message:
      "Disabling this stops verifying the server certificate and exposes you to man-in-the-middle attacks. Continue only if you are sure this service is trustworthy.",
    confirm: "Disable & retry",
    cancel: "Cancel",
    disabledToast: "TLS verification turned off; retrying…",
  },
  compressHint:
    "Send /compact in the chat to compress the conversation history, then retry.",
  diagnosticsCopied: "Diagnostics copied to clipboard",
  diagnosticsCopyFailed: "Could not copy to clipboard",
};

export default chatErrors;

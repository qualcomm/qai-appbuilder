// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useAppBuilderChatBridge — App Builder → Chat injection bridge (block-4 G4).
 *
 * V1 parity: `useAppBuilder.sendToChat(payload)` → app.js
 * `_sendToChatHandler` → `useChat.appendUserMessage(...)`, plus
 * `toolParamsForChat` projected into the outgoing turn's `tool_params`.
 *
 * V2 keeps the chat / app_builder contexts isolated (AGENTS §3.2): the
 * front-end bridge never reaches into `qai.chat` from `qai.app_builder`;
 * it only mutates the shared `chatTabs` Pinia store. The backend
 * `system_prompt_builder` app-builder branch (driven by
 * `tool_mode === "app-builder"`) resolves the per-Pack SKILL files +
 * pack catalog server-side, so the bridge does NOT have to ship SKILL
 * text — it only has to (a) switch the active tab into app-builder mode
 * and (b) carry the workbench selection in `tool_params`.
 *
 * V1 行为对齐（V1 `frontend/js/app.js`
 * 第 555-631 行 `sendToChatHandler` + DynamicOutput.js 第 38-164 行
 * `buildSendPayload` / `buildErrorPrompt`）：
 *   1) 错误分支（V1 564-575）：`run.error` 命中 OOM/NOT_INSTALLED/generic
 *      时使用 `appBuilder.errorPromptTemplate` i18n 模板拼出 errorPrompt，
 *      不附图，直接发送，发送后折叠 workbench。
 *   2) 成功分支：拼接 `prompt` + `summary` + 文本类附件
 *      （`---\n{fullText}` 完整原文），不走截断；图片类附件抓取为 File
 *      送进 chat 的 pendingImages（与 attach 按钮等价），等图片就位再
 *      发送。
 *   3) 末尾折叠 workbench（让用户看到对话流）。
 *
 * Responsibilities:
 *   - `sendToChat()`  — switch the active tab to app-builder mode, write
 *     the app-builder `tool_params` projection onto the tab, and append a
 *     user message composed from the (editable) send-to-chat prompt +
 *     the run output summary. Returns the appended message id (or null).
 *   - `applyToolParams()` — push the current `toolParamsForChat`
 *     projection onto the active tab without injecting a message (used
 *     when only the selection changed).
 */
import { useI18n } from "vue-i18n";

import { useChatTabsStore, type ToolParams } from "@/stores/chatTabs";
import {
  useAppBuilderStore,
  type AppBuilderToolParams,
  type AppRun,
} from "@/stores/appBuilder";
import { workbenchOpen } from "@/composables/app-builder/useAppBuilderModeUi";
import { enqueuePendingImage } from "@/composables/chat/usePendingChatImages";
import { enqueueChatSubmit } from "@/composables/chat/usePendingChatSubmit";

export interface UseAppBuilderChatBridge {
  /**
   * Inject the current run result into the active chat tab as a user
   * message AND trigger a real LLM turn (V1 app.js:600-625 parity:
   * `inputText = text; nextTick(() => sendMessage())`). `prompt` overrides
   * the store's `sendToChatPrompt` when provided.
   *
   * The composed text is handed to the chat composer via the
   * `usePendingChatSubmit` intent queue, which runs the composer's normal
   * submit path (image upload + `pushUserMessage` + `transport.send`). Image
   * attachments — when present — are pre-fetched as Files and forwarded into
   * the composer's pending-images queue first so the submit uploads them.
   *
   * Returns the composed content string when a turn was dispatched, or null
   * when there was nothing to send (no active tab / empty content).
   */
  sendToChat(prompt?: string): string | null;
  /** Push the current app-builder tool-params projection onto the tab. */
  applyToolParams(): void;
}

/** Attachment entry the bridge derives from `run.output` (V1 buildSendPayload parity). */
interface BridgeAttachment {
  kind: "image" | "text" | "audio" | "json";
  /** Repo-relative path (e.g. `data/output/x.png`) or empty. */
  path: string;
  /** Browser-usable URL (`data:` / `blob:` / `http(s):` pass-through, else `/` + path). */
  url: string;
  /** Display name (basename or fallback). */
  name: string;
  /** MIME type (best-effort). */
  mime: string;
}

function projectionToToolParams(p: AppBuilderToolParams): Partial<ToolParams> {
  const out: Partial<ToolParams> = {};
  if (p.selected_model_id !== null) out.selected_model_id = p.selected_model_id;
  if (Array.isArray(p.selected_model_ids) && p.selected_model_ids.length > 0) {
    out.selected_model_ids = [...p.selected_model_ids];
  }
  if (p.selected_model_name !== null) {
    out.selected_model_name = p.selected_model_name;
  }
  if (p.category !== null) out.category = p.category;
  if (p.variant_id !== null) out.variant_id = p.variant_id;
  if (p.last_run_summary !== null) out.last_run_summary = p.last_run_summary;
  return out;
}

/**
 * Resolve a repo-relative output path to a browser-usable URL.
 * V1 parity: app.js line 609
 *   `a.url || ('/' + String(a.path || '').replace(/^\/+/, '').replace(/\\/g, '/'))`
 * `data:` / `blob:` / `http(s):` URLs are returned as-is.
 */
function _resolvePathToUrl(path: string): string {
  if (path === "") return "";
  if (/^(data:|blob:|https?:)/i.test(path)) return path;
  const norm = path.replace(/^\/+/, "").replace(/\\/g, "/");
  return "/" + norm;
}

/** Best-effort basename extractor (handles both `/` and `\`). */
function _basename(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] ?? "";
}

/** Best-effort MIME guess from extension (image-only fallback). */
function _guessImageMime(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
  if (ext === "png") return "image/png";
  if (ext === "gif") return "image/gif";
  if (ext === "webp") return "image/webp";
  if (ext === "bmp") return "image/bmp";
  return "image/png";
}

/**
 * Parse `run.error` (a single string in V2; V1 split it into `code` + `message`)
 * into best-effort `{ code, message }` for the i18n template. Convention used by
 * the run pipeline: errors look like `CODE: message`, `CODE` alone, or free text.
 */
function _splitRunError(
  errorMessage: string,
): { code: string; message: string } {
  if (errorMessage === "") return { code: "UNKNOWN", message: "" };
  const m = errorMessage.match(/^([A-Z][A-Z0-9_]*)\s*[:-]\s*(.*)$/s);
  if (m !== null) return { code: m[1] ?? "UNKNOWN", message: m[2] ?? "" };
  // Plain code (no colon)
  if (/^[A-Z][A-Z0-9_]+$/.test(errorMessage)) {
    return { code: errorMessage, message: "" };
  }
  return { code: "UNKNOWN", message: errorMessage };
}

/**
 * Derive the run's display name from store state. The store has
 * `selected_model_name` on its tool-params projection; fall back to
 * `run.modelId`.
 */
function _runDisplayName(
  run: AppRun,
  toolParams: AppBuilderToolParams,
): string {
  const name = toolParams.selected_model_name;
  if (typeof name === "string" && name !== "") return name;
  return run.modelId;
}

/**
 * Build the attachment list from `run.output` (V1 DynamicOutput.buildSendPayload
 * parity, lines 38-147). Picks fields by V1 priority:
 *   image: image_path / depth_map_path / result_image_path / output_path
 *   audio: audio_path
 *   text:  fullText / text  (only flagged so caller knows to inline)
 */
function _buildAttachmentsFromOutput(
  output: Record<string, unknown> | null,
): BridgeAttachment[] {
  if (output === null) return [];
  const out: BridgeAttachment[] = [];

  // Image candidates (V1 line 76-81 + line 131 in useAppBuilder _probeOutputAlive).
  // Order matches V1 `image_path` first, then derived/secondary outputs.
  const imageKeys = [
    "image_path",
    "imageUrl",
    "depth_map_path",
    "result_image_path",
    "output_path",
  ];
  const seenImagePaths = new Set<string>();
  for (const k of imageKeys) {
    const v = output[k];
    if (typeof v !== "string" || v === "") continue;
    if (seenImagePaths.has(v)) continue;
    seenImagePaths.add(v);
    const url = _resolvePathToUrl(v);
    const name = _basename(v) || `result.png`;
    out.push({
      kind: "image",
      path: v,
      url,
      name,
      mime: _guessImageMime(name),
    });
  }

  // Audio (V1 line 130-136).
  const audioPath = output.audio_path;
  if (typeof audioPath === "string" && audioPath !== "") {
    out.push({
      kind: "audio",
      path: audioPath,
      url: _resolvePathToUrl(audioPath),
      name: _basename(audioPath) || "result.wav",
      mime: "audio/wav",
    });
  }

  // Text (V1 line 95-100 / 114-119): a sentinel entry — caller checks
  // `output.fullText` / `output.text` itself.
  const hasFullText =
    typeof output.fullText === "string" && (output.fullText as string) !== "";
  const hasText =
    !hasFullText &&
    typeof output.text === "string" &&
    (output.text as string) !== "";
  if (hasFullText || hasText) {
    out.push({
      kind: "text",
      path: "",
      url: "",
      name: "transcript.txt",
      mime: "text/plain",
    });
  }

  return out;
}

/**
 * Convert a `data:` URL to a `Blob` (sync). For `http(s):` / `/` paths the
 * caller must `fetch()` the URL.
 */
function _dataUrlToBlob(dataUrl: string): Blob | null {
  const m = dataUrl.match(/^data:([^;,]+)(?:;base64)?,(.*)$/s);
  if (m === null) return null;
  const mime = m[1] ?? "application/octet-stream";
  const payload = m[2] ?? "";
  const isBase64 = /;base64,/.test(dataUrl);
  try {
    if (isBase64) {
      const bin = atob(payload);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      return new Blob([bytes], { type: mime });
    }
    return new Blob([decodeURIComponent(payload)], { type: mime });
  } catch {
    return null;
  }
}

/**
 * Resolve an image attachment to a `File` (V1 app.js line 605-619 parity:
 * fetch → blob → File). Returns null on failure (caller logs warning and
 * proceeds without that attachment, matching V1 `Promise.allSettled`).
 */
async function _attachmentToFile(att: BridgeAttachment): Promise<File | null> {
  if (att.url === "") return null;
  try {
    let blob: Blob | null;
    if (att.url.startsWith("data:")) {
      blob = _dataUrlToBlob(att.url);
    } else {
      const resp = await fetch(att.url);
      if (!resp.ok) {
         
        console.warn(
          `[appBuilder→chat] failed to fetch attachment ${att.url}: HTTP ${resp.status}`,
        );
        return null;
      }
      blob = await resp.blob();
    }
    if (blob === null) return null;
    const finalMime = blob.type !== "" ? blob.type : att.mime;
    return new File([blob], att.name || "result.png", { type: finalMime });
  } catch (e) {
     
    console.warn(`[appBuilder→chat] failed to attach ${att.url}`, e);
    return null;
  }
}

export function useAppBuilderChatBridge(): UseAppBuilderChatBridge {
  const tabs = useChatTabsStore();
  const ab = useAppBuilderStore();
  const { t } = useI18n();

  function applyToolParams(): void {
    const tab = tabs.activeTab;
    if (tab === null) return;
    tabs.setActiveMode(tab.id, "app-builder");
    tabs.setToolParams(tab.id, projectionToToolParams(ab.toolParamsForChat));
  }

  /**
   * Build the success-branch message text (V1 app.js 578-602):
   *   - editable prompt (sendToChatPrompt or override)
   *   - run summary (existing `composeSendToChatMessage` projects this)
   *   - if a text attachment is detected, append `---\n{fullText}` (or `text`)
   *     verbatim — NOT through the `summariseOutput` 2000-char cap, so long
   *     transcripts/OCR don't get truncated (V1 line 592-596).
   *   - if image attachments are detected but the chat composer's
   *     `pendingImages` API is unavailable, inline image URLs as a
   *     `Generated images:` block so the user still sees the asset
   *     references (degraded fallback; see HEAD-OF-FILE comment + report).
   */
  function _buildSuccessText(
    run: AppRun,
    effectivePrompt: string,
    attachments: BridgeAttachment[],
  ): string {
    // Re-use the store's compose helper for the prompt + short summary.
    const base = ab.composeSendToChatMessage(effectivePrompt);
    const lines: string[] = [];
    if (base !== "") lines.push(base);

    // Append full text verbatim (V1 line 592-596).
    const output = run.output ?? null;
    if (output !== null) {
      const hasFullText = attachments.some((a) => a.kind === "text");
      if (hasFullText) {
        const fullText =
          typeof output.fullText === "string" && output.fullText !== ""
            ? (output.fullText as string)
            : typeof output.text === "string"
              ? (output.text as string)
              : "";
        const trimmed = fullText.trim();
        if (trimmed !== "") lines.push("---\n" + trimmed);
      }
    }

    return lines.join("\n").trim();
  }

  /**
   * Build the error-branch message text (V1 app.js 564-568 + DynamicOutput.js
   * 153-164 `buildErrorPrompt`): use the locale `appBuilder.errorPromptTemplate`
   * with `{model}/{code}/{message}` placeholders. No attachments are sent in
   * this branch.
   */
  function _buildErrorText(run: AppRun): string {
    const tp = ab.toolParamsForChat;
    const modelName = _runDisplayName(run, tp);
    const errorMessage = run.error ?? "";
    const { code, message } = _splitRunError(errorMessage);
    // i18n template already exists in en/zh-CN/zh-TW (`appBuilder.errorPromptTemplate`).
    const tpl = t("appBuilder.errorPromptTemplate", {
      model: modelName,
      code,
      message,
    });
    if (typeof tpl === "string" && tpl !== "") return tpl;
    // Fallback (V1 line 565): `[{name}] failed: {code}`
    return `[${modelName}] failed: ${code}`;
  }

  function sendToChat(prompt?: string): string | null {
    const tab = tabs.activeTab;
    if (tab === null) return null;
    const run = ab.currentRun;

    // 1) ensure the tab is in app-builder mode + carries the selection so
    //    the backend SKILL branch fires on the next turn.
    applyToolParams();

    // 2) decide branch — error vs success — based on run.error/status.
    //    V1 (app.js 564) keys off `payload.type === 'app-builder-error'`,
    //    which DynamicOutput sets when `r.status === 'failed'` and `r.error`
    //    is non-null. Mirror that here.
    const isError =
      run !== null &&
      run.status === "failed" &&
      (run.error ?? null) !== null &&
      run.error !== "";

    let content = "";
    let attachments: BridgeAttachment[] = [];

    if (isError && run !== null) {
      // Error branch — no attachments, fixed prompt template (V1 app.js:564).
      content = _buildErrorText(run);
    } else if (run !== null) {
      // Success branch — derive attachments from run.output.
      attachments = _buildAttachmentsFromOutput(run.output);
      const effectivePrompt = prompt ?? ab.sendToChatPrompt;
      content = _buildSuccessText(run, effectivePrompt, attachments);
      // Image attachments are forwarded as real chat-pending Files in step 3
      // below (V1 parity); no longer inlined as text URLs.
    } else {
      // No run yet → fall back to the legacy compose path (prompt only).
      const effectivePrompt = prompt ?? ab.sendToChatPrompt;
      content = ab.composeSendToChatMessage(effectivePrompt);
    }

    if (content.trim() === "") return null;

    // 3) Dispatch the turn through the chat composer (V1 app.js:600-625:
    //    `inputText = text; nextTick(() => sendMessage())`). We do NOT
    //    `appendMessage` here — that only added a display row without ever
    //    triggering generation (the V2 regression this fixes). Instead we hand
    //    the composed text to the composer via the `usePendingChatSubmit`
    //    intent queue, which runs the composer's normal submit (image upload +
    //    `pushUserMessage` + `transport.send`) so the LLM actually replies.
    //
    //    Image attachments are forwarded as real chat-pending Files first (V1
    //    `chatComposable.addImageFile` in app.js:613-615), then — only after
    //    they settle into the composer's pending-images queue — we enqueue the
    //    submit, so the composer uploads them in the same turn (V1 awaited the
    //    image promises before `sendMessage()`, app.js:622-624).
    const imageAtts = attachments.filter((a) => a.kind === "image");
    if (imageAtts.length > 0) {
      void Promise.allSettled(imageAtts.map((a) => _attachmentToFile(a))).then(
        (settled) => {
          for (const res of settled) {
            if (res.status === "fulfilled" && res.value !== null) {
              enqueuePendingImage(res.value);
            }
          }
          enqueueChatSubmit(content);
        },
      );
    } else {
      enqueueChatSubmit(content);
    }

    // 4) Collapse the workbench so the user sees the chat stream
    //    (V1 line 627: `abWorkbenchCollapsed.value = true`).
    workbenchOpen.value = false;

    return content;
  }

  return { sendToChat, applyToolParams };
}

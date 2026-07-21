// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `usePromptSnapshot` — prompt-snapshot modal state + lifecycle for the
 * chat message list (V1 `useForgeConfig.js:20-127`).
 *
 * Extracted from `ChatMessageList.vue` (F4 cohesion split). Owns the modal
 * state and every operation on it (open / close / per-message collapse /
 * expand-all / collapse-all / copy / text+preview formatting) plus the
 * Escape-to-close global key handler. The rendering of the modal lives in
 * `components/chat/PromptSnapshotPanel.vue`; this composable is the single
 * source of truth for its state and is shared into the panel as props.
 *
 * Per-call (not a singleton): the modal is owned by whichever message list
 * instance hosts it, mirroring V1 where the snapshot state lived on the
 * single chat view.
 */
import { ref, onMounted, onBeforeUnmount, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { apiJson } from "@/api";
import { useToast } from "@/composables/useToast";

export interface PromptSnapshotMsg {
  role: string;
  content: unknown;
}

/**
 * Non-message wire fields captured alongside the messages (backend
 * `request_options`): the resolved tool schemas, `tool_choice`, sampling
 * params and (local turns) `session_id` — i.e. the rest of the REAL request
 * sent to the model, not just the messages. All fields optional because they
 * vary by model type (cloud vs local) and mode (translate sends no tools).
 */
export interface PromptSnapshotRequestOptions {
  tools?: unknown[];
  tool_choice?: unknown;
  sampling?: Record<string, unknown>;
  session_id?: string;
}

/** One tool parsed for human-readable display: name + description + the list
 *  of its parameter names (with a `required` flag). The raw OpenAI schema is
 *  kept for the "view raw" fallback so no information is lost. */
export interface PromptSnapshotToolView {
  name: string;
  description: string;
  params: { name: string; required: boolean }[];
  raw: string;
}

/** Parse a `request_options.tools` array (OpenAI function-calling schemas) into
 *  display-friendly tool views, preserving the wire ORDER (the order the model
 *  actually receives the tools). Unknown / malformed entries are tolerated. */
export function parsePromptSnapshotTools(
  tools: unknown[] | undefined,
): PromptSnapshotToolView[] {
  if (!Array.isArray(tools)) return [];
  const out: PromptSnapshotToolView[] = [];
  for (const entry of tools) {
    if (entry === null || typeof entry !== "object") continue;
    const fn = (entry as Record<string, unknown>)["function"];
    const fnObj =
      fn !== null && typeof fn === "object"
        ? (fn as Record<string, unknown>)
        : (entry as Record<string, unknown>);
    const name = typeof fnObj["name"] === "string" ? fnObj["name"] : "(unnamed)";
    const description =
      typeof fnObj["description"] === "string" ? fnObj["description"] : "";
    const params: { name: string; required: boolean }[] = [];
    const parameters = fnObj["parameters"];
    if (parameters !== null && typeof parameters === "object") {
      const p = parameters as Record<string, unknown>;
      const required = Array.isArray(p["required"])
        ? (p["required"] as unknown[]).filter(
            (x): x is string => typeof x === "string",
          )
        : [];
      const props = p["properties"];
      if (props !== null && typeof props === "object") {
        for (const key of Object.keys(props as Record<string, unknown>)) {
          params.push({ name: key, required: required.includes(key) });
        }
      }
    }
    out.push({
      name,
      description,
      params,
      raw: JSON.stringify(entry, null, 2),
    });
  }
  return out;
}

/** Flatten `request_options.sampling` (+ tool_choice / session_id) into an
 *  ordered key/value list for a compact display table. */
export function parsePromptSnapshotParams(
  opts: PromptSnapshotRequestOptions | null,
): { key: string; value: string }[] {
  if (opts === null) return [];
  const rows: { key: string; value: string }[] = [];
  const sampling = opts.sampling;
  if (sampling !== undefined && sampling !== null) {
    for (const key of Object.keys(sampling)) {
      rows.push({ key, value: stringifyValue(sampling[key]) });
    }
  }
  if (opts.tool_choice !== undefined) {
    rows.push({ key: "tool_choice", value: stringifyValue(opts.tool_choice) });
  }
  if (typeof opts.session_id === "string" && opts.session_id !== "") {
    rows.push({ key: "session_id", value: opts.session_id });
  }
  return rows;
}

function stringifyValue(v: unknown): string {
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

export interface PromptSnapshotState {
  visible: boolean;
  loading: boolean;
  error: string | null;
  snapshotError: string | null;
  requestId: string | null;
  modelId: string;
  timeStr: string;
  messages: PromptSnapshotMsg[];
  collapsed: Record<number, boolean>;
  requestOptions: PromptSnapshotRequestOptions | null;
}

export interface UsePromptSnapshot {
  promptSnapshotModal: Ref<PromptSnapshotState>;
  openPromptSnapshot: (requestId: string) => Promise<void>;
  closePromptSnapshot: () => void;
  togglePromptMsg: (idx: number) => void;
  expandAllPromptMsgs: () => void;
  collapseAllPromptMsgs: () => void;
  promptMsgText: (msg: PromptSnapshotMsg) => string;
  promptMsgPreview: (msg: PromptSnapshotMsg) => string;
  copyPromptSnapshot: () => Promise<void>;
  copyPromptMsg: (idx: number) => Promise<void>;
}

export function usePromptSnapshot(
  onSnapshotExpired?: (requestId: string) => void,
): UsePromptSnapshot {
  const { t } = useI18n();
  const toast = useToast();

  const promptSnapshotModal = ref<PromptSnapshotState>({
    visible: false,
    loading: false,
    error: null,
    snapshotError: null,
    requestId: null,
    modelId: "",
    timeStr: "",
    messages: [],
    collapsed: {},
    requestOptions: null,
  });

  async function openPromptSnapshot(requestId: string): Promise<void> {
    promptSnapshotModal.value = {
      visible: true,
      loading: true,
      error: null,
      snapshotError: null,
      requestId,
      modelId: "",
      timeStr: "",
      messages: [],
      collapsed: {},
      requestOptions: null,
    };
    try {
      const data = await apiJson<{
        request_id: string;
        model_id?: string;
        timestamp?: string;
        snapshot_error?: string | null;
        messages?: PromptSnapshotMsg[];
        request_options?: PromptSnapshotRequestOptions | null;
      }>("GET", `/api/prompt-snapshot/${requestId}`);
      // Filter out empty assistant messages (tool-call rounds with no text).
      const msgs = (data.messages ?? []).filter(
        (m) => !(m.role === "assistant" && !String(m.content ?? "").trim()),
      );
      // System messages default to collapsed (usually long); others expanded.
      const collapsed: Record<number, boolean> = {};
      msgs.forEach((m, i) => {
        collapsed[i] = m.role === "system";
      });
      promptSnapshotModal.value = {
        visible: true,
        loading: false,
        error: null,
        snapshotError: data.snapshot_error ?? null,
        requestId,
        modelId: data.model_id ?? "",
        timeStr: data.timestamp
          ? new Date(data.timestamp).toLocaleTimeString()
          : "",
        messages: msgs,
        collapsed,
        requestOptions: data.request_options ?? null,
      };
    } catch (e: unknown) {
      // 404 = snapshot expired (service restarted; snapshots live in memory
      // only). V1 parity (useForgeConfig.js:64-76): besides closing the
      // (just-opened) dialog, NOTIFY the host so it can drop the dead
      // request_id from the owning message — otherwise the 📄 button keeps
      // showing (it gates on request_id) and every click re-opens→404→closes,
      // i.e. "flashes once then vanishes" on every click forever. Clearing the
      // id makes the button permanently disappear (§State-Truth-First: the
      // button must reflect whether the snapshot REALLY still exists).
      const status = (e as Record<string, unknown>)?.["status"];
      if (status === 404) {
        promptSnapshotModal.value.visible = false;
        if (onSnapshotExpired !== undefined) {
          onSnapshotExpired(requestId);
        }
        return;
      }
      promptSnapshotModal.value.loading = false;
      promptSnapshotModal.value.error =
        e instanceof Error
          ? e.message
          : t("forgeConfig.snapshotLoadFailed", { msg: String(e) });
    }
  }

  function closePromptSnapshot(): void {
    promptSnapshotModal.value.visible = false;
  }

  function togglePromptMsg(idx: number): void {
    promptSnapshotModal.value.collapsed[idx] =
      !promptSnapshotModal.value.collapsed[idx];
  }

  function expandAllPromptMsgs(): void {
    const c: Record<number, boolean> = {};
    promptSnapshotModal.value.messages.forEach((_, i) => {
      c[i] = false;
    });
    promptSnapshotModal.value.collapsed = c;
  }

  function collapseAllPromptMsgs(): void {
    const c: Record<number, boolean> = {};
    promptSnapshotModal.value.messages.forEach((_, i) => {
      c[i] = true;
    });
    promptSnapshotModal.value.collapsed = c;
  }

  function promptMsgText(msg: PromptSnapshotMsg): string {
    const c = msg.content;
    if (typeof c === "string") return c;
    return JSON.stringify(c, null, 2);
  }

  function promptMsgPreview(msg: PromptSnapshotMsg): string {
    const text = promptMsgText(msg);
    const first = text.replace(/\s+/g, " ").trim();
    return first.length > 80 ? first.slice(0, 80) + "…" : first;
  }

  async function copyPromptSnapshot(): Promise<void> {
    const lines = promptSnapshotModal.value.messages.map(
      (m) => `### [${m.role}]\n${promptMsgText(m)}`,
    );
    // Copy MUST mirror exactly what the dialog SHOWS: the header + the message
    // blocks. The dialog renders `request_options` as a structured tools /
    // sampling view (PromptSnapshotPanel.vue) and NEVER as a raw JSON block, so
    // the copy must not append a `[request_options]` JSON dump either — doing
    // so made the clipboard diverge from the on-screen content (extra JSON the
    // user never saw). The real wire tools/sampling still live in the snapshot
    // and are visible in the dialog's request-options section; they are simply
    // not part of the copied text, keeping copy ≡ display.
    const text = `# Prompt Snapshot\nModel: ${promptSnapshotModal.value.modelId}\nTime: ${promptSnapshotModal.value.timeStr}\n\n${lines.join("\n\n---\n\n")}`;
    try {
      await navigator.clipboard.writeText(text);
      // V1 parity (useForgeConfig.js:122-123): success toast on copy. V2 had
      // dropped the feedback (silent copy) — restore it so the user gets the
      // "已复制到剪贴板" confirmation like V1.
      toast.success(t("forgeConfig.copiedToClipboard"));
    } catch {
      toast.error(t("forgeConfig.copyFailed"));
    }
  }

  // V1 parity (useForgeConfig.js:129-139 copyPromptMsg): each message card has
  // its own copy button that copies just that message's full text. V2 had
  // dropped this (only the whole-snapshot copy remained) — restore it so the
  // per-message copy behaviour matches V1.
  async function copyPromptMsg(idx: number): Promise<void> {
    const m = promptSnapshotModal.value.messages[idx];
    if (m === undefined) return;
    try {
      await navigator.clipboard.writeText(promptMsgText(m));
      // V1 parity (useForgeConfig.js:135): "已复制 #{n} ({role})" toast.
      toast.success(t("forgeConfig.msgCopied", { n: idx + 1, role: m.role }));
    } catch {
      toast.error(t("forgeConfig.copyFailed"));
    }
  }

  // V1 parity (app.js:2291-2294): Escape closes the prompt-snapshot modal.
  function onGlobalKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape" && promptSnapshotModal.value.visible) {
      event.stopPropagation();
      closePromptSnapshot();
    }
  }
  onMounted(() => {
    window.addEventListener("keydown", onGlobalKeydown);
  });
  onBeforeUnmount(() => {
    window.removeEventListener("keydown", onGlobalKeydown);
  });

  return {
    promptSnapshotModal,
    openPromptSnapshot,
    closePromptSnapshot,
    togglePromptMsg,
    expandAllPromptMsgs,
    collapseAllPromptMsgs,
    promptMsgText,
    promptMsgPreview,
    copyPromptSnapshot,
    copyPromptMsg,
  };
}

// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useComposerSubmit` — chat-composer send / stop / keyboard gating
 * (ARCH-1 cohesion split, extracted verbatim from `ChatComposer.vue`).
 *
 * Owns the submit lifecycle and its gating, with ZERO behaviour change:
 *   - `canSubmit` / `showStop` — CC/OC vs normal-chat submit/stop gating
 *     (V1 app.js:3227).
 *   - `inputPlaceholder` / `footerHintText` — placeholder + footer hint
 *     text that reflect the streaming-here vs idle state (V1
 *     index.html:1461 / 2230).
 *   - `onSubmit` — CC/OC routing + image upload + `submit` emit
 *     (V1 app.js:3250-3308).
 *   - `onStop` — CC/OC interrupt or `cancel` emit (V1 stopStreaming:3236).
 *   - `onKeydown` / `tryEnqueueWhileStreaming` — Enter submits or enqueues
 *     while streaming (V1 useChat.js:2809-2835 `handleEnter`).
 *
 * The composer passes in its own `text` ref, the auto-resize callback, the
 * pending-image uploader, the shared CC/OC composables, and the parent
 * `emit`, so this composable stays free of template / DOM coupling.
 */
import { computed, nextTick, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore, MAX_QUEUE_SIZE } from "@/stores/chatTabs";
import { useToast } from "@/composables/useToast";
import { useChatControlChannel } from "@/composables/chat/useChatControlChannel";
import type { useClaudeCode } from "@/composables/useClaudeCode";
import type { useOpenCode } from "@/composables/useOpenCode";

type ClaudeCode = ReturnType<typeof useClaudeCode>;
type OpenCode = ReturnType<typeof useOpenCode>;

export interface ComposerSubmitEmit {
  (e: "submit", prompt: string): void;
  (e: "cancel"): void;
}

export function useComposerSubmit(opts: {
  text: Ref<string>;
  autoResize: () => void;
  uploadPendingImages: () => Promise<string>;
  claudeCode: ClaudeCode;
  openCode: OpenCode;
  emit: ComposerSubmitEmit;
  /**
   * Optional side-channel invoked with the trimmed, plain-text prompt every
   * time a turn is actually sent (normal chat, CC, OC, and the programmatic
   * App-Builder "Send to Chat" path all flow through `onSubmit`). Used by the
   * composer to record prompt history WITHOUT this composable having to depend
   * on the history store — keeps the submit lifecycle decoupled and testable.
   */
  onSent?: (prompt: string) => void;
}) {
  const {
    text,
    autoResize,
    uploadPendingImages,
    claudeCode,
    openCode,
    emit,
    onSent,
  } = opts;
  const { t } = useI18n();
  const store = useChatTabsStore();
  const toast = useToast();

  const activeTab = computed(() => store.activeTab);
  const status = computed(() => activeTab.value?.status ?? "idle");

  const canSubmit = computed(() => {
    // CC/OC mode (V1 叠加式): submit gates on an active coding session + not
    // already streaming there, NOT on the chat-tab status (V1 app.js:3227).
    if (claudeCode.isCCMode.value) {
      return (
        claudeCode.activeSessionId.value !== null &&
        !claudeCode.streaming.value &&
        text.value.trim().length > 0
      );
    }
    if (openCode.isOCMode.value) {
      return (
        openCode.activeSessionId.value !== null &&
        !openCode.streaming.value &&
        text.value.trim().length > 0
      );
    }
    if (activeTab.value === null) return false;
    if (status.value !== "idle" && status.value !== "error") return false;
    return text.value.trim().length > 0;
  });

  const showStop = computed(() => {
    // CC/OC mode: stop button mirrors the coding-session stream (V1
    // app.js:3227 isStreamingHere). Falls back to chat-tab status otherwise.
    if (claudeCode.isCCMode.value) return claudeCode.streaming.value;
    if (openCode.isOCMode.value) return openCode.streaming.value;
    return status.value === "streaming" || status.value === "aborting";
  });

  // V1 parity (index.html:1461) — placeholder reflects the input state.
  // V1 has three states (blocked-by-other-session / streaming / normal);
  // the V2 single-active-tab model has no cross-session "blocked" state,
  // so we expose the two that apply: streaming-here vs idle. Text matches
  // V1's `input.placeholder` / `input.streamingPlaceholder`.
  const inputPlaceholder = computed<string>(() =>
    showStop.value ? t("input.streamingPlaceholder") : t("input.placeholder"),
  );

  // V1 parity (index.html:2230) — footer hint text when the input is EMPTY
  // and voice is idle. Three V1 states collapse to two in the V2
  // single-active-tab model (no cross-session "blocked" state):
  //   - streaming-here → "Enter to add to queue" (D6, pairs with the queue)
  //   - idle           → "Enter to send · Shift+Enter for newline"
  // The char-count branch (D5) and voice-status branch (D7) are handled in
  // the template; this computed only covers the empty-input idle/streaming
  // case so the template stays declarative.
  const footerHintText = computed<string>(() =>
    showStop.value ? t("index.enterToQueue") : t("input.enterSendHint"),
  );

  async function onSubmit(): Promise<void> {
    if (!canSubmit.value) return;
    const trimmed = text.value.trim();
    if (trimmed === "") return;
    // CC/OC mode (V1 叠加式 app.js:3250-3308): route the message to the active
    // coding session via the shared core instead of the normal chat transport.
    // Images are not forwarded to OC (V1 line 3298: OC send has no image arg).
    if (claudeCode.isCCMode.value) {
      text.value = "";
      void nextTick(() => autoResize());
      onSent?.(trimmed);
      await claudeCode.sendMessage(trimmed);
      return;
    }
    if (openCode.isOCMode.value) {
      text.value = "";
      void nextTick(() => autoResize());
      onSent?.(trimmed);
      await openCode.sendMessage(trimmed);
      return;
    }
    // Upload any pending images first; failures keep their thumbnails
    // red but do NOT block the text send (matches V1 behaviour).
    const imagePrefix = await uploadPendingImages();
    emit("submit", `${imagePrefix}${trimmed}`);
    onSent?.(trimmed);
    text.value = "";
    // Collapse the textarea back to one row after sending.
    void nextTick(() => autoResize());
  }

  function onStop(): void {
    // CC/OC mode: interrupt the coding-session stream (V1 stopStreaming:3236).
    if (claudeCode.isCCMode.value) {
      void claudeCode.interrupt();
      return;
    }
    if (openCode.isOCMode.value) {
      void openCode.interrupt();
      return;
    }
    emit("cancel");
  }

  function onKeydown(ev: KeyboardEvent): void {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      // V1 parity (useChat.js:2809-2835 `handleEnter`): while the current
      // chat tab is streaming, Enter ENQUEUES the input instead of being
      // ignored — the queued messages auto-send when the turn finishes
      // (ChatView's streaming→idle watcher). CC/OC modes keep their own
      // session gating (canSubmit) and have no queue, so they fall through
      // to the normal submit path below.
      //
      // `tryEnqueueWhileStreaming` is async (it uploads any attached images at
      // enqueue time). We must NOT fall through to `onSubmit` synchronously
      // while it is still deciding/awaiting, or a streaming-tab Enter would
      // both enqueue AND submit. Chain off the resolved boolean: only submit
      // when the keypress was NOT consumed by the queue (idle tab).
      void tryEnqueueWhileStreaming().then((consumed) => {
        if (!consumed) {
          void onSubmit();
        }
      });
    }
  }

  /**
   * Enqueue an explicit prompt onto the active tab's message queue WITH the
   * same user feedback as the manual Enter path: `queued` → success toast,
   * `full` → warning toast (never a silent drop). Returns the store result so
   * callers can branch further if needed.
   *
   * Used by both the manual Enter-while-streaming path
   * (`tryEnqueueWhileStreaming`, which passes the textarea text) and the
   * programmatic App-Builder "Send to Chat" drain (`ChatComposer
   * .drainExternalSubmit`, which passes the composed result message) so the two
   * share ONE enqueue+toast implementation (no duplicated toast / i18n /
   * MAX_QUEUE_SIZE logic). Does NOT touch the textarea `text` ref — the manual
   * path clears it itself after a successful queue.
   */
  function enqueueWithFeedback(
    prompt: string,
    imagePrefix = "",
  ): "queued" | "full" | "empty" | "no-tab" {
    const tab = activeTab.value;
    if (tab === null) return "no-tab";
    const result = store.enqueueMessage(tab.id, prompt, imagePrefix);
    if (result === "full") {
      toast.warning(t("chat.queueFull", { max: MAX_QUEUE_SIZE }));
    } else if (result === "queued") {
      toast.success(t("chat.queueAdded"));
    }
    return result;
  }

  /**
   * If the active chat tab is currently streaming (normal chat mode, NOT
   * CC/OC), enqueue the trimmed input and clear the textarea. Returns true
   * when the keypress was consumed by the queue (caller must NOT also
   * submit). Mirrors V1 `handleEnter`'s enqueue branch (useChat.js:2820).
   *
   * Image parity (text + image both queue): any pending images are uploaded
   * HERE — at enqueue time — via the SAME `uploadPendingImages()` a normal
   * submit uses, and the resulting `![name](url)` markdown prefix is stored on
   * the queue item. `uploadPendingImages()` already returns "" when there are
   * no images and removes successfully-uploaded ones from the composer's
   * pending list (so they are not re-attached to the NEXT message — exactly
   * like a normal submit). Uploading at enqueue time (not at re-send) matches
   * the normal-submit ordering. The re-send (`ChatView.resendQueuedItem`)
   * recombines `imagePrefix + text` so the dequeued turn resolves the image to
   * vision blocks exactly like a fresh image submit (WS/SSE
   * `_extract_image_refs`). An image-only message (blank text + an image) is
   * allowed; a fully-empty Enter is consumed but enqueues nothing.
   */
  async function tryEnqueueWhileStreaming(): Promise<boolean> {
    // CC/OC streaming is governed by their own composables and has no
    // message queue in V1 — let those modes use the normal submit gate.
    if (claudeCode.isCCMode.value || openCode.isOCMode.value) return false;
    const tab = activeTab.value;
    if (tab === null) return false;
    // Only queue while THIS tab is actively streaming / aborting.
    if (tab.status !== "streaming" && tab.status !== "aborting") return false;
    const trimmed = text.value.trim();
    // Upload any pending images first (same as a normal submit). Returns "" +
    // is a no-op when there are no images; self-clears uploaded ones.
    const imagePrefix = await uploadPendingImages();
    // Nothing to queue: blank text AND no image uploaded → consume the Enter.
    if (trimmed === "" && imagePrefix === "") return true;
    // Shared enqueue + toast feedback (queued→success / full→warning).
    const result = enqueueWithFeedback(trimmed, imagePrefix);
    if (result === "queued") {
      text.value = "";
      void nextTick(() => autoResize());
    }
    return true;
  }

  // ── Mid-turn user injection (V2 enhancement — the "inject" button) ───────
  // Distinct from the Enter queue: an injection is folded into the SAME
  // in-flight run at the inter-round seam (between tool rounds), rather than
  // sent as a fresh turn after the current one ends. It is only meaningful in
  // normal chat mode while THIS tab is streaming (CC/OC have their own session
  // model and no agentic tool loop to inject between).
  const canInject = computed<boolean>(() => {
    if (claudeCode.isCCMode.value || openCode.isOCMode.value) return false;
    const tab = activeTab.value;
    if (tab === null) return false;
    if (tab.status !== "streaming" && tab.status !== "aborting") return false;
    return text.value.trim().length > 0;
  });

  /**
   * Append `extra` to the current composer draft for re-editing (the queue
   * bubble's ✎ "edit" affordance, F6). Appends after the existing draft with a
   * newline separator (never overwrites what the user is already typing —
   * AGENTS.md confirmed "追加在现有草稿后"), then re-runs auto-resize and
   * focuses the textarea end via the caller's `nextTick`. No-op for blank
   * `extra`.
   */
  function appendToDraft(extra: string): void {
    if (extra === "") return;
    const cur = text.value;
    text.value = cur === "" ? extra : `${cur}\n${extra}`;
    void nextTick(() => autoResize());
  }

  /**
   * Submit the current input as a mid-turn injection (the "inject" button).
   *
   * Control-plane ONLY (user decision 2026-06-24): the text is folded into the
   * SAME in-flight run via the control WebSocket → the backend's inter-round
   * seam → an `injected_message` data frame that commits the bubble. It is
   * NEVER written into the pending send-queue (which would degrade it to the
   * Enter-queue's "send a fresh turn after this one ends" behaviour — the
   * reported bug).
   *
   * Flow:
   *   1. Insert an optimistic grey / pending `role:user` bubble straight into
   *      the conversation (NOT the queue) so the user sees their text fold in
   *      immediately.
   *   2. Ensure the control WS is `ready` (await the handshake; the channel is
   *      normally pre-opened when the turn started, so this resolves at once).
   *   3. Send the `inject` control frame. On success clear the textarea and let
   *      the backend `injected_message` frame RECONCILE the pending bubble into
   *      a committed one.
   *   4. On failure (control WS genuinely unavailable) remove the optimistic
   *      bubble and surface an error toast — no silent queue fallback.
   *
   * Returns true when the input was consumed (so a caller wiring this to a key
   * need not also submit).
   */
  async function injectWhileStreaming(): Promise<boolean> {
    if (!canInject.value) return false;
    const tab = activeTab.value;
    if (tab === null) return false;
    const trimmed = text.value.trim();
    if (trimmed === "") return false;
    // Image parity: upload any pending images FIRST (the SAME upload口径 as a
    // normal submit / the Enter-queue) and inline them as `![name](url)`
    // markdown PREFIX on the injection text — identical to how a normal submit
    // carries images. The backend extracts these refs from the text at the
    // inter-round seam and resolves them to vision blocks (so the model sees
    // the image); the optimistic + committed bubbles render the markdown image.
    // Because the SAME `imagePrefix + trimmed` string is used for the bubble
    // content, the `inject` frame text, AND the backend's `injected_message`
    // reconciliation key, the pending bubble pairs cleanly with the committed
    // one (no duplicate). `uploadPendingImages` returns "" + is a no-op when
    // there are no images; it self-clears uploaded ones.
    const imagePrefix = await uploadPendingImages();
    const injectText = `${imagePrefix}${trimmed}`;
    // 1. Optimistic grey bubble in the CONVERSATION (never the queue panel).
    const inserted = store.insertPendingInjection(tab.id, injectText);
    if (inserted.result !== "inserted") {
      // "empty" / "no-tab": nothing recorded — leave the input untouched.
      return true;
    }
    // Clear the textarea up-front (the bubble now carries the text); restored
    // on a hard failure below so the user can retry without retyping.
    text.value = "";
    void nextTick(() => autoResize());
    // 2 + 3. Ensure the control channel is connected, then send the frame.
    // The channel is normally pre-opened at turn start (useChatTransport.send),
    // so `whenReady` resolves immediately; the short timeout covers a
    // reconnect / initial-load race.
    const channel = useChatControlChannel();
    const ready = await channel.whenReady(2000);
    const sent = ready && channel.sendInject(tab.id, injectText);
    if (!sent) {
      // 4. Control plane unavailable — roll back the optimistic bubble, restore
      // the draft, and tell the user (NO queue fallback — user decision).
      // Restore the FULL `injectText` (image markdown prefix + text), NOT just
      // the typed text: the image was already uploaded (and its thumbnail
      // consumed from the composer), so dropping the markdown here would lose
      // the image entirely. The uploaded `/api/images/files/..` URL stays
      // valid, so the restored markdown re-renders + re-resolves on the user's
      // next send/inject — nothing is lost on a failed inject.
      store.removePendingInjection(tab.id, inserted.id);
      text.value = injectText;
      void nextTick(() => autoResize());
      toast.error(
        t(
          "chat.injectFailed",
          "注入失败：控制连接不可用，请重试",
        ),
      );
    }
    return true;
  }

  return {
    activeTab,
    status,
    canSubmit,
    showStop,
    inputPlaceholder,
    footerHintText,
    onSubmit,
    onStop,
    onKeydown,
    enqueueWithFeedback,
    canInject,
    injectWhileStreaming,
    appendToDraft,
  };
}

// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `usePendingImages` — chat-composer image attachment state + lifecycle
 * (V1 `useChat.js:194-225` handlePaste + `app.js:604-619` addImageFile).
 *
 * Extracted from `ChatComposer.vue` (F1① cohesion split). Owns the
 * per-composer pending-image queue and every operation that mutates it:
 *   - `ingestImageFile` / `onFilesSelected` — file-picker intake
 *   - `handlePaste`                          — clipboard image paste
 *   - `drainExternalImages`                  — App Builder → Chat bridge
 *     forwarding (consumes `usePendingChatImages` intent queue)
 *   - `uploadPendingImages`                  — POST each image, return the
 *     markdown snippet to prepend to the outgoing prompt
 *
 * This is a per-call composable (NOT a module singleton): each
 * `ChatComposer` instance gets its own `pendingImages` ref, exactly as
 * V1's local `pendingImages` lived inside the composer. The cross-
 * component intake bridge (`usePendingChatImages`) stays the sole shared
 * channel; this composable just integrates its drained files into the
 * local queue, preserving the single-owner model documented there.
 *
 * The conversation-title seed (used when lazily creating a conversation
 * to upload into) is supplied via the `text` ref so the composable does
 * not need to reach back into the composer for input state.
 */
import { ref, onMounted, watch, type Ref } from "vue";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useI18n } from "vue-i18n";
import {
  pendingFileIntake,
  drainPendingImages,
} from "@/composables/chat/usePendingChatImages";
import { apiJson, ApiError } from "@/api";

export interface PendingImage {
  id: string;
  name: string;
  mime: string;
  /** raw base64 (no data: prefix) for upload payload */
  b64: string;
  /** data URL for thumbnail preview */
  dataUrl: string;
  /** failed flag — surfaces a red border but does not block text send */
  failed?: boolean;
}

interface UploadedImageResponse {
  url?: string;
  id?: string;
  [key: string]: unknown;
}

export interface UsePendingImages {
  pendingImages: Ref<PendingImage[]>;
  openFilePicker: () => void;
  ingestImageFile: (file: File) => Promise<void>;
  onFilesSelected: (ev: Event) => Promise<void>;
  removePendingImage: (id: string) => void;
  handlePaste: (event: ClipboardEvent) => void;
  uploadPendingImages: () => Promise<string>;
}

function genLocalId(): string {
  return `img-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("FileReader failed"));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("Unexpected non-string FileReader result"));
        return;
      }
      resolve(result);
    };
    reader.readAsDataURL(file);
  });
}

/**
 * @param textRef        The composer textarea value, used as the seed
 *                       title when a conversation must be lazily created
 *                       before uploading (V1 used the prompt's first 80
 *                       chars).
 * @param fileInputRef   Ref to the hidden `<input type=file>` element so
 *                       `openFilePicker` can trigger the native picker and
 *                       reset its value after selection.
 */
export function usePendingImages(
  textRef: Ref<string>,
  fileInputRef: Ref<HTMLInputElement | null>,
): UsePendingImages {
  const store = useChatTabsStore();
  const { t } = useI18n();
  const pendingImages = ref<PendingImage[]>([]);

  function openFilePicker(): void {
    const el = fileInputRef.value;
    if (el) el.click();
  }

  /**
   * Convert one File into a `PendingImage` and push it into the pending
   * list. Shared between the user-driven file picker (`onFilesSelected`)
   * and the cross-component intake queue (`pendingFileIntake`, used by the
   * App Builder → Chat bridge to forward run-output images, V1
   * `app.js:604-619` parity).
   */
  async function ingestImageFile(file: File): Promise<void> {
    if (!file.type.startsWith("image/")) return;
    try {
      const dataUrl = await readAsDataUrl(file);
      const commaIdx = dataUrl.indexOf(",");
      const b64 = commaIdx >= 0 ? dataUrl.slice(commaIdx + 1) : "";
      pendingImages.value = [
        ...pendingImages.value,
        {
          id: genLocalId(),
          name: file.name,
          mime: file.type || "image/png",
          b64,
          dataUrl,
        },
      ];
    } catch {
      // skip unreadable file
    }
  }

  // Drain any files queued externally (e.g. App Builder forwarding
  // run-output images) when this composer mounts and on every queue change.
  async function drainExternalImages(): Promise<void> {
    const files = drainPendingImages();
    for (const f of files) await ingestImageFile(f);
  }
  onMounted(() => {
    void drainExternalImages();
  });
  watch(
    () => pendingFileIntake.value.length,
    (n) => {
      if (n > 0) void drainExternalImages();
    },
  );

  async function onFilesSelected(ev: Event): Promise<void> {
    const input = ev.target as HTMLInputElement | null;
    const files = input?.files;
    if (!files || files.length === 0) return;
    for (const file of Array.from(files)) {
      await ingestImageFile(file);
    }
    // reset so selecting the same file again still triggers `change`.
    if (input) input.value = "";
  }

  function removePendingImage(id: string): void {
    pendingImages.value = pendingImages.value.filter((p) => p.id !== id);
  }

  /**
   * Handle `paste` on the textarea (V1 `useChat.js:194-225` `handlePaste`).
   * When the clipboard contains an image (e.g. a screenshot or a copied
   * image), extract the first image item, ingest it as a pending image and
   * `preventDefault()` to stop the raw image bytes being pasted as garbled
   * text. Pure-text pastes fall through to the browser default. After an
   * image paste, restore textarea focus on the next frame (preventDefault
   * can drop it).
   */
  function handlePaste(event: ClipboardEvent): void {
    const items = event.clipboardData?.items;
    if (!items) return;

    let hasImage = false;
    for (const item of items) {
      if (item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) {
          event.preventDefault();
          void ingestImageFile(file);
          hasImage = true;
        }
        break;
      }
    }

    if (hasImage) {
      requestAnimationFrame(() => {
        const el = event.target as HTMLTextAreaElement | null;
        if (el && !el.disabled && document.activeElement !== el) {
          el.focus();
        }
      });
    }
  }

  async function ensureConversationId(): Promise<string | null> {
    const tab = store.activeTab;
    if (!tab) return null;
    if (tab.conversationId !== null && tab.conversationId !== "") {
      return tab.conversationId;
    }
    try {
      const res = await apiJson<{ id: string }>(
        "POST",
        "/api/chat/conversations",
        { title: textRef.value.slice(0, 80) || t("chat.tab.untitled") },
      );
      store.setConversationId(tab.id, res.id);
      return res.id;
    } catch (err) {
      void (err instanceof ApiError ? err.code : err);
      return null;
    }
  }

  /**
   * Upload all pending images ONCE, returning the structured uploaded refs
   * (the `/api/images/files/...` URL + display name for each success, in
   * order). Failed uploads keep their thumbnails red but do NOT block the
   * send; successful uploads are removed from the pending queue (so they are
   * not re-attached to the next message). `uploadPendingImages` builds the
   * markdown prefix on top of this; keeping the structured result in a single
   * helper means there is exactly ONE upload口径 (and a future raw-ref caller
   * can reuse it without duplicating the upload loop).
   */
  async function uploadAllPending(): Promise<{ url: string; name: string }[]> {
    if (pendingImages.value.length === 0) return [];
    const convId = await ensureConversationId();
    if (convId === null) {
      // Mark all as failed; don't block text send.
      pendingImages.value = pendingImages.value.map((p) => ({ ...p, failed: true }));
      return [];
    }
    const remaining: PendingImage[] = [];
    const uploaded: { url: string; name: string }[] = [];
    for (const img of pendingImages.value) {
      try {
        const res = await apiJson<UploadedImageResponse>(
          "POST",
          "/api/images/upload",
          {
            conv_id: convId,
            msg_id: img.id,
            b64_data: img.b64,
            mime_type: img.mime,
          },
        );
        const ref = typeof res.url === "string"
          ? res.url
          : typeof res.id === "string"
            ? res.id
            : "";
        if (ref !== "") {
          uploaded.push({ url: ref, name: img.name });
        }
      } catch (err) {
        void (err instanceof ApiError ? err.code : err);
        remaining.push({ ...img, failed: true });
      }
    }
    pendingImages.value = remaining;
    return uploaded;
  }

  /**
   * Upload all pending images, returning the markdown snippet to
   * prepend (or append) to the prompt. Failed uploads mark their
   * thumbnails red; successful uploads are removed from the queue.
   */
  async function uploadPendingImages(): Promise<string> {
    const uploaded = await uploadAllPending();
    if (uploaded.length === 0) return "";
    return uploaded.map((u) => `![${u.name}](${u.url})`).join("\n") + "\n";
  }

  return {
    pendingImages,
    openFilePicker,
    ingestImageFile,
    onFilesSelected,
    removePendingImage,
    handlePaste,
    uploadPendingImages,
  };
}

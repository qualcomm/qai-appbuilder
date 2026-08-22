// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * `useQuestionImages` — per-question pending-image queues for the in-conversation
 * ChatQuestionCard (V2 enhancement).
 *
 * The question card paginates across several questions; the user may attach
 * (paste / pick) images on ANY page, and each image must stay bound to the
 * question it was added on (Bug 1+2: the old single `usePendingImages` queue
 * merged every page's images and folded them all into whatever page was open
 * at submit time). This composable keeps an INDEPENDENT `PendingImage[]` per
 * question index and uploads each question's images separately at submit.
 *
 * Why NOT reuse `usePendingImages` per question (one instance per page):
 *   - `usePendingImages` runs `onMounted` + a watcher that DRAINS the global
 *     `pendingFileIntake` queue (the App Builder → Chat image bridge). Opening
 *     one instance PER question would make several owners race to consume that
 *     single global queue → images mis-routed / duplicated. The question card's
 *     images are added by the user inside the card and never come from that
 *     bridge, so this composable deliberately does NOT touch `pendingFileIntake`.
 *
 * The ingest (FileReader → base64) and upload (`POST /api/images/upload`,
 * lazily creating a conversation via `POST /api/chat/conversations`) mirror
 * `usePendingImages` so the wire contract is identical — only the per-question
 * keying differs. Returns the same `![name](url)` markdown the model expects.
 */
import { reactive, type Ref } from "vue";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useI18n } from "vue-i18n";
import { apiJson, ApiError } from "@/api";
import type { PendingImage } from "@/composables/chat/usePendingImages";

interface UploadedImageResponse {
  url?: string;
  id?: string;
  [key: string]: unknown;
}

export interface UseQuestionImages {
  /** Pending images for question index `qi` (lazily created, empty array). */
  imagesFor: (qi: number) => PendingImage[];
  /** Trigger the (shared) hidden file picker for question `qi`. */
  openFilePicker: (qi: number) => void;
  /** `<input type=file> @change` handler — ingests into question `qi`. */
  onFilesSelected: (qi: number, ev: Event) => Promise<void>;
  /** Remove one pending image from question `qi`'s queue. */
  removeImage: (qi: number, id: string) => void;
  /** `textarea @paste` handler — ingests a clipboard image into question `qi`. */
  handlePaste: (qi: number, event: ClipboardEvent) => void;
  /** Upload question `qi`'s images; returns the `![name](url)\n…` markdown
   *  (empty when none / all failed). Successful uploads leave the queue;
   *  failures stay marked `failed`. */
  uploadFor: (qi: number) => Promise<string>;
}

function genLocalId(): string {
  return `qimg-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Shared empty array for the read-only accessor of an unseeded question
 *  (avoids per-render allocation + avoids mutating reactive state in render). */
const EMPTY: PendingImage[] = [];

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
 * @param titleSeed    Seed title used when a conversation must be lazily
 *                     created before uploading (first 80 chars), mirroring
 *                     `usePendingImages.ensureConversationId`.
 * @param fileInputRef Shared hidden `<input type=file>`; `openFilePicker`
 *                     stamps the target question index on it (via the caller)
 *                     and clicks it.
 */
export function useQuestionImages(
  titleSeed: Ref<string>,
  fileInputRef: Ref<HTMLInputElement | null>,
): UseQuestionImages {
  const store = useChatTabsStore();
  const { t } = useI18n();
  // Per-question queues. `reactive` so template reads stay reactive.
  const queues = reactive<Record<number, PendingImage[]>>({});

  /** Read-only accessor (safe to call during render): never mutates `queues`,
   *  so it does not trigger Vue's "mutated during render" warning. */
  function imagesFor(qi: number): PendingImage[] {
    return queues[qi] ?? EMPTY;
  }

  /** Ensure + return the (writable) queue for `qi`, used by ingest/upload. */
  function ensureQueue(qi: number): PendingImage[] {
    if (queues[qi] === undefined) queues[qi] = [];
    return queues[qi]!;
  }

  async function ingestImageFile(qi: number, file: File): Promise<void> {
    if (!file.type.startsWith("image/")) return;
    try {
      const dataUrl = await readAsDataUrl(file);
      const commaIdx = dataUrl.indexOf(",");
      const b64 = commaIdx >= 0 ? dataUrl.slice(commaIdx + 1) : "";
      queues[qi] = [
        ...ensureQueue(qi),
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

  function openFilePicker(_qi: number): void {
    const el = fileInputRef.value;
    if (el) el.click();
  }

  async function onFilesSelected(qi: number, ev: Event): Promise<void> {
    const input = ev.target as HTMLInputElement | null;
    const files = input?.files;
    if (!files || files.length === 0) return;
    for (const file of Array.from(files)) {
      await ingestImageFile(qi, file);
    }
    // reset so selecting the same file again still triggers `change`.
    if (input) input.value = "";
  }

  function removeImage(qi: number, id: string): void {
    queues[qi] = ensureQueue(qi).filter((p) => p.id !== id);
  }

  function handlePaste(qi: number, event: ClipboardEvent): void {
    const items = event.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) {
          event.preventDefault();
          void ingestImageFile(qi, file);
        }
        break;
      }
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
        { title: titleSeed.value.slice(0, 80) || t("chat.tab.untitled") },
      );
      store.setConversationId(tab.id, res.id);
      return res.id;
    } catch (err) {
      void (err instanceof ApiError ? err.code : err);
      return null;
    }
  }

  async function uploadFor(qi: number): Promise<string> {
    const queue = imagesFor(qi);
    if (queue.length === 0) return "";
    const convId = await ensureConversationId();
    if (convId === null) {
      queues[qi] = queue.map((p) => ({ ...p, failed: true }));
      return "";
    }
    const remaining: PendingImage[] = [];
    const fragments: string[] = [];
    for (const img of queue) {
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
        const ref =
          typeof res.url === "string"
            ? res.url
            : typeof res.id === "string"
              ? res.id
              : "";
        if (ref !== "") {
          fragments.push(`![${img.name}](${ref})`);
        }
      } catch (err) {
        void (err instanceof ApiError ? err.code : err);
        remaining.push({ ...img, failed: true });
      }
    }
    queues[qi] = remaining;
    return fragments.length === 0 ? "" : fragments.join("\n") + "\n";
  }

  return {
    imagesFor,
    openFilePicker,
    onFilesSelected,
    removeImage,
    handlePaste,
    uploadFor,
  };
}

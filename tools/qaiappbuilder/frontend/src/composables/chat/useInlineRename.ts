// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useInlineRename — reusable "double-click to rename in place" state machine.
 *
 * Extracted from `ChatTabStrip.vue` (where a chat tab's title was renamed by
 * double-clicking, editing an inline `<input>`, then committing on Enter or
 * blur — with Escape to cancel and IME composition guards). Every other UI
 * surface that needs the same "click a label → edit inline → Enter/blur
 * commits / Escape cancels" pattern (sidebar row rename, sub-agent chip
 * rename in the future, etc.) can share this composable and get identical
 * semantics for free instead of copying the fiddly IME + trim + blur race
 * handling.
 *
 * What it owns (UI-interaction layer only):
 *   - Editing state: `renamingId` (which item is being renamed; `null` idle)
 *     + `draft` (v-model target for the inline `<input>`).
 *   - Lifecycle: `start(id, currentTitle)` opens the editor and seeds the
 *     draft; `commit()` fires the `onCommit` callback with the trimmed draft;
 *     `cancel()` closes the editor without committing.
 *   - Semantics: Enter → `commit()`; Escape → `cancel()`; blur → `commit()`.
 *     Empty-after-trim (default) or unchanged draft → `cancel()` instead of
 *     `commit()` (never persist a blank / no-op).
 *   - IME composition guard: while the user is mid-composition (Pinyin /
 *     Kana), Enter selects a candidate, NOT a commit. `onCompositionStart`
 *     / `onCompositionEnd` handlers toggle an internal `isComposing` flag
 *     that gates `commit()`.
 *   - Loading gate: while a previous `commit()` is still in flight
 *     (`isLoading=true`), further `commit()` calls are ignored and a second
 *     `start()` is a no-op. The consumer flips loading on/off via
 *     `beginLoading()` / `endLoading()` around its async persistence call.
 *
 * What it deliberately does NOT own (stays in the consuming component):
 *   - Persistence (`apiJson` PATCH, toast on failure, cross-store sync). The
 *     `onCommit(id, newTitle)` callback is where the consumer hooks its
 *     persistence chain — an in-place rename talks to the same PATCH endpoint
 *     regardless of surface, but the store fan-out is component-specific
 *     (ChatTabStrip touches `conversationsStore.rename` +
 *     `chatTabsStore.renameTabsByConversation`, a sidebar row might only
 *     touch the first). Keeping persistence in the consumer preserves the
 *     "composable never imports stores" boundary the rest of the chat
 *     composables observe.
 *   - Focus / selection of the inline `<input>` on `start()`. That needs the
 *     consumer's own template ref (a function-ref inside `v-for` collects
 *     into an array — see `ChatTabStrip.setEditInputRef`) and a `nextTick`
 *     tick before `.focus()/.select()` — trying to own that inside a
 *     composable would force every consumer to expose an internal ref back
 *     to us.
 *
 * Typical wiring (see `ChatTabStrip.vue` for the reference implementation):
 *
 * ```
 * const rename = useInlineRename<TabId>({
 *   onCommit: async (id, newTitle) => {
 *     rename.beginLoading();
 *     try {
 *       await apiJson("PATCH", `/api/chat/conversations/${convId}`, {
 *         title: newTitle,
 *       });
 *       // fan out to whatever stores mirror the title …
 *     } catch (err) {
 *       toast.error(t("chat.renameFailed"));
 *     } finally {
 *       rename.endLoading();
 *     }
 *   },
 * });
 * // template: @keydown.enter="rename.onEnter" @keydown.escape="rename.onEscape"
 * //           @blur="rename.onBlur" @compositionstart="rename.onCompositionStart"
 * //           @compositionend="rename.onCompositionEnd"
 * ```
 */
import { ref, type Ref } from "vue";

export interface UseInlineRenameOptions<TId> {
  /**
   * Called on a successful commit. Receives the edited item id and the
   * trimmed new title. The consumer performs persistence + store fan-out
   * here (see file header). The composable calls `cancel()` immediately
   * after invoking this callback — a follow-up rename requires a fresh
   * `start()`, matching the visual "editor closes on commit" contract.
   */
  onCommit: (id: TId, newTitle: string) => void | Promise<void>;
  /**
   * Optional hook fired when the editor closes without committing
   * (Escape / blur-with-empty / blur-with-no-change). Consumers usually
   * don't need this, but it's handy for a "reset dirty flag" cleanup.
   */
  onCancel?: () => void;
  /**
   * When `true` (default), an empty-after-trim draft is treated as a
   * cancellation instead of a commit — a blank title makes no sense as a
   * conversation label. Set to `false` for surfaces where empty is a
   * legitimate value.
   */
  trimEmpty?: boolean;
}

export interface UseInlineRenameReturn<TId> {
  /** Which item is currently being renamed; `null` when idle. */
  renamingId: Ref<TId | null>;
  /** The inline `<input>`'s v-model target. */
  draft: Ref<string>;
  /** True while a commit is in flight (see `beginLoading` / `endLoading`). */
  isLoading: Ref<boolean>;
  /** `true` iff `id` is the currently-editing item. */
  isRenaming: (id: TId) => boolean;
  /** Open the editor for `id`, seeding the draft with `currentTitle`. */
  start: (id: TId, currentTitle: string) => void;
  /**
   * Commit the current draft. No-op if we're not renaming, mid-IME
   * composition, mid-loading, or the trimmed draft is empty
   * (when `trimEmpty=true`) / unchanged from the seed.
   */
  commit: () => Promise<void>;
  /** Close the editor without committing. */
  cancel: () => void;
  /**
   * The consumer wraps its async persistence call between these two so
   * `isLoading` reflects the in-flight state and further `commit()` calls
   * are ignored while the previous is still resolving.
   */
  beginLoading: () => void;
  endLoading: () => void;

  // ── Convenience DOM handlers (consumer can wire these directly) ──────────
  /** `@keydown.enter="onEnter"` — commits (unless mid-IME). */
  onEnter: (ev: KeyboardEvent) => void;
  /** `@keydown.escape="onEscape"` — cancels. */
  onEscape: (ev: KeyboardEvent) => void;
  /** `@blur="onBlur"` — commits (unless mid-IME / already cancelled). */
  onBlur: () => void;
  /** `@compositionstart="onCompositionStart"` — opens the IME guard. */
  onCompositionStart: () => void;
  /** `@compositionend="onCompositionEnd"` — closes the IME guard. */
  onCompositionEnd: () => void;
}

export function useInlineRename<TId = string>(
  opts: UseInlineRenameOptions<TId>,
): UseInlineRenameReturn<TId> {
  const trimEmpty = opts.trimEmpty ?? true;

  const renamingId = ref<TId | null>(null) as Ref<TId | null>;
  const draft = ref<string>("");
  const isLoading = ref<boolean>(false);
  // Remember the seed title so an unchanged draft can short-circuit to
  // cancel (no need to hit the network for a no-op rename). Kept module-
  // local (not exported) so the consumer can't accidentally desync it.
  let seedTitle = "";
  // IME composition guard: while the user is composing a candidate (Pinyin
  // / Kana), Enter selects a candidate; committing on that Enter would
  // discard the composed characters.
  const isComposing = ref<boolean>(false);

  function isRenaming(id: TId): boolean {
    return renamingId.value === id;
  }

  function start(id: TId, currentTitle: string): void {
    // A concurrent start while a previous commit is in flight would corrupt
    // the state machine (draft would seed with the wrong title, isLoading
    // would still be true from the previous cycle).
    if (isLoading.value) return;
    renamingId.value = id;
    draft.value = currentTitle;
    seedTitle = currentTitle;
    isComposing.value = false;
  }

  function cancel(): void {
    if (renamingId.value === null) return;
    renamingId.value = null;
    draft.value = "";
    seedTitle = "";
    isComposing.value = false;
    // Don't touch isLoading: the consumer's async persistence flow owns it.
    opts.onCancel?.();
  }

  async function commit(): Promise<void> {
    // Guards mirror the original ChatTabStrip.commitEdit contract:
    //   - not renaming → nothing to commit
    //   - mid-IME → Enter picked a candidate, not a submit
    //   - mid-loading → previous commit still resolving; ignore duplicates
    if (renamingId.value === null || isComposing.value || isLoading.value) {
      return;
    }
    const currentId = renamingId.value;
    const trimmed = draft.value.trim();
    // Blank (with trimEmpty) or no-change → treat as cancel; never persist
    // an empty or no-op title.
    if ((trimEmpty && trimmed === "") || trimmed === seedTitle) {
      cancel();
      return;
    }
    // Fire the callback. The consumer decides whether to close the editor
    // on failure (typically: keep it open so the user can retry / see the
    // toast), but on success we close immediately. `onCommit` may be async;
    // we don't await it here because the consumer's loading gate
    // (beginLoading/endLoading) is the visible signal — awaiting inside
    // commit() would risk a component-lifecycle race in tests.
    await opts.onCommit(currentId, trimmed);
    // If the callback flagged an error via `beginLoading` without a matching
    // `endLoading`+success cancel path, isLoading stays true and the
    // consumer keeps the editor open until they release the gate. Success
    // path: the consumer's `finally { endLoading() }` runs AFTER we return
    // here, so we cancel BEFORE the release. That is deliberate — closing
    // the editor is the visible confirmation of a successful commit, and
    // the ordering matches the reference ChatTabStrip flow.
    if (renamingId.value === currentId) {
      cancel();
    }
  }

  function beginLoading(): void {
    isLoading.value = true;
  }

  function endLoading(): void {
    isLoading.value = false;
  }

  // ── DOM handlers ──────────────────────────────────────────────────────────
  function onEnter(ev: KeyboardEvent): void {
    ev.preventDefault();
    void commit();
  }

  function onEscape(ev: KeyboardEvent): void {
    ev.preventDefault();
    cancel();
  }

  function onBlur(): void {
    // Blur can fire AFTER cancel() (Escape → cancel() → blur), so guard.
    if (renamingId.value === null) return;
    void commit();
  }

  function onCompositionStart(): void {
    isComposing.value = true;
  }

  function onCompositionEnd(): void {
    isComposing.value = false;
  }

  return {
    renamingId,
    draft,
    isLoading,
    isRenaming,
    start,
    commit,
    cancel,
    beginLoading,
    endLoading,
    onEnter,
    onEscape,
    onBlur,
    onCompositionStart,
    onCompositionEnd,
  };
}

// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * useMentionAutocomplete — `@<name>` autocomplete for the chat composer.
 *
 * Provides:
 *   * a reactive open/close state driven by the textarea's caret position
 *     (open when the caret sits inside an `@<prefix>` token that has not yet
 *     been closed by whitespace / `@` / a sentence-ending punctuation char);
 *   * a filtered candidate list (current discussion roster minus
 *     already-mentioned names; case-insensitive prefix match against the
 *     active typing prefix);
 *   * `acceptCandidate(name)` — inserts the chosen name at the caret and
 *     emits the next-caret position back to the textarea.
 *
 * Single-Agent chat: the host gates this composable behind a discussion-mode
 * check, so the autocomplete only appears in multi-Agent discussions.
 *
 * Design notes:
 *   - The mention RegExp here is the FRONTEND mirror of the backend rule in
 *     ``src/qai/chat/application/use_cases/orchestrate_discussion._MENTION_RE``
 *     so what the user sees highlighted matches exactly what the backend
 *     resolves. Trailing punctuation (`, ; . ， 、 ； 。 !? ！？`) terminates a
 *     mention token client-side too.
 *   - The state machine is intentionally stateless wrt browser focus: a blur
 *     event closes the popover via the host (the host calls `close()` on blur);
 *     selection-change events update `position()` via `update()`.
 */

import { computed, ref, type ComputedRef, type Ref } from "vue";

/** A roster candidate descriptor — only the fields the popover renders. */
export interface MentionCandidate {
  /** Display name shown in the popover row + inserted into the textarea. */
  name: string;
  /** Theme-aware palette colour token (CSS `var(--...)`). */
  color: string | null;
  /** Optional model id rendered as the secondary line. */
  modelId: string | null;
}

/** Public surface of {@link useMentionAutocomplete}. */
export interface UseMentionAutocomplete {
  /** True iff the popover should be visible right now. */
  readonly open: Readonly<Ref<boolean>>;
  /** The candidates matching the active `@<prefix>` (filtered + ordered). */
  readonly candidates: ComputedRef<MentionCandidate[]>;
  /** Currently-highlighted candidate index (0-based). */
  readonly activeIndex: Readonly<Ref<number>>;
  /**
   * Inspect the textarea's current text + caret position and decide whether
   * the popover should open / close / refilter. Cheap; safe to call from
   * `@input` / `@keyup` / `@click` (selection-change) handlers.
   */
  update(text: string, caret: number): void;
  /** Close the popover unconditionally (e.g. blur, Esc). */
  close(): void;
  /** Move the highlight (±1) wrapping at the ends. No-op when closed. */
  move(delta: 1 | -1): void;
  /**
   * Accept the highlighted candidate: returns the next `{ text, caret }` to
   * apply to the textarea, or `null` if nothing is selected. The caller
   * commits the result (`text.value = ...; caret = ...`) and re-focuses.
   */
  accept(text: string, caret: number): { text: string; caret: number } | null;
  /** Whether the popover has at least one candidate (for the Enter capture). */
  readonly hasCandidates: ComputedRef<boolean>;
}

/** Punctuation that terminates a mention token (matches backend rule). */
const _MENTION_STOP_RE = /[\s@,，、；;.。!?！？]/;

/**
 * Scan ``text`` backwards from ``caret`` looking for the start of an unclosed
 * ``@<prefix>`` token. Returns the prefix (without the ``@``) and the offset
 * of the ``@`` sign, or ``null`` if the caret is not inside such a token.
 */
function activeMentionAt(
  text: string,
  caret: number,
): { prefix: string; atIndex: number } | null {
  if (caret <= 0 || caret > text.length) return null;
  // Walk back over non-stop chars to find the `@`.
  let i = caret - 1;
  while (i >= 0) {
    const ch = text[i];
    if (ch === "@") {
      // Found the marker — make sure it's not part of an email (``a@b``):
      // accept only when the char before `@` is empty or whitespace/punct.
      const prev = i > 0 ? text[i - 1] : "";
      if (prev !== "" && !/[\s,，、；;.。!?！？]/.test(prev ?? "")) {
        return null;
      }
      return { prefix: text.slice(i + 1, caret), atIndex: i };
    }
    if (_MENTION_STOP_RE.test(ch ?? "")) {
      return null;
    }
    i -= 1;
  }
  return null;
}

/**
 * Build the composable.
 *
 * @param roster — reactive list of all discussion participants (typically
 *   `useDiscussion().participants`); the composable derives candidates from
 *   this list every time {@link UseMentionAutocomplete.update} is called.
 */
export function useMentionAutocomplete(
  roster: ComputedRef<readonly MentionCandidate[]> | Ref<readonly MentionCandidate[]>,
): UseMentionAutocomplete {
  const open = ref(false);
  const activeIndex = ref(0);
  /** The current prefix the user is typing after the `@`. */
  const prefix = ref("");
  /** Index of the `@` marker in the textarea so `accept` can splice. */
  const atIndex = ref(-1);
  /**
   * Names already mentioned earlier in the text — excluded from the
   * suggestion list so the user can't mention the same role twice on a turn.
   * Computed once per `update()` call.
   */
  const usedNames = ref<Set<string>>(new Set());

  const candidates = computed<MentionCandidate[]>(() => {
    if (!open.value) return [];
    const wanted = prefix.value.trim().toLowerCase();
    const used = usedNames.value;
    const filtered = roster.value
      .filter((c) => !used.has(c.name.toLowerCase()))
      .filter((c) =>
        wanted === "" ? true : c.name.toLowerCase().includes(wanted),
      );
    // Prefer prefix-matches first, then substring matches (gives a more
    // intuitive ordering when the roster mixes long names with short ones).
    filtered.sort((a, b) => {
      const aPrefix = a.name.toLowerCase().startsWith(wanted) ? 0 : 1;
      const bPrefix = b.name.toLowerCase().startsWith(wanted) ? 0 : 1;
      if (aPrefix !== bPrefix) return aPrefix - bPrefix;
      return a.name.localeCompare(b.name);
    });
    return filtered;
  });

  const hasCandidates = computed(() => candidates.value.length > 0);

  function update(text: string, caret: number): void {
    const active = activeMentionAt(text, caret);
    if (active === null) {
      open.value = false;
      prefix.value = "";
      atIndex.value = -1;
      return;
    }
    prefix.value = active.prefix;
    atIndex.value = active.atIndex;
    // Re-derive "already used" excluding the CURRENT token (a half-typed
    // mention may share characters with a previously-completed one).
    const before = text.slice(0, active.atIndex);
    const after = text.slice(active.atIndex + 1 + active.prefix.length);
    const others = `${before} ${after}`;
    const found = new Set<string>();
    const re = /@([^\s@,，、；;.。!?！？]+)/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(others)) !== null) {
      if (m[1]) found.add(m[1].toLowerCase());
    }
    usedNames.value = found;
    open.value = true;
    if (activeIndex.value >= candidates.value.length) {
      activeIndex.value = 0;
    }
  }

  function close(): void {
    open.value = false;
    prefix.value = "";
    atIndex.value = -1;
    activeIndex.value = 0;
  }

  function move(delta: 1 | -1): void {
    if (!open.value) return;
    const list = candidates.value;
    if (list.length === 0) return;
    const next = (activeIndex.value + delta + list.length) % list.length;
    activeIndex.value = next;
  }

  function accept(
    text: string,
    caret: number,
  ): { text: string; caret: number } | null {
    if (!open.value || atIndex.value < 0) return null;
    const list = candidates.value;
    if (list.length === 0) return null;
    const picked = list[activeIndex.value] ?? list[0];
    if (!picked) return null;
    // Splice the `@<prefix>` segment with `@<name> ` (trailing space so the
    // user can type the next mention or question body without an extra key).
    const before = text.slice(0, atIndex.value);
    const after = text.slice(caret);
    const inserted = `@${picked.name} `;
    const nextText = `${before}${inserted}${after}`;
    const nextCaret = before.length + inserted.length;
    close();
    return { text: nextText, caret: nextCaret };
  }

  return {
    open,
    candidates,
    activeIndex,
    update,
    close,
    move,
    accept,
    hasCandidates,
  };
}

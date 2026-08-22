// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * usePromptRecall — shell-like ↑/↓ browsing of previously-sent prompts.
 *
 *   ↑ (ArrowUp)   → older prompts (time near → far)
 *   ↓ (ArrowDown) → newer prompts, finally the draft that was being typed
 *
 * NOT a second history store: the entries come from the ONE recent list owned
 * by `usePromptHistory` (the same list the clock-icon popover renders and
 * `recordSent` feeds). This composable adds only a browse cursor.
 *
 * ── Why the whole decision lives here ───────────────────────────────────────
 * `handleArrow` takes the raw key + caret facts and returns what the textarea
 * should become. Mirrors `useMentionAutocomplete`, where the caret arithmetic
 * is also inside the composable and the component only reads/writes the DOM —
 * so the guards that decide "browse history vs move the caret" are unit
 * testable instead of buried in a 2600-line SFC.
 *
 * ── Entry / exit semantics ──────────────────────────────────────────────────
 * Entering: ↑ starts browsing only when the caret sits at offset 0 with no
 * selection. Deliberately NOT "caret is on the first line": the textarea
 * soft-wraps (auto-resize + CSS max-height), so a long single-paragraph draft
 * has many visual lines and no `\n` at all — a first-line test would hijack
 * ordinary ↑ inside it. Offset 0 has no false positives, and covers the
 * dominant case (empty draft) for free.
 *
 * Browsing: while browsing an untouched entry ↑/↓ are pure history navigation
 * and are always consumed — same as a shell prompt, where ↑ never walks the
 * caret inside the recalled line. The caret is parked at the end of the
 * recalled text (matching the popover's `fillFromHistory` behaviour).
 *
 * Exiting: any text change this composable did not author ends browsing — see
 * `observeText`, which the host calls from its single `watch(text)`. Typing,
 * pasting, IME commits, voice-transcript appends, prompt-enhance replacement,
 * `appendToDraft`, mention accept and sending all flow through that one watch,
 * so browsing can never be left stale over text it no longer owns (a
 * per-callsite `reset()` sprinkle would silently miss the programmatic writers,
 * which do not fire the native `input` event).
 *
 * ── Cursor state ────────────────────────────────────────────────────────────
 * `pos === -1` → editing the live draft. `pos === i` → showing `entries[i]`.
 * `entries` is a SNAPSHOT of the recent texts taken when browsing starts, so a
 * concurrent `recordSent` from another composer instance, or a row deleted /
 * history cleared in the popover mid-browse, cannot shift the cursor onto a
 * different prompt (the shared list is a mutable module-level ref; an index
 * into the live list would silently drift).
 *
 * Per composer instance (each `<ChatComposer>` browses independently); only the
 * underlying recent list is shared.
 */
import { usePromptHistory } from "./usePromptHistory";

/** The keyboard facts `handleArrow` needs — a `KeyboardEvent` satisfies it. */
export interface RecallKey {
  key: string;
  shiftKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  isComposing: boolean;
}

/**
 * What the host should do with a consumed arrow key. Always `preventDefault`;
 * `text === null` means "keep the textarea as it is" (already at the oldest
 * entry — the caret stays parked instead of walking into the recalled text).
 *
 * `caretHint` follows the direction-of-travel rule: after ↑ we park the caret
 * at the FIRST visual line so the very next ↑ hops further into the past with
 * no manual navigation, and after ↓ we park it at the LAST visual line so the
 * next ↓ hops toward the present. This makes the arrow keys behave as a
 * "keep pressing" rewind/fast-forward, matching how shells behave. Draft
 * restore leaves the caret at the natural end of the user's own text.
 */
export interface RecallOutcome {
  text: string | null;
  caretHint: "start" | "end";
}

export interface UsePromptRecall {
  /**
   * Decide what ↑/↓ should do. Returns `null` when the key is none of our
   * business (wrong key, modifier held, IME composing, active selection, or
   * the caret has room to move within the current text) — the host must then
   * fall through to its normal handling.
   *
   * `line` reports whether the caret sits on the first / last VISUAL line of
   * the textarea (soft-wrap aware). Shell-like: arrows navigate the text
   * itself first; only at a boundary do they flip to history navigation.
   */
  handleArrow(
    key: RecallKey,
    text: string,
    caret: number,
    selectionEnd: number,
    line: { atFirstLine: boolean; atLastLine: boolean },
  ): RecallOutcome | null;
  /**
   * Feed every textarea value change here (from the host's `watch(text)`).
   * A value we did not hand out ends browsing.
   */
  observeText(next: string): void;
  /** Force-exit browsing. */
  reset(): void;
  /** True while browsing history rather than the user's own draft. */
  isRecalling(): boolean;
}

export function usePromptRecall(): UsePromptRecall {
  const { recent } = usePromptHistory();

  /** -1 = editing the live draft; >= 0 = index into `entries`. */
  let pos = -1;
  /** Recent texts, newest first, frozen when browsing started. */
  let entries: readonly string[] = [];
  /** The draft stashed on entry, restored when walking past the newest entry. */
  let stashedDraft = "";
  /** Last value we handed to the host — lets `observeText` spot foreign edits. */
  let applied: string | null = null;

  function isRecalling(): boolean {
    return pos >= 0;
  }

  function reset(): void {
    pos = -1;
    entries = [];
    stashedDraft = "";
    applied = null;
  }

  /**
   * Hand a value out, remembering it so `observeText` won't treat it as foreign.
   * The `caretHint` says where the caller should park the caret AFTER writing
   * the value — only for this single write. Once the value lands the user is
   * free to move the caret anywhere; the "boundary-triggers-hop" logic in
   * `handleArrow` picks up from wherever they leave it.
   */
  function apply(text: string, caretHint: "start" | "end"): RecallOutcome {
    applied = text;
    return { text, caretHint };
  }

  function older(): RecallOutcome {
    if (pos < entries.length - 1) {
      pos += 1;
      // ↑ = travelling into the past. Park the caret at the first line so a
      // follow-up ↑ (no manual caret move) hops further back without the user
      // walking through the recalled text first.
      return apply(entries[pos] ?? "", "start");
    }
    // Oldest entry reached: consume the key so the caret stays parked, and
    // hint "start" so if the host is called on the FIRST-ever `older()` (i.e.
    // history has zero entries — currently unreachable because we short-circuit
    // in `handleArrow`, but defensive) it still lands consistently.
    return { text: null, caretHint: "start" };
  }

  function newer(): RecallOutcome {
    if (pos === 0) {
      const draft = stashedDraft;
      reset();
      applied = draft;
      // Draft restore: caret at the end is the natural resting point for
      // one's own text (mirrors `fillFromHistory` in the popover).
      return { text: draft, caretHint: "end" };
    }
    pos -= 1;
    // ↓ = travelling toward the present. Park the caret at the last line so
    // a follow-up ↓ hops toward newer / the draft without walking through.
    return apply(entries[pos] ?? "", "end");
  }

  /** DOM facts the host supplies: caret arithmetic + visual-line boundaries. */
  function handleArrow(
    key: RecallKey,
    text: string,
    caret: number,
    selectionEnd: number,
    line: { atFirstLine: boolean; atLastLine: boolean },
  ): RecallOutcome | null {
    const up = key.key === "ArrowUp";
    if (!up && key.key !== "ArrowDown") return null;
    // Modified arrows are established shortcuts / selection gestures, and a
    // mid-composition arrow belongs to the IME candidate window.
    if (key.shiftKey || key.ctrlKey || key.altKey || key.metaKey) return null;
    if (key.isComposing) return null;
    // Only a collapsed caret: an active selection means the user is selecting.
    if (selectionEnd !== caret) return null;

    // Shell-like boundary semantics: arrows navigate WITHIN the text first;
    // only when the caret has nowhere further to move in that direction does
    // the arrow flip to history navigation. This matches the terminal / most
    // REPL behaviour and lets users freely edit / re-read a multi-line recall
    // without every ↑ jumping to an older entry.
    if (up && !line.atFirstLine) return null;
    if (!up && !line.atLastLine) return null;

    if (isRecalling()) {
      return up ? older() : newer();
    }
    // ↓ from the last line of one's own draft has nothing more to walk to —
    // recall is a strictly upward gesture from a draft.
    if (!up) return null;

    const list = recent.value.map((e) => e.text);
    if (list.length === 0) return null;
    entries = list;
    stashedDraft = text;
    pos = 0;
    return apply(entries[0] ?? "", "start");
  }

  function observeText(next: string): void {
    if (!isRecalling()) return;
    if (next !== applied) reset();
  }

  return { handleArrow, observeText, reset, isRecalling };
}

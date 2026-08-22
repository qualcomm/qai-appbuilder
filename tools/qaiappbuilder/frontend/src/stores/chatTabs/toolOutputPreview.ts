// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Bounded head+tail preview for streaming tool-card output (perf: stop the
 * O(n²) accumulation that塞满界面 + crawls the UI).
 *
 * A streaming-capable tool (notably `exec`) can emit a huge amount of output —
 * a single command may produce tens of thousands of lines / several megabytes.
 * Each live increment arrives as a `partial=true` `tool_result` SSE frame
 * carrying a `delta` chunk (see `frameHandlers.handleToolResult`). The naive
 * reducer did `output = (output ?? "") + delta` per frame and replaced the
 * whole `messages` array each time, so:
 *   - the appended string grew without bound (the card alone held megabytes),
 *   - every frame did an O(n) string concat over the accumulated text, and
 *   - the dependent re-render walked the ever-growing output → O(n²) overall,
 * making a chatty command "fast at first, then a crawl" and stuffing the DOM
 * with text no human reads while it scrolls past.
 *
 * The fix is to keep only a BOUNDED preview of the live stream — a frozen head
 * plus a rolling tail — and drop the middle, inserting a fold marker that tells
 * the user the output was大 and the full text is retrievable. The card never
 * holds more than `HEAD_BYTES + TAIL_BYTES + marker` characters regardless of
 * how much the tool emits. (The canonical full output is persisted by the
 * backend and可用 the `read` tool取回 — the card is a live preview, not the
 * system of record.)
 *
 * This module is intentionally store-agnostic (no Pinia / Vue import) and the
 * preview rendering is a pure function, so the head/tail/fold logic is directly
 * unit-testable.
 */

/** Head preview budget — the first ~8KB of the stream is frozen once filled so
 *  the user always sees where the command started (the command line / opening
 *  banner). Measured in UTF-16 code units (`String.length`); not byte-exact,
 *  which is fine for a preview. */
export const HEAD_BYTES = 8 * 1024;

/** Tail preview budget — the most recent ~64KB is kept in a rolling buffer so
 *  the user always sees the latest output (where errors / completion land).
 *  Far larger than the head because the tail is what the user actively watches
 *  while a command runs. */
export const TAIL_BYTES = 64 * 1024;

/** Build the fold marker inserted between the frozen head and the rolling tail
 *  once the middle has been dropped. States how many characters were elided and
 *  that the complete output is retrievable, so the preview never silently lies
 *  about being truncated. */
export function foldMarker(foldedChars: number): string {
  return `\n…[已折叠 ${foldedChars} 字节，输出过大；完整输出请用 read 工具取回]…\n`;
}

/**
 * A bounded accumulator for one tool card's live streaming output.
 *
 * Invariants:
 *   - `head` is filled up to `HEAD_BYTES` then FROZEN (never rewritten) — a
 *     one-time fill, so appending is never O(total) on the head side.
 *   - `tail` is a rolling buffer capped at `TAIL_BYTES`: when an append would
 *     exceed the cap, the oldest characters are dropped from its front. Each
 *     append touches only the incoming delta plus the (bounded) overflow it
 *     evicts — O(delta + overflow), NOT O(total accumulated).
 *   - `dropped` counts characters discarded from the MIDDLE (evicted from the
 *     tail front after the head froze) — i.e. the folded span.
 *
 * `render()` produces the displayable preview string (head + fold marker +
 * tail) and is the only place the (bounded) pieces are concatenated.
 */
export class BoundedOutputBuffer {
  private head = "";
  private headFrozen = false;
  /** Rolling tail kept as a single string capped at `TAIL_BYTES`. */
  private tail = "";
  /** Total characters dropped from the middle (folded span size). */
  private dropped = 0;
  /** Total characters ever appended (for callers that want the true size). */
  private total = 0;

  constructor(
    private readonly headLimit: number = HEAD_BYTES,
    private readonly tailLimit: number = TAIL_BYTES,
  ) {}

  /** Append a streaming delta. Bounded work: fills the head once, then rolls
   *  the tail, evicting overflow from its front into the `dropped` counter. */
  append(delta: string): void {
    if (delta === "") return;
    this.total += delta.length;

    let rest = delta;
    // 1) Fill the frozen head first (one-time, until it reaches headLimit).
    if (!this.headFrozen) {
      const room = this.headLimit - this.head.length;
      if (room > 0) {
        const take = rest.slice(0, room);
        this.head += take;
        rest = rest.slice(take.length);
      }
      if (this.head.length >= this.headLimit) {
        this.headFrozen = true;
      }
      if (rest === "") return;
    }

    // 2) Remainder goes into the rolling tail; evict overflow from its front.
    this.tail += rest;
    const overflow = this.tail.length - this.tailLimit;
    if (overflow > 0) {
      // Dropping from the tail front discards the OLDEST tail chars → middle.
      this.tail = this.tail.slice(overflow);
      this.dropped += overflow;
    }
  }

  /** True when any middle content has been folded away (head froze AND the tail
   *  rolled), i.e. the preview is not the complete output. */
  isFolded(): boolean {
    return this.dropped > 0;
  }

  /** Total characters appended over the buffer's life (pre-fold true size). */
  totalLength(): number {
    return this.total;
  }

  /** Render the bounded preview: frozen head + fold marker (when folded) +
   *  rolling tail. When nothing was folded this is exactly the head followed by
   *  the tail (== the full output so far for small streams). */
  render(): string {
    if (this.dropped === 0) {
      // Not folded yet: head still filling OR head frozen but tail未溢出. The
      // displayable text is simply everything seen so far, in order.
      return this.head + this.tail;
    }
    return this.head + foldMarker(this.dropped) + this.tail;
  }
}

/**
 * Pure one-shot bounded preview of an already-complete string (used for the
 * FINAL settled frame / seed of a card whose first frame already carries a
 * large consolidated result). Mirrors `BoundedOutputBuffer.render()`'s shape so
 * live and terminal previews look identical.
 */
export function renderBoundedPreview(
  text: string,
  headLimit: number = HEAD_BYTES,
  tailLimit: number = TAIL_BYTES,
): string {
  if (text.length <= headLimit + tailLimit) {
    // Small enough to show whole — no fold.
    return text;
  }
  const head = text.slice(0, headLimit);
  const tail = text.slice(text.length - tailLimit);
  const folded = text.length - headLimit - tailLimit;
  return head + foldMarker(folded) + tail;
}

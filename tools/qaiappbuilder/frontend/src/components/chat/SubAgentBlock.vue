<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * SubAgentBlock — render one sub-agent block inside an assistant turn.
 *
 * V2 UX redesign (2026-07-01, user-directed) — TWO-ROW layout:
 *   Row 1 (header — compact, single-line):
 *     [status icon SVG] [name (bold)] [type badge] [rounds] │ [open pill]
 *     [stop pill] [toggle arrow]
 *   Row 2 (prompt preview — up to 3 lines, always visible):
 *     the truncated prompt the LLM sent to the sub-agent, wrapped up to
 *     3 lines with ellipsis; hover shows the full text via ``title``.
 *
 * Rationale for the two-row split (vs the earlier single-row header):
 *   - The old header tried to fit the name AND the prompt on ONE line,
 *     which competed for space and yielded 3-char names ("读取 u...")
 *     next to 6-char prompts ("请读取文件...") — neither useful.
 *   - Row 2 as an INDEPENDENT block gives the prompt real space (3 lines
 *     ≈ enough to read the task at a glance) while row 1 stays clean.
 *   - Preview is visible in BOTH collapsed and expanded states so the
 *     user can scan a conversation and identify each sub-agent without
 *     expanding every card. The expanded body does NOT repeat the
 *     prompt — it only shows the sub-agent's OUTPUT (text + tool calls
 *     + errors).
 *
 * Other design points (unchanged from the earlier redesign):
 *   - Status glyphs are proper inline SVGs (animated spinner for
 *     running / aborting, check for done, warning for error) — no emoji.
 *   - Name is the LLM-supplied ``block.name`` (V2 §3.1 tail-appended,
 *     plumbed from the ``agent`` tool's ``name`` param →
 *     SubAgentSession.title → ``subagent_start`` frame); falls back to
 *     ``subAgentFallbackName`` (i18n "SubAgent N") when the model did
 *     not supply one — no regression.
 *   - Type badge is an i18n pill for ``block.subagent_type``
 *     (``general`` / ``explore``, both localised); hidden on legacy
 *     blocks / unknown types.
 *   - Left status-colour strip communicates the block state without
 *     the "coloured box" heaviness.
 *
 * V1 parity preserved:
 *   - ``_collapsed`` is still toggled in place so history rehydration
 *     keeps the user's expanded/collapsed choice.
 *   - Per-round timeline (text → tools → text → tools) via SHARED
 *     ToolCallList (``todowrite`` → TaskListCard / rest → ToolExecPanel),
 *     gated by ``ui.showToolMessages`` (AppHeader "Tool Calls" toggle).
 *   - Header click / Enter / Space toggles the body (same interaction
 *     surface V1 had). The preview row is NOT clickable — clicking it
 *     lets the user select the prompt text (a common expectation), and
 *     the header remains the sole "expand/collapse" affordance.
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import type {
  SubAgentBlock,
  SubAgentErrorKind,
  ToolCallView,
} from "@/stores/chatTabs";
import { useUiStore } from "@/stores/ui";
import { renderMarkdown } from "@/composables/markdown";
import { useMermaidRender } from "@/composables/useMermaidRender";
import ToolCallList from "./ToolCallList.vue";

const props = defineProps<{
  /** The block to render. Mutated in place when the user toggles the
   *  collapsed state (V1 in-place pattern; preserves the preference
   *  across re-renders without extra store actions). */
  block: SubAgentBlock;
}>();

/** V2 enhancement: surface an "open this sub-agent in a new tab" action so
 *  the user can inspect its full transcript and take over the conversation.
 *  Only emitted when the block carries a persistent `subagent_id` (the parent
 *  wires this to `chatTabs.openSubAgentTab`). */
const emit = defineEmits<{
  (e: "open-subagent", subagentId: string): void;
  (e: "stop-subagent", subagentId: string): void;
  /** Per-call cancel of ONE of this inline sub-agent's tool calls. Bubbles the
   *  `callId` to the parent (ChatMessageList.onCancelTool), which routes it to
   *  the parent turn's tab so the backend cancels just that tool and the
   *  sub-agent continues — same single-tool semantics as a main-agent card. */
  (e: "cancel-tool", callId: string): void;
}>();

const { t } = useI18n();

/** Tool-card visibility gate — shared with the main agent (ui.showToolMessages,
 *  driven by the AppHeader "Tool Calls" toggle). */
const ui = useUiStore();

/** Map this block's ordered per-round `turns` into the render model consumed
 *  by ToolCallList — same shape the main agent uses (`todowrite` →
 *  TaskListCard, rest → ToolExecPanel). See prior implementation notes
 *  (V1 chat.css §1717-1812) for the `timestamp` unmount-survival anchor. */
const turnViews = computed<
  { key: number; content: string; tools: ToolCallView[] }[]
>(() => {
  return props.block.turns.map((turn) => ({
    key: turn.roundIndex,
    content: turn.content,
    tools: turn.tools.map((tool, index) => ({
      // v-for key stability (2026-07-20): prefer the backend-provided
      // `tool_call_id` so a same-physical tool call retains the same DOM node
      // across array reshuffles (mapper reordering / mid-stream inserts /
      // cancellations reflow the tools array). A plain `${roundIndex}-${index}`
      // key would drift on any reshuffle, remounting ToolExecPanel and
      // resetting its local `userToggled` state — which would silently undo
      // a user's manual "expand this card" action once the tool completes.
      // Fallback (`tool.name+index`) covers historical sub-agent frames where
      // the backend didn't emit `tool_call_id`; still more stable than raw
      // index because same-position calls with different names diverge.
      key: tool.tool_call_id
        ? `${turn.roundIndex}-${tool.tool_call_id}`
        : `${turn.roundIndex}-${tool.name}-${index}`,
      // Forward the sub-agent tool call's `tool_call_id` as `callId` so the
      // per-tool stop button (ToolExecPanel `canCancel`) is shown on sub-agent
      // tool cards. The backend now tracks sub-agent-internal tool call ids:
      // `cancel_tool(tab_id, call_id)` marks the call on the parent tab's
      // stream-abort handle, and the sub-agent's tool round polls the SAME
      // handle's `consume_cancel_tool(call_id)` (agent_tool.py `_tool_executor`
      // via the shared skeleton's `cancel_check`), cancelling JUST that one
      // tool (→ exec tree-kill) while the sub-agent turn continues — the same
      // single-tool semantics as a main-agent tool card. When the model
      // omitted a tool_call_id the value is undefined and `canCancel` stays
      // false (button hidden), avoiding a stop that could not be routed.
      callId: tool.tool_call_id,
      toolName: tool.name,
      args: tool.args,
      result: tool.result,
      status:
        tool.status ??
        (tool.result !== undefined
          ? tool.ok === false
            ? "error"
            : "done"
          : "running"),
      outputSize: tool.outputSize,
      truncated: tool.truncated,
      durationMs: tool.duration_ms,
      timestamp: tool.ts,
    })),
  }));
});

// Native Mermaid rendering for ```mermaid``` blocks in sub-agent narration.
const bodyEl = ref<HTMLElement | null>(null);
const mermaidContentKey = computed(() =>
  turnViews.value.map((turn) => turn.content).join("\u0000"),
);
const expanded = computed(() => !props.block._collapsed);
useMermaidRender(bodyEl, {
  content: [mermaidContentKey, expanded],
  labels: () => ({
    rendering: t("chat.mermaid.rendering"),
    renderError: (message: string) => t("chat.mermaid.renderError", { message }),
    errorDefault: t("chat.mermaid.errorDefault"),
    errorEmpty: t("chat.mermaid.errorEmpty"),
  }),
});

function openInTab(): void {
  const id = props.block.subagent_id;
  if (id !== undefined && id !== "") {
    emit("open-subagent", id);
  }
}

function stopSubAgent(): void {
  if (props.block.status === "aborting") {
    return;
  }
  const id = props.block.subagent_id;
  if (id !== undefined && id !== "") {
    emit("stop-subagent", id);
  }
}

/** Human-readable name (V2 §3.1 tail-appended): the LLM-supplied task label
 *  when spawning (persisted as SubAgentSession.title). Falls back to
 *  "SubAgent N" (i18n) so nothing regresses when the model didn't supply
 *  a name. `index + 1` matches the 1-based convention V1 used in headers. */
const displayName = computed(() => {
  const raw = props.block.name;
  if (typeof raw === "string" && raw.trim() !== "") {
    return raw;
  }
  return t("index.subAgentFallbackName", { n: props.block.index + 1 });
});

/** Type badge label (V2 §3.1 tail-appended). Known types get an i18n label
 *  (`general` → "通用" / "General", `explore` → "探索" / "Explore"); an
 *  unknown non-empty value falls back to the raw string (Title-Cased) so a
 *  future backend profile shows *something* useful; absent → badge hidden. */
const typeBadgeLabel = computed<string | null>(() => {
  const t2 = props.block.subagent_type;
  if (typeof t2 !== "string" || t2 === "") return null;
  const key = t2.toLowerCase();
  if (key === "general" || key === "agent") {
    return t("index.subAgentTypeGeneral");
  }
  if (key === "explore") {
    return t("index.subAgentTypeExplore");
  }
  // Unknown profile — Title-Case the raw value so it at least reads cleanly.
  return t2.charAt(0).toUpperCase() + t2.slice(1);
});

/** CSS modifier for the type badge so each type has its own accent tint.
 *  Unknown types fall through to the neutral variant. */
const typeBadgeClass = computed<string>(() => {
  const t2 = props.block.subagent_type;
  if (typeof t2 !== "string" || t2 === "") return "";
  const key = t2.toLowerCase();
  if (key === "general" || key === "agent") return "subagent-block-type--general";
  if (key === "explore") return "subagent-block-type--explore";
  return "subagent-block-type--neutral";
});

/** Rounds counter i18n label ("N 轮" / "N rounds"). English needs singular
 *  "1 round" vs plural "N rounds" (grammar). CJK has no plural form so the
 *  singular key is a synonym of the plural (kept for structural symmetry so
 *  the JS branch below stays uniform across locales — no i18n lookup miss). */
const roundsLabel = computed(() => {
  const n = props.block.rounds;
  if (n === 1) {
    return t("index.subAgentRoundsCountOne", { n });
  }
  return t("index.subAgentRoundsCount", { n });
});

/** N.37 — per-kind presentation of a typed sub-agent failure.
 *
 *  A TS map (not an if-else ladder) keyed by the `SubAgentErrorKind` union, so
 *  the compiler REQUIRES an entry for every kind: adding a kind to the union
 *  (mirroring a new backend `SubAgentErrorKind`) fails the build here until
 *  its icon + colour are chosen, instead of silently rendering an unstyled
 *  card.
 *
 *  Colour tokens are the project's EXISTING semantic status tokens from
 *  `styles/variables.css` (verified present in both the dark root block and
 *  the light-theme override, so every choice tracks the theme):
 *    `--text-muted` — de-emphasised: nothing ran / user chose to stop, so this
 *                     is informational, not alarming.
 *    `--warning`    — recoverable infrastructure fault, worth a retry.
 *    `--error`      — the run genuinely failed mid-execution.
 *    `--accent`     — a budget/quota boundary: a policy limit rather than a
 *                     malfunction, so it reads as brand-blue, not red.
 *  (The plan named `--muted` / `--warn` / `--danger`; those tokens do NOT
 *  exist in this codebase and inventing them would fork the palette, so the
 *  semantically-closest existing tokens are used.)
 *
 *  Icons are emoji here rather than the inline SVGs used for the lifecycle
 *  status glyph: these are per-kind diagnostic markers in a text row, not the
 *  card's primary status affordance, and a one-glyph-per-kind set keeps the
 *  mapping legible without shipping five more SVG paths.
 */
const ERROR_STYLES: Record<
  SubAgentErrorKind,
  { icon: string; colorVar: string }
> = {
  preflight: { icon: "ℹ️", colorVar: "--text-muted" },
  isolation: { icon: "⚠️", colorVar: "--warning" },
  execution: { icon: "❌", colorVar: "--error" },
  budget: { icon: "💰", colorVar: "--accent" },
  interrupted: { icon: "⏹️", colorVar: "--text-muted" },
};

/** The typed-failure view model, or `null` when this block is not a typed
 *  failure (a clean block, or a legacy `subagent_error` that carries only the
 *  bare `error` string — that path still renders via `block.error`). */
const typedError = computed<
  | {
      icon: string;
      colorVar: string;
      kind: SubAgentErrorKind;
      message: string;
      hint: string;
    }
  | null
>(() => {
  const kind = props.block.errorKind;
  if (props.block.status !== "error" || kind === undefined) return null;
  const style = ERROR_STYLES[kind];
  // Prefer the backend's specific hint; fall back to the localised per-kind
  // default so the card ALWAYS tells the user what to do next (a typed
  // failure with no guidance is the case this fallback exists for).
  const hint =
    props.block.recoveryHint ?? t(`chat.subAgent.errDefaultHint.${kind}`);
  return {
    icon: style.icon,
    colorVar: style.colorVar,
    kind,
    message: props.block.errorMessage ?? props.block.error ?? "",
    hint,
  };
});

function toggleCollapsed(): void {
  // V1 mutates ``block._collapsed`` directly on the block object; we
  // do the same so persistence keeps the user-set preference across
  // re-renders.
  // eslint-disable-next-line vue/no-mutating-props -- V1 parity: _collapsed is persisted on the block object itself
  props.block._collapsed = !props.block._collapsed;
}
</script>

<template>
  <div
    class="subagent-block"
    :class="`subagent-status-${block.status}`"
    :data-testid="`subagent-block-${block.index}`"
  >
    <!-- Status-colour left strip: a thin accent bar replacing the old full
         border tint. Cleaner, and its colour communicates the block state
         at a glance without competing with the card's content. -->
    <div class="subagent-block-strip" aria-hidden="true" />
    <div class="subagent-block-content-wrap">
      <div class="subagent-block-header">
        <!-- Status icon: inline SVG per state (spinner / check / warn / dot),
             sized 14px + tinted with the state colour. Replaces the old
             emoji. `aria-hidden` because the state is already conveyed by
             `aria-busy` on running affordances + textual labels. -->
        <span class="subagent-block-icon" aria-hidden="true">
          <template v-if="block.status === 'running' || block.status === 'background' || block.status === 'aborting'">
            <svg class="subagent-icon-spinner" width="14" height="14" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-dasharray="42 42" />
            </svg>
          </template>
          <template v-else-if="block.status === 'done'">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.75" stroke-linecap="round" stroke-linejoin="round">
              <path d="M5 12l4.5 4.5L19 7" />
            </svg>
          </template>
          <template v-else-if="block.status === 'error'">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="9.5" />
              <path d="M12 8v5" />
              <path d="M12 16.5v.01" />
            </svg>
          </template>
          <template v-else>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
              <circle cx="12" cy="12" r="3" />
            </svg>
          </template>
        </span>

        <!-- Name (LLM-supplied task label, falls back to "SubAgent N"). -->
        <span class="subagent-block-name">{{ displayName }}</span>

        <!-- Parallel-suffix "/M" only when this parent turn dispatched >1. -->
        <span
          v-if="block.total > 1"
          class="subagent-block-parallel"
          :aria-label="`${block.index + 1} / ${block.total}`"
        >{{ block.index + 1 }} / {{ block.total }}</span>

        <!-- Type badge — i18n pill for `general` / `explore`. Hidden when
             the backend did not resolve a type (legacy frames). -->
        <span
          v-if="typeBadgeLabel !== null"
          class="subagent-block-type"
          :class="typeBadgeClass"
        >{{ typeBadgeLabel }}</span>

        <!-- Rounds counter (visible once the block is done). -->
        <span
          v-if="block.status === 'done' && block.rounds > 0"
          class="subagent-block-rounds"
        >{{ roundsLabel }}</span>

        <!-- N.18: detached-spawn chip. The sub-agent is executing while the
             parent turn already moved on, so the user needs to see that this
             is live-but-not-blocking work (the spinner alone reads the same as
             an inline running block). -->
        <span
          v-if="block.status === 'background'"
          class="subagent-block-background"
        >{{ t("index.subAgentStatusBackground") }}</span>


        <!-- Right-aligned action group: open + stop + toggle arrow. -->
        <div class="subagent-block-actions">
          <button
            v-if="block.subagent_id"
            type="button"
            class="subagent-block-open"
            :title="t('index.subAgentOpenInTab')"
            :aria-label="t('index.subAgentOpenInTab')"
            @click.stop="openInTab"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M15 3h6v6" />
              <path d="M10 14 21 3" />
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
            </svg>
            <span class="subagent-block-open-label">{{ t("index.subAgentOpenInTab") }}</span>
          </button>
          <button
            v-if="block.subagent_id && (block.status === 'running' || block.status === 'background' || block.status === 'aborting')"
            type="button"
            class="subagent-block-stop"
            :class="{ 'subagent-block-stop--aborting': block.status === 'aborting' }"
            :disabled="block.status === 'aborting'"
            :title="block.status === 'aborting' ? t('index.subAgentStopping') : t('index.subAgentStop')"
            :aria-label="block.status === 'aborting' ? t('index.subAgentStopping') : t('index.subAgentStop')"
            :aria-busy="block.status === 'aborting' ? 'true' : 'false'"
            @click.stop="stopSubAgent"
          >
            <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <rect x="6" y="6" width="12" height="12" rx="1.5" />
            </svg>
            <span class="subagent-block-stop-label">{{ block.status === 'aborting' ? t("index.subAgentStopping") : t("index.subAgentStop") }}</span>
          </button>
        </div>
      </div>
      <!-- Second row — prompt preview.
           Independent line below the header row so the header stays clean
           (name + badges + actions) and the prompt gets real room to
           breathe: 3-line clamp (roughly 3-5 lines of text at the current
           font size), ellipsis on overflow. Visible in BOTH collapsed and
           expanded states so the user can identify each sub-agent card at
           a glance without expanding it. The full prompt is NOT repeated
           inside the expanded body — the expanded body only shows the
           sub-agent's OUTPUT (text + tool calls + errors). -->
      <div
        v-if="block.prompt_preview"
        class="subagent-block-preview"
        :title="block.prompt_preview"
      >{{ block.prompt_preview }}</div>
      <!-- §opaque-subagent (2026-08-07): the SubAgentBlock inside a
           parent tab is no longer an inline live-transcript window.
           A sub-agent's real-time work (assistant reasoning + tool
           cards + tool outputs) is auto-parked after a short foreground
           window and continues in the background beyond it — keeping a
           truncated snapshot inline (frozen at park time) confuses
           users who expected it to keep updating live.

           Instead the header + preview + "Open in new tab" pill (row 1
           / row 2 above) act as a lightweight signpost.  Users who
           want the full live transcript click "Open in new tab" and
           get the dedicated sub-agent tab — a wider view, independent
           scroll, and complete history with no truncation.

           The main-agent LLM has the parallel ``sub_agent action=inspect``
           tool (returns a concise markdown transcript so it can answer
           "how is the sub-agent doing?" questions) plus ``list`` /
           ``status`` / ``wait`` / ``stop`` — the reference design's
           ``history://<id>`` semantics via a single tool.  So both the
           user path and the main-agent path stay first-class; only the
           misleading inline live-transcript is gone.

           Error text is still surfaced here so a failed sub-agent's
           message reaches the user without them having to click through.

           N.37: when the backend classified the failure (``errorKind``), the
           card renders the TYPED form — per-kind icon + colour + a recovery
           hint — so the user learns WHAT kind of failure this was and what to
           do next, instead of reading a bare red sentence. A legacy /
           unclassified failure keeps the plain single-line form below (the two
           are mutually exclusive: ``typedError`` is non-null only when a kind
           is present). -->
      <div
        v-if="typedError !== null"
        class="subagent-block-error subagent-block-error--typed"
        :style="{ color: `var(${typedError.colorVar})` }"
        :data-testid="`subagent-error-${typedError.kind}`"
        :data-error-kind="typedError.kind"
      >
        <div class="subagent-block-error-head">
          <span class="subagent-block-error-icon" aria-hidden="true">{{
            typedError.icon
          }}</span>
          <span class="subagent-block-error-message">{{
            typedError.message
          }}</span>
        </div>
        <div
          v-if="typedError.hint"
          class="subagent-block-error-hint"
          data-testid="subagent-error-hint"
        >{{ typedError.hint }}</div>
      </div>
      <div
        v-else-if="block.error"
        class="subagent-block-error"
      >
        {{ block.error }}
      </div>
    </div>
  </div>
</template>

<style scoped>
/*
 * V2 UX redesign (2026-07-01):
 *   - Left status-colour strip (3px) replaces the full-border tint —
 *     communicates the block state without the "coloured box" heaviness.
 *   - Inline SVG status icons (14px) tinted with `--info` / `--warning` /
 *     `--success` / `--error` — the running spinner has its own animation.
 *   - Type badge = capsule pill with a subtle accent-tinted background.
 *   - All colours use existing design tokens (`--border` / `--space-*` /
 *     `--info` / `--success` / `--warning` / `--error` / `--text-*` /
 *     `--bg-secondary` / `--bg-tertiary` / `--bg-hover`) so light/dark
 *     themes track without extra work.
 */
.subagent-block {
  display: flex;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: var(--space-2);
  overflow: hidden;
  background: var(--bg-secondary);
  font-size: 0.88em;
  transition: border-color var(--transition), box-shadow var(--transition);
}
.subagent-block:hover { border-color: color-mix(in srgb, var(--border) 60%, var(--text-secondary)); }

/* Left status-colour strip — 3px accent bar communicating the block state. */
.subagent-block-strip {
  flex: 0 0 3px;
  background: var(--border);
  transition: background var(--transition);
}
.subagent-block.subagent-status-running  .subagent-block-strip { background: var(--info); }
/* N.18 `background` = detached spawn. Same live spinner as `running`; the
   accent is a full `--info` OUTLINE (not just the strip) so a block the
   parent turn is not blocked on is distinguishable at a glance. */
.subagent-block.subagent-status-background .subagent-block-strip { background: var(--info); }
.subagent-block.subagent-status-aborting .subagent-block-strip { background: var(--warning); }
.subagent-block.subagent-status-done     .subagent-block-strip { background: var(--success); }
.subagent-block.subagent-status-error    .subagent-block-strip { background: var(--error); }
.subagent-block.subagent-status-background {
  border-color: color-mix(in srgb, var(--info) 55%, transparent);
}
.subagent-block.subagent-status-background:hover {
  border-color: var(--info);
}

.subagent-block-content-wrap {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.subagent-block-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 8px var(--space-3);
  cursor: pointer;
  user-select: none;
  background: var(--bg-tertiary);
  transition: background var(--transition);
}
/* Header highlights on hover of the ENTIRE card (not just header) so the
   preview row on line 2 and the header on line 1 read as ONE unit — hovering
   the preview must not leave the header looking cold/detached from the
   preview. Since the preview is a sibling of the header (not nested inside
   it), we can't rely on ``.subagent-block-header:hover`` alone. */
.subagent-block:hover .subagent-block-header { background: var(--bg-hover); }
.subagent-block-header:focus-visible {
  /* inset box-shadow — an ``outline`` would get clipped in the four
     corners by the wrapper's ``border-radius: 8px + overflow: hidden``,
     leaving a broken focus ring. box-shadow ignores overflow clipping. */
  outline: none;
  box-shadow: inset 0 0 0 2px var(--info);
}

/* Status icon: SVG tinted with the state colour. */
.subagent-block-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  color: var(--text-secondary);
}
.subagent-block.subagent-status-running  .subagent-block-icon { color: var(--info); }
.subagent-block.subagent-status-background .subagent-block-icon { color: var(--info); }
.subagent-block.subagent-status-aborting .subagent-block-icon { color: var(--warning); }
.subagent-block.subagent-status-done     .subagent-block-icon { color: var(--success); }
.subagent-block.subagent-status-error    .subagent-block-icon { color: var(--error); }

.subagent-icon-spinner {
  animation: subagent-spin 1s linear infinite;
}
@keyframes subagent-spin {
  from { transform: rotate(0deg); }
  to   { transform: rotate(360deg); }
}

/* Name: the primary title. Shrinkable (unlike the fixed pills to its right)
   so a very long name yields space to the preview / actions instead of
   forcing them off-screen; ``min-width: 0`` lets ``text-overflow: ellipsis``
   kick in when needed. No fixed % cap — narrow containers get proportional
   truncation via flex-shrink; wide containers show the full name. */
.subagent-block-name {
  font-weight: 600;
  color: var(--text-primary);
  min-width: 0;
  flex-shrink: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Parallel index "N / M" — only visible when the parent dispatched >1
   sub-agents. Small monospaced tag next to the name. */
.subagent-block-parallel {
  flex-shrink: 0;
  font-size: 0.78em;
  color: var(--text-secondary);
  font-family: var(--font-mono, ui-monospace, monospace);
  padding: 1px 6px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--border) 40%, transparent);
}

/* Type badge — i18n pill, one accent per known type. */
.subagent-block-type {
  flex-shrink: 0;
  font-size: 0.72em;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 999px;
  letter-spacing: 0.02em;
  line-height: 1.4;
  border: 1px solid transparent;
  white-space: nowrap;
}
.subagent-block-type--general {
  color: var(--info);
  background: color-mix(in srgb, var(--info) 14%, transparent);
  border-color: color-mix(in srgb, var(--info) 30%, transparent);
}
.subagent-block-type--explore {
  color: var(--accent, var(--info));
  background: color-mix(in srgb, var(--accent, var(--info)) 14%, transparent);
  border-color: color-mix(in srgb, var(--accent, var(--info)) 30%, transparent);
}
.subagent-block-type--neutral {
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--border) 50%, transparent);
  border-color: var(--border);
}

.subagent-block-rounds {
  flex-shrink: 0;
  color: var(--text-secondary);
  font-size: 0.8em;
}

/* Detached-spawn chip — reads as a live-state label, tinted with `--info`
   to match the block outline. */
.subagent-block-background {
  flex-shrink: 0;
  padding: 0 var(--space-2);
  border: 1px solid color-mix(in srgb, var(--info) 30%, transparent);
  border-radius: 999px;
  background: color-mix(in srgb, var(--info) 14%, transparent);
  color: var(--info);
  font-size: 0.78em;
  line-height: 1.6;
  white-space: nowrap;
}
/* Prompt preview — the "what is this sub-agent working on" details.
   Standalone SECOND ROW (below the header), NOT a flex item inside the
   header. Two goals:
     1) Give the name/badges/actions the full header width so they don't
        get crushed into 3-char ellipsis by a long prompt.
     2) Give the prompt real space (up to 3 lines) so the user can
        identify the task at a glance even while the block is collapsed —
        the whole point of showing a preview here.
   Visible in BOTH collapsed and expanded states. The full text lives on
   the ``title`` attribute so the user can hover to see the untruncated
   prompt. The expanded body does NOT repeat this text (it only shows the
   sub-agent's OUTPUT / tools / errors). */
.subagent-block-preview {
  padding: 4px var(--space-3) 8px;
  color: var(--text-secondary);
  font-size: 0.85em;
  line-height: 1.5;
  /* Do NOT use ``white-space: pre-wrap`` here — it conflicts with
     ``display: -webkit-box`` / ``-webkit-line-clamp``: pre-wrap's hard
     line-breaks count as visual lines, so a prompt with 3 ``\n`` chars
     would exhaust the 3-line budget immediately; and on Chromium the
     combination can cause line-clamp to stop working entirely (the box
     never collapses). ``normal`` lets the browser reflow the text
     naturally so the clamp counts rendered lines correctly. Long paths /
     URLs are handled by ``overflow-wrap: break-word`` below — this only
     breaks a word when it is genuinely wider than the line (e.g. a long
     path); ordinary English/CJK text still breaks on word/character
     boundaries so readability is preserved. ``anywhere`` is deliberately
     avoided because it would arbitrarily fragment CJK text and short
     English words mid-string. */
  white-space: normal;
  overflow-wrap: break-word;
  word-break: break-word;
  /* Multi-line ellipsis (3 lines). ``-webkit-line-clamp`` is well-
     supported in modern Chromium / Firefox / WebKit despite the vendor
     prefix. When the prompt is shorter than 3 lines the box collapses
     naturally to fit — the 3 lines is an UPPER bound, not a min-height. */
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* When the block is expanded, the preview sits above the tool timeline
   and needs a subtle bottom separator so the eye finds where the
   sub-agent's OUTPUT actually starts. Collapsed state has no body under
   it, so no separator needed there. */
.subagent-block-content-wrap > .subagent-block-preview + .subagent-block-body {
  border-top: 1px solid var(--border);
  padding-top: var(--space-2);
}

/* Right-aligned action group. */
.subagent-block-actions {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

/* Open-in-new-tab pill: subtle by default, accent-tinted on hover. */
.subagent-block-open {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--border);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  cursor: pointer;
  padding: 3px 9px;
  border-radius: 999px;
  font-size: 0.76em;
  font-weight: 500;
  line-height: 1.4;
  white-space: nowrap;
  transition:
    background var(--transition),
    color var(--transition),
    border-color var(--transition);
}
.subagent-block-open:hover {
  background: color-mix(in srgb, var(--info) 14%, transparent);
  border-color: var(--info);
  color: var(--info);
}
.subagent-block-open:focus-visible {
  outline: 2px solid var(--info);
  outline-offset: 1px;
}

/* Stop pill — danger-tinted, only shown while running / aborting. */
.subagent-block-stop {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--border);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  cursor: pointer;
  padding: 3px 9px;
  border-radius: 999px;
  font-size: 0.76em;
  font-weight: 500;
  line-height: 1.4;
  white-space: nowrap;
  transition:
    background var(--transition),
    color var(--transition),
    border-color var(--transition);
}
.subagent-block-stop:hover {
  background: color-mix(in srgb, var(--error) 14%, transparent);
  border-color: var(--error);
  color: var(--error);
}
.subagent-block-stop:focus-visible {
  outline: 2px solid var(--error);
  outline-offset: 1px;
}
/* Aborting state — warning tint, disabled cursor. */
.subagent-block-stop--aborting,
.subagent-block-stop[disabled] {
  cursor: not-allowed;
  background: color-mix(in srgb, var(--warning) 12%, transparent);
  border-color: var(--warning);
  color: var(--warning);
}
.subagent-block-stop--aborting:hover,
.subagent-block-stop[disabled]:hover {
  background: color-mix(in srgb, var(--warning) 12%, transparent);
  border-color: var(--warning);
  color: var(--warning);
}

/* Toggle chevron — a right-pointing arrow that rotates to point down
   when the block is expanded (a smooth 0.15s transition). Rotation is
   driven by a class modifier + a dedicated CSS rule (NOT an inline
   `:style` binding — mixing `:style` with a static `style` attribute
   loses the `transition` on state change in Vue 3, breaking the
   animation on expand). */
.subagent-block-toggle {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  color: var(--text-secondary);
}
.subagent-block-toggle-icon {
  transform: rotate(0deg);
  transition: transform 0.15s;
}
.subagent-block-toggle-icon--expanded {
  transform: rotate(90deg);
}

/* Body — appears when the block is expanded. Separator between the
   header/preview area and the body is applied by ONE of two sibling
   rules depending on which siblings are actually rendered:
     * preview present  → ``preview + body`` rule (line ~555) applies
     * preview absent   → ``header + body`` rule below applies
   Exactly one of the two matches at a time (they are mutually
   exclusive by DOM adjacency), so the body always gets exactly one
   top border. Never both, never neither. */
.subagent-block-body {
  padding: var(--space-2) var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.subagent-block-header + .subagent-block-body {
  border-top: 1px solid var(--border);
}

.subagent-block-content {
  margin-top: var(--space-1);
  padding-top: var(--space-1);
  border-top: 1px dashed var(--border);
  color: var(--text-primary);
  font-size: 0.9em;
}
.subagent-block-content:first-child {
  margin-top: 0;
  padding-top: 0;
  border-top: none;
}
.subagent-block-error {
  color: var(--error);
  font-size: 0.88em;
  padding: var(--space-1) 0;
}

/* N.37 — typed failure card. The per-kind colour arrives as an inline
   `color: var(--<token>)` binding (the token name is data, so it cannot be a
   static rule); everything structural lives here. `currentColor` derivations
   mean the icon, the hint and the tinted background all follow whichever
   token the kind selected — one binding, four coordinated surfaces. */
.subagent-block-error--typed {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-1) var(--space-2);
  border-left: 2px solid currentColor;
  border-radius: 0 4px 4px 0;
  background: color-mix(in srgb, currentColor 8%, transparent);
}
.subagent-block-error-head {
  display: flex;
  align-items: baseline;
  gap: var(--space-1);
}
.subagent-block-error-icon {
  flex-shrink: 0;
  font-size: 0.95em;
  line-height: 1.3;
}
.subagent-block-error-message {
  min-width: 0;
  font-weight: 500;
  overflow-wrap: anywhere;
}
/* Hint is de-emphasised relative to the cause: the user reads WHAT failed
   first, then HOW to act. Indented to the message's text column so the two
   lines read as one block rather than two peers. */
.subagent-block-error-hint {
  padding-left: calc(0.95em + var(--space-1));
  color: var(--text-secondary);
  font-size: 0.92em;
  overflow-wrap: anywhere;
}
</style>

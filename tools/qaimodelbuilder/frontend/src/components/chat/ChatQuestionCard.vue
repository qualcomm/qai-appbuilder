<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ChatQuestionCard — in-conversation, paginated question card driven by the
 * `question` chat tool (V2 enhancement; V1 has no equivalent).
 *
 * Rendered inside the assistant message's tool-call area (ChatMessageList →
 * ToolCallList) in place of the generic ToolExecPanel whenever
 * `call.tool === "question"`, mirroring how `todowrite` renders TaskListCard.
 *
 * Replaces the former centred modal (ChatQuestionDialog): the card is a
 * full-width surface that scrolls with the conversation. It supports the
 * backend's NEW multi-question wire shape (`arguments.questions = [...]`) as
 * well as the LEGACY single-question shape (parsed in `parseQuestions`).
 *
 * Three states:
 *   - ANSWERING — per-question option list (radio/checkbox) + a free-text
 *     "type your own answer" entry with image paste/attach; paginated across
 *     questions with prev/next + a collapse/expand chevron.
 *   - REVIEW    — "Check your answers": a summary of every question's chosen
 *     answer with Back / Submit.
 *   - ANSWERED  — read-only: each question lists all options with the chosen
 *     ones ticked, plus any custom text + image thumbnails (click → lightbox).
 *
 * The card is interactive while it has NO recorded answer (`result` empty);
 * once the tool call's `result` (the answer string) is present — live after
 * submit OR rehydrated from history — it renders the read-only ANSWERED state.
 * The answer is composed front-end into ONE round-trippable string (the
 * backend feeds `answer` to the model verbatim — see `composeAnswer` /
 * `parseAnswer`). Theme tokens only (AGENTS.md §3.10 / §5.3).
 */
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useChatTabsStore } from "@/stores/chatTabs";
import {
  composeAnswer,
  parseAnswer,
  parseQuestions,
  stripAnswerToolResultPrefix,
} from "@/stores/chatTabs/harnessToolFrames";
import type { PendingQuestionItem } from "@/stores/_chatTabsTypes";
import { useQuestionImages } from "@/composables/chat/useQuestionImages";
import { useLightbox } from "@/composables/useLightbox";

const props = defineProps<{
  /** Raw `arguments` of the question tool call (`{ questions: [...] }` or the
   *  legacy single-question shape). */
  args: Record<string, unknown>;
  /** The tool call's recorded output = the answer string. Non-empty ⇒ the
   *  card renders the read-only ANSWERED state (live after submit / history). */
  result?: string;
  /** Originating `tool_call` frame id — compared against the tab's active
   *  pending-question pointer to know if THIS card is the live one. */
  frameId?: string;
  /** Owning tab id (so submit can route through `store.answerQuestion`). */
  tabId?: string;
}>();

const { t } = useI18n();
const store = useChatTabsStore();

// ── Question definitions (normalised; ≥1 entry) ───────────────────────────
const questions = computed<PendingQuestionItem[]>(() => parseQuestions(props.args));
const total = computed(() => questions.value.length);
const isMulti = computed(() => total.value > 1);

// ── Answered (read-only) detection ────────────────────────────────────────
// The tool RESULT fed back to the model is wrapped by the backend
// (`QuestionToolHandler.execute` in `src/qai/chat/adapters/harness_tools.py`)
// as `The user answered: <composed>`. `stripAnswerToolResultPrefix` removes
// that fixed prefix so `parseAnswer` sees `Q1 (...)` at the start of the
// first line — otherwise `^Q\d+` fails and Q1's answer + images get lost.
const answerString = computed(() =>
  stripAnswerToolResultPrefix((props.result ?? "").trim()),
);
const isAnswered = computed(() => (props.result ?? "").trim() !== "");

// ── Per-question working answer state (ANSWERING) ──────────────────────────
// `selected[i]` = Set of checked option labels for question i.
// `custom[i]`   = free-text answer for question i.
const selected = reactive<Record<number, Set<string>>>({});
const custom = reactive<Record<number, string>>({});
const page = ref(0);
const inReview = ref(false);
const collapsed = ref(false);

function resetWorkingState(): void {
  for (const k of Object.keys(selected)) delete selected[Number(k)];
  for (const k of Object.keys(custom)) delete custom[Number(k)];
  for (let i = 0; i < total.value; i++) {
    selected[i] = new Set<string>();
    custom[i] = "";
  }
  page.value = 0;
  inReview.value = false;
}

// Re-seed whenever a NEW question batch becomes active (frame id changes) or
// the question count changes. Skip while answered (read-only needs no state).
watch(
  () => [props.frameId, total.value, isAnswered.value] as const,
  () => {
    if (!isAnswered.value) resetWorkingState();
  },
  { immediate: true },
);

// ── Images (per-QUESTION queues) ──────────────────────────────────────────
// Each question owns an INDEPENDENT pending-image queue (Bug 1+2 fix): images
// added on Q1 / Q2 stay bound to their own question and are uploaded + folded
// into THAT question's answer at submit, instead of all merging into whatever
// page was open. `useQuestionImages` deliberately does NOT drain the global
// App-Builder→Chat intake queue (the card's images are user-added in-card), so
// there is no double-consume race. A single shared hidden file input is reused:
// the picker is only ever triggered from the current page, so the `@change`
// handler ingests into `page.value`.
const fileInputRef = ref<HTMLInputElement | null>(null);
const titleSeed = ref("");
watch(
  () => custom[page.value] ?? "",
  (v) => {
    titleSeed.value = v;
  },
);
const images = useQuestionImages(titleSeed, fileInputRef);

const lightbox = useLightbox();

// ── Helpers ───────────────────────────────────────────────────────────────
function optionDescription(opt: PendingQuestionItem["options"][number]): string {
  return opt.description ?? "";
}

/** The user touched the card (picked an option, typed a custom answer, pasted
 *  / attached an image, paged through questions). That means they are BACK at
 *  the computer, so the away auto-answer countdown for THIS question must stop
 *  immediately — they will answer manually now. Implemented as a per-frame
 *  suppression (same mechanism as the explicit "skip auto-answer" ✕), which
 *  flips `countdownActive` to false and clears the timer via its watcher. The
 *  tab-level switch stays ON so the NEXT question still auto-answers if the
 *  user steps away again. No-op when the feature is off / not counting.
 *
 *  Note: `countdownActive` (computed) is defined later in setup() but is only
 *  READ here at event time (well after setup runs), so there is no TDZ risk.
 *  Vue tolerates this forward reference for the same reason event handlers can
 *  reference any in-scope binding regardless of declaration order. */
function markUserEngaged(): void {
  const id = props.tabId;
  if (id === undefined || id === "" || props.frameId === undefined) return;
  if (!countdownActive.value) return;
  store.suppressAwayAutoAnswerForFrame(id, props.frameId);
}

/** Custom-textarea paste handler — counts as engagement (pasting an image or
 *  text is "the user is back"), then forwards the event to the image hook. */
function onCustomPaste(qi: number, event: ClipboardEvent): void {
  markUserEngaged();
  images.handlePaste(qi, event);
}

/** Attach button — opens the file picker and counts as engagement. */
function onAttachClick(): void {
  markUserEngaged();
  images.openFilePicker(page.value);
}

function toggleSelect(qi: number, label: string, multiple: boolean): void {
  markUserEngaged();
  const set = selected[qi] ?? new Set<string>();
  if (multiple) {
    const next = new Set(set);
    if (next.has(label)) next.delete(label);
    else next.add(label);
    selected[qi] = next;
  } else {
    // Single-select: replace.
    selected[qi] = set.has(label) ? new Set<string>() : new Set<string>([label]);
  }
}

function isSelected(qi: number, label: string): boolean {
  return selected[qi]?.has(label) ?? false;
}

/** The joined answer text for one question (selected labels + custom text). */
function answerForQuestion(qi: number): string {
  const parts = [...(selected[qi] ?? new Set<string>())];
  const c = (custom[qi] ?? "").trim();
  if (c !== "") parts.push(c);
  return parts.join(", ");
}

const currentQuestion = computed<PendingQuestionItem | undefined>(
  () => questions.value[page.value],
);

const isFirstPage = computed(() => page.value === 0);
const isLastPage = computed(() => page.value >= total.value - 1);

/** Whether the current page has SOME answer: a selected option, custom text,
 *  OR at least one attached image (images count as an answer so a page with
 *  only an image is not blocked from advancing / submitting). */
const currentHasAnswer = computed(
  () =>
    answerForQuestion(page.value).trim() !== "" ||
    images.imagesFor(page.value).length > 0,
);

function goPrev(): void {
  markUserEngaged();
  if (page.value > 0) page.value--;
}
function goNext(): void {
  markUserEngaged();
  if (page.value < total.value - 1) page.value++;
}

function enterReview(): void {
  markUserEngaged();
  inReview.value = true;
}
function backFromReview(): void {
  markUserEngaged();
  inReview.value = false;
}

async function submit(): Promise<void> {
  const id = props.tabId;
  if (id === undefined || id === "") return;
  // Upload EACH question's own images and fold their markdown into THAT
  // question's custom answer (per-question binding — Bug 1+2 fix). Sequential
  // per question keeps the lazy conversation-create idempotent.
  for (let i = 0; i < questions.value.length; i++) {
    const md = await images.uploadFor(i);
    if (md !== "") {
      custom[i] = `${(custom[i] ?? "").trim()}\n${md}`.trim();
    }
  }
  const answers = questions.value.map((_, i) => answerForQuestion(i));
  const composed = composeAnswer(questions.value, answers);
  if (composed.trim() === "") return;
  store.answerQuestion(id, composed);
}

function skip(): void {
  const id = props.tabId;
  if (id === undefined || id === "") return;
  store.answerQuestion(id, t("chat.question.skipped"));
}

// ── Away auto-answer countdown (V2 enhancement; per-tab, default-off) ───────
// When the OWNING tab has away auto-answer enabled, an ACTIVE (answering)
// question card counts down `timeoutSeconds` and, on expiry, auto-answers with
// the tab's preset prompt (or the current-locale default) through the SAME
// `store.answerQuestion` path a manual answer uses — so the model keeps going
// instead of stalling. Strictly per-tab: a card reads only its own tab's
// settings. Never fires for a read-only / review card, a non-active frame, or
// a frame the user has suppressed ("don't auto-answer this question").
const owningTab = computed(() =>
  props.tabId === undefined || props.tabId === ""
    ? undefined
    : store.tabs.find((tb) => tb.id === props.tabId),
);

const awaySettings = computed(() => owningTab.value?.awayAutoAnswer);

/** Is THIS card the live, answerable one (active frame, still answering)? */
const isLiveAnswerable = computed(
  () =>
    !isAnswered.value &&
    !inReview.value &&
    props.tabId !== undefined &&
    props.tabId !== "" &&
    props.frameId !== undefined &&
    props.frameId !== "" &&
    owningTab.value?.pendingQuestion?.frameId === props.frameId,
);

/** This exact frame was suppressed via "don't auto-answer this question". */
const isSuppressed = computed(
  () =>
    props.frameId !== undefined &&
    owningTab.value?.awayAutoAnswerSuppressedFrameId === props.frameId,
);

/** Countdown should run: feature on for this tab, card live, not suppressed. */
const countdownActive = computed(
  () =>
    awaySettings.value?.enabled === true &&
    isLiveAnswerable.value &&
    !isSuppressed.value,
);

const remainingSeconds = ref(0);
let countdownTimer: ReturnType<typeof setInterval> | null = null;

function clearCountdown(): void {
  if (countdownTimer !== null) {
    clearInterval(countdownTimer);
    countdownTimer = null;
  }
}

/** The formatted MM:SS for the countdown hint line. */
const remainingLabel = computed(() => {
  const total = Math.max(0, remainingSeconds.value);
  const mm = Math.floor(total / 60);
  const ss = total % 60;
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
});

function fireAutoAnswer(): void {
  clearCountdown();
  const id = props.tabId;
  const tab = owningTab.value;
  // Re-validate at fire time (race guard): the tab may have answered / switched
  // / disabled / suppressed between the last tick and now.
  if (
    id === undefined ||
    id === "" ||
    tab === undefined ||
    tab.awayAutoAnswer?.enabled !== true ||
    tab.pendingQuestion?.frameId !== props.frameId ||
    tab.awayAutoAnswerSuppressedFrameId === props.frameId
  ) {
    return;
  }
  const preset = (tab.awayAutoAnswer.prompt ?? "").trim();
  const text =
    preset !== "" ? preset : t("chat.awayQuestionAutoAnswer.defaultPrompt");
  store.answerQuestion(id, text);
}

// Start / restart the countdown whenever it becomes active (or its driving
// frame changes); stop + reset otherwise. A snapshot of `timeoutSeconds` is
// taken at start (settings edited mid-countdown apply to the NEXT question,
// matching the plan) — re-keying on frameId restarts cleanly for a new card.
watch(
  () => [countdownActive.value, props.frameId] as const,
  ([active]) => {
    clearCountdown();
    if (!active) {
      remainingSeconds.value = 0;
      return;
    }
    const secs = awaySettings.value?.timeoutSeconds ?? 0;
    remainingSeconds.value = secs;
    if (secs <= 0) {
      fireAutoAnswer();
      return;
    }
    countdownTimer = setInterval(() => {
      remainingSeconds.value -= 1;
      if (remainingSeconds.value <= 0) {
        fireAutoAnswer();
      }
    }, 1000);
  },
  { immediate: true },
);

onBeforeUnmount(clearCountdown);

/** User clicked "don't auto-answer this question" — suppress only this frame
 *  (the tab-level switch stays on for the next question). */
function skipAutoAnswerThisQuestion(): void {
  const id = props.tabId;
  if (id === undefined || id === "" || props.frameId === undefined) return;
  store.suppressAwayAutoAnswerForFrame(id, props.frameId);
}

// ── Read-only (ANSWERED) derivation ────────────────────────────────────────
interface AnsweredView {
  question: PendingQuestionItem;
  /** Option labels the user chose (matched against parsed answer). */
  chosen: Set<string>;
  /** Free-text remainder (custom answer minus matched option labels). */
  customText: string;
  /** Image urls extracted from the custom text markdown. */
  images: { alt: string; url: string }[];
}

const IMAGE_MD_RE = /!\[([^\]]*)\]\(([^)\s]+)\)/g;

function extractImagesFrom(text: string): { alt: string; url: string }[] {
  const out: { alt: string; url: string }[] = [];
  IMAGE_MD_RE.lastIndex = 0;
  let m = IMAGE_MD_RE.exec(text);
  while (m !== null) {
    const url = m[2];
    if (url !== undefined && url !== "") out.push({ alt: m[1] ?? "", url });
    m = IMAGE_MD_RE.exec(text);
  }
  return out;
}

const answeredViews = computed<AnsweredView[]>(() => {
  if (!isAnswered.value) return [];
  const perQuestion = parseAnswer(questions.value, answerString.value);
  return questions.value.map((q, i) => {
    const raw = perQuestion[i] ?? "";
    // `answerForQuestion` joins selected option labels + the custom text with
    // ", ", and any multi-line custom text / image markdown lives on the lines
    // AFTER the first (uploadFor appends image md on its own line).
    // So option labels can only appear in the FIRST line; split that against
    // the labels and treat the rest (plus any non-label first-line parts) as
    // free text. This avoids a multi-line custom answer swallowing a label.
    const firstNewline = raw.indexOf("\n");
    const firstLine = firstNewline === -1 ? raw : raw.slice(0, firstNewline);
    const restLines = firstNewline === -1 ? "" : raw.slice(firstNewline + 1);
    const chosen = new Set<string>();
    const parts = firstLine.split(",").map((p) => p.trim()).filter((p) => p !== "");
    const remaining: string[] = [];
    for (const part of parts) {
      if (q.options.some((o) => o.label === part)) chosen.add(part);
      else remaining.push(part);
    }
    // Custom text = leftover first-line parts (joined) + the continuation
    // lines verbatim (preserves multi-line text and image markdown).
    const customJoined = [remaining.join(", "), restLines]
      .filter((s) => s.trim() !== "")
      .join("\n");
    return {
      question: q,
      chosen,
      customText: customJoined.replace(IMAGE_MD_RE, "").trim(),
      images: extractImagesFrom(customJoined),
    };
  });
});

function openImage(url: string): void {
  lightbox.open(url);
}
</script>

<template>
  <div
    v-if="total > 0"
    class="question-card"
    data-testid="chat-question-card"
  >
    <!-- ───────────────────────── ANSWERED (read-only) ─────────────────── -->
    <template v-if="isAnswered">
      <!-- Header is click-to-toggle (V2 enhancement): clicking anywhere on it
           (title text + chevron) flips `collapsed`, matching the ANSWERING
           state's existing chevron behaviour. Keeps `--static` for backwards
           compatible styling but layers a button-like affordance on top. -->
      <div
        class="question-card-header question-card-header--static question-card-header--clickable"
        role="button"
        tabindex="0"
        :aria-expanded="!collapsed"
        :aria-label="t('chat.question.collapse')"
        data-testid="chat-question-header-toggle"
        @click="collapsed = !collapsed"
        @keydown.enter.prevent="collapsed = !collapsed"
        @keydown.space.prevent="collapsed = !collapsed"
      >
        <span class="question-card-title">
          <svg
            class="question-card-glyph"
            viewBox="0 0 24 24"
            aria-hidden="true"
            focusable="false"
          >
            <circle cx="12" cy="12" r="10" class="question-card-glyph-bg" />
            <path
              d="M9.2 9.2c0-1.6 1.3-2.7 2.9-2.7s2.8 1 2.8 2.5c0 1.2-.6 1.9-1.7 2.6-1 .6-1.4 1.2-1.4 2.1v.4"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
            />
            <circle cx="12" cy="17.2" r="1.05" fill="currentColor" />
          </svg>
          {{ t("chat.question.answeredTitle", { count: total }) }}
        </span>
        <span class="question-card-nav">
          <span
            class="question-card-arrow question-card-arrow--static"
            aria-hidden="true"
          >{{ collapsed ? "▾" : "▴" }}</span>
        </span>
      </div>
      <div
        v-if="!collapsed"
        class="question-card-body"
      >
        <div
          v-for="(av, qi) in answeredViews"
          :key="qi"
          class="question-answered-item"
        >
          <p class="question-answered-q">
            <span v-if="isMulti" class="question-answered-index">{{ qi + 1 }}.</span>
            {{ av.question.question }}
          </p>
          <ul
            v-if="av.question.options.length > 0"
            class="question-answered-options"
          >
            <li
              v-for="(opt, oi) in av.question.options"
              :key="oi"
              class="question-answered-option"
              :class="{ 'question-answered-option--chosen': av.chosen.has(opt.label) }"
            >
              <span class="question-answered-check">{{ av.chosen.has(opt.label) ? "✓" : "" }}</span>
              <span class="question-answered-label">{{ opt.label }}</span>
              <span
                v-if="optionDescription(opt) !== ''"
                class="question-answered-desc"
              >{{ optionDescription(opt) }}</span>
            </li>
          </ul>
          <p
            v-if="av.customText !== ''"
            class="question-answered-custom"
          >{{ av.customText }}</p>
          <div
            v-if="av.images.length > 0"
            class="question-answered-images"
          >
            <img
              v-for="(im, ii) in av.images"
              :key="ii"
              :src="im.url"
              :alt="im.alt"
              class="question-answered-thumb"
              @click="openImage(im.url)"
            >
          </div>
        </div>
      </div>
    </template>

    <!-- ───────────────────────── REVIEW ──────────────────────────────── -->
    <template v-else-if="inReview">
      <!-- Header is click-to-toggle (V2 enhancement); the footer (Back /
           Submit) stays visible even when collapsed so the user is never
           locked out of advancing the question. -->
      <div
        class="question-card-header question-card-header--static question-card-header--clickable"
        role="button"
        tabindex="0"
        :aria-expanded="!collapsed"
        :aria-label="t('chat.question.collapse')"
        data-testid="chat-question-header-toggle"
        @click="collapsed = !collapsed"
        @keydown.enter.prevent="collapsed = !collapsed"
        @keydown.space.prevent="collapsed = !collapsed"
      >
        <span class="question-card-title">
          <svg
            class="question-card-glyph"
            viewBox="0 0 24 24"
            aria-hidden="true"
            focusable="false"
          >
            <circle cx="12" cy="12" r="10" class="question-card-glyph-bg" />
            <path
              d="M9.2 9.2c0-1.6 1.3-2.7 2.9-2.7s2.8 1 2.8 2.5c0 1.2-.6 1.9-1.7 2.6-1 .6-1.4 1.2-1.4 2.1v.4"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
            />
            <circle cx="12" cy="17.2" r="1.05" fill="currentColor" />
          </svg>
          {{ t("chat.question.reviewTitle") }}
        </span>
        <span class="question-card-nav">
          <span
            class="question-card-arrow question-card-arrow--static"
            aria-hidden="true"
          >{{ collapsed ? "▾" : "▴" }}</span>
        </span>
      </div>
      <div
        v-if="!collapsed"
        class="question-card-body"
      >
        <div
          v-for="(q, qi) in questions"
          :key="qi"
          class="question-review-item"
        >
          <p class="question-review-q">
            <span v-if="isMulti" class="question-answered-index">{{ qi + 1 }}.</span>
            {{ q.question }}
          </p>
          <p
            class="question-review-a"
            :class="{ 'question-review-a--empty': answerForQuestion(qi).trim() === '' }"
          >
            {{ answerForQuestion(qi).trim() || t("chat.question.noAnswer") }}
          </p>
          <!-- Pending images attached to THIS question (not yet uploaded;
               shown so the review reflects what will be submitted). -->
          <div
            v-if="images.imagesFor(qi).length > 0"
            class="question-review-images"
          >
            <img
              v-for="img in images.imagesFor(qi)"
              :key="img.id"
              :src="img.dataUrl"
              :alt="img.name"
              :title="img.name"
              class="question-answered-thumb"
              @click="openImage(img.dataUrl)"
            >
          </div>
        </div>
      </div>
      <div class="question-card-footer">
        <button
          type="button"
          class="btn btn-ghost"
          data-testid="chat-question-back"
          @click="backFromReview"
        >
          {{ t("chat.question.back") }}
        </button>
        <button
          type="button"
          class="btn btn-primary"
          data-testid="chat-question-submit"
          @click="submit"
        >
          {{ t("chat.question.submit") }}
        </button>
      </div>
    </template>

    <!-- ───────────────────────── ANSWERING ────────────────────────────── -->
    <template v-else>
      <div class="question-card-header">
        <!-- Title is click-to-toggle (V2 enhancement): clicking the title area
             flips `collapsed`, matching ANSWERED/REVIEW. The away-countdown
             badge and the nav buttons (prev / next / chevron) live alongside
             it inside the header — they handle their own clicks and never
             bubble into this toggle because they are siblings, not children. -->
        <span
          class="question-card-title question-card-title--clickable"
          role="button"
          tabindex="0"
          :aria-expanded="!collapsed"
          :aria-label="t('chat.question.collapse')"
          data-testid="chat-question-header-toggle"
          @click="collapsed = !collapsed"
          @keydown.enter.prevent="collapsed = !collapsed"
          @keydown.space.prevent="collapsed = !collapsed"
        >
          <svg
            class="question-card-glyph"
            viewBox="0 0 24 24"
            aria-hidden="true"
            focusable="false"
          >
            <circle cx="12" cy="12" r="10" class="question-card-glyph-bg" />
            <path
              d="M9.2 9.2c0-1.6 1.3-2.7 2.9-2.7s2.8 1 2.8 2.5c0 1.2-.6 1.9-1.7 2.6-1 .6-1.4 1.2-1.4 2.1v.4"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
            />
            <circle cx="12" cy="17.2" r="1.05" fill="currentColor" />
          </svg>
          <template v-if="isMulti">
            {{ t("chat.question.progress", { index: page + 1, total }) }}
          </template>
          <template v-else>
            {{ currentQuestion?.header || t("chat.question.title") }}
          </template>
        </span>
        <!-- Away auto-answer countdown badge — V2 enhancement. Sits in the
             header (not the footer) so it stays visible above the fold even on
             long question cards; high-contrast accent so the "auto-answer is
             pending" state is immediately obvious. -->
        <span
          v-if="countdownActive"
          class="question-card-away-badge"
          data-testid="chat-question-away-hint"
          :title="t('chat.awayQuestionAutoAnswer.countdown', { time: remainingLabel })"
        >
          <svg
            class="question-card-away-badge-icon"
            viewBox="0 0 24 24"
            aria-hidden="true"
            focusable="false"
          >
            <circle cx="12" cy="13" r="8" fill="none" stroke="currentColor" stroke-width="1.8" />
            <path
              d="M12 9v4l2.5 2.5"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
            <path
              d="M5 3 2 6"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
            />
            <path
              d="M22 6l-3-3"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
            />
          </svg>
          <span class="question-card-away-badge-time">{{ remainingLabel }}</span>
          <button
            type="button"
            class="question-card-away-badge-skip"
            data-testid="chat-question-away-skip"
            :title="t('chat.awayQuestionAutoAnswer.skipThisQuestion')"
            :aria-label="t('chat.awayQuestionAutoAnswer.skipThisQuestion')"
            @click.stop="skipAutoAnswerThisQuestion"
          >✕</button>
        </span>
        <span class="question-card-nav">
          <button
            v-if="isMulti"
            type="button"
            class="question-card-arrow"
            :disabled="isFirstPage"
            :aria-label="t('chat.question.prev')"
            data-testid="chat-question-page-prev"
            @click="goPrev"
          >‹</button>
          <button
            v-if="isMulti"
            type="button"
            class="question-card-arrow"
            :disabled="isLastPage"
            :aria-label="t('chat.question.next')"
            data-testid="chat-question-page-next"
            @click="goNext"
          >›</button>
          <button
            type="button"
            class="question-card-arrow"
            :aria-expanded="!collapsed"
            :aria-label="t('chat.question.collapse')"
            @click="collapsed = !collapsed"
          >{{ collapsed ? "▾" : "▴" }}</button>
        </span>
      </div>

      <div
        v-if="!collapsed && currentQuestion"
        class="question-card-body"
      >
        <p
          v-if="isMulti && (currentQuestion.header ?? '') !== ''"
          class="question-card-header-text"
        >{{ currentQuestion.header }}</p>
        <p class="question-card-q">{{ currentQuestion.question }}</p>
        <p class="question-card-sub">
          {{ currentQuestion.multiple ? t("chat.question.multiHint") : t("chat.question.singleHint") }}
        </p>

        <div
          v-if="currentQuestion.options.length > 0"
          class="question-card-options"
        >
          <label
            v-for="(opt, oi) in currentQuestion.options"
            :key="oi"
            class="question-card-option"
            :class="{ 'question-card-option--selected': isSelected(page, opt.label) }"
            data-testid="chat-question-option"
          >
            <input
              :type="currentQuestion.multiple ? 'checkbox' : 'radio'"
              class="question-card-option-input"
              :checked="isSelected(page, opt.label)"
              @change="toggleSelect(page, opt.label, currentQuestion.multiple)"
            >
            <span class="question-card-option-body">
              <span class="question-card-option-label">{{ opt.label }}</span>
              <span
                v-if="optionDescription(opt) !== ''"
                class="question-card-option-desc"
              >{{ optionDescription(opt) }}</span>
            </span>
          </label>
        </div>

        <!-- Free-text custom answer + image attach. -->
        <div class="question-card-custom">
          <label class="question-card-custom-label">
            {{ t("chat.question.customLabel") }}
          </label>
          <textarea
            v-model="custom[page]"
            class="question-card-custom-input"
            rows="2"
            :placeholder="t('chat.question.customPlaceholder')"
            data-testid="chat-question-custom"
            @input="markUserEngaged"
            @paste="onCustomPaste(page, $event)"
          />
          <div class="question-card-attach-row">
            <button
              type="button"
              class="question-card-attach-btn"
              data-testid="chat-question-attach"
              @click="onAttachClick"
            >
              📎 {{ t("chat.question.addImage") }}
            </button>
            <input
              ref="fileInputRef"
              type="file"
              accept="image/*"
              multiple
              class="question-card-file-input"
              @change="images.onFilesSelected(page, $event)"
            >
          </div>
          <div
            v-if="images.imagesFor(page).length > 0"
            class="question-card-thumbs"
          >
            <div
              v-for="img in images.imagesFor(page)"
              :key="img.id"
              class="question-card-thumb-item"
              :class="{ 'question-card-thumb-item--failed': img.failed }"
              :title="img.name"
            >
              <img
                :src="img.dataUrl"
                :alt="img.name"
                class="question-card-thumb"
                @click="openImage(img.dataUrl)"
              >
              <button
                type="button"
                class="question-card-thumb-remove"
                :aria-label="t('chat.question.removeImage')"
                @click="images.removeImage(page, img.id)"
              >✕</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Away auto-answer countdown lives in the HEADER (high-visibility),
           not down here in the footer area — see header badge above. -->

      <div
        v-if="!collapsed"
        class="question-card-footer"
      >
        <button
          type="button"
          class="btn btn-ghost"
          data-testid="chat-question-skip"
          @click="skip"
        >
          {{ t("chat.question.skip") }}
        </button>
        <span class="question-card-footer-spacer" />
        <button
          v-if="isMulti && !isFirstPage"
          type="button"
          class="btn btn-ghost"
          data-testid="chat-question-prev"
          @click="goPrev"
        >
          {{ t("chat.question.prevStep") }}
        </button>
        <button
          v-if="isMulti && !isLastPage"
          type="button"
          class="btn btn-primary"
          :disabled="!currentHasAnswer"
          data-testid="chat-question-next"
          @click="goNext"
        >
          {{ t("chat.question.nextStep") }}
        </button>
        <button
          v-else
          type="button"
          class="btn btn-primary"
          :disabled="!currentHasAnswer"
          data-testid="chat-question-review"
          @click="isMulti ? enterReview() : submit()"
        >
          {{ isMulti ? t("chat.question.review") : t("chat.question.submit") }}
        </button>
      </div>
    </template>

    <!-- Lightbox overlay for answered/pending image thumbnails. -->
    <Teleport to="body">
      <div
        v-if="lightbox.isOpen.value"
        class="lightbox-overlay"
        role="dialog"
        aria-modal="true"
        @click="lightbox.close"
        @wheel.prevent="lightbox.onWheel"
      >
        <img
          v-if="lightbox.src.value"
          :src="lightbox.src.value"
          class="lightbox-image"
          :style="lightbox.imageStyle.value"
          alt=""
          @click.stop
          @mousedown.prevent="lightbox.onDragStart"
          @dblclick="lightbox.reset"
        >
        <button
          type="button"
          class="lightbox-close"
          :aria-label="t('common.close')"
          :title="t('common.close')"
          @click.stop="lightbox.close"
        >
          ✕
        </button>
      </div>
    </Teleport>
  </div>
</template>

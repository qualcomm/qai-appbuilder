<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
// AppBuilderEmptyState — the dark "welcome" screen shown in the App Builder
// chat when there are no messages yet. Guides the user through the 3-step
// WebUI-authoring flow, offers model-aware example prompt chips (Phase 2,
// P2.2), and lists the user's generated app PROJECTS ("My generated apps")
// so they can run / open one without scrolling back through chat.
//
// "My generated apps" is backed by `GET /api/app-builder/apps` (Phase 4,
// plan §5.1) via the appBuilder store's `fetchApps()`. Each row exposes a
// "Run & open" affordance that starts the app's managed process
// (`store.runApp`) and opens the backend-returned loopback URL
// (`http://127.0.0.1:<port>/`) in a new tab. The full run controls (stop /
// logs / manual command) live in the ModeFrame Apps menu; here it is minimal.
//
// Constraints:
//   - Clicking an example chip only FILLS the composer (emits "fill-prompt");
//     it never submits — the parent owns submission.
//   - Only a backend-returned loopback URL is opened (never an LLM-authored
//     URL — plan §5.8).
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { useAppBuilderStore } from "@/stores/appBuilder";
import { useChatTabsStore } from "@/stores/chatTabs";
import { useToast } from "@/composables/useToast";
import type { AppEntry } from "@/stores/appBuilder";

// A manifest example item — only `name` and `inputs` are reliably present.
interface ManifestExample {
  name?: string;
  inputs?: Record<string, unknown>;
  paramsOverride?: Record<string, unknown>;
}

interface Chip {
  key: string;
  label: string;
  prompt: string;
}

const emit = defineEmits<{ "fill-prompt": [prompt: string] }>();

const store = useAppBuilderStore();
const chatTabsStore = useChatTabsStore();
const { t } = useI18n();
const toast = useToast();

// ── "Go to Model Builder" sub-step (Sprint 2, feedback 7C) ──────────────────
// Users landing on the App Builder empty state may not yet have converted
// their model. Rather than force them to hunt for the mode switcher, expose
// a next-to-step-1 sub-step that flips the active tab into Model Builder
// mode in place — reuses the store's canonical `setActiveMode` action so the
// mode change flows through the same code path the toolbar mode switcher
// uses (State-Truth-First — one truth source, no drift).
function switchToModelBuilder(): void {
  const tabId = chatTabsStore.activeTabId;
  if (tabId === null) return;
  chatTabsStore.setActiveMode(tabId, "model-build");
}


// ── generated app projects ("My generated apps", Phase 4) ───────────────────

const generatedApps = computed<AppEntry[]>(() => store.apps);

/** Map a store error code (app_builder.<suffix>) to localized toast text. */
function localizedError(code: string | null): string {
  const suffix = (code ?? "").replace(/^app_builder\./, "");
  const known = [
    "app_not_found",
    "app_invalid",
    "app_already_running",
    "port_in_use",
    "no_bindable_port",
    "app_start_failed",
    "app_not_running",
  ];
  const key = known.includes(suffix) ? suffix : "unknown";
  return t(`appBuilder.apps.errors.${key}`);
}

/**
 * Open a backend-returned loopback URL in a new tab. Only accepts a
 * `http://127.0.0.1:<port>/` (or localhost) URL — never an arbitrary/off-origin
 * URL (plan §5.8).
 */
function openLoopbackUrl(url: string): void {
  if (!/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url)) return;
  window.open(url, "_blank", "noopener");
}

/**
 * Run the app's managed process then open the returned loopback URL. The full
 * run controls (stop / logs / manual command) live in the ModeFrame Apps menu.
 */
async function runAndOpen(app: AppEntry): Promise<void> {
  try {
    const res = await store.runApp(app.id);
    if (typeof res.url === "string" && res.url !== "") {
      openLoopbackUrl(res.url);
    }
  } catch {
    toast.error(localizedError(store.appRunStateOf(app.id)?.error ?? null));
  }
}

// ── helpers ────────────────────────────────────────────────────────────────

const selectedNames = computed(() =>
  store.selectedModelInfos.map((m) => m.title).join(", "),
);

const hasModel = computed(() => store.selectedModelIds.length > 0);

// ── example chips (Phase 2, P2.2 — model-aware) ─────────────────────────────

const chips = computed<Chip[]>(() => {
  const out: Chip[] = [];

  // One chip per selected model. The LABEL describes the build ACTION for that
  // model (e.g. "Build an app with MeloTTS (Chinese)") so it stays consistent
  // with the authoring PROMPT it fills — the earlier design borrowed the
  // manifest example's NAME (e.g. "Classical Poem") for the label while filling
  // a generic build-app prompt, which read as a mismatch. The manifest example
  // (if any) now only contributes a concrete input hint appended to the prompt.
  for (const model of store.selectedModelInfos) {
    const manifest = store.manifestCache[model.id];
    const examples = (manifest?.examples ?? []) as ManifestExample[];

    const label = t("appBuilder.authoring.chipLabel", { model: model.title });

    // Fully localized prompt — no raw English fragments. The `[{model}]`
    // already identifies the model, so we dropped the unlocalized `({task})`
    // taxonomy tag (it produced mixed-language text like "（Tts）"). Any
    // concrete input hint is appended via a localized template too.
    let prompt = t("appBuilder.authoring.chipPrompt", { model: model.title });
    const inputs = examples[0]?.inputs ?? {};
    const textHint = inputs["text"];
    if (typeof textHint === "string" && textHint.trim().length > 0) {
      prompt += " " + t("appBuilder.authoring.chipHintText", {
        text: textHint.trim(),
      });
    } else if (inputs["image"] !== undefined && inputs["image"] !== null) {
      prompt += " " + t("appBuilder.authoring.chipHintImage");
    }

    out.push({ key: model.id, label, prompt });
    if (out.length >= 6) return out.slice(0, 6);
  }

  // Fallback: no model selected → 3 generic starters.
  if (out.length === 0) {
    for (const n of [1, 2, 3] as const) {
      out.push({
        key: `generic${n}`,
        label: t(`appBuilder.authoring.generic${n}`),
        prompt: t(`appBuilder.authoring.generic${n}Prompt`),
      });
    }
  }

  return out.slice(0, 6);
});

function onChipClick(chip: Chip): void {
  emit("fill-prompt", chip.prompt);
}

// ── Discoverability: reveal + highlight the primary chip on model selection ─
// The "Build an app with <model>" chip is the empty state's primary CTA, but
// it only materializes AFTER the user selects a model — and it sits mid-page,
// below the 3 steps. New users, whose attention was on the top-of-page model
// dropdown, can miss that a clickable action just appeared for them.
//
// Design (least-disruptive, most-discoverable):
//   1. When the selected model set changes (initial pick OR swap), on the next
//      DOM tick scroll the chip container into view with
//      ``block:"nearest"`` — this is a NO-OP when the chips are already
//      visible (so users who can see them are not jerked around) and only
//      brings them into view when they were below the fold.
//   2. Toggle a one-shot ``--attention`` class that gently pulses the chip
//      border for ~1.4s so the eye is drawn to the newly-actionable element.
//   3. ``@media (prefers-reduced-motion: reduce)`` removes the pulse and
//      forces ``scroll-behavior: auto`` (see CSS below) — accessibility.
//
// We deliberately do NOT use ``scrollIntoView({block:"center"})`` — that
// scrolls even when the chip is already in view, which is disorienting. And
// we do not auto-focus the chip (would trap keyboard users unexpectedly).
const chipsRef = ref<HTMLElement | null>(null);
const chipsAttention = ref(false);
let _attentionTimer: ReturnType<typeof setTimeout> | null = null;

watch(
  // Track the selected-model set as a stable string; fires on any change
  // (first selection, swap, add, remove) but not on unrelated re-renders.
  () => store.selectedModelIds.slice().sort().join(","),
  (curr, prev) => {
    // Only reveal when we actually have a chip to reveal (curr non-empty).
    // Skip the initial mount snapshot to avoid stealing scroll on page open
    // when a model was already selected from a prior session.
    if (prev === undefined) return;
    if (!curr) return;
    void nextTick(() => {
      const el = chipsRef.value;
      if (!el) return;
      try {
        el.scrollIntoView({ behavior: "smooth", block: "nearest" });
      } catch {
        // Older browsers: fall back to the sync form (no smooth arg).
        el.scrollIntoView();
      }
      // One-shot pulse. Restart the timer if a swap happens mid-pulse.
      chipsAttention.value = true;
      if (_attentionTimer !== null) clearTimeout(_attentionTimer);
      _attentionTimer = setTimeout(() => {
        chipsAttention.value = false;
        _attentionTimer = null;
      }, 1400);
    });
  },
);

// ── "What you'll get" illustrative samples (Improvement #8) ─────────────────
// Static, non-clickable example cards that set expectations for typical app
// outputs. Purely illustrative i18n strings — not real thumbnails or actions.
interface Sample {
  key: string;
  title: string;
  desc: string;
}

const samples = computed<Sample[]>(() =>
  ([1, 2, 3] as const).map((n) => ({
    key: `sample${n}`,
    title: t(`appBuilder.authoring.sample${n}Title`),
    desc: t(`appBuilder.authoring.sample${n}Desc`),
  })),
);

// ── lifecycle ────────────────────────────────────────────────────────────────

onMounted(async () => {
  if (store.models.length === 0) {
    await store.fetchModels();
  }
  void store.fetchApps();
});
</script>

<template>
  <div class="ab-empty" data-testid="app-builder-empty-state">
    <header class="ab-empty-head">
      <h2 class="ab-empty-title">{{ t("appBuilder.authoring.title") }}</h2>
      <p class="ab-empty-subtitle">{{ t("appBuilder.authoring.subtitle") }}</p>
    </header>

    <ol class="ab-empty-steps">
      <li class="ab-empty-step">
        <span class="ab-empty-step-num">1</span>
        <div class="ab-empty-step-body">
          <span class="ab-empty-step-text">{{
            t("appBuilder.authoring.step1")
          }}</span>
          <span
            v-if="!hasModel"
            class="ab-empty-hint ab-empty-hint--warn"
            >{{ t("appBuilder.authoring.noModelHint") }}</span
          >
          <span v-else class="ab-empty-hint">{{
            t("appBuilder.authoring.modelSelectedHint", { names: selectedNames })
          }}</span>
          <!-- Sub-step (Sprint 2, feedback 7C): if the user has NOT yet
               converted a model, guide them straight into Model Builder so
               the whole "convert → promote → build app" flow is discoverable
               from a cold empty state. The link switches the active tab's
               mode via the SAME `setActiveMode` action the toolbar mode
               switcher uses — no bespoke route, one truth source. -->
          <p class="ab-empty-step-substep" data-testid="ab-empty-goto-model-builder">
            <span class="ab-empty-step-substep-text">{{
              t("appBuilder.authoring.step1NeedConversion")
            }}</span>
            <button
              type="button"
              class="ab-empty-step-substep-link"
              data-testid="ab-empty-goto-model-builder-btn"
              @click="switchToModelBuilder"
            >
              → {{ t("appBuilder.authoring.step1GoToModelBuilder") }}
            </button>
          </p>
        </div>
      </li>
      <li class="ab-empty-step">
        <span class="ab-empty-step-num">2</span>
        <div class="ab-empty-step-body">
          <span class="ab-empty-step-text">{{
            t("appBuilder.authoring.step2")
          }}</span>
        </div>
      </li>
      <li class="ab-empty-step">
        <span class="ab-empty-step-num">3</span>
        <div class="ab-empty-step-body">
          <span class="ab-empty-step-text">{{
            t("appBuilder.authoring.step3")
          }}</span>
        </div>
      </li>
    </ol>

    <div
      ref="chipsRef"
      class="ab-empty-chips"
      :class="{ 'ab-empty-chips--attention': chipsAttention }"
    >
      <button
        v-for="chip in chips"
        :key="chip.key"
        type="button"
        class="ab-empty-chip"
        data-testid="app-builder-example-chip"
        :aria-label="chip.label"
        :title="chip.prompt"
        @click="onChipClick(chip)"
      >
        {{ chip.label }}
      </button>
    </div>

    <!-- "What you'll get" illustrative samples (Improvement #8): static,
         non-clickable example cards describing typical generated apps. -->
    <section class="ab-empty-samples">
      <h3 class="ab-empty-samples-title">
        {{ t("appBuilder.authoring.samplesTitle") }}
      </h3>
      <div class="ab-empty-samples-grid">
        <div
          v-for="sample in samples"
          :key="sample.key"
          class="ab-empty-sample"
          data-testid="app-builder-sample-card"
        >
          <span class="ab-empty-sample-title">{{ sample.title }}</span>
          <span class="ab-empty-sample-desc">{{ sample.desc }}</span>
        </div>
      </div>
    </section>

    <!-- "My generated apps" (Phase 4, plan §9.2): run a generated app project
         and open it in a new tab. Backed by GET /api/app-builder/apps; hidden
         when empty. The full run controls (stop / logs / manual command) live
         in the ModeFrame Apps menu — here it is a minimal run+open. Only a
         backend-returned loopback URL is opened (never file:// / off-origin). -->
    <section
      v-if="generatedApps.length > 0"
      class="ab-empty-apps"
    >
      <h3 class="ab-empty-apps-title">
        {{ t("appBuilder.authoring.myAppsTitle") }}
      </h3>
      <p class="ab-empty-apps-hint">
        {{ t("appBuilder.authoring.myAppsHint") }}
      </p>
      <ul class="ab-empty-apps-list">
        <li
          v-for="app in generatedApps"
          :key="app.id"
        >
          <button
            type="button"
            class="ab-empty-app-row"
            data-testid="app-builder-generated-app"
            :title="app.name"
            :aria-label="t('appBuilder.apps.runAria')"
            @click="runAndOpen(app)"
          >
            <span class="ab-empty-app-name">{{ app.name }}</span>
            <span class="ab-empty-app-open">{{ t("appBuilder.apps.run") }} ↗</span>
          </button>
        </li>
      </ul>
    </section>

    <!-- Help / explanation note (Improvement #10): plain explanatory text.
         No docs URL convention exists in the app, so this is not a link. -->
    <p class="ab-empty-help" data-testid="app-builder-help-note">
      {{ t("appBuilder.authoring.helpNote") }}
    </p>
  </div>
</template>

<style scoped>
.ab-empty {
  display: flex;
  flex-direction: column;
  gap: 20px;
  max-width: 720px;
  margin: 0 auto;
  padding: 32px 24px;
  color: #e6e6e6;
}

.ab-empty-head {
  text-align: center;
}

.ab-empty-title {
  margin: 0 0 8px;
  font-size: 1.5rem;
  font-weight: 600;
  color: #f5f5f5;
}

.ab-empty-subtitle {
  margin: 0;
  font-size: 0.95rem;
  line-height: 1.5;
  color: #a0a0a8;
}

.ab-empty-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.ab-empty-step {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 12px 16px;
  background: #1c1c22;
  border: 1px solid #2b2b33;
  border-radius: 10px;
}

.ab-empty-step-num {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: #3a3a45;
  color: #fff;
  font-size: 0.85rem;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.ab-empty-step-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.ab-empty-step-text {
  font-size: 0.95rem;
  color: #e6e6e6;
}

.ab-empty-hint {
  font-size: 0.82rem;
  color: #8a8a92;
}

.ab-empty-hint--warn {
  color: #e0a94a;
}

/* Sub-step under step 1 (feedback 7C): guides the user to Model Builder when
   they haven't converted a model yet. Deliberately subdued — a two-line
   supporting hint under the step text, NOT a competing CTA (the primary
   flow is still "select model → describe app → generate", but this side
   ramp is there for the "I don't have a converted model yet" case). */
.ab-empty-step-substep {
  margin: 4px 0 0;
  display: inline-flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px;
  font-size: 0.82rem;
  color: #8a8a92;
  line-height: 1.4;
}

.ab-empty-step-substep-link {
  background: transparent;
  border: none;
  padding: 0;
  color: #7fa7ff;
  font-size: 0.82rem;
  font-weight: 500;
  cursor: pointer;
  text-decoration: none;
  transition: color 0.12s ease;
}
.ab-empty-step-substep-link:hover,
.ab-empty-step-substep-link:focus-visible {
  color: #a4c1ff;
  text-decoration: underline;
}

.ab-empty-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}

.ab-empty-chip {
  max-width: 100%;
  padding: 8px 14px;
  background: #23232b;
  border: 1px solid #34343f;
  border-radius: 999px;
  color: #d8d8e0;
  font-size: 0.85rem;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.ab-empty-chip:hover {
  background: #2d2d38;
  border-color: #4a4a58;
}

/* One-shot attention pulse when the primary "Build an app with <model>" chip
   first appears after a model is selected (see the selectedModelIds watch).
   Draws the eye to the newly-actionable CTA so new users notice it. The
   class is removed after ~1.4s so it does not loop. */
.ab-empty-chips--attention .ab-empty-chip {
  animation: ab-chip-attention 1.4s ease-out 1;
}
@keyframes ab-chip-attention {
  0% {
    border-color: #6d5efc;
    box-shadow: 0 0 0 0 rgba(109, 94, 252, 0.55);
  }
  40% {
    border-color: #6d5efc;
    box-shadow: 0 0 0 6px rgba(109, 94, 252, 0);
  }
  100% {
    border-color: #34343f;
    box-shadow: 0 0 0 0 rgba(109, 94, 252, 0);
  }
}
/* Accessibility: users who prefer reduced motion get neither the pulse nor a
   smooth scroll jump. */
@media (prefers-reduced-motion: reduce) {
  .ab-empty-chips--attention .ab-empty-chip {
    animation: none;
  }
  .ab-empty {
    scroll-behavior: auto;
  }
}

.ab-empty-samples {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.ab-empty-samples-title {
  margin: 0;
  font-size: 0.95rem;
  font-weight: 600;
  color: #cfcfd6;
  text-align: center;
}

.ab-empty-samples-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}

/* Illustrative-only "what you can build" examples. Deliberately NOT card-like
   (no filled background, no full border, default cursor) so they don't read as
   clickable — they set expectations, they are not actions. A subtle left
   accent bar marks them as descriptive examples. */
.ab-empty-sample {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 12px;
  border-left: 2px solid #3a3a45;
  cursor: default;
}

.ab-empty-sample-title {
  font-size: 0.9rem;
  font-weight: 600;
  color: #cfcfd6;
}

.ab-empty-sample-desc {
  font-size: 0.82rem;
  color: #a0a0a8;
  line-height: 1.4;
}

.ab-empty-help {
  margin: 0;
  font-size: 0.82rem;
  line-height: 1.5;
  color: #8a8a92;
  text-align: center;
}

/* "My generated apps" list (Improvement #9). */
.ab-empty-apps {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ab-empty-apps-title {
  margin: 0;
  font-size: 0.9rem;
  font-weight: 600;
  color: #b8b8c0;
}
.ab-empty-apps-hint {
  margin: 0;
  font-size: 0.8rem;
  color: #8a8a92;
}
.ab-empty-apps-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ab-empty-app-row {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 14px;
  background: #1c1c22;
  border: 1px solid #2b2b33;
  border-radius: 8px;
  color: #e6e6e6;
  font-size: 0.88rem;
  cursor: pointer;
  text-align: left;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.ab-empty-app-row:hover {
  background: #26262e;
  border-color: #3a3a45;
}
.ab-empty-app-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ab-empty-app-open {
  flex: 0 0 auto;
  color: #7fa7ff;
  font-weight: 700;
}
</style>

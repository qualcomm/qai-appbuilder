<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * Application header — V1-style topbar.
 *
 * Uses global CSS classes from `layout.css` (`.topbar`, `.topbar-title`)
 * and `components.css` (`.btn-ghost`).
 *
 * Layout:
 *   - Left:  dynamic view title (from route meta.titleKey)
 *   - Right: store-driven action list. Each registered <HeaderAction>
 *            is rendered as a button with consistent classes/aria.
 *
 * The action list is owned by `useHeaderActionsStore` and populated by
 * the active view via `useHeaderActions(...)` — see V1 parity note in
 * `stores/headerActions.ts`. AppHeader itself is now a pure renderer:
 * no view-specific knowledge, no inline `v-if` blocks.
 *
 * Overflow menu: actions marked `overflow: true` are collected into a
 * "⋯" dropdown, keeping the topbar compact for low-frequency operations.
 */
import { computed, ref, onMounted, onBeforeUnmount } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import { useHeaderActionsStore } from "@/stores/headerActions";
import { useUiStore } from "@/stores/ui";
import ChatTabStrip from "@/components/chat/ChatTabStrip.vue";

const { t } = useI18n();
const route = useRoute();
const headerActions = useHeaderActionsStore();
const ui = useUiStore();

const viewTitle = computed(() => {
  const key = route.meta?.titleKey as string | undefined;
  return key ? t(key) : t("app.title");
});

// On the chat route the multi-session tab strip takes the place of the
// plain view title (the tabs already say "which session you're in"). This
// keeps everything on ONE topbar row — no extra vertical strip eating into
// the conversation area (方案 A). Other routes keep showing their title.
const isChatView = computed(
  () => route.path === "/chat" || route.path.startsWith("/chat/"),
);

// --- Overflow menu logic ---
const primaryActions = computed(() =>
  headerActions.actions.filter((a) => !a.overflow),
);
const overflowActions = computed(() =>
  headerActions.actions.filter((a) => a.overflow),
);

const overflowOpen = ref(false);
const overflowMenuRef = ref<HTMLElement | null>(null);

function toggleOverflow() {
  overflowOpen.value = !overflowOpen.value;
}

function handleOverflowAction(onClick: () => void | Promise<void>) {
  overflowOpen.value = false;
  // The action may be async; swallow rejections so a failing handler does not
  // surface as an unhandled promise rejection.
  void Promise.resolve(onClick()).catch(() => {
    /* handler is responsible for its own error UX (toast etc.) */
  });
}

function onClickOutside(e: MouseEvent) {
  if (
    overflowMenuRef.value &&
    !overflowMenuRef.value.contains(e.target as Node)
  ) {
    overflowOpen.value = false;
  }
}

onMounted(() => {
  document.addEventListener("click", onClickOutside, true);
});
onBeforeUnmount(() => {
  document.removeEventListener("click", onClickOutside, true);
});
</script>

<template>
  <header
    class="topbar"
    :aria-label="t('layout.header_aria')"
    role="banner"
  >
    <!-- Skip-to-content accessibility link -->
    <a
      class="topbar-skip"
      href="#main-content"
    >
      {{ t("layout.skip_to_content") }}
    </a>

    <!-- Mobile hamburger: visible only on small screens (≤768px) where the
         sidebar is off-screen by default. Toggles the `mobile-open` class on
         the sidebar via the ui store's `mobileSidebarOpen` flag. -->
    <button
      type="button"
      class="topbar-hamburger"
      :title="t('layout.toggle_sidebar')"
      :aria-label="t('layout.toggle_sidebar')"
      :aria-expanded="ui.mobileSidebarOpen"
      @click="ui.toggleMobileSidebar()"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18">
        <line x1="3" y1="6" x2="21" y2="6" />
        <line x1="3" y1="12" x2="21" y2="12" />
        <line x1="3" y1="18" x2="21" y2="18" />
      </svg>
    </button>

    <!-- Chat route: the multi-session tab strip occupies the title slot and
         flexes to fill the middle of the topbar. Other routes: plain view
         title (left side). -->
    <ChatTabStrip
      v-if="isChatView"
      class="topbar-tab-strip"
    />
    <span
      v-else
      class="topbar-title"
    >
      {{ viewTitle }}
    </span>

    <!-- Right-side toolbar actions — driven entirely by the store -->
    <div class="topbar-actions">
      <!-- Primary (non-overflow) actions rendered directly -->
      <button
        v-for="action in primaryActions"
        :key="action.id"
        type="button"
        :class="[
          'btn',
          action.variant === 'primary' ? 'btn-primary' : 'btn-ghost',
          'btn-sm',
          'topbar-action-btn',
          action.extraClass,
          { active: action.pressed === true },
        ]"
        :disabled="action.disabled === true"
        :title="action.title ?? action.label"
        :aria-label="action.title ?? action.label"
        :aria-pressed="action.pressed"
        :data-testid="action.testId"
        @click="action.onClick"
      >
        <!-- eslint-disable vue/no-v-html -->
        <span
          v-if="action.iconSvg"
          aria-hidden="true"
          class="topbar-action-icon-svg"
          v-html="action.iconSvg"
        />
        <!-- eslint-enable vue/no-v-html -->
        <span
          v-else-if="action.icon"
          aria-hidden="true"
          class="topbar-action-icon-emoji"
        >{{ action.icon }}</span>
        <span class="topbar-action-label">{{ action.label }}</span>
      </button>

      <!-- Overflow "⋯" menu for low-frequency actions -->
      <div
        v-if="overflowActions.length > 0"
        ref="overflowMenuRef"
        class="topbar-overflow-wrapper"
      >
        <button
          type="button"
          class="btn btn-ghost btn-sm topbar-action-btn topbar-overflow-trigger"
          :title="t('layout.overflowMenu')"
          :aria-label="t('layout.overflowMenu')"
          :aria-expanded="overflowOpen"
          aria-haspopup="menu"
          data-testid="topbar-overflow-btn"
          @click="toggleOverflow"
        >
          <span aria-hidden="true" class="topbar-overflow-icon">&#x22EF;</span>
        </button>
        <div
          v-if="overflowOpen"
          class="topbar-overflow-panel"
          role="menu"
        >
          <button
            v-for="action in overflowActions"
            :key="action.id"
            type="button"
            class="topbar-overflow-item"
            role="menuitem"
            :disabled="action.disabled === true"
            :title="action.title ?? action.label"
            :data-testid="action.testId"
            @click="handleOverflowAction(action.onClick)"
          >
            <!-- eslint-disable vue/no-v-html -->
            <span
              v-if="action.iconSvg"
              aria-hidden="true"
              class="topbar-overflow-item-icon topbar-action-icon-svg"
              v-html="action.iconSvg"
            />
            <!-- eslint-enable vue/no-v-html -->
            <span
              v-else-if="action.icon"
              aria-hidden="true"
              class="topbar-overflow-item-icon topbar-action-icon-emoji"
            >{{ action.icon }}</span>
            <span class="topbar-overflow-item-label">{{ action.label }}</span>
          </button>
        </div>
      </div>
    </div>
  </header>
</template>

<style>
/* Minimal non-scoped overrides for topbar layout — bulk styles come from
   layout.css (.topbar, .topbar-title) and components.css (.btn-ghost).
   Only structural additions specific to AppHeader live here. */

.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
}

.topbar-skip {
  position: absolute;
  left: -9999px;
}
.topbar-skip:focus {
  position: static;
  background: var(--accent);
  color: #fff;
  padding: 4px 8px;
  border-radius: 6px;
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-left: auto;
}

/* --- Header action button: icon-only by default, expand label on hover --- */
.topbar-action-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  /* Must override global .btn gap (6px) that creates dead space between the
     icon and the 0-width hidden label, pushing the icon off-center. */
  gap: 0 !important;
  overflow: hidden;
  white-space: nowrap;
  /* Force symmetric padding so icon is visually centered when label is hidden.
     Must override .btn-sm's asymmetric padding (4px 10px from layout.css). */
  padding: 0 8px !important;
  /* Lock to a true square in icon-only state. Using a fixed height (not just
     min-height) + line-height:1 stops the global .btn font-size from adding
     extra line-box height that made the button look taller than it is wide. */
  height: 32px;
  min-width: 32px;
  line-height: 1;
  transition: opacity 0.2s ease;
}

.topbar-action-btn:disabled {
  opacity: 0.5;
}

/* Label: hidden by default, expand on hover */
.topbar-action-label {
  display: inline-block;
  max-width: 0;
  width: 0;
  opacity: 0;
  overflow: hidden;
  padding-left: 0;
  /* When collapsed, don't participate in flex layout at all so the icon
     stays perfectly centered via the button's justify-content: center. */
  pointer-events: none;
  transition:
    max-width 0.25s ease,
    opacity 0.2s ease 0.05s,
    padding-left 0.25s ease,
    width 0.25s ease;
}

.topbar-action-btn:hover .topbar-action-label {
  max-width: 8em;
  width: auto;
  opacity: 1;
  padding-left: 4px;
  pointer-events: auto;
}

/* Inline SVG icons coming from HeaderAction.iconSvg should not push
   the label down — keep them aligned with the button text. */
.topbar-action-icon-svg {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 16px;
  height: 16px;
}
.topbar-action-icon-svg svg {
  display: block;
  width: 16px !important;
  height: 16px !important;
}

/* Emoji icons — keep consistent sizing with SVG icons */
.topbar-action-icon-emoji {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  font-size: 14px;
  line-height: 1;
}

/* Chat multi-session tab strip embedded in the topbar (方案 A). It takes the
   title's place and flexes to fill the gap between the (now title-less) left
   edge and the right-side action buttons, so tabs live on the SAME row as the
   toolbar — no separate strip row, no vertical space taken from the chat area.
   Tab overflow (native scrollbar → "⋯" menu) is handled INSIDE ChatTabStrip;
   here we only size it. The component's own bottom border is removed since it
   now sits inside the topbar (which has its own bottom border). */
.topbar-tab-strip {
  flex: 1 1 auto;
  min-width: 0;
  border-bottom: 0;
}

/* --- Overflow menu ("⋯" trigger + dropdown panel) --- */
.topbar-overflow-wrapper {
  position: relative;
}

.topbar-overflow-trigger {
  font-size: 18px;
  font-weight: bold;
  letter-spacing: 1px;
}

.topbar-overflow-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  line-height: 1;
}

.topbar-overflow-panel {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  min-width: 160px;
  background: var(--glass-bg);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
  padding: 4px 0;
  z-index: 100;
}

.topbar-overflow-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
  white-space: nowrap;
  transition: background 0.15s ease;
}

.topbar-overflow-item:hover:not(:disabled) {
  background: var(--bg-hover);
}

.topbar-overflow-item:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.topbar-overflow-item-icon {
  flex-shrink: 0;
}

.topbar-overflow-item-label {
  flex: 1;
}

/* Mobile hamburger button — hidden by default on desktop, shown on small screens */
.topbar-hamburger {
  display: none;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border: none;
  background: transparent;
  color: var(--text-primary);
  cursor: pointer;
  border-radius: 6px;
  flex-shrink: 0;
  transition: background 0.15s ease;
}
.topbar-hamburger:hover {
  background: var(--bg-hover);
}
@media (max-width: 768px) {
  .topbar-hamburger {
    display: inline-flex;
  }
}
</style>

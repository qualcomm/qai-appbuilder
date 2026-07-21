// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Application route table.
 *
 * S5 PR-050: 7 view-level routes (per refactor-plan §11.3 step 2).
 * S5 PR-053: adds an optional `:tabId` child to `/chat` so the
 *            multi-tab Chat composable family (PR-054) can deep-link
 *            into a specific tab without renaming the existing
 *            `chat` route.
 *
 * The 7 sidebar views (chat / channels / skills / downloads /
 * service / security / settings) mirror V1's 7 `currentView` values
 * one-to-one. V1's tool workflows (model-builder / app-builder / code /
 * translate / ppt) are NOT standalone pages — they live inside the chat
 * composer toolbar (`activeToolMode`), exactly as in V1. The earlier
 * standalone `/tools`, `/tools/*`, `/app-builder`, `/ai-coding` and the
 * duplicate `/updates` routes had no sidebar entry, no inbound links,
 * and duplicated chat-toolbar functionality, so they were removed (the
 * canonical downloads page is `/downloads`).
 */
import type { RouteRecordRaw } from "vue-router";

export const routes: RouteRecordRaw[] = [
  {
    path: "/",
    redirect: "/chat",
  },
  {
    path: "/chat",
    name: "chat",
    component: () => import("@/views/ChatView.vue"),
    meta: { titleKey: "nav.chat" },
    children: [
      {
        path: ":tabId",
        name: "chat-tab",
        component: () => import("@/views/ChatView.vue"),
        meta: { titleKey: "nav.chat" },
      },
    ],
  },
  {
    path: "/channels",
    name: "channels",
    component: () => import("@/views/ChannelsView.vue"),
    meta: { titleKey: "nav.channels" },
  },
  {
    path: "/skills",
    name: "skills",
    component: () => import("@/views/SkillsView.vue"),
    meta: { titleKey: "nav.skills" },
  },
  {
    path: "/downloads",
    name: "downloads",
    component: () => import("@/views/DownloadsView.vue"),
    meta: { titleKey: "nav.downloads" },
  },
  {
    path: "/service",
    name: "service",
    component: () => import("@/views/ServiceView.vue"),
    meta: { titleKey: "nav.service" },
  },
  {
    path: "/security",
    name: "security",
    component: () => import("@/views/SecurityView.vue"),
    meta: { titleKey: "nav.security" },
  },
  {
    path: "/settings",
    name: "settings",
    component: () => import("@/views/SettingsView.vue"),
    meta: { titleKey: "nav.settings" },
  },
  {
    path: "/:pathMatch(.*)*",
    name: "not-found",
    component: () => import("@/views/ChatView.vue"),
  },
];

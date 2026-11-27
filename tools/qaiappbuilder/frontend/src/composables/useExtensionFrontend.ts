// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Extension front-end modules (design doc route A).
 *
 * An extension may ship an ES module (`ui.module` in its manifest). The host
 * imports it at run time (`import(url)`), calls its `activate(api)` ONCE and
 * hands it `api` -- the extension's whole view of the host:
 *
 *   api.name              the extension's name
 *   api.locale            the active UI locale (`en`, `zh-CN`, or `zh-TW`)
 *   api.vue               the HOST's Vue (h, ref, computed, ...). The module must NOT
 *                         `import "vue"`: a bare specifier does not resolve in the
 *                         browser, and a second Vue copy would break reactivity.
 *                         Use render functions (no template compiler).
 *   api.fetch(path, init) fetch `/api/ext/<name>/<path>` (the extension's own HTTP
 *                         routes) with the session + CSRF handled by the host
 *   api.upload(path, file) POST raw ZIP bytes to an extension route
 *   api.mcpStatus()       read the global MCP status
 *   api.setMcpEnabled(on) update the global MCP enabled switch
 *   api.settings.get()    the user's non-secret settings (`GET .../settings`)
 *   api.panel(spec)       register the extension's panel (`{title, component}`)
 *   api.toast.success|error(text)
 *   api.sendPrompt(text)  send `text` as the user's next chat message
 *   api.onDispose(fn)     run `fn` when the extension is unloaded / reloaded
 *
 * Trust: this is the extension's JavaScript running in the app's own page, with
 * the user's session -- the same trust level as its Python code (design doc §8),
 * not a sandbox. The install dialog says so.
 *
 * Reload: the browser cannot unload an ES module. A reload loads the new
 * version under a new `?v=` URL, runs the old version's `onDispose` callbacks
 * and drops its registrations; the old code stays in memory until the page is
 * reloaded (only visible while developing an extension).
 */
import * as Vue from "vue";
import { markRaw, reactive, type Component } from "vue";

import { apiBytes, apiJson } from "@/api/http";
import { useToast } from "@/composables/useToast";
import { useUiStore } from "@/stores/ui";

/** A fixed label or one label per UI locale (`en`, `zh-CN`, `zh-TW`). */
export type LocalizedText = string | Readonly<Record<string, string>>;

export interface ExtensionPanelSpec {
  /** Shown in the panel header and on the composer button. */
  title: LocalizedText;
  /** A component built with the host Vue's render functions. */
  component: Component;
}

interface Loaded {
  version: string; // the module URL (carries ?v=)
  panel: ExtensionPanelSpec | null;
  dispose: (() => void)[];
}

/** Everything the host gives an extension to build components with. */
const HOST_VUE = Object.freeze({
  h: Vue.h,
  ref: Vue.ref,
  shallowRef: Vue.shallowRef,
  reactive: Vue.reactive,
  computed: Vue.computed,
  watch: Vue.watch,
  watchEffect: Vue.watchEffect,
  onMounted: Vue.onMounted,
  onUnmounted: Vue.onUnmounted,
  nextTick: Vue.nextTick,
  defineComponent: Vue.defineComponent,
  Fragment: Vue.Fragment,
  Teleport: Vue.Teleport,
  Transition: Vue.Transition,
  withModifiers: Vue.withModifiers,
  withKeys: Vue.withKeys,
});

function isLocalizedText(value: unknown): value is LocalizedText {
  if (typeof value === "string") return value.trim() !== "";
  return (
    value !== null &&
    typeof value === "object" &&
    Object.values(value).some((v) => typeof v === "string" && v.trim() !== "")
  );
}

/** Resolve an extension label for the active UI locale. */
export function resolveLocalizedText(value: LocalizedText, locale: string): string {
  if (typeof value === "string") return value;
  const pick = (key: string): string | undefined => {
    const text = value[key];
    return typeof text === "string" && text.trim() !== "" ? text : undefined;
  };
  return (
    pick(locale) ??
    (locale.startsWith("zh") ? (pick("zh-CN") ?? pick("zh-TW")) : undefined) ??
    pick("en") ??
    Object.values(value).find((text) => typeof text === "string" && text.trim() !== "") ??
    ""
  );
}

const loaded = reactive(new Map<string, Loaded>());
const errors = reactive(new Map<string, string>());
let sendPromptHandler: ((text: string) => void) | null = null;

/** The composer registers how to send an extension-originated prompt. */
export function setExtensionPromptSender(fn: ((text: string) => void) | null): void {
  sendPromptHandler = fn;
}

function buildApi(name: string, entry: Loaded) {
  const toast = useToast();
  const base = `/api/ext/${encodeURIComponent(name)}/`;
  return Object.freeze({
    name,
    get locale() {
      return useUiStore().locale;
    },
    vue: HOST_VUE,
    async fetch(path: string, init: { method?: string; body?: unknown } = {}): Promise<unknown> {
      const clean = path.replace(/^\/+/, "");
      if (clean.split("/").includes("..")) throw new Error("path must stay inside the extension");
      const method = (init.method ?? "GET").toUpperCase() as
        | "GET"
        | "POST"
        | "PUT"
        | "PATCH"
        | "DELETE";
      return apiJson(method, base + clean, init.body);
    },
    async upload(path: string, file: Blob): Promise<unknown> {
      const clean = path.replace(/^\/+/, "");
      if (clean.split("/").includes("..")) throw new Error("path must stay inside the extension");
      return apiBytes(base + clean, file);
    },
    mcpStatus: (): Promise<unknown> => apiJson("GET", "/api/chat/mcp/servers"),
    setMcpEnabled: (enabled: boolean): Promise<unknown> =>
      apiJson("PATCH", "/api/chat/mcp/enabled", { enabled }),
    settings: Object.freeze({
      get: async (): Promise<Record<string, unknown>> => {
        const r = await apiJson<{ settings: { key: string; type: string; value?: unknown }[] }>(
          "GET",
          `/api/extensions/${encodeURIComponent(name)}/settings`,
        );
        return Object.fromEntries(
          r.settings.filter((s) => s.type !== "secret").map((s) => [s.key, s.value ?? null]),
        );
      },
    }),
    panel(spec: ExtensionPanelSpec): void {
      if (!spec || !isLocalizedText(spec.title) || !spec.component) {
        throw new Error("panel({ title, component }) needs a title and a component");
      }
      entry.panel = { title: spec.title, component: markRaw(spec.component) };
    },
    toast: Object.freeze({
      success: (text: string) => toast.success(String(text)),
      error: (text: string) => toast.error(String(text)),
    }),
    sendPrompt(text: string): void {
      if (typeof text === "string" && text.trim() !== "" && sendPromptHandler !== null) {
        sendPromptHandler(text);
      }
    },
    onDispose(fn: () => void): void {
      if (typeof fn === "function") entry.dispose.push(fn);
    },
  });
}

/** Drop an extension's registrations and run its dispose callbacks. */
export function unloadExtensionFrontend(name: string): void {
  const entry = loaded.get(name);
  loaded.delete(name);
  errors.delete(name);
  for (const fn of entry?.dispose ?? []) {
    try {
      fn();
    } catch (err) {
      console.warn(`[extension ${name}] dispose failed`, err);
    }
  }
}

/** Load (or reload, when `url` changed) one extension's front-end module. */
export async function loadExtensionFrontend(name: string, url: string): Promise<void> {
  const current = loaded.get(name);
  if (current?.version === url) return; // already running exactly this version
  if (current) unloadExtensionFrontend(name);
  const entry: Loaded = { version: url, panel: null, dispose: [] };
  try {
    // A runtime URL the bundler must not (and cannot) resolve.
    const mod = (await import(/* @vite-ignore */ url)) as { activate?: (api: unknown) => unknown };
    if (typeof mod.activate !== "function") throw new Error("the module has no activate() export");
    await mod.activate(buildApi(name, entry));
    loaded.set(name, entry);
    errors.delete(name);
  } catch (err) {
    for (const fn of entry.dispose) {
      try {
        fn();
      } catch {
        /* the module failed anyway */
      }
    }
    errors.set(name, err instanceof Error ? err.message : String(err));
  }
}

/** Bring the running modules in line with `GET /api/extensions/ui`. */
export async function syncExtensionFrontends(
  entries: { name: string; module?: string | null }[],
): Promise<void> {
  const wanted = new Map(entries.filter((e) => e.module).map((e) => [e.name, e.module as string]));
  for (const name of [...loaded.keys()]) {
    if (!wanted.has(name)) unloadExtensionFrontend(name);
  }
  for (const name of [...errors.keys()]) {
    if (!wanted.has(name)) errors.delete(name);
  }
  await Promise.all([...wanted].map(([name, url]) => loadExtensionFrontend(name, url)));
}

export function useExtensionFrontend() {
  return {
    /** The panel an extension registered (`null` when it has none / failed). */
    panelOf: (name: string): ExtensionPanelSpec | null => loaded.get(name)?.panel ?? null,
    errorOf: (name: string): string => errors.get(name) ?? "",
    isLoaded: (name: string): boolean => loaded.has(name),
  };
}

/** Test helper. */
export function __resetExtensionFrontendForTests(): void {
  for (const name of [...loaded.keys()]) unloadExtensionFrontend(name);
  loaded.clear();
  errors.clear();
  sendPromptHandler = null;
}

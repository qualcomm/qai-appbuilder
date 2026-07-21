// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Command palette composable.
 *
 * Provides the registered command list and an Escape-to-close listener.
 * Opening the palette is bound to Ctrl/Cmd+. (V1 parity: `app.js:2253`
 * — Ctrl+. opens the command palette) via the central `useKeymap` in
 * `App.vue`. Ctrl/Cmd+K is intentionally NOT bound here — in V1 that
 * combo toggles the model-selection dropdown (`app.js:2260`), so the
 * chat composer owns it.
 *
 * The shortcut listener is attached on first call from a component
 * setup and torn down on unmount; multiple components may call
 * `useCommandPalette()` safely, each gets its own listener registration.
 */
import { computed, onBeforeUnmount, ref, type ComputedRef, type Ref } from "vue";
import { useRouter } from "vue-router";
import { useCommandPaletteStore } from "@/stores/commandPalette";

export interface PaletteCommand {
  id: string;
  /** i18n message key OR pre-translated label. */
  label: string;
  /** Optional category — UI groups commands by category. */
  category?: string;
  /** Optional leading icon (emoji / glyph), V1 parity per-item icon. */
  icon?: string;
  /** Optional keyboard shortcut hint shown as a trailing <kbd>. */
  shortcut?: string;
  run: () => void | Promise<void>;
}

const externalCommands: Ref<PaletteCommand[]> = ref([]);

export function registerPaletteCommand(cmd: PaletteCommand): () => void {
  externalCommands.value = [...externalCommands.value, cmd];
  return () => {
    externalCommands.value = externalCommands.value.filter(
      (c) => c.id !== cmd.id,
    );
  };
}

export function useCommandPalette(options?: {
  /** Localised navigation labels indexed by route name. */
  navLabels?: Readonly<Record<string, string>>;
  /** Set false to skip the global Escape-to-close binding (useful in tests). */
  bindShortcut?: boolean;
  /**
   * Set false to omit the built-in per-route navigation commands. The
   * global `AppCommandPalette` supplies its own grouped navigation via the
   * "actions" category (`useAppCommands`), so it disables these to avoid a
   * duplicate ungrouped "nav" section.
   */
  includeNavCommands?: boolean;
}): {
  open: ComputedRef<boolean>;
  query: ComputedRef<string>;
  commands: ComputedRef<readonly PaletteCommand[]>;
  filtered: ComputedRef<readonly PaletteCommand[]>;
  show: () => void;
  hide: () => void;
  toggle: () => void;
  setQuery: (value: string) => void;
} {
  const store = useCommandPaletteStore();
  const router = useRouter();

  const navCommands = computed<readonly PaletteCommand[]>(() => {
    const labels = options?.navLabels ?? {};
    const routes = router.getRoutes();
    const seen = new Set<string>();
    const result: PaletteCommand[] = [];
    for (const r of routes) {
      const name = typeof r.name === "string" ? r.name : null;
      if (name === null) {
        continue;
      }
      if (seen.has(name) || name === "not-found") {
        continue;
      }
      seen.add(name);
      const label = labels[name] ?? name;
      result.push({
        id: `nav:${name}`,
        category: "nav",
        label,
        run: () => {
          void router.push({ name });
        },
      });
    }
    return result;
  });

  const commands = computed<readonly PaletteCommand[]>(() => {
    const nav =
      options?.includeNavCommands === false ? [] : navCommands.value;
    return [...nav, ...externalCommands.value];
  });

  const filtered = computed<readonly PaletteCommand[]>(() => {
    const q = store.query.trim().toLowerCase();
    if (q === "") {
      return commands.value;
    }
    return commands.value.filter((c) => c.label.toLowerCase().includes(q));
  });

  const bind = options?.bindShortcut !== false;
  // Opening the palette is owned by the central `useKeymap` in App.vue
  // (Ctrl/Cmd+. — V1 app.js:2253). This listener only closes the palette on
  // Escape; it deliberately does NOT bind Ctrl/Cmd+K, which V1 reserves for
  // toggling the model-selection dropdown (app.js:2260, owned by ChatComposer).
  const handler = (ev: KeyboardEvent): void => {
    if (ev.key === "Escape" && store.open) {
      store.hide();
    }
  };

  if (bind && typeof window !== "undefined") {
    window.addEventListener("keydown", handler);
    onBeforeUnmount(() => {
      window.removeEventListener("keydown", handler);
    });
  }

  return {
    open: computed(() => store.open),
    query: computed(() => store.query),
    commands,
    filtered,
    show: () => store.show(),
    hide: () => store.hide(),
    toggle: () => store.toggle(),
    setQuery: (value: string) => store.setQuery(value),
  };
}

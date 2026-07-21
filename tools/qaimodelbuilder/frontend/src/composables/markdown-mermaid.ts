// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Native Mermaid diagram rendering for committed Markdown.
 *
 * This project's Mermaid rendering:
 *   - marker contract is `.mermaid-code[data-kind="mermaid"]` wrapping a
 *     `pre > code[data-lang="mermaid"]` (NOT a `data-component=
 *     "markdown-code"`), matching what `renderMarkdown` emits here.
 *   - theme variables derive from THIS project's CSS tokens
 *     (`--bg-*` / `--text-*` / `--border*` / `--accent*` / `--error`),
 *     resolved live from the rendered DOM, so light (`html.light`) and dark
 *     (`:root`) themes both work.
 *   - no VS Code (`--vscode-*`) variables, no action toolbar.
 *
 * TWO ISOLATED PIPELINES:
 *   - The GLOBAL Markdown sanitiser (`markdown.ts`) stays HTML-only and
 *     NEVER allows SVG tags.
 *   - Mermaid's generated SVG is sanitised HERE through a SEPARATE DOMPurify
 *     SVG profile (`svgConfig`) before being injected into the dedicated
 *     diagram panel. The two never share a config.
 *
 * Rendering is asynchronous and serialised through a single queue (Mermaid's
 * global `initialize` is not concurrency-safe). Each block is idempotent via
 * `data-mermaid-hash` (source) + `data-mermaid-theme` (resolved theme vars):
 * re-running the hook on unchanged content is a no-op. `signal.aborted` and
 * `root.isConnected` guard against rendering into a torn-down component.
 *
 * Failure path: the original code block is left visible and an error label
 * is shown, so malformed Mermaid degrades to readable source.
 */
import DOMPurify from "dompurify";

/** Separate SVG sanitiser config — distinct from the HTML-only Markdown one.
 *
 * `html: true` is REQUIRED in addition to the svg profiles: Mermaid renders
 * node labels (flowchart / state / class …) inside `<foreignObject>` as HTML
 * (`<div><span>label</span></div>`) under the default `htmlLabels: true`.
 * Without the html profile DOMPurify strips that inner HTML, leaving the node
 * SHAPES but NO TEXT (the "diagrams render but labels are missing" bug). We
 * keep `foreignObject` explicitly and still hard-forbid script / event-handler
 * / external-navigation vectors so enabling html stays safe for untrusted
 * model output (this SVG profile is isolated from the global Markdown one). */
const svgConfig = {
  USE_PROFILES: { html: true, svg: true, svgFilters: true },
  ADD_TAGS: ["foreignObject"],
  FORBID_TAGS: ["script", "iframe", "object", "embed", "base", "form"],
  FORBID_CONTENTS: ["script"],
  FORBID_ATTR: [
    "onload",
    "onerror",
    "onclick",
    "onmouseover",
    "onmouseenter",
    "onmouseleave",
    "onfocus",
    "onblur",
    "onanimationstart",
    "onanimationend",
    "ontransitionend",
  ],
};

type Mermaid = typeof import("mermaid").default;

/** User-facing strings (passed from the Vue layer for i18n). */
export interface MermaidLabels {
  readonly rendering: string;
  readonly renderError: (message: string) => string;
  readonly errorDefault: string;
  readonly errorEmpty: string;
  /** Toolbar — "Copy" trigger button. */
  readonly copy: string;
  /** Toolbar — "Download" trigger button. */
  readonly download: string;
  /** Copy menu — "Copy source". */
  readonly copySource: string;
  /** Copy menu — "Copy SVG". */
  readonly copySvg: string;
  /** Copy menu — "Copy PNG". */
  readonly copyPng: string;
  /** Download menu — "Download SVG". */
  readonly downloadSvg: string;
  /** Download menu — "Download PNG". */
  readonly downloadPng: string;
  /** Transient feedback after a successful copy. */
  readonly copied: string;
  /** Tooltip / aria hint on the diagram (click to zoom). */
  readonly zoomHint: string;
}

const DEFAULT_LABELS: MermaidLabels = {
  rendering: "Rendering Mermaid diagram...",
  renderError: (message) => `Mermaid render failed: ${message}`,
  errorDefault: "Unable to render Mermaid diagram.",
  errorEmpty: "Mermaid rendered an empty diagram.",
  copy: "Copy",
  download: "Download",
  copySource: "Copy source",
  copySvg: "Copy SVG",
  copyPng: "Copy PNG",
  downloadSvg: "Download SVG",
  downloadPng: "Download PNG",
  copied: "Copied",
  zoomHint: "Click to zoom",
};

const cache: { promise?: Promise<Mermaid>; id: number; queue: Promise<void> } = {
  id: 0,
  queue: Promise.resolve(),
};

/** Lazy-load mermaid once (keeps it out of the initial bundle). */
async function load(): Promise<Mermaid> {
  if (!cache.promise) {
    cache.promise = import("mermaid").then((mod) => mod.default);
  }
  return cache.promise;
}

/** FNV-1a 32-bit hash → base36, used as a stable idempotency key. */
function fnv1a(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(36);
}

function parseColor(color: string): number[] | undefined {
  const value = color.trim();
  const hex = /^#([0-9a-f]{6})/i.exec(value);
  if (hex?.[1]) {
    return [
      parseInt(hex[1].slice(0, 2), 16),
      parseInt(hex[1].slice(2, 4), 16),
      parseInt(hex[1].slice(4, 6), 16),
    ];
  }
  const short = /^#([0-9a-f]{3})/i.exec(value);
  if (short?.[1]) {
    return short[1].split("").map((p) => parseInt(`${p}${p}`, 16));
  }
  const rgb = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i.exec(value);
  if (rgb?.[1] && rgb[2] && rgb[3]) {
    return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])];
  }
  return undefined;
}

/** Resolve a CSS value (possibly `var(...)`) to a concrete colour string. */
function resolve(root: Element, value: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  if (!trimmed.includes("var(")) return trimmed;

  const doc = root.ownerDocument;
  const probe = doc.createElement("span");
  probe.style.color = trimmed;
  probe.style.position = "absolute";
  probe.style.visibility = "hidden";
  probe.style.pointerEvents = "none";

  const parent = root instanceof HTMLElement ? root : doc.body;
  parent.appendChild(probe);
  const color = getComputedStyle(probe).color.trim();
  probe.remove();
  return color || trimmed;
}

/** First resolvable CSS custom property (by name list), else fallback. */
function cssVar(root: Element, names: string[], fallback: string): string {
  const style = getComputedStyle(root);
  for (const name of names) {
    const value = resolve(root, style.getPropertyValue(name));
    if (value) return value;
  }
  return resolve(root, fallback) ?? fallback;
}

/** Whether the current theme is dark — `html.light` class wins, else luminance. */
function isDark(root: Element, background: string): boolean {
  const html = root.ownerDocument.documentElement;
  if (html.classList.contains("light")) return false;

  const scheme = getComputedStyle(root).colorScheme;
  if (scheme.includes("dark")) return true;
  if (scheme.includes("light")) return false;

  const rgb = parseColor(background);
  if (!rgb) return true;
  const [r, g, b] = rgb;
  if (r === undefined || g === undefined || b === undefined) return true;
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.5;
}

/** Build a Mermaid config whose themeVariables follow THIS project's tokens. */
function config(root: Element) {
  const style = getComputedStyle(root);
  const background = cssVar(
    root,
    ["--bg-code", "--bg-tertiary", "--bg-secondary"],
    style.backgroundColor || "#1e2230",
  );
  const panel = cssVar(root, ["--bg-tertiary", "--bg-secondary"], background);
  const alt = cssVar(root, ["--bg-hover", "--bg-secondary"], panel);
  const text = cssVar(
    root,
    ["--text-primary", "--code-text"],
    style.color || "#f0f2f8",
  );
  const weak = cssVar(root, ["--text-secondary", "--text-muted"], text);
  const border = cssVar(root, ["--border", "--border-light"], weak);
  const accent = cssVar(root, ["--accent", "--info"], "#7c6cff");
  const critical = cssVar(root, ["--error"], "#f87171");
  const criticalBg = cssVar(root, ["--banner-error-bg"], alt);

  return {
    startOnLoad: false,
    securityLevel: "strict" as const,
    suppressErrorRendering: true,
    theme: "base" as const,
    // Render node labels as native SVG <text>, NOT HTML inside <foreignObject>.
    // Under securityLevel:"strict" Mermaid defaults to htmlLabels:true, which
    // emits labels as XHTML in a <foreignObject>; DOMPurify (even with the html
    // profile) drops that foreign-namespace HTML during SVG sanitisation,
    // leaving node SHAPES with NO visible TEXT. Forcing htmlLabels:false makes
    // labels plain SVG <text>, which the svg profile preserves — and it is the
    // safer posture for untrusted model output (no embedded HTML at all).
    htmlLabels: false,
    flowchart: { htmlLabels: false },
    themeVariables: {
      darkMode: isDark(root, background),
      background,
      textColor: text,
      mainBkg: panel,
      nodeBorder: border,
      lineColor: weak,
      primaryColor: panel,
      primaryTextColor: text,
      primaryBorderColor: border,
      secondaryColor: alt,
      tertiaryColor: background,
      classText: text,
      labelColor: text,
      actorLineColor: weak,
      actorBkg: panel,
      actorBorder: border,
      actorTextColor: text,
      fillType0: panel,
      fillType1: alt,
      fillType2: background,
      fontSize: "14px",
      fontFamily: "var(--font-sans)",
      noteTextColor: text,
      noteBkgColor: alt,
      noteBorderColor: border,
      critBorderColor: critical,
      critBkgColor: criticalBg,
      taskTextColor: text,
      taskTextOutsideColor: text,
      taskTextLightColor: text,
      sectionBkgColor: panel,
      sectionBkgColor2: alt,
      altBackground: panel,
      linkColor: accent,
      compositeBackground: panel,
      compositeBorder: border,
      titleColor: text,
      edgeLabelBackground: background,
    },
  };
}

/** Serialise async renders through a single queue (mermaid.initialize is global). */
function enqueue<T>(run: () => Promise<T>): Promise<T> {
  const next = cache.queue.then(run, run);
  cache.queue = next.then(
    () => undefined,
    () => undefined,
  );
  return next;
}

/**
 * Sanitise a Mermaid SVG string through the SEPARATE SVG profile.
 * Exported for testing the isolation boundary.
 */
export function sanitizeMermaidSvg(svg: string): string {
  if (!DOMPurify.isSupported) return "";
  return DOMPurify.sanitize(svg, svgConfig);
}

// ─── Export / copy / download primitives (pure browser API) ─
// Plain TS functions consumed by the Vue event-delegation layer. No extra libraries:
// SVG serialisation + canvas raster + Clipboard / anchor download only.

/** Serialise an `<svg>` element to a standalone XML string (xmlns ensured). */
export function serializeSvg(svg: SVGElement): string {
  const clone = svg.cloneNode(true) as SVGElement;
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  return new XMLSerializer().serializeToString(clone);
}

/** Build a UTF-8-safe `data:` URL for the given MIME type + textual content. */
function dataUrl(type: string, content: string): string {
  // `unescape(encodeURIComponent(...))` round-trips UTF-8 → Latin-1 so btoa
  // never throws on multi-byte characters (node labels, CJK, emoji).
  return `data:${type};base64,${btoa(unescape(encodeURIComponent(content)))}`;
}

/** `data:image/svg+xml` URL for an `<svg>` element (used by zoom + download). */
export function svgToDataUrl(svg: SVGElement): string {
  return dataUrl("image/svg+xml", serializeSvg(svg));
}

/** Rasterise an `<svg>` to a PNG `data:` URL via an offscreen canvas. */
export async function svgToPngDataUrl(svg: SVGElement): Promise<string> {
  // Prefer the intrinsic viewBox size; fall back to the laid-out box; min 1px.
  const box =
    svg instanceof SVGSVGElement ? svg.viewBox?.baseVal : undefined;
  let width = box && box.width > 0 ? box.width : 0;
  let height = box && box.height > 0 ? box.height : 0;
  if (width <= 0 || height <= 0) {
    const rect = svg.getBoundingClientRect();
    width = rect.width;
    height = rect.height;
  }
  width = Math.max(1, Math.round(width));
  height = Math.max(1, Math.round(height));

  const src = svgToDataUrl(svg);
  const img = new Image();
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve();
    img.onerror = () => reject(new Error("svg image load failed"));
    img.src = src;
  });

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas 2d context unavailable");
  ctx.drawImage(img, 0, 0, width, height);
  return canvas.toDataURL("image/png");
}

/** Trigger a browser download of `href` as `filename` (data-URL as href). */
function triggerDownload(href: string, filename: string): void {
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Copy the raw Mermaid source text to the clipboard. */
export async function copyMermaidSource(source: string): Promise<void> {
  if (!navigator.clipboard) throw new Error("clipboard unavailable");
  await navigator.clipboard.writeText(source);
}

/** Copy the serialised SVG markup to the clipboard (as text). */
export async function copyMermaidSvg(svg: SVGElement): Promise<void> {
  if (!navigator.clipboard) throw new Error("clipboard unavailable");
  await navigator.clipboard.writeText(serializeSvg(svg));
}

/** Copy a PNG raster of the diagram to the clipboard (SVG text fallback). */
export async function copyMermaidPng(svg: SVGElement): Promise<void> {
  if (!navigator.clipboard) throw new Error("clipboard unavailable");
  const url = await svgToPngDataUrl(svg);
  const blob = await (await fetch(url)).blob();
  if (typeof ClipboardItem === "undefined") {
    // Clipboard image write unsupported — degrade to SVG text copy.
    await navigator.clipboard.writeText(serializeSvg(svg));
    return;
  }
  await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
}

/** Download the diagram as an `.svg` file. */
export function downloadMermaidSvg(svg: SVGElement): void {
  triggerDownload(svgToDataUrl(svg), "mermaid-diagram.svg");
}

/** Download the diagram as a `.png` file. */
export async function downloadMermaidPng(svg: SVGElement): Promise<void> {
  triggerDownload(await svgToPngDataUrl(svg), "mermaid-diagram.png");
}

/** The five toolbar actions, matched 1:1 by the `data-mermaid-action` value. */
export type MermaidAction =
  | "copy-source"
  | "copy-svg"
  | "copy-png"
  | "download-svg"
  | "download-png";

/** Extract the rendered `<svg>` from a mermaid marker wrapper, if present. */
export function mermaidSvgOf(wrapper: HTMLElement): SVGElement | null {
  return wrapper.querySelector<SVGElement>(
    '[data-slot="mermaid-panel"] svg',
  );
}

/** Extract the raw Mermaid source from a mermaid marker wrapper. */
export function mermaidSourceOf(wrapper: HTMLElement): string {
  return (
    wrapper.querySelector('pre > code[data-lang="mermaid"]')?.textContent ?? ""
  );
}

/**
 * Run a toolbar action against a mermaid marker wrapper. Returns `true` when the
 * action was a "copy" that completed (so the caller can show "copied" feedback),
 * `false` for downloads / no-ops. Throws on failure so the caller can ignore.
 */
export async function runMermaidAction(
  action: MermaidAction,
  wrapper: HTMLElement,
): Promise<boolean> {
  const svg = mermaidSvgOf(wrapper);
  switch (action) {
    case "copy-source": {
      const source = mermaidSourceOf(wrapper);
      if (!source) return false;
      await copyMermaidSource(source);
      return true;
    }
    case "copy-svg":
      if (!svg) return false;
      await copyMermaidSvg(svg);
      return true;
    case "copy-png":
      if (!svg) return false;
      await copyMermaidPng(svg);
      return true;
    case "download-svg":
      if (!svg) return false;
      downloadMermaidSvg(svg);
      return false;
    case "download-png":
      if (!svg) return false;
      await downloadMermaidPng(svg);
      return false;
    default:
      return false;
  }
}

function message(err: unknown, labels: MermaidLabels): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  return labels.errorDefault;
}

/** Find / create the diagram panel as the first child of the marker wrapper. */
function panel(wrapper: HTMLElement): HTMLDivElement {
  const found = Array.from(wrapper.children).find(
    (child): child is HTMLDivElement =>
      child instanceof HTMLDivElement &&
      child.getAttribute("data-slot") === "mermaid-panel",
  );
  if (found) return found;

  const el = wrapper.ownerDocument.createElement("div");
  el.setAttribute("data-slot", "mermaid-panel");
  el.className = "mermaid-panel";
  wrapper.insertBefore(el, wrapper.firstChild);
  return el;
}

/**
 * Inject the copy/download toolbar above the diagram panel (idempotent). The
 * toolbar carries `data-mermaid-action` on each action button; the Vue layer's
 * event delegation resolves clicks to `runMermaidAction`. Re-running on a
 * theme switch / re-render only refreshes the (i18n) labels — it never appends
 * a second toolbar (guarded by the existing `[data-slot="mermaid-toolbar"]`).
 */
function toolbar(wrapper: HTMLElement, labels: MermaidLabels): void {
  const doc = wrapper.ownerDocument;
  let bar = wrapper.querySelector<HTMLElement>(
    '[data-slot="mermaid-toolbar"]',
  );
  if (!bar) {
    bar = doc.createElement("div");
    bar.setAttribute("data-slot", "mermaid-toolbar");
    bar.className = "mermaid-toolbar";
    // Toolbar sits ABOVE the panel (panel is the wrapper's first child).
    wrapper.insertBefore(bar, wrapper.firstChild);
  }

  const menu = (
    trigger: { label: string; cls: string },
    items: { action: MermaidAction; label: string }[],
  ): string => {
    const buttons = items
      .map(
        (it) =>
          `<button type="button" class="mermaid-menu-item" role="menuitem" ` +
          `data-mermaid-action="${it.action}">${escapeText(it.label)}</button>`,
      )
      .join("");
    return (
      `<div class="mermaid-menu" data-mermaid-menu>` +
      `<button type="button" class="mermaid-tool-btn ${trigger.cls}" ` +
      `data-mermaid-menu-trigger aria-haspopup="menu" aria-expanded="false">` +
      `${escapeText(trigger.label)}</button>` +
      `<div class="mermaid-menu-list" role="menu">${buttons}</div>` +
      `</div>`
    );
  };

  bar.innerHTML =
    menu(
      { label: labels.copy, cls: "mermaid-tool-btn--copy" },
      [
        { action: "copy-source", label: labels.copySource },
        { action: "copy-svg", label: labels.copySvg },
        { action: "copy-png", label: labels.copyPng },
      ],
    ) +
    menu(
      { label: labels.download, cls: "mermaid-tool-btn--download" },
      [
        { action: "download-svg", label: labels.downloadSvg },
        { action: "download-png", label: labels.downloadPng },
      ],
    );
}

/** Escape user-facing label text for safe innerHTML insertion. */
function escapeText(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function fail(
  wrapper: HTMLElement,
  pre: HTMLPreElement,
  err: unknown,
  labels: MermaidLabels,
): void {
  const el = panel(wrapper);
  el.setAttribute("data-state", "error");
  el.textContent = labels.renderError(message(err, labels));
  wrapper.setAttribute("data-mermaid-state", "error");
  pre.hidden = false;
}

/**
 * True if the root contains at least one renderable mermaid block. Blocks
 * inside a `[data-mermaid-skip]` subtree (e.g. the live streaming bubble) are
 * ignored — the diagram only renders once the turn commits.
 */
export function hasMermaid(root: HTMLElement): boolean {
  for (const block of root.querySelectorAll(
    'pre > code[data-lang="mermaid"]',
  )) {
    if (!block.closest("[data-mermaid-skip]")) return true;
  }
  return false;
}

async function toSvg(
  renderer: Mermaid,
  source: string,
  cfg: ReturnType<typeof config>,
): Promise<{ svg: string }> {
  return enqueue(async () => {
    renderer.initialize(cfg);
    await renderer.parse(source);
    return renderer.render(`qai-mermaid-${fnv1a(source)}-${cache.id++}`, source);
  });
}

/**
 * Walk the committed DOM under `root`, rendering every mermaid marker block to
 * an SVG diagram. Idempotent (hash + theme). Safe to call repeatedly (mount /
 * content change / theme switch). Aborts cleanly when `signal.aborted` or the
 * node detaches.
 */
export async function renderMermaid(
  root: HTMLElement,
  signal: { aborted: boolean },
  input?: Partial<MermaidLabels>,
): Promise<void> {
  const labels: MermaidLabels = { ...DEFAULT_LABELS, ...input };
  const blocks = Array.from(
    root.querySelectorAll('pre > code[data-lang="mermaid"]'),
  );
  if (blocks.length === 0) return;

  let renderer: Mermaid | undefined;
  try {
    renderer = await load();
  } catch (err) {
    for (const block of blocks) {
      const pre = block.parentElement;
      const wrapper = pre?.parentElement;
      if (block.closest("[data-mermaid-skip]")) continue;
      if (!(pre instanceof HTMLPreElement)) continue;
      if (!(wrapper instanceof HTMLElement)) continue;
      if (wrapper.getAttribute("data-kind") !== "mermaid") continue;
      fail(wrapper, pre, err, labels);
    }
    return;
  }
  if (!renderer) return;

  for (const block of blocks) {
    if (signal.aborted || !root.isConnected) return;
    if (!(block instanceof HTMLElement)) continue;
    // Skip blocks inside a streaming (or otherwise opted-out) subtree: the
    // diagram renders only after the turn commits (product decision).
    if (block.closest("[data-mermaid-skip]")) continue;

    const pre = block.parentElement;
    if (!(pre instanceof HTMLPreElement)) continue;

    const wrapper = pre.parentElement;
    if (!(wrapper instanceof HTMLElement)) continue;
    if (wrapper.getAttribute("data-kind") !== "mermaid") continue;

    const source = block.textContent ?? "";
    if (!source.trim()) continue;

    const cfg = config(wrapper);
    const hash = fnv1a(source);
    const theme = fnv1a(JSON.stringify(cfg.themeVariables));
    const state = wrapper.getAttribute("data-mermaid-state");
    if (
      state === "rendered" &&
      wrapper.getAttribute("data-mermaid-hash") === hash &&
      wrapper.getAttribute("data-mermaid-theme") === theme
    ) {
      // Already rendered for this exact source + theme — no-op.
      pre.hidden = true;
      continue;
    }

    // Keep the previously-rendered diagram visible while re-rendering the SAME
    // source (theme switch) to avoid a flash back to source.
    const keep =
      state === "rendered" && wrapper.getAttribute("data-mermaid-hash") === hash;

    wrapper.setAttribute("data-mermaid-hash", hash);
    wrapper.setAttribute("data-mermaid-theme", theme);
    wrapper.setAttribute("data-mermaid-state", "rendering");

    const el = panel(wrapper);
    if (!keep) {
      el.setAttribute("data-state", "rendering");
      el.textContent = labels.rendering;
      pre.hidden = false;
    } else {
      pre.hidden = true;
    }

    try {
      const result = await toSvg(renderer, source, cfg);
      if (signal.aborted || !root.isConnected || !wrapper.isConnected) return;

      const safe = sanitizeMermaidSvg(result.svg);
      if (!safe.trim() || !/<svg[\s>]/i.test(safe)) {
        throw new Error(labels.errorEmpty);
      }

      el.setAttribute("data-state", "rendered");
      el.innerHTML = safe;
      // Make the diagram a click-to-zoom target + inject the action toolbar.
      // Both are idempotent so theme re-renders never duplicate/leak handlers.
      el.setAttribute("data-mermaid-zoom", "");
      el.setAttribute("title", labels.zoomHint);
      toolbar(wrapper, labels);
      wrapper.setAttribute("data-mermaid-state", "rendered");
      pre.hidden = true;
    } catch (err) {
      if (signal.aborted || !root.isConnected || !wrapper.isConnected) return;
      fail(wrapper, pre, err, labels);
    }
  }
}

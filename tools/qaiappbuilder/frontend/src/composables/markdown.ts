// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Markdown rendering pipeline.
 *
 * S7.5 L8 PR-804.
 *
 * Pipeline: source markdown -> marked -> highlight.js per-language ->
 * DOMPurify sanitisation -> safe HTML string ready to inject as
 * `v-html`. The pipeline is deliberately synchronous and dependency-
 * pure: no DOM access, no network. Tests can call `renderMarkdown`
 * with arbitrary input.
 *
 * Why a custom pipeline rather than `marked`'s built-in highlight or
 * a plug-in stack: we need every emitted HTML node to be sanitised by
 * DOMPurify *after* highlighting, because hljs returns spans with
 * class names (e.g. `<span class="hljs-keyword">`) that DOMPurify must
 * be configured to allow. Configuring DOMPurify in one place inside
 * this module keeps the policy auditable.
 *
 * Security policy:
 *   - DOMPurify ALLOW_TAGS includes the markdown subset: a / p /
 *     ul-ol-li / blockquote / pre-code / em / strong / etc., plus
 *     <span> with class attribute (for hljs).
 *   - SVG / iframe / form / script / object / embed are forbidden.
 *   - href / src URLs are bound to http/https/mailto schemes.
 *
 * Mermaid:
 *   A ```mermaid``` fenced block is NOT highlighted; instead it is emitted
 *   as a stable marker block — a `<div class="mermaid-code" data-kind=
 *   "mermaid" data-mermaid-state="pending">` wrapper around
 *   `<pre><code class="language-mermaid" data-lang="mermaid">{source}</code>`.
 *   The raw diagram source lives ONLY in the `<code>` textContent (escaped),
 *   never in a data attribute. This synchronous function does
 *   NOT render the SVG; the post-render hook (`useMermaidRender` →
 *   `renderMermaid`) walks committed DOM and replaces the marker with the
 *   sanitised SVG. The GLOBAL Markdown sanitiser here STAYS HTML-only (no SVG
 *   tags); the Mermaid SVG goes through a SEPARATE DOMPurify SVG profile in
 *   `markdown-mermaid.ts`. The small set of marker attributes the hook needs
 *   (`data-lang` / `data-kind` / `data-mermaid-state` / `data-mermaid-hash` /
 *   `data-mermaid-theme`) is whitelisted EXPLICITLY in `BASE_ALLOWED_ATTR`
 *   (not via a `data-*` wildcard), keeping the marker policy auditable in one
 *   place.
 *
 * The chat / ai_coding render path uses `renderMarkdown(md)` directly.
 */
import DOMPurify, { type Config as DOMPurifyConfig } from "dompurify";
import hljs from "highlight.js/lib/common";
import { marked, type MarkedOptions, type Tokens } from "marked";

/**
 * Markdown options surface.
 */
export interface RenderMarkdownOptions {
  /**
   * Additional tags to whitelist beyond the default markdown subset.
   * Use sparingly — every entry widens the XSS surface.
   */
  readonly extraAllowedTags?: readonly string[];
  /**
   * Override `marked` parser flags. Common flags:
   *   - `breaks: true`   — convert `\n` to `<br>` (chat-style)
   *   - `gfm: true`      — GitHub-flavoured Markdown (default)
   */
  readonly markedOptions?: MarkedOptions;
  /**
   * PERF: skip highlight.js automatic language detection (`highlightAuto`)
   * for code blocks WITHOUT an explicit language hint. `highlightAuto` is the
   * single most expensive step (it tries every common grammar) and it runs on
   * the WHOLE accumulated text on every streaming flush, making long live
   * answers crawl (O(n²) over a turn). When `true`, unhinted blocks render as
   * escaped plain `<pre><code>` (still readable); explicit-language blocks are
   * still highlighted (cheap, single grammar). Use this for the LIVE streaming
   * bubble only — the committed message is rendered once with full auto-detect.
   */
  readonly fastCodeBlocks?: boolean;
}

const BASE_ALLOWED_TAGS = [
  "a",
  "p",
  "br",
  "hr",
  "ul",
  "ol",
  "li",
  "blockquote",
  "pre",
  "code",
  "em",
  "strong",
  "del",
  "s",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "table",
  "thead",
  "tbody",
  "tr",
  "td",
  "th",
  "span",
  "div",
  "img",
  // Code-block header copy button (V1 utils.js:48-51).
  "button",
] as const;

const BASE_ALLOWED_ATTR = [
  "href",
  "title",
  "target",
  "rel",
  "src",
  "alt",
  "width",
  "height",
  "class",
  "lang",
  "data-language",
  // Code-block copy button (V1 utils.js:42-52). The button carries the
  // raw code in `data-code` (encodeURIComponent'd) and is marked with
  // `data-code-copy`; ChatMessageList delegates click → clipboard. We
  // deliberately do NOT allow inline `onclick` (unlike V1 which used
  // ADD_ATTR:['onclick']) — the V2 security terminal keeps the handler
  // out of the sanitised HTML and runs it via event delegation instead.
  "type",
  "data-code-copy",
  "data-code",
  // Mermaid marker block (markdown-mermaid.ts hook contract). The diagram
  // SOURCE is NOT carried in a data attribute — it lives in the <code>
  // textContent only. These marker attributes are listed
  // explicitly (not via a `data-*` wildcard) so the contract is auditable in
  // one place; the post-render hook reads them to locate blocks + drive
  // idempotent render/cache state. (DOMPurify permits `data-*` by default in
  // this project's config — listing them keeps the policy explicit and means
  // they survive even if a future change sets `ALLOW_DATA_ATTR: false`.)
  "data-lang",
  "data-kind",
  "data-mermaid-state",
  "data-mermaid-hash",
  "data-mermaid-theme",
] as const;

// Cached DOMPurify config: precomputed once per (extraTags) tuple.
let cachedConfigKey: string | null = null;
let cachedConfig: DOMPurifyConfig | null = null;

function buildPurifyConfig(
  extra: readonly string[] = [],
): DOMPurifyConfig {
  const key = extra.slice().sort().join("|");
  if (cachedConfigKey === key && cachedConfig !== null) {
    return cachedConfig;
  }
  cachedConfigKey = key;
  cachedConfig = {
    ALLOWED_TAGS: [...BASE_ALLOWED_TAGS, ...extra],
    ALLOWED_ATTR: [...BASE_ALLOWED_ATTR],
    // Forbid javascript: / data: text URLs in href/src.
    ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel):|#|\/[^/]|\.\.?\/)/i,
    // Strip rather than reject — we render best-effort HTML.
    KEEP_CONTENT: true,
  };
  return cachedConfig;
}

// Configure marked once at module load.
marked.setOptions({
  gfm: true,
  breaks: false,
  pedantic: false,
});

// Disable single-tilde strikethrough (~text~) while keeping double-tilde
// (~~text~~). V1 utils.js:22-39 parity: marked's built-in del rule uses
// /^(~~?).../ which matches both ~ and ~~. Override the tokenizer so only
// ~~ triggers <del>.
marked.use({
  tokenizer: {
    del(src: string) {
      const match = /^(~~)(?=[^\s~])([\s\S]*?[^\s~])\1(?!~)/.exec(src);
      if (match) {
        return {
          type: "del" as const,
          raw: match[0]!,
          text: match[2]!,
          tokens: this.lexer.inlineTokens(match[2]!),
        };
      }
      return undefined;
    },
  },
});

/**
 * Render markdown source to a sanitised HTML string.
 *
 * Always returns a string; on parse failure the raw markdown is
 * escaped and returned as plain text wrapped in `<p>`. This guarantees
 * `v-html="renderMarkdown(text)"` is safe even for malformed input.
 */
export function renderMarkdown(
  source: string,
  opts: RenderMarkdownOptions = {},
): string {
  if (typeof source !== "string" || source === "") {
    return "";
  }

  // 1. marked: markdown -> HTML. Use a custom code-block renderer that
  //    runs hljs over the language hint so the resulting <code> already
  //    carries the `hljs ...` class set DOMPurify is configured to keep.
  const renderer = new marked.Renderer();
  renderer.code = ({ text, lang: rawLang }: Tokens.Code): string => {
    const lang = (rawLang ?? "").trim().split(/\s+/)[0] ?? "";
    // Mermaid: emit a stable MARKER block instead of highlighting. The raw
    // diagram source is kept (escaped) as the <code> textContent only — the
    // post-render hook (`useMermaidRender`) reads it to render the SVG via a
    // SEPARATE sanitiser. During streaming the marker simply shows as a
    // plain code block (the hook only runs on committed DOM), so the user
    // sees the source until the diagram is ready. No `data-*` source leak.
    if (lang === "mermaid") {
      const langLabel = "mermaid";
      const encoded = encodeURIComponent(text);
      return (
        `<div class="code-block-header">` +
        `<span class="code-lang">${escapeHtml(langLabel)}</span>` +
        `<button class="copy-btn" data-code-copy data-code="${encoded}" type="button">⧉ Copy</button>` +
        `</div>` +
        `<div class="mermaid-code" data-kind="mermaid" data-mermaid-state="pending">` +
        `<pre><code class="language-mermaid" data-lang="mermaid">${escapeHtml(text)}</code></pre>` +
        `</div>`
      );
    }
    let html: string;
    if (lang !== "" && hljs.getLanguage(lang)) {
      try {
        html = hljs.highlight(text, { language: lang, ignoreIllegals: true })
          .value;
      } catch {
        html = escapeHtml(text);
      }
    } else {
      // No (or unknown) language hint — mirror V1 (utils.js:45) and let
      // highlight.js auto-detect. `highlight.js/lib/common` ships the
      // common-language subset, on which `highlightAuto` is available.
      // PERF: during live streaming (`fastCodeBlocks`) skip the costly
      // auto-detection and render escaped plain text — the committed render
      // (memoized, full auto-detect) produces the final highlighted output.
      if (opts.fastCodeBlocks === true) {
        html = escapeHtml(text);
      } else {
        try {
          html = hljs.highlightAuto(text).value;
        } catch {
          html = escapeHtml(text);
        }
      }
    }
    const cls = lang === "" ? "hljs" : `hljs language-${lang}`;
    // V1 utils.js:48-52 emits a header row (lang label + copy button)
    // before the <pre><code>. The copy button is rendered with
    // `data-code-copy` + the raw source in `data-code` (URI-encoded);
    // ChatMessageList wires the click via event delegation rather than
    // an inline onclick (V2 security terminal — see DOMPurify config).
    const langLabel = lang === "" ? "code" : lang;
    const encoded = encodeURIComponent(text);
    return (
      `<div class="code-block-header">` +
      `<span class="code-lang">${escapeHtml(langLabel)}</span>` +
      `<button class="copy-btn" data-code-copy data-code="${encoded}" type="button">⧉ Copy</button>` +
      `</div>` +
      `<pre><code class="${cls}">${html}</code></pre>`
    );
  };

  let rawHtml: string;
  try {
    const parsed = marked.parse(source, {
      ...opts.markedOptions,
      renderer,
    });
    // marked.parse() can return Promise in async mode; we configure
    // sync above so cast is safe.
    rawHtml = typeof parsed === "string" ? parsed : "";
  } catch {
    rawHtml = `<p>${escapeHtml(source)}</p>`;
  }

  // 2. DOMPurify sanitise.
  const config = buildPurifyConfig(opts.extraAllowedTags);
  // dompurify.sanitize returns string when RETURN_DOM is false (default).
  return DOMPurify.sanitize(rawHtml, config) as unknown as string;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

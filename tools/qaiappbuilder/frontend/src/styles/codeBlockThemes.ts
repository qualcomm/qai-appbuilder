// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * Theme-scoped highlight.js code-block styling.
 *
 * Problem (V1/legacy defect): `main.ts` used to import both bundled
 * highlight.js themes as plain global CSS:
 *
 *     import "highlight.js/styles/github.css";        // light
 *     import "highlight.js/styles/github-dark.css";   // dark (imported last)
 *
 * Both themes define the SAME plain selectors (`.hljs`, `.hljs-addition`,
 * `.hljs-deletion`, …) at equal specificity, so the one imported LAST
 * (`github-dark.css`) always won — in BOTH the app's light and dark themes.
 * Result: every code block and every tool-result diff (write / edit /
 * apply_patch) rendered with dark backgrounds (`#0d1117`, green `#033a16`,
 * red `#67060c`) even when the surrounding app UI was in light mode, giving
 * the jarring "light page, dark code" mismatch.
 *
 * The app toggles theme via the `html.light` class (see
 * `composables/useTheme.ts`); `:root` (no class) is the dark default. A bare
 * `import "...css"` cannot be scoped at the import site, so instead we import
 * each theme as an inline STRING (Vite `?inline`) and re-emit it wrapped in
 * the matching theme selector. This makes highlight.js follow the app theme
 * for free, consistently across chat messages and all diff-producing tools,
 * and keeps tracking upstream theme updates without hand-copying selectors.
 */

// Vite `?inline` returns the processed CSS as a string instead of injecting
// it into the document. We scope and inject it ourselves below.
import darkThemeCss from "highlight.js/styles/github-dark.css?inline";
import lightThemeCss from "highlight.js/styles/github.css?inline";

/**
 * Prefix every top-level style rule in a highlight.js theme stylesheet with a
 * scope selector so it only applies under the matching app theme.
 *
 * highlight.js `github*.css` themes are flat: a sequence of
 * `selectorList { decls }` rules with no nested at-rules / media queries. This
 * lightweight pass prefixes each rule's selector list and avoids pulling in a
 * full CSS parser. Comment blocks (`/* … *\/`) are stripped first; rule bodies
 * (`{ … }`) are preserved verbatim; only the selector preludes are rewritten
 * (`a, b { … }` → `<scope> a, <scope> b { … }`).
 *
 * Robustness guards (so a future theme swap can't silently emit broken CSS):
 *  - Statement at-rules that end in `;` (e.g. `@charset "UTF-8";`,
 *    `@import url(...);`) carry NO selector and are passed through unchanged.
 *    A prelude may contain such statements glued to the following rule's
 *    selector (the regex captures everything up to the next `{`); we split on
 *    `;`, keep the at-statements verbatim, and scope only the trailing selector.
 *  - A block at-rule prelude (`@media`/`@supports`/`@keyframes` …) is NOT a
 *    plain selector list — prefixing it would corrupt the stylesheet. Such a
 *    rule is passed through UNCHANGED (left global). The two bundled themes
 *    have none today; this just fails safe if one is added later.
 *  - A `:root` selector is kept unscoped too: it targets the document root and
 *    prefixing it (`html.light :root`) would never match. (No bundled theme
 *    uses it today, but cheap insurance.)
 *
 * Exported for unit testing; not part of the module's public surface otherwise.
 */
export function scopeThemeCss(css: string, scope: string): string {
  // Strip comments first so braces inside them never confuse the splitter.
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  return withoutComments.replace(
    /([^{}]+)(\{[^{}]*\})/g,
    (match, prelude: string, body: string) => {
      // A prelude may be a bare selector list, OR one or more `;`-terminated
      // at-statements (@charset/@import) followed by the rule's selector list.
      // Keep everything up to and including the last `;` verbatim; scope only
      // the trailing selector segment.
      const lastSemi = prelude.lastIndexOf(";");
      const leading = lastSemi >= 0 ? prelude.slice(0, lastSemi + 1) : "";
      const selectorPart = prelude.slice(lastSemi + 1).trim();

      // Block at-rule (e.g. @media { … }) — passing a non-selector prelude to
      // the scoper would produce invalid CSS, so leave the whole rule global.
      if (selectorPart.startsWith("@")) return match;
      if (selectorPart === "") {
        // No selector (e.g. a trailing `@import url(...);` with no rule) —
        // emit the leading at-statements + the body unchanged.
        return `${leading}${body}`;
      }
      const selectors = selectorPart
        .split(",")
        .map((sel) => sel.trim())
        .filter((sel) => sel.length > 0)
        .map((sel) => (sel === ":root" ? sel : `${scope} ${sel}`))
        .join(", ");
      if (!selectors) return `${leading}${body}`;
      return `${leading}${leading ? " " : ""}${selectors} ${body}`;
    },
  );
}

/**
 * Inject the theme-scoped highlight.js CSS exactly once.
 *
 * - Dark theme rules are scoped to `html:not(.light)` (the `:root` default).
 * - Light theme rules are scoped to `html.light`.
 *
 * Must run before the app mounts so the first paint of any code block / diff
 * is already correctly themed.
 */
export function installCodeBlockThemes(): void {
  if (document.getElementById("hljs-themes")) return;
  const style = document.createElement("style");
  style.id = "hljs-themes";
  style.textContent = [
    scopeThemeCss(darkThemeCss, "html:not(.light)"),
    scopeThemeCss(lightThemeCss, "html.light"),
  ].join("\n");
  document.head.appendChild(style);
}

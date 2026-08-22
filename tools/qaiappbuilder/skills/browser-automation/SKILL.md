---
name: browser-automation
description: Use when working with the `browser` tool — navigating or inspecting a page, clicking through a flow, filling and submitting a form, acting inside a logged-in session, or screenshotting a rendered page — and especially when a page will not cooperate — a selector matches nothing, content is missing because it renders with JavaScript, an interaction has no effect, or a browser must be attached to instead of launched.
tags: browser, browser-tool, web, automation, playwright, selectors, cdp
use_for: Companion reference for the `browser` tool — navigating and interacting with JavaScript-rendered pages, filling forms, clicking through flows, acting in a logged-in session, screenshotting a page, and diagnosing a selector or interaction that did not work
---

# Browser Automation

**Companion reference for the `browser` tool.** The tool does the acting (open
a tab, run ops or page JavaScript, close); this skill covers the parts the tool
signature cannot convey — which browser kind to use, how to pick selectors that
survive a re-render, who is responsible for starting and stopping a browser, and
what to do when a page will not cooperate.

Use it for pages plain fetching (`web_fetch`) cannot handle: JavaScript-rendered
content, forms, multi-step flows, and content behind a logged-in session.

## When to Use

- The page renders its content with JavaScript (a `web_fetch` returns an empty
  or shell page).
- You need to **interact**: click a button, fill and submit a form, choose from
  a dropdown, scroll to trigger lazy loading.
- You need to act inside a **logged-in session** (attach to the user's already
  authenticated Chrome/Edge via `kind: "connected"`).
- You need a **screenshot** of a rendered page.

Also load it after a `browser` action misbehaved: a selector matched nothing,
the content you expected is absent, a click or fill had no effect, or a tab
went stale mid-flow.

Prefer `web_fetch` for a simple "read the text of this URL" — it is lighter.
Prefer `web_search` to find pages. Use `browser` when you must *interact*.

## The Three Actions

1. **open** — acquire a named tab and optionally navigate.
   - `kind: "headless"` (default) — a managed background browser the tool
     starts AND stops for you. Best for most tasks.
   - `kind: "connected"`, `app: {cdp_url}` — attach to an ALREADY-RUNNING
     browser over CDP. Use for a VISIBLE browser or a logged-in session. YOU
     start it first with the `background_process` tool (see example) — never
     ask the user to. This tool never terminates a connected browser.
2. **run** — act on the tab, either:
   - a JavaScript `code` body run in the page (with a `tab` DOM facade and
     `display(x)`; may `return` a value), OR
   - a single named `op`: `observe`, `aria_snapshot`, `goto`, `click`, `fill`,
     `type`, `press`, `select`, `scroll`, `screenshot`, `extract`,
     `wait_for_selector`, `wait_for_url`.
3. **close** — release a tab (`all: true` for every tab). For a `headless`
   browser the process is stopped automatically. For a `connected` browser
   `close` only detaches — stop the browser via the `background_process` that
   started it.

## Selectors

CSS by default, plus: `text/<substring>`, `xpath/<expr>`, `aria/<name>`, and
`ref/<eN>` — where `eN` ids come from a prior `observe`. Example:
`ref/e3`, `text/Sign in`, `input[name=q]`.

## How to Use

1. `browser(action="open", url=...)` to get a tab.
2. `browser(action="run", op="observe")` to list actionable elements with ids.
3. Act: `browser(action="run", op="click", selector="ref/e2")` or
   `browser(action="run", op="fill", selector="input[name=q]", value="...")`.
4. Read: `browser(action="run", op="extract")` or `op="aria_snapshot"`.
5. `browser(action="close", all=true)` when done.

## Example — connected (you start & stop the browser yourself)

```
User: Log into the intranet dashboard (I'm already signed in) and read the build status.
# 1) Start a debuggable browser with background_process (NOT exec — it must
#    outlive the call). Dedicated profile + IPv4 port. Locate Chrome/Edge for
#    THIS OS (Windows: Program Files; macOS: /Applications; Linux: /usr/bin).
#    Do NOT ask the user to launch it.
→ background_process(action="start", name="cdp-chrome",
      cmd='"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
          '--remote-debugging-port=9222 --user-data-dir="%TEMP%\\qai-cdp"')
# 2) Attach (use 127.0.0.1, not localhost) and drive it.
→ browser(action="open", kind="connected", app={"cdp_url":"http://127.0.0.1:9222"},
          url="http://intranet/dashboard")
→ browser(action="run", op="observe")           # find the elements
→ browser(action="run", op="click", selector="text/Builds")
→ browser(action="run", op="extract", selector="#status")
→ browser(action="close", all=true)              # detaches; browser keeps running
# 3) Stop the browser via the process that started it.
→ background_process(action="stop", name="cdp-chrome")
# 4) OPTIONAL — guarantee no leftover children. `stop` frees the debug port
#    immediately and ends the launcher, but Chrome's launcher exits at once and
#    its real browser/renderer processes re-parent away, so a pid-tree kill can
#    miss them (a few idle background chrome.exe may linger, holding no port).
#    To force them gone, match ONLY your own --user-data-dir and tree-kill:
#    Windows PowerShell —
#      Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
#        Where-Object { $_.CommandLine -like '*qai-cdp*' } |
#        ForEach-Object { taskkill /F /T /PID $_.ProcessId }
#    NEVER `Stop-Process chrome` / kill by name alone — that would also close
#    the user's own everyday Chrome. Filter on YOUR profile path, nothing else.
```

## Example — headless (managed, no manual launch)

```
→ browser(action="open", url="https://example.com")   # headless is the default
→ browser(action="run", op="observe")
→ browser(action="run", op="extract")
→ browser(action="close", all=true)                   # tool stops the browser
```

## Boundary

The `browser` tool controls the **browser only**. To read files, fetch URLs
directly, or run shell commands, use the `read`, `webfetch`, and `exec` tools —
JavaScript run inside the page cannot reach the host filesystem or network.

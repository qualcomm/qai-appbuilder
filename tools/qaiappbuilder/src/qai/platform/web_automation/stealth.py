# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Minimal anti-fingerprint patch shared by browser-automation consumers.

Python has no mature stealth library, so this module hand-writes a small
``page.add_init_script`` payload that runs in every frame before page scripts.
It covers the handful of signals a bot-detector most commonly reads and that a
headless Chromium leaks: ``navigator.webdriver``, a missing ``window.chrome``
runtime, an empty plugin/language list, timezone, and the ``UNMASKED_*`` WebGL
vendor/renderer strings that betray SwiftShader software rendering. It is
deliberately conservative — not a full fingerprint-spoofing framework.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

__all__ = ["apply_stealth", "stealth_init_script"]

#: Locale advertised to the page.
_LANGUAGES = ("en-US", "en")

#: Timezone reported to ``Intl`` so a headless box without a configured TZ does
#: not stand out.
_TIMEZONE = "America/Los_Angeles"

#: ``navigator.platform``. MUST stay consistent with
#: ``web_automation.context_options.CHROME_USER_AGENT`` (a macOS Chrome UA) and
#: with the Mac-flavoured WebGL strings below — a UA/platform mismatch is a
#: single-comparison automation tell.
_PLATFORM = "MacIntel"

#: Vertical pixels the browser's own UI (tab strip + address bar + bookmarks)
#: takes above the viewport. Any plausible non-zero value works; the tell is
#: ``outerHeight == innerHeight``, which cannot happen in a real window.
_WINDOW_CHROME_HEIGHT = 87

#: WebGL vendor/renderer spoofed onto the two ``UNMASKED_*`` parameters
#: (0x9245 / 0x9246) that otherwise expose "Google SwiftShader".
_WEBGL_VENDOR = "Intel Inc."
_WEBGL_RENDERER = "Intel Iris OpenGL Engine"

_UNMASKED_VENDOR = 0x9245
_UNMASKED_RENDERER = 0x9246


def stealth_init_script() -> str:
    """Return the JavaScript injected before any page script runs.

    Pure function of the module constants so it can be unit-tested without a
    live browser and reused verbatim by :func:`apply_stealth`.
    """
    languages = ", ".join(f'"{lang}"' for lang in _LANGUAGES)
    return f"""
(() => {{
  Object.defineProperty(navigator, 'webdriver', {{
    get: () => undefined,
    configurable: true,
  }});
  Object.defineProperty(navigator, 'languages', {{
    get: () => [{languages}],
    configurable: true,
  }});
  // ``navigator.platform`` MUST agree with the UA string. The context declares
  // a macOS Chrome UA (see web_automation.context_options.CHROME_USER_AGENT)
  // while Playwright leaves platform at the host's real value ("Win32" on this
  // machine) — a one-line contradiction that any detector cross-checks, and the
  // cheapest possible tell.
  Object.defineProperty(navigator, 'platform', {{
    get: () => "{_PLATFORM}",
    configurable: true,
  }});
  // Plugins and mimeTypes must be CONSISTENT: real Chrome exposes the PDF
  // viewer entries in both. Faking a non-empty `plugins` while leaving
  // `mimeTypes` empty is itself a signature — the pair cannot occur in a real
  // browser. Both are built from one table so they can never drift apart.
  const pdfMimes = [
    {{ type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }},
    {{ type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' }},
  ];
  const pluginData = [
    {{ name: 'PDF Viewer', filename: 'internal-pdf-viewer' }},
    {{ name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' }},
    {{ name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' }},
    {{ name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer' }},
    {{ name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer' }},
  ];
  const makeArrayLike = (items) => {{
    const arr = items.slice();
    arr.item = (i) => arr[i] ?? null;
    arr.namedItem = (n) => arr.find((e) => e.name === n || e.type === n) ?? null;
    return arr;
  }};
  const mimeEntries = pdfMimes.map((m) => ({{
    type: m.type,
    suffixes: m.suffixes,
    description: m.description,
  }}));
  const pluginEntries = pluginData.map((p) => ({{
    name: p.name,
    filename: p.filename,
    description: 'Portable Document Format',
    length: mimeEntries.length,
    item: (i) => mimeEntries[i] ?? null,
    namedItem: (n) => mimeEntries.find((m) => m.type === n) ?? null,
  }}));
  Object.defineProperty(navigator, 'plugins', {{
    get: () => makeArrayLike(pluginEntries),
    configurable: true,
  }});
  Object.defineProperty(navigator, 'mimeTypes', {{
    get: () => makeArrayLike(mimeEntries),
    configurable: true,
  }});
  // ``pdfViewerEnabled`` must agree with the plugin table faked above: real
  // Chrome reports true whenever the internal PDF viewer is present, so
  // advertising the viewer plugin while this stays false is self-contradictory.
  Object.defineProperty(navigator, 'pdfViewerEnabled', {{
    get: () => true,
    configurable: true,
  }});
  // A macOS desktop Chrome reports 0 touch points. Headless Chromium defaults to
  // 10, which contradicts the macOS UA / platform this fingerprint claims.
  Object.defineProperty(navigator, 'maxTouchPoints', {{
    get: () => 0,
    configurable: true,
  }});
  if (!window.chrome) {{
    window.chrome = {{}};
  }}
  window.chrome.runtime = window.chrome.runtime || {{}};
  // Real Chrome carries these three legacy members on window.chrome. Headless
  // omits them, so their ABSENCE alongside a present `chrome` object is itself
  // the signal. Values are shaped plausibly rather than left as bare stubs.
  if (!window.chrome.app) {{
    window.chrome.app = {{
      isInstalled: false,
      InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }},
      RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }},
    }};
  }}
  if (!window.chrome.csi) {{
    window.chrome.csi = () => ({{
      startE: Date.now(), onloadT: Date.now(), pageT: performance.now(), tran: 15,
    }});
  }}
  if (!window.chrome.loadTimes) {{
    window.chrome.loadTimes = () => {{
      const t = performance.timing || {{}};
      const base = (t.navigationStart || Date.now()) / 1000;
      return {{
        requestTime: base,
        startLoadTime: base,
        commitLoadTime: base + 0.02,
        finishDocumentLoadTime: base + 0.15,
        finishLoadTime: base + 0.25,
        firstPaintTime: base + 0.2,
        firstPaintAfterLoadTime: 0,
        navigationType: 'Other',
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
        npnNegotiatedProtocol: 'h2',
        wasAlternateProtocolAvailable: false,
        connectionInfo: 'h2',
      }};
    }};
  }}
  const origQuery = window.navigator.permissions
    && window.navigator.permissions.query;
  if (origQuery) {{
    window.navigator.permissions.query = (parameters) => (
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({{ state: Notification.permission }})
        : origQuery(parameters)
    );
  }}
  try {{
    const DateTimeFormat = Intl.DateTimeFormat;
    const patched = function (...args) {{
      const opts = args[1] || {{}};
      if (!opts.timeZone) {{
        opts.timeZone = "{_TIMEZONE}";
        args[1] = opts;
      }}
      return new DateTimeFormat(...args);
    }};
    patched.prototype = DateTimeFormat.prototype;
    Intl.DateTimeFormat = patched;
  }} catch (err) {{ /* leave Intl untouched on failure */ }}
  const patchGl = (proto) => {{
    if (!proto || !proto.getParameter) {{
      return;
    }}
    const original = proto.getParameter;
    proto.getParameter = function (parameter) {{
      if (parameter === {_UNMASKED_VENDOR}) {{
        return "{_WEBGL_VENDOR}";
      }}
      if (parameter === {_UNMASKED_RENDERER}) {{
        return "{_WEBGL_RENDERER}";
      }}
      return original.apply(this, [parameter]);
    }};
  }};
  patchGl(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGl(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  // A real browser window is TALLER than its viewport: the tab strip, address
  // bar and bookmarks occupy vertical space, so `outerHeight > innerHeight`
  // always. Headless Chromium reports them equal, which is a one-comparison
  // tell that no amount of navigator patching hides.
  try {{
    const chromeUi = {_WINDOW_CHROME_HEIGHT};
    Object.defineProperty(window, 'outerHeight', {{
      get: () => window.innerHeight + chromeUi,
      configurable: true,
    }});
    Object.defineProperty(window, 'outerWidth', {{
      get: () => window.innerWidth,
      configurable: true,
    }});
  }} catch (err) {{ /* leave the dimensions alone if they are not configurable */ }}
}})();
"""


async def apply_stealth(page: Page) -> None:
    """Install the fingerprint patch on ``page`` before navigation.

    Registers the init script so it re-runs on every navigation and subframe
    for the lifetime of the page. Call this immediately after creating the page
    and before the first ``goto``.
    """
    await page.add_init_script(stealth_init_script())

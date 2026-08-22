# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""In-page JavaScript facade + DOM-walk scripts for the browser tool.

Two page-side script builders, both run via ``page.evaluate``:

* :func:`observe_script` — walks the live DOM, tags every actionable element
  with a stable ``data-<ns>-ref="eN"`` attribute, and returns a compact list of
  ``{id, role, name, tag, value, states}`` entries the model can act on with
  ``ref/eN`` selectors. Version-stable (pure DOM; no removed accessibility API).

* :func:`run_wrapper` — wraps a model-authored JS ``code`` body in an async IIFE
  that exposes a ``tab`` facade (DOM-scoped: query/click/fill/type/text/observe)
  plus ``display(x)``. The body runs in the PAGE context, so it can drive the
  DOM but cannot reach the host filesystem, network, or other tools (the safety
  boundary — host capabilities live in the ``read``/``web_fetch``/``exec``/
  ``agent`` tools instead). The wrapper returns ``{value, displays}``.

The ``data-<ns>-ref`` attribute namespace is the project's own ``qai`` token —
deliberately neutral (no forbidden vendor keyword).
"""

from __future__ import annotations

__all__ = ["REF_ATTR", "observe_script", "run_wrapper"]

#: Data-attribute used to tag observed elements for ``ref/eN`` resolution.
REF_ATTR = "data-qai-ref"

# Shared JS: role inference + actionable-element predicate + name extraction.
_DOM_HELPERS = r"""
const REF_ATTR = "%(ref_attr)s";
const roleOf = (el) => {
  const explicit = el.getAttribute && el.getAttribute("role");
  if (explicit) return explicit;
  const tag = el.tagName ? el.tagName.toLowerCase() : "";
  if (tag === "a" && el.hasAttribute("href")) return "link";
  if (tag === "button") return "button";
  if (tag === "select") return "combobox";
  if (tag === "textarea") return "textbox";
  if (tag === "input") {
    const t = (el.getAttribute("type") || "text").toLowerCase();
    if (t === "checkbox") return "checkbox";
    if (t === "radio") return "radio";
    if (t === "submit" || t === "button" || t === "reset") return "button";
    if (t === "hidden") return "";
    return "textbox";
  }
  return tag;
};
const nameOf = (el) => {
  const aria = el.getAttribute && el.getAttribute("aria-label");
  if (aria) return aria.trim();
  const labelledby = el.getAttribute && el.getAttribute("aria-labelledby");
  if (labelledby) {
    const ref = document.getElementById(labelledby);
    if (ref && ref.textContent) return ref.textContent.trim();
  }
  if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
    if (el.id) {
      const lab = document.querySelector('label[for="' + el.id + '"]');
      if (lab && lab.textContent) return lab.textContent.trim();
    }
    const ph = el.getAttribute("placeholder");
    if (ph) return ph.trim();
    const nm = el.getAttribute("name");
    if (nm) return nm.trim();
  }
  const txt = (el.textContent || "").trim().replace(/\s+/g, " ");
  return txt.length > 120 ? txt.slice(0, 117) + "..." : txt;
};
const isActionable = (el) => {
  const tag = el.tagName ? el.tagName.toLowerCase() : "";
  if (["a", "button", "input", "select", "textarea"].includes(tag)) {
    if (tag === "input" && (el.getAttribute("type") || "").toLowerCase() === "hidden") {
      return false;
    }
    return true;
  }
  if (el.hasAttribute && (el.hasAttribute("onclick") || el.hasAttribute("role")
      || el.hasAttribute("tabindex") || el.isContentEditable)) {
    return true;
  }
  return false;
};
const isVisible = (el) => {
  const style = window.getComputedStyle(el);
  if (!style || style.visibility === "hidden" || style.display === "none") return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const statesOf = (el) => {
  const s = [];
  if (el.disabled) s.push("disabled");
  if (el.checked) s.push("checked");
  if (el.required) s.push("required");
  if (el.readOnly) s.push("readonly");
  if (el.getAttribute && el.getAttribute("aria-expanded")) {
    s.push("expanded=" + el.getAttribute("aria-expanded"));
  }
  return s;
};
"""

_OBSERVE_BODY = r"""
(() => {
  %(helpers)s
  document.querySelectorAll("[" + REF_ATTR + "]").forEach((el) => el.removeAttribute(REF_ATTR));
  const out = [];
  let n = 0;
  const all = document.querySelectorAll("*");
  for (const el of all) {
    if (!isActionable(el)) continue;
    if (%(viewport_only)s && !isVisible(el)) continue;
    n += 1;
    const id = "e" + n;
    el.setAttribute(REF_ATTR, id);
    const role = roleOf(el);
    if (!role) continue;
    const entry = { id: id, role: role, tag: el.tagName.toLowerCase(), name: nameOf(el) };
    const val = (el.value !== undefined && el.value !== null) ? String(el.value) : "";
    if (val) entry.value = val.length > 80 ? val.slice(0, 77) + "..." : val;
    const st = statesOf(el);
    if (st.length) entry.states = st;
    out.push(entry);
  }
  return out;
})()
"""

_RUN_BODY = r"""
async () => {
  %(helpers)s
  const __displays = [];
  const display = (x) => { __displays.push(x); };
  const __resolveEl = (sel) => {
    if (typeof sel !== "string") throw new Error("selector must be a string");
    const s = sel.trim();
    const bare = s.match(/^@?(e\d+)$/);
    if (bare) return document.querySelector("[" + REF_ATTR + '="' + bare[1] + '"]');
    if (s.startsWith("ref/")) {
      const refId = s.slice(4).trim();
      if (!/^e\d+$/.test(refId)) throw new Error("bad ref id: " + refId);
      return document.querySelector("[" + REF_ATTR + '="' + refId + '"]');
    }
    if (s.startsWith("text/")) {
      const needle = s.slice(5);
      const nodes = document.querySelectorAll("a,button,[role],input,textarea,select,span,div,li");
      for (const el of nodes) {
        if ((el.textContent || "").includes(needle)) return el;
      }
      return null;
    }
    if (s.startsWith("xpath/")) {
      const r = document.evaluate(s.slice(6), document, null,
        XPathResult.FIRST_ORDERED_NODE_TYPE, null);
      return r.singleNodeValue;
    }
    if (s.startsWith("css/")) return document.querySelector(s.slice(4));
    if (s.startsWith("pierce/")) return document.querySelector(s.slice(7));
    return document.querySelector(s);
  };
  const tab = {
    url: () => window.location.href,
    title: () => document.title,
    query: (sel) => { const el = __resolveEl(sel); return el ? nameOf(el) : null; },
    text: (sel) => {
      const el = sel ? __resolveEl(sel) : document.body;
      return el ? (el.innerText || el.textContent || "") : "";
    },
    click: (sel) => {
      const el = __resolveEl(sel);
      if (!el) throw new Error("click: no element matches " + sel);
      el.click(); return true;
    },
    fill: (sel, value) => {
      const el = __resolveEl(sel);
      if (!el) throw new Error("fill: no element matches " + sel);
      el.focus(); el.value = ""; el.value = String(value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    },
    type: (sel, value) => tab.fill(sel, value),
    check: (sel) => {
      const el = __resolveEl(sel);
      if (!el) throw new Error("check: no element matches " + sel);
      el.checked = true;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    },
    observe: () => {
      document.querySelectorAll("[" + REF_ATTR + "]").forEach((el) => el.removeAttribute(REF_ATTR));
      const out = []; let n = 0;
      for (const el of document.querySelectorAll("*")) {
        if (!isActionable(el)) continue;
        n += 1; const id = "e" + n; el.setAttribute(REF_ATTR, id);
        const role = roleOf(el); if (!role) continue;
        out.push({ id: id, role: role, tag: el.tagName.toLowerCase(), name: nameOf(el) });
      }
      return out;
    },
  };
  const __run = async () => { %(code)s };
  const __value = await __run();
  return { value: __value === undefined ? null : __value, displays: __displays };
}
"""


def observe_script(*, viewport_only: bool = False) -> str:
    """Return the ``page.evaluate`` expression that tags + lists actionable nodes."""
    body = _OBSERVE_BODY % {
        "helpers": _DOM_HELPERS % {"ref_attr": REF_ATTR},
        "viewport_only": "true" if viewport_only else "false",
    }
    return body


def run_wrapper(code: str) -> str:
    """Wrap model JS ``code`` in the page-context ``tab`` facade IIFE.

    The wrapped function returns ``{value, displays}``; ``code`` may ``return`` a
    value and call ``display(x)`` / use the ``tab`` DOM facade. Runs in the page
    context only — no host access.
    """
    # ``code`` is embedded as a function body; it is model-authored JS that runs
    # in the sandboxed page context (not host Python). json.dumps is NOT used
    # because the body must be live JS, not a string literal.
    return _RUN_BODY % {
        "helpers": _DOM_HELPERS % {"ref_attr": REF_ATTR},
        "code": code,
    }

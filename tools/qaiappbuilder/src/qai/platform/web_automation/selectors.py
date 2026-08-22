# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Selector-engine parsing for the interactive browser tool.

Maps the reference tool's selector vocabulary onto Playwright locator syntax so
the model uses the same prefixes it would against the reference:

* ``text/substring``  → Playwright ``text=`` engine
* ``xpath/expr``      → ``xpath=`` engine
* ``aria/Name``       → a role/name locator request (resolved by the caller via
  ``get_by_role``-style lookup; here we surface the parsed name)
* ``ref/eN`` (also bare ``eN`` / ``@eN``) → the per-tab element-ref registry
* ``pierce/sel``      → plain CSS (Playwright locators pierce shadow DOM by
  default), accepted as an alias
* anything else       → CSS (the default engine)

Unknown ``<prefix>/`` engines raise :class:`ValueError` so the tool surfaces a
clean ``ToolError`` instead of silently waiting out a timeout on a bogus
selector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

__all__ = ["ParsedSelector", "parse_selector"]

SelectorKind = Literal["css", "text", "xpath", "aria", "ref"]

#: Recognised ``<engine>/`` prefixes. ``pierce`` is folded into ``css``.
_KNOWN_ENGINES = {"text", "xpath", "aria", "ref", "pierce", "css"}

#: Bare element-ref forms: ``e5``, ``@e5``.
_BARE_REF_RE = re.compile(r"^@?(e\d+)$")


@dataclass(frozen=True, slots=True)
class ParsedSelector:
    """A selector split into its engine ``kind`` and engine-specific ``value``.

    * ``css``   — ``value`` is a CSS selector for ``page.locator(value)``.
    * ``text``  — ``value`` is the substring for ``page.get_by_text(value)``.
    * ``xpath`` — ``value`` is an XPath expression for ``page.locator("xpath=" + value)``.
    * ``aria``  — ``value`` is the accessible name for a role/name lookup.
    * ``ref``   — ``value`` is the element-ref id (``eN``) resolved via the tab
      ref registry.
    """

    kind: SelectorKind
    value: str


def parse_selector(selector: str) -> ParsedSelector:
    """Parse a model-supplied selector string into a :class:`ParsedSelector`.

    Raises :class:`ValueError` for a non-string, an empty selector, or an
    unknown ``<prefix>/`` engine.
    """
    if not isinstance(selector, str):
        raise ValueError(
            f"selector must be a string, got {type(selector).__name__}"
        )
    stripped = selector.strip()
    if not stripped:
        raise ValueError("selector must be a non-empty string")

    # Bare / @-prefixed element ref (eN) — the observe() registry shorthand.
    bare = _BARE_REF_RE.match(stripped)
    if bare is not None:
        return ParsedSelector(kind="ref", value=bare.group(1))

    prefix, sep, rest = stripped.partition("/")
    if sep == "/" and prefix in _KNOWN_ENGINES:
        engine = prefix
        payload = rest
        if engine == "pierce":
            # Playwright locators pierce shadow DOM by default → treat as CSS.
            return ParsedSelector(kind="css", value=payload)
        if engine == "css":
            return ParsedSelector(kind="css", value=payload)
        if engine == "ref":
            ref = payload.strip()
            # A ref id denotes the same observe-registry ``eN`` id space as the
            # bare form; constrain it (it is interpolated into a CSS attribute
            # selector downstream, so an unconstrained value could alter the
            # query).
            if not _BARE_REF_RE.match(f"@{ref}") and not _BARE_REF_RE.match(ref):
                raise ValueError(
                    "ref/ selector requires an element id like ref/e5 "
                    "(from a prior observe)"
                )
            return ParsedSelector(kind="ref", value=ref)
        if engine == "text":
            return ParsedSelector(kind="text", value=payload)
        if engine == "xpath":
            return ParsedSelector(kind="xpath", value=payload)
        if engine == "aria":
            return ParsedSelector(kind="aria", value=payload)

    # A ``<prefix>/`` shape whose prefix is NOT a known engine is an error:
    # this is almost always a typo'd engine (e.g. ``role/…``) and silently
    # treating it as CSS would fail obscurely much later.
    if sep == "/" and prefix and prefix.isalpha() and "/" not in prefix:
        # Heuristic: a short alpha token followed by '/' looks like an engine.
        # A real CSS selector like ``a/b`` is not valid CSS anyway, and a URL
        # is never a selector, so rejecting here is safe and helpful.
        if len(prefix) <= 12 and prefix not in _KNOWN_ENGINES:
            raise ValueError(
                f"unknown selector engine {prefix!r}/ — use one of "
                "css/, text/, xpath/, aria/, ref/, pierce/ (or a bare CSS selector)"
            )

    # Default: treat the whole string as a CSS selector.
    return ParsedSelector(kind="css", value=stripped)

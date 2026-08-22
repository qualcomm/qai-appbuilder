# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""HTML parsing helper for the scrape engines.

Prefers ``selectolax`` (fast C parser) and transparently falls back to
``lxml.html`` when selectolax is unavailable (e.g. no ARM64 wheel). Both back a
tiny uniform surface: parse to a document, run a CSS selector, read text /
attributes. Engines depend only on this surface, never on either library
directly, so the fallback is invisible to them.

If neither library is importable, :func:`parse_html` raises ``ImportError`` at
call time; the engine loader guards engine construction so a scrape engine that
cannot parse simply does not register.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["Document", "Node", "parse_html", "parser_backend"]


class Node(Protocol):
    """Minimal element surface used by engines."""

    def css(self, selector: str) -> list[Node]: ...

    def css_first(self, selector: str) -> Node | None: ...

    def text(self, *, strip: bool = True) -> str: ...

    def attr(self, name: str) -> str | None: ...


class Document(Protocol):
    """Parsed document root."""

    def css(self, selector: str) -> list[Node]: ...

    def css_first(self, selector: str) -> Node | None: ...


def parser_backend() -> str:
    """Return the active backend name: ``"selectolax"`` / ``"lxml"`` / ``""``."""
    try:
        import selectolax.parser  # noqa: F401
    except ImportError:
        pass
    else:
        return "selectolax"
    try:
        import lxml.html  # noqa: F401
    except ImportError:
        return ""
    else:
        return "lxml"


# ---- selectolax adapter --------------------------------------------------


class _SelectolaxNode:
    __slots__ = ("_node",)

    def __init__(self, node: object) -> None:
        self._node = node

    def css(self, selector: str) -> list[Node]:
        return [_SelectolaxNode(n) for n in self._node.css(selector)]

    def css_first(self, selector: str) -> Node | None:
        found = self._node.css_first(selector)
        return _SelectolaxNode(found) if found is not None else None

    def text(self, *, strip: bool = True) -> str:
        text = self._node.text(deep=True, strip=False)
        return text.strip() if strip else text

    def attr(self, name: str) -> str | None:
        return self._node.attributes.get(name)


# ---- lxml adapter --------------------------------------------------------


class _LxmlNode:
    __slots__ = ("_node",)

    def __init__(self, node: object) -> None:
        self._node = node

    def css(self, selector: str) -> list[Node]:
        return [_LxmlNode(n) for n in self._node.cssselect(selector)]

    def css_first(self, selector: str) -> Node | None:
        found = self._node.cssselect(selector)
        return _LxmlNode(found[0]) if found else None

    def text(self, *, strip: bool = True) -> str:
        text = self._node.text_content()
        return text.strip() if strip else text

    def attr(self, name: str) -> str | None:
        return self._node.get(name)


def parse_html(html: str) -> Document:
    """Parse ``html`` with the best available backend.

    Raises ``ImportError`` if neither selectolax nor lxml is importable.
    """
    backend = parser_backend()
    if backend == "selectolax":
        from selectolax.parser import HTMLParser

        return _SelectolaxNode(HTMLParser(html).root)  # type: ignore[return-value]
    if backend == "lxml":
        import lxml.html

        return _LxmlNode(lxml.html.fromstring(html))  # type: ignore[return-value]
    raise ImportError("no HTML parser available (need selectolax or lxml)")

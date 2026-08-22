# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Stable abstraction layer for the pluggable web-search provider family.

This module holds the two extension-core types every search backend speaks:

* :class:`SearchResult` — the **uniform** structured result every backend
  returns (so the ``web_search`` tool renders one shape regardless of which
  backend produced it).
* :class:`SearchProviderPort` — the **single extension point** for "add a new
  search backend". A new source (Brave / Google / Bing / …) = one new class
  implementing this Protocol; the registry, the tool handler, and the DI
  wiring stay untouched.

shared-kernel placement
-----------------------
This module lives in the shared-kernel package ``qai.platform.web_search`` and
ships to BOTH editions: the multi-engine ``web`` provider that speaks this port
is available internally AND externally. Only the intranet CEBot backend (in the
external-excluded ``qai.platform.edition.web_search``) is internal-only and is
registered at the DI seam solely when ``settings.is_internal``.

Layering: this module depends only on the stdlib. It imports nothing from any
``qai.<context>`` package, so the shared-kernel ``qai.platform`` never inverts
into a bounded context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["SearchProviderPort", "SearchResult"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResult:
    """One structured search hit, uniform across every search backend.

    * ``title`` — the human-readable title of the hit (e.g. a document title
      or a page ``<title>``).
    * ``url`` — the canonical link to the source (may be ``""`` when the
      backend returns a snippet with no addressable link).
    * ``snippet`` — a short extract / passage giving the model context about
      what the hit contains.
    * ``score`` — the backend's relevance score (``None`` when the backend
      does not surface one). **Preserved verbatim** so a downstream caller can
      rank / threshold; the CEBot family DOES carry a per-hit score that the
      chat-side ``CebotMapper`` rendering path discards — this structured path
      keeps it.
    * ``source`` — a short provenance tag (the originating backend id, e.g.
      ``"cebot"``, optionally enriched with a page number), so the model can
      tell where a hit came from when several backends are merged later.
    """

    title: str
    url: str
    snippet: str
    score: float | None
    source: str


@runtime_checkable
class SearchProviderPort(Protocol):
    """A search backend: turn a query into a list of :class:`SearchResult`.

    This is the ONLY contract a new search source must satisfy. Implementations
    are async (search backends are network calls). ``count`` is the desired
    maximum number of results; a backend may return fewer. ``**kwargs`` lets a
    specific backend accept extra knobs without widening this base signature
    (e.g. a future Brave provider's ``freshness``), keeping the port stable.
    """

    async def search(
        self, query: str, *, count: int = 5, **kwargs: object
    ) -> list[SearchResult]:
        """Return up to ``count`` results for ``query`` (may be empty).

        Raises an exception (e.g. a transport / HTTP error) on failure so the
        caller can surface a clear error rather than a silent empty list.
        """
        ...

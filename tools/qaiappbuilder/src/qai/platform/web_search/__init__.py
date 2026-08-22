# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Pluggable web-search provider framework (shared kernel, both editions).

This shared-kernel package backs the ``web_search`` chat/coding tool and ships
to BOTH editions (``qai.platform`` is only excluded under
``qai.platform.edition``, which this package is NOT part of):

* :class:`SearchResult` / :class:`SearchProviderPort` — the stable abstraction
  every backend speaks (``ports``).
* :class:`SearchProviderRegistry` — selects a backend by id, with a default
  (``registry``).
* :class:`IndependentSearchProvider` — the multi-engine ``web`` provider
  (baidu / bing / mojeek / google_browser / brave / tavily / gemini), available
  internally AND externally (``independent``).

The one internal-only backend — CEBot (intranet RAG, endpoint in the excluded
edition config) — lives in :mod:`qai.platform.edition.web_search`; it imports
the neutral types from here (downstream ``edition`` → upstream ``platform`` is
legal). The DI seam registers the ``web`` provider on both editions and CEBot
only when ``settings.is_internal``.

Layering: this package depends only on the stdlib (via ``config`` / ``ports`` /
``registry``) and its own ``independent`` sub-tree. It imports nothing from any
bounded context or from ``qai.platform.edition``, so the shared kernel never
inverts.
"""

from __future__ import annotations

from qai.platform.web_search.independent import IndependentSearchProvider
from qai.platform.web_search.ports import (
    SearchProviderPort,
    SearchResult,
)
from qai.platform.web_search.registry import (
    SearchProviderRegistry,
    UnknownSearchProviderError,
)

__all__ = [
    "IndependentSearchProvider",
    "SearchProviderPort",
    "SearchProviderRegistry",
    "SearchResult",
    "UnknownSearchProviderError",
]

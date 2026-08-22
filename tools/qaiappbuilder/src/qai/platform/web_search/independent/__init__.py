# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Independent multi-engine web-search backend (shared kernel, both editions).

The ``web`` search provider: a single :class:`SearchProviderPort` implementation
(``provider_id="web"``) that fans a query out across a configurable set of public
search engines (keyless HTML scrapers, keyed HTTP APIs, and a browser-backed
engine), merges the results, and records per-engine health used to order the
fallback chain.

Only :class:`IndependentSearchProvider` is exported. Engines, the aggregator,
the HTTP client, HTML parsing, URL utilities, health scoring, and the engine
loader are all implementation details behind this one facade.

This sub-tree lives in the shared-kernel package ``qai.platform.web_search``
and ships to BOTH editions, so the ``web`` provider is available internally AND
externally. The DI seam registers it under both editions.
"""

from __future__ import annotations

from qai.platform.web_search.independent.provider import (
    IndependentSearchProvider,
)

__all__ = ["IndependentSearchProvider"]

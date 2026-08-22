# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Engine-layer contract (internal to the ``independent`` package).

An :class:`Engine` is one concrete search source (Mojeek, Brave, Google, ...).
It is deliberately NOT the public ``SearchProviderPort`` (in the parent
``web_search.ports``): that port is the single ``web`` provider seen by the
registry, while engines are
the private multiplicity behind it, driven by the aggregator.

Engines return :class:`EngineHit` (title/url/snippet/score) — the aggregator
merges hits from several engines and the provider maps the merged list onto the
uniform :class:`SearchResult`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

__all__ = ["Engine", "EngineHit", "EngineQuery", "EngineType"]

#: What kind of transport an engine uses. Drives aggregator scheduling (browser
#: engines get a longer minimum wait) and UI grouping.
EngineType = Literal["http_keyless", "http_api", "browser"]

#: Optional recency filter passed through ``EngineQuery``. Each engine maps it
#: onto its own upstream parameter (or ignores it if unsupported).
Recency = Literal["day", "week", "month", "year"]


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineHit:
    """One result from a single engine, before cross-engine merge.

    ``rank`` is the 0-based position the engine returned this hit at (used by
    the aggregator's rank-fusion). ``score`` is the engine's own relevance
    score when it surfaces one, else ``None``.
    """

    title: str
    url: str
    snippet: str
    rank: int
    score: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineQuery:
    """Everything an engine needs to run one search.

    ``recency`` and the credential are optional; a keyless engine ignores the
    credential, an engine without recency support ignores ``recency``.
    """

    query: str
    count: int
    recency: Recency | None = None
    credential: str | None = None


@runtime_checkable
class Engine(Protocol):
    """A single search source.

    Implementations MUST:

    * expose ``engine_id`` (stable id, matches the ``[[search_engines]]`` TOML
      entry and the health-score primary key) and ``engine_type``;
    * ``search`` asynchronously, returning ranked :class:`EngineHit` (possibly
      empty) or raising an :class:`~.errors.EngineError` subclass on failure so
      the aggregator can classify the outcome for scoring.
    """

    engine_id: str
    engine_type: EngineType

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        """Return ranked hits for ``query`` (may be empty); raise on failure."""
        ...

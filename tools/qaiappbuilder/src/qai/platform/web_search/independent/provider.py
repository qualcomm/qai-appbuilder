# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""The ``web`` search provider: the single facade over the engine multiplicity.

:class:`IndependentSearchProvider` implements the parent
``SearchProviderPort`` (``provider_id="web"``). One ``search`` call:

1. resolves the enabled engines and orders them by health score
   (:mod:`scoring`), best first, ties broken by the spec ``priority_hint``;
2. runs them concurrently through the :class:`~.aggregator.Aggregator`
   (soft/hard deadlines, browser minimum-wait, RRF fusion), recording each
   engine's outcome back into the score store;
3. maps the fused :class:`~.engines.base.EngineHit` list onto the uniform
   :class:`~qai.platform.web_search.ports.SearchResult`.

Engines and their order come entirely from configuration
(``[[search_engines]]`` + ``[independent_search]``), so adding or removing an
engine never touches this class.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from qai.platform.logging import get_logger
from qai.platform.web_search.independent.aggregator import Aggregator
from qai.platform.web_search.independent.engine_loader import (
    build_engines,
    load_engine_specs,
)
from qai.platform.web_search.independent.engines.base import (
    EngineQuery,
)
from qai.platform.web_search.independent.errors import EngineTlsError
from qai.platform.web_search.independent.quota import QuotaStore, QuotaWarning
from qai.platform.web_search.independent.scoring import ScoreStore
from qai.platform.web_search.ports import SearchResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from qai.platform.web_search.independent.engine_loader import EngineSpec
    from qai.platform.web_search.independent.engines.base import (
        Engine,
        EngineHit,
    )

__all__ = ["IndependentSearchProvider", "last_quota_warning"]

_log = get_logger(__name__)

#: Per-task context variable for the most recent quota warning emitted by a
#: keyed-engine search. Consumers (e.g. the chat tool bridge) read this after
#: ``provider.search()`` returns; it is ``None`` when no threshold was crossed.
last_quota_warning: ContextVar[QuotaWarning | None] = ContextVar(
    "last_quota_warning", default=None
)

_PROVIDER_ID = "web"

_DEFAULT_SOFT_DEADLINE = 5.0
_DEFAULT_HARD_DEADLINE = 30.0
_DEFAULT_BROWSER_MIN_WAIT = 12.0


def _deadlines(independent_cfg: dict[str, object]) -> tuple[float, float, float]:
    """Resolve aggregator deadlines from ``[independent_search]`` (or defaults)."""

    def _float(key: str, default: float) -> float:
        value = independent_cfg.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value)
        return default

    return (
        _float("soft_deadline_seconds", _DEFAULT_SOFT_DEADLINE),
        _float("hard_deadline_seconds", _DEFAULT_HARD_DEADLINE),
        _float("browser_min_wait_seconds", _DEFAULT_BROWSER_MIN_WAIT),
    )


class IndependentSearchProvider:
    """Multi-engine ``web`` search backend (implements ``SearchProviderPort``).

    Construction is cheap: the engine specs are parsed once, but engines are
    (re)built lazily per search so a credential added at runtime is picked up
    without a restart. The score store is shared across calls.
    """

    __slots__ = (
        "_bg_tasks",
        "_deadlines",
        "_quota_store",
        "_score_store",
        "_secret_store",
        "_specs",
    )

    def __init__(
        self,
        *,
        engine_specs_raw: list[dict[str, object]],
        independent_cfg: dict[str, object],
        score_store: ScoreStore,
        quota_store: QuotaStore | None = None,
        secret_store: Any | None = None,
    ) -> None:
        self._specs: list[EngineSpec] = load_engine_specs(engine_specs_raw)
        self._score_store = score_store
        self._quota_store = quota_store
        self._secret_store = secret_store
        self._deadlines = _deadlines(independent_cfg)
        self._bg_tasks: set[asyncio.Task[None]] = set()

    async def search(
        self, query: str, *, count: int = 5, **kwargs: object
    ) -> list[SearchResult]:
        """Return up to ``count`` fused results across the enabled engines.

        Strategy:
        1. If any keyed (API-key) engine is available and not quota-exhausted,
           try **one** (the highest-ranked) — never multiple keyed engines at
           once.  On success → track quota, return results.
        2. If the keyed engine fails or none is available, fall back to the
           keyless engines which run in parallel through the Aggregator.

        The ``quota_warning`` attribute is set on the returned list (as a side
        effect) when a threshold notification should be surfaced.
        """
        recency = kwargs.get("recency")
        _log.info(
            "web_search.independent.search_begin",
            extra={"query": query, "count": count, "recency": recency},
        )
        engines = await self._ordered_enabled_engines()
        if not engines:
            _log.warning("web_search.independent.no_enabled_engines")
            return []

        _log.info(
            "web_search.independent.engines_selected",
            extra={"engines": [e.engine_id for e in engines]},
        )
        engine_query = EngineQuery(
            query=query,
            count=count,
            recency=recency if isinstance(recency, str) else None,  # type: ignore[arg-type]
        )

        # --- Phase 1: keyed engine (single, priority-first) ---
        keyed, keyless = self._partition_engines(engines)
        _log.info(
            "web_search.independent.partition",
            extra={
                "keyed_engines": [e.engine_id for e in keyed],
                "keyless_engines": [e.engine_id for e in keyless],
            },
        )
        keyed_result = await self._try_keyed_engine(keyed, engine_query)
        if keyed_result is not None:
            _log.info(
                "web_search.independent.keyed_phase_succeeded",
                extra={"result_count": len(keyed_result)},
            )
            return keyed_result
        _log.info("web_search.independent.keyed_phase_exhausted_or_empty")

        # --- Phase 2: keyless aggregator (parallel, fused) ---
        if not keyless:
            _log.warning("web_search.independent.no_keyless_engines_available")
            return []
        _log.info(
            "web_search.independent.keyless_phase_begin",
            extra={"engines": [e.engine_id for e in keyless]},
        )

        aggregator = Aggregator(
            keyless,
            soft_deadline=self._deadlines[0],
            hard_deadline=self._deadlines[1],
            browser_min_wait_seconds=self._deadlines[2],
            on_outcome=self._record_outcome,
        )
        try:
            merged = await aggregator.run(engine_query)
        finally:
            await _aclose_engine_clients(keyless)
        _log.info(
            "web_search.independent.search_done",
            extra={
                "result_count": len(merged.hits),
                "contributing_engines": list(merged.engine_ids),
            },
        )
        if not merged.hits and merged.tls_cause is not None:
            _log.warning("web_search.independent.all_engines_tls_failed")
            raise EngineTlsError(
                _PROVIDER_ID, "all engines failed TLS certificate verification"
            ) from merged.tls_cause
        return [_to_result(hit) for hit in merged.hits]

    def _partition_engines(
        self, engines: list[Engine]
    ) -> tuple[list[Engine], list[Engine]]:
        """Split ordered engines into keyed (API-key) vs keyless."""
        spec_map = {s.engine_id: s for s in self._specs}
        keyed: list[Engine] = []
        keyless: list[Engine] = []
        for engine in engines:
            spec = spec_map.get(engine.engine_id)
            if spec and spec.requires_credential:
                keyed.append(engine)
            else:
                keyless.append(engine)
        return keyed, keyless

    async def _try_keyed_engine(
        self, keyed: list[Engine], engine_query: EngineQuery
    ) -> list[SearchResult] | None:
        """Try the best available keyed engine (single call). Returns results or None.

        Skips engines whose quota is exhausted. On success, increments the quota
        counter and publishes a :data:`last_quota_warning` contextvar for the
        caller to surface.
        """
        last_quota_warning.set(None)
        for engine in keyed:
            # Quota gate: skip if exhausted
            if self._quota_store is not None:
                if await self._quota_store.is_exhausted(engine.engine_id):
                    _log.info(
                        "web_search.independent.keyed_engine_quota_exhausted",
                        extra={"engine_id": engine.engine_id},
                    )
                    continue

            _log.info(
                "web_search.independent.keyed_engine_trying",
                extra={"engine_id": engine.engine_id},
            )
            try:
                hits = await asyncio.wait_for(
                    engine.search(engine_query),
                    timeout=self._deadlines[1],
                )
            except Exception:  # noqa: BLE001
                _log.warning(
                    "web_search.independent.keyed_engine_failed",
                    extra={"engine_id": engine.engine_id},
                    exc_info=True,
                )
                self._record_outcome(engine.engine_id, False, "exception")
                # Try to close this engine's client before moving on
                await _aclose_engine_clients([engine])
                continue

            if not hits:
                _log.info(
                    "web_search.independent.keyed_engine_no_results",
                    extra={"engine_id": engine.engine_id},
                )
                self._record_outcome(engine.engine_id, False, "empty")
                await _aclose_engine_clients([engine])
                continue

            # Success!
            self._record_outcome(engine.engine_id, True, None)
            await _aclose_engine_clients([engine])

            # Track quota
            if self._quota_store is not None:
                try:
                    _info, warning = await self._quota_store.increment(
                        engine.engine_id
                    )
                    last_quota_warning.set(warning)
                except Exception:  # noqa: BLE001
                    _log.warning(
                        "web_search.independent.quota_increment_failed",
                        extra={"engine_id": engine.engine_id},
                        exc_info=True,
                    )

            _log.info(
                "web_search.independent.keyed_engine_success",
                extra={
                    "engine_id": engine.engine_id,
                    "result_count": len(hits),
                },
            )
            return [_to_result(hit) for hit in hits]

        # All keyed engines failed or exhausted
        return None

    async def _ordered_enabled_engines(self) -> list[Engine]:
        """Build enabled engines and order them best-health-first."""
        built = build_engines(self._specs, secret_store=self._secret_store)
        # Map built engine instances back to their spec for the priority hint.
        hint_by_id = {spec.engine_id: spec.priority_hint for spec in self._specs}

        enabled: list[tuple[tuple[int, int], Engine]] = []
        disabled_by_score: list[str] = []
        for engine in built:
            engine_id = engine.engine_id
            if not await self._score_store.is_enabled(engine_id):
                disabled_by_score.append(engine_id)
                continue
            key = await self._score_store.sort_key(
                engine_id, hint_by_id.get(engine_id, 0)
            )
            enabled.append((key, engine))

        enabled.sort(key=lambda pair: pair[0])
        _log.info(
            "web_search.independent.ordered_engines",
            extra={
                "built_count": len(built),
                "enabled": [(e.engine_id, k) for k, e in enabled],
                "disabled_by_score": disabled_by_score,
            },
        )
        return [engine for _key, engine in enabled]

    def _record_outcome(
        self, engine_id: str, success: bool, outcome_type: str | None
    ) -> None:
        """Aggregator outcome callback → fire-and-forget score update.

        Scheduled on the running loop so the aggregator is never blocked by a
        DB write; a scoring failure is logged and swallowed (it must never break
        a search that already produced results).
        """
        _log.info(
            "web_search.independent.engine_outcome",
            extra={
                "engine_id": engine_id,
                "success": success,
                "outcome_type": outcome_type,
            },
        )

        async def _run() -> None:
            try:
                await self._score_store.record_outcome(
                    engine_id, success, outcome_type
                )
            except Exception:  # noqa: BLE001 - scoring must not break search
                _log.warning(
                    "web_search.independent.score_record_failed",
                    extra={"engine_id": engine_id},
                    exc_info=True,
                )

        try:
            task = asyncio.ensure_future(_run())
        except RuntimeError:
            # No running loop (e.g. a sync test harness): drop the fire-and-forget
            # update rather than crash the search that already produced results.
            _run().close()
            return
        # Retain a strong reference until completion: an un-referenced task can be
        # garbage-collected mid-flight, silently dropping the score write.
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)


def _to_result(hit: EngineHit) -> SearchResult:
    """Map a fused :class:`EngineHit` onto the uniform :class:`SearchResult`."""
    return SearchResult(
        title=hit.title,
        url=hit.url,
        snippet=hit.snippet,
        score=hit.score,
        source=_PROVIDER_ID,
    )


async def _aclose_engine_clients(engines: list[Engine]) -> None:
    """Best-effort close of every HTTP engine's owned ``httpx`` client.

    Only engines that self-provisioned an :class:`HttpClient` (stored as
    ``_http``) are closed; the browser engine (which owns a shared, idle-managed
    lifecycle instead) exposes no ``_http`` and is left running. Each close is
    isolated so one failure never blocks the rest.
    """
    for engine in engines:
        http = getattr(engine, "_http", None)
        aclose = getattr(http, "aclose", None)
        if aclose is None:
            continue
        try:
            await aclose()
        except Exception:  # noqa: BLE001 - cleanup must never surface to the caller
            _log.warning(
                "web_search.independent.http_client_close_failed",
                extra={"engine_id": getattr(engine, "engine_id", "?")},
                exc_info=True,
            )

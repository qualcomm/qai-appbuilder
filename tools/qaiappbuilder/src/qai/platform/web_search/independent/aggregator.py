# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Aggregator: run several engines concurrently and fuse their hits.

The aggregator is the multiplicity behind the single public ``web`` provider.
It drives every configured :class:`~.engines.base.Engine` in parallel under a
soft/hard deadline pair, merges the surviving hits with Reciprocal Rank Fusion
(RRF), and hands the fused list back to the provider (which maps it onto the
uniform ``SearchResult``).

Scheduling avoids ``asyncio.gather`` (which would block on the slowest engine).
Instead it uses ``asyncio.wait`` with the soft deadline and returns as soon as
the soft-deadline semantics are satisfied — but a browser engine is granted a
separate minimum-wait window so a slow-but-only search source is never
systematically excluded (see plan §3.4 / §7.8):

* at the soft deadline, return early only when at least one non-browser engine
  has already returned OR the completed set is non-empty and no still-pending
  browser engine is inside its minimum-wait window;
* otherwise keep waiting until a result arrives or the hard deadline elapses;
* finally cancel whatever is still pending and drain a short finalization
  window so results that landed just before cancellation are not lost.

Each engine outcome (success or failure) is reported through an optional
``on_outcome`` callback so the caller can feed the health scorer; a single
engine raising never aborts the run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from qai.platform.web_search.independent.engines.base import (
    Engine,
    EngineHit,
    EngineQuery,
)
from qai.platform.web_search.independent.errors import (
    outcome_type_for,
    ssl_cause_in_chain,
)
from qai.platform.web_search.independent.url_utils import dedup_key

__all__ = ["AggregatedResults", "Aggregator", "OutcomeCallback"]

_LOGGER = logging.getLogger(__name__)

#: Default aggregator deadlines / browser minimum wait (seconds). Overridable
#: per instance; the real values come from ``[independent_search]`` TOML.
#:
#: Sized from measured engine latency, not guessed. Plain-HTTP engines land in
#: 1.5-2.7s, Exa's MCP path routinely needs ~6s, and the browser upgrade longer
#: still. The previous 5s soft deadline cut exactly where Exa lands — the engine
#: with the highest measured authority — so a fast-but-weak engine could define
#: the whole result set. 10s covers the observed spread; 20s bounds the worst
#: case for a run that has not yet gathered enough sources.
_DEFAULT_SOFT_DEADLINE = 10.0
_DEFAULT_HARD_DEADLINE = 20.0
#: Kept below the soft deadline: a browser engine that has not answered within
#: the soft window will not beat the plain-HTTP engines, and a minimum wait past
#: the checkpoint would silently push every search out to it.
_DEFAULT_BROWSER_MIN_WAIT = 9.0

#: RRF constant — the standard damping term that flattens the contribution of
#: lower ranks while still rewarding top placements across engines.
_RRF_K = 60

#: ``engine_type`` value that gets the longer minimum-wait window.
_BROWSER_ENGINE_TYPE = "browser"

#: How many engines must have produced hits before the soft deadline may cut the
#: remaining ones short. Rank fusion needs at least two independent sources to do
#: anything useful; with one, the "merge" is just that engine's own ordering, so
#: a single fast-but-poor engine would silently define the whole result set
#: (observed: Bing's six off-topic pages cancelled Exa and Firecrawl mid-flight).
#:
#: This is the FLOOR, applied once the soft deadline has elapsed.
_MIN_CONTRIBUTORS_FOR_EARLY_RETURN = 2

#: Inside the soft window a HIGHER bar applies, because the engines are fast
#: enough that stopping at two throws away sources that were about to land.
#:
#: Measured (direct, no proxy): bing answered at 0.50s and exa at 0.55s, so the
#: two-contributor bar was met at 1.05s — while baidu (1.53s) and firecrawl
#: (1.75s) had not even finished their request and were cancelled every single
#: time. Fusing 2 sources instead of 4-5 costs real ranking quality: RRF's whole
#: advantage is cross-engine agreement, and Chinese queries lost Baidu outright.
_SOFT_WINDOW_CONTRIBUTORS = 3

#: Nothing is cancelled before this: a floor on how long the slower engines get,
#: independent of how fast the first ones answered. Sized from the measured
#: spread above — 2.5s covers baidu/firecrawl's ~1.8s with headroom, so a normal
#: run fuses 4-5 sources for ~1.5s more wall clock than the old 2-source cut.
_MIN_ENGINE_WAIT_SECONDS = 2.5

#: Callback invoked once per engine task: ``(engine_id, success, outcome_type)``
#: where ``outcome_type`` is ``None`` on success and a scoring failure type
#: (from :func:`errors.outcome_type_for`) otherwise.
OutcomeCallback = Callable[[str, bool, str | None], None]


@dataclass(frozen=True, slots=True, kw_only=True)
class AggregatedResults:
    """Fused, deduplicated, RRF-ranked hits ready for the provider to map.

    ``hits`` is ordered by descending fusion score and already truncated to the
    requested ``count``. ``engine_ids`` records which engines contributed at
    least one surviving hit (useful for diagnostics / scoring context).
    """

    hits: list[EngineHit]
    engine_ids: tuple[str, ...] = field(default_factory=tuple)
    #: The first ``ssl.SSLError`` seen among failed engines this run, if any.
    #: Kept so the provider can, when the run produced no hits and every
    #: failure was TLS, re-raise a TLS error whose ``__cause__`` still carries
    #: the ``ssl.SSLCertVerificationError`` — feeding the chat classifier's
    #: existing "configure TLS and retry" path. ``None`` when no TLS failure
    #: occurred (the common case), so existing callers are unaffected.
    tls_cause: BaseException | None = None


@dataclass(slots=True)
class _Accumulated:
    """Mutable per-URL fusion state while merging engine hits."""

    hit: EngineHit
    score: float
    order: int


class Aggregator:
    """Concurrent multi-engine search with soft/hard deadlines and RRF fusion.

    ``engines`` is expected pre-sorted by health score (best first); the fusion
    order and tie-breaking preserve that arrival/priority order.
    """

    __slots__ = (
        "_browser_min_wait",
        "_engines",
        "_hard_deadline",
        "_on_outcome",
        "_soft_deadline",
        "_tls_cause",
    )

    def __init__(
        self,
        engines: list[Engine],
        *,
        soft_deadline: float = _DEFAULT_SOFT_DEADLINE,
        hard_deadline: float = _DEFAULT_HARD_DEADLINE,
        browser_min_wait_seconds: float = _DEFAULT_BROWSER_MIN_WAIT,
        on_outcome: OutcomeCallback | None = None,
    ) -> None:
        self._engines = engines
        self._soft_deadline = soft_deadline
        self._hard_deadline = hard_deadline
        self._browser_min_wait = browser_min_wait_seconds
        self._on_outcome = on_outcome
        # First ssl.SSLError seen among failed engines this run (see
        # AggregatedResults.tls_cause). Reset at the start of every ``run``.
        self._tls_cause: BaseException | None = None

    async def run(self, query: EngineQuery) -> AggregatedResults:
        """Search every engine concurrently and return the fused, ranked hits.

        Returns an empty :class:`AggregatedResults` when no engine produced
        hits; never propagates a single engine's failure.
        """
        self._tls_cause = None
        if not self._engines:
            return AggregatedResults(hits=[])

        started = time.monotonic()
        tasks: dict[asyncio.Task[list[EngineHit]], Engine] = {
            asyncio.create_task(self._run_engine(engine, query)): engine
            for engine in self._engines
        }
        pending = set(tasks)

        pending = await self._await_soft(pending, tasks, started)
        pending = await self._await_extended(pending, tasks, started)
        await self._finalize(pending, started)

        return self._fuse(tasks, query.count)

    async def _run_engine(
        self, engine: Engine, query: EngineQuery
    ) -> list[EngineHit]:
        """Run one engine, reporting its outcome; re-raise so fusion skips it.

        ``asyncio.wait`` does not surface child exceptions, so the outcome is
        recorded here and the exception is re-raised only to mark the task as
        failed (``_fuse`` reads results defensively).
        """
        try:
            hits = await engine.search(query)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # one engine must not abort the whole run
            outcome = outcome_type_for(exc)
            if outcome == "tls" and self._tls_cause is None:
                # Preserve the underlying ssl.SSLError so the provider can
                # re-raise it (via ``from``) when the whole run failed on TLS,
                # keeping ssl.SSLCertVerificationError reachable for the chat
                # classifier. Prefer the ssl exception itself over the httpx
                # wrapper so the chain from the re-raise is as short as possible.
                self._tls_cause = ssl_cause_in_chain(exc) or exc
            _LOGGER.warning(
                "engine %s failed (%s): %s", engine.engine_id, outcome, exc
            )
            self._report(engine.engine_id, success=False, outcome_type=outcome)
            raise
        else:
            # Full per-engine dump. Diagnosing "the search returned junk" needs
            # to know WHICH engine produced WHICH url — a first-title-only line
            # cannot distinguish "engine returned nothing", "engine returned
            # off-topic pages", and "engine returned good pages that the merge
            # then dropped". Logged at INFO (one block per engine per search) so
            # it is present in the normal application log without a debug flag.
            _LOGGER.info(
                "engine %s succeeded: %d hits (first: %s)",
                engine.engine_id,
                len(hits),
                hits[0].title[:60] if hits else "<none>",
            )
            if hits:
                for hit in hits:
                    _LOGGER.info(
                        "  [%s] #%d %s | %s | %s",
                        engine.engine_id,
                        hit.rank,
                        hit.title[:90],
                        hit.url[:150],
                        (hit.snippet or "")[:160].replace("\n", " "),
                    )
            else:
                # A zero-hit "success" is the silent failure mode that has bitten
                # this codebase repeatedly (undecodable compression, markup drift,
                # a refusal page that parses cleanly). Call it out explicitly so
                # it is never mistaken for "this engine legitimately found
                # nothing" when reading the log.
                _LOGGER.warning(
                    "engine %s returned ZERO hits without raising — verify the "
                    "response was really an empty result set and not an "
                    "unparsed/refused page",
                    engine.engine_id,
                )
            self._report(engine.engine_id, success=True, outcome_type=None)
            return hits

    def _report(
        self, engine_id: str, *, success: bool, outcome_type: str | None
    ) -> None:
        """Forward one engine outcome to the caller's callback, if any."""
        if self._on_outcome is None:
            return
        try:
            self._on_outcome(engine_id, success, outcome_type)
        except Exception:  # scoring must never break a search
            _LOGGER.exception("on_outcome callback raised for %s", engine_id)

    async def _await_soft(
        self,
        pending: set[asyncio.Task[list[EngineHit]]],
        tasks: dict[asyncio.Task[list[EngineHit]], Engine],
        started: float,
    ) -> set[asyncio.Task[list[EngineHit]]]:
        """Wait for the soft checkpoint, returning as soon as it is satisfied.

        The soft deadline is a CEILING, not a fixed dwell: the run returns the
        moment enough engines have produced hits, and only waits the full window
        when they have not. A plain ``asyncio.wait(timeout=soft)`` cannot do this
        — it sleeps out the whole timeout even after the condition is already
        met — so this steps through completions and re-checks after each.

        A pending BROWSER engine additionally gets its minimum-wait window, since
        it is structurally slower than the plain-HTTP engines and would otherwise
        never win a race against them.
        """
        # The floor is bounded by this instance's soft deadline: a caller that
        # configures a short window (tests, latency-sensitive callers) means it,
        # and a floor longer than the whole window would invert the two.
        floor = min(_MIN_ENGINE_WAIT_SECONDS, self._soft_deadline)
        while pending:
            now = time.monotonic() - started
            soft_left = self._soft_deadline - now
            if soft_left <= 0:
                break
            # Cap each slice so the decision is re-evaluated right after the
            # min-wait floor lapses. Without this cap a single hung engine holds
            # `asyncio.wait` until the soft deadline, and the run waits the full
            # window even though every other engine finished seconds ago.
            slice_left = soft_left
            if now < floor:
                slice_left = min(slice_left, floor - now)
            done, pending = await asyncio.wait(
                pending, timeout=slice_left, return_when=asyncio.FIRST_COMPLETED
            )
            if self._can_return_early(pending, tasks, started=started):
                return pending
            if not done and (time.monotonic() - started) >= floor:
                # A slice expired with nothing new AND the floor has passed, so
                # the remaining engines are simply slow; stop spinning on them.
                break

        if self._can_return_early(pending, tasks, started=started):
            return pending

        wait_left = self._browser_min_wait - (time.monotonic() - started)
        browser_pending = any(
            tasks[task].engine_type == _BROWSER_ENGINE_TYPE for task in pending
        )
        if pending and browser_pending and wait_left > 0:
            _, pending = await asyncio.wait(pending, timeout=wait_left)
        return pending

    async def _await_extended(
        self,
        pending: set[asyncio.Task[list[EngineHit]]],
        tasks: dict[asyncio.Task[list[EngineHit]], Engine],
        started: float,
    ) -> set[asyncio.Task[list[EngineHit]]]:
        """Keep waiting past the soft deadline until the result set is usable.

        Engaged when the soft checkpoint was reached with FEWER than
        :data:`_MIN_CONTRIBUTORS_FOR_EARLY_RETURN` engines having produced hits.
        Measured engine latencies span a wide range (plain-HTTP engines land in
        1.5-2.7s while Exa's MCP path routinely needs ~6s and Mojeek's browser
        upgrade longer still), so cutting at the soft deadline with only one
        contributor throws away the slower — and by measured quality, often
        better — sources.

        Stops as soon as the contributor threshold is met, or the hard deadline
        elapses, whichever comes first. Waiting is bounded and never exceeds the
        hard deadline.
        """
        if not pending:
            return pending

        while pending:
            if self._contributor_count(tasks) >= _MIN_CONTRIBUTORS_FOR_EARLY_RETURN:
                break
            hard_left = self._hard_deadline - (time.monotonic() - started)
            if hard_left <= 0:
                break
            done, pending = await asyncio.wait(
                pending,
                timeout=hard_left,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                # Timed out with tasks still running: the hard deadline is up.
                break
        return pending

    async def _finalize(
        self, pending: set[asyncio.Task[list[EngineHit]]], started: float
    ) -> None:
        """Cancel stragglers, then drain a short window for near-done tasks."""
        if not pending:
            return
        for task in pending:
            task.cancel()
        tail = self._hard_deadline - (time.monotonic() - started)
        await asyncio.wait(pending, timeout=max(tail, 0.0))

    def _can_return_early(
        self,
        pending: set[asyncio.Task[list[EngineHit]]],
        tasks: dict[asyncio.Task[list[EngineHit]], Engine],
        *,
        started: float,
    ) -> bool:
        """Soft-deadline early-return test.

        Returning early is only justified once we hold results we would actually
        be happy to serve. Three things this must NOT do:

        * **Stop on a completed-but-empty engine.** ``_succeeded`` means "did not
          raise", which a silent zero-hit engine also satisfies. Treating that as
          grounds to return abandoned the engines still in flight and yielded an
          empty search while good results were seconds away.
        * **Let the fastest engine speak for all of them.** Observed live: Bing
          took 11.5s and came back with six off-topic pages; that alone satisfied
          the old test, so Exa and Firecrawl — which had the authoritative
          nobelprize.org hits — were cancelled mid-flight and never contributed.
        * **Cut before the slower engines have had a fair chance.** Measured
          direct: bing 0.50s + exa 0.55s met a two-contributor bar at 1.05s, so
          baidu (1.53s) and firecrawl (1.75s) were cancelled on EVERY run and
          never reached the merge at all. Two rules fix that: a hard
          :data:`_MIN_ENGINE_WAIT_SECONDS` floor before anything is cancelled,
          and a higher contributor bar (:data:`_SOFT_WINDOW_CONTRIBUTORS`) while
          the soft window is still open.

        Past the soft deadline the bar relaxes to
        :data:`_MIN_CONTRIBUTORS_FOR_EARLY_RETURN`: by then waiting has stopped
        paying, and holding out for a third source would only add latency.
        """
        if not pending:
            return True
        elapsed = time.monotonic() - started
        # Floor: nothing is cancelled this early, however many have landed. Bound
        # by the configured window so a caller asking for a short soft deadline
        # (tests, latency-sensitive paths) is not overridden by the global floor.
        if elapsed < min(_MIN_ENGINE_WAIT_SECONDS, self._soft_deadline):
            return False
        contributors = self._contributor_count(tasks)
        # Inside the soft window aim for more sources; after it, accept fewer.
        required = (
            _SOFT_WINDOW_CONTRIBUTORS
            if elapsed < self._soft_deadline
            else _MIN_CONTRIBUTORS_FOR_EARLY_RETURN
        )
        if contributors >= required:
            # Enough independent sources to fuse; further waiting buys little.
            return not self._browser_pending(pending, tasks)
        if contributors == 0:
            return False
        # Short of the bar: keep waiting while any other plain-HTTP engine could
        # still land, so a fast-but-poor engine cannot define the result set.
        return not self._nonbrowser_pending(pending, tasks)

    @staticmethod
    def _contributor_count(
        tasks: dict[asyncio.Task[list[EngineHit]], Engine],
    ) -> int:
        """How many completed engines returned at least one hit."""
        return sum(1 for task in tasks if _result_or_none(task))

    @staticmethod
    def _browser_pending(
        pending: set[asyncio.Task[list[EngineHit]]],
        tasks: dict[asyncio.Task[list[EngineHit]], Engine],
    ) -> bool:
        return any(
            tasks[task].engine_type == _BROWSER_ENGINE_TYPE for task in pending
        )

    @staticmethod
    def _nonbrowser_pending(
        pending: set[asyncio.Task[list[EngineHit]]],
        tasks: dict[asyncio.Task[list[EngineHit]], Engine],
    ) -> bool:
        return any(
            tasks[task].engine_type != _BROWSER_ENGINE_TYPE for task in pending
        )

    @staticmethod
    def _has_nonempty_result(
        tasks: dict[asyncio.Task[list[EngineHit]], Engine],
    ) -> bool:
        """True when any completed engine returned a non-empty hit list."""
        return any(bool(_result_or_none(task)) for task in tasks)

    def _fuse(
        self,
        tasks: dict[asyncio.Task[list[EngineHit]], Engine],
        count: int,
    ) -> AggregatedResults:
        """Merge, dedup and RRF-rank every completed engine's hits.

        Same URL across engines accumulates ``1 / (k + rank)`` and keeps the
        longest snippet. Ties break on first-arrival engine order (which mirrors
        the health-sorted input order).
        """
        merged: dict[str, _Accumulated] = {}
        contributors: list[str] = []
        failed_engines: list[str] = []
        order = 0
        for engine in self._engines:
            task = _task_for(tasks, engine)
            hits = _result_or_none(task)
            if not hits:
                failed_engines.append(engine.engine_id)
                continue
            contributed = False
            for hit in hits:
                order = self._merge_hit(merged, hit, order)
                contributed = True
            if contributed:
                contributors.append(engine.engine_id)

        _LOGGER.info(
            "aggregator fuse: contributors=%s, failed/empty=%s, merged_urls=%d",
            contributors,
            failed_engines,
            len(merged),
        )

        ranked = sorted(merged.values(), key=lambda acc: (-acc.score, acc.order))
        hits = [
            _with_score(acc.hit, acc.score) for acc in ranked[: max(count, 0)]
        ]
        # Final ranking, so the log shows what the caller actually receives and
        # which candidates the RRF cut dropped. Without this the per-engine dumps
        # above cannot explain why a good hit failed to surface.
        _LOGGER.info("aggregator ranked %d/%d kept:", len(hits), len(merged))
        for hit in hits:
            _LOGGER.info(
                "  [merged] score=%.4f %s | %s",
                hit.score if hit.score is not None else 0.0,
                hit.title[:90],
                hit.url[:150],
            )
        dropped = ranked[len(hits) :]
        if dropped:
            _LOGGER.info(
                "aggregator dropped %d below the count cut: %s",
                len(dropped),
                [acc.hit.url[:80] for acc in dropped[:8]],
            )
        return AggregatedResults(
            hits=hits,
            engine_ids=tuple(contributors),
            tls_cause=self._tls_cause,
        )

    @staticmethod
    def _merge_hit(
        merged: dict[str, _Accumulated], hit: EngineHit, order: int
    ) -> int:
        """Fold one engine hit into the merged map; return the next order tick."""
        key = dedup_key(hit.url)
        contribution = 1.0 / (_RRF_K + hit.rank)
        existing = merged.get(key)
        if existing is None:
            merged[key] = _Accumulated(hit=hit, score=contribution, order=order)
            return order + 1
        existing.score += contribution
        if len(hit.snippet) > len(existing.hit.snippet):
            existing.hit = hit
        return order


def _succeeded(task: asyncio.Task[list[EngineHit]]) -> bool:
    """True when ``task`` finished without cancellation or exception."""
    return task.done() and not task.cancelled() and task.exception() is None


def _result_or_none(
    task: asyncio.Task[list[EngineHit]],
) -> list[EngineHit] | None:
    """Return a task's hits, or ``None`` if it is unfinished / failed."""
    if not _succeeded(task):
        return None
    return task.result()


def _task_for(
    tasks: dict[asyncio.Task[list[EngineHit]], Engine], engine: Engine
) -> asyncio.Task[list[EngineHit]]:
    """Reverse-lookup the task that ran ``engine``."""
    for task, candidate in tasks.items():
        if candidate is engine:
            return task
    msg = f"no task for engine {engine.engine_id}"
    raise KeyError(msg)


def _with_score(hit: EngineHit, score: float) -> EngineHit:
    """Return a copy of ``hit`` carrying its fused RRF ``score``."""
    return EngineHit(
        title=hit.title,
        url=hit.url,
        snippet=hit.snippet,
        rank=hit.rank,
        score=score,
    )

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cross-agent tool concurrency budget (parallel tool execution).

A small, dependency-free ``asyncio.Semaphore`` budget that bounds how many
tool calls run concurrently across the WHOLE chat turn — both the main agent
and any sub-agents share ONE instance (DI wires the same object into both, see
``apps/api/_chat_di.py``). This prevents the "N sub-agents x M tools-per-round
x exec subprocess" process storm: without a shared budget each layer would
independently fan out and the machine could spawn dozens of concurrent
subprocesses (parallel-tool-execution-design.md §5).

It is **platform-neutral and edition-agnostic** (sibling of
``background_tasks.py``): it knows nothing about any bounded context, HTTP, or
editions. Any feature needing a tool-concurrency budget can reuse it.

Two layers (design §5):

* a TOTAL semaphore bounding all concurrent tool calls of this turn;
* per-category BUCKET semaphores (currently ``exec`` — the heaviest, spawns
  subprocesses). A call acquires the total + its bucket; everything that is not
  a known heavy category just takes the total.

Acquisition order is always total → bucket, release is reverse, so two callers
can never deadlock on the pair.

State-Truth-First: "how many are running?" is the real semaphore state; there
is no optimistic counter. A budget of <= 0 means "unbounded" (the manager is
effectively a no-op) so tests / minimal containers can opt out.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

#: Tool names that spawn OS subprocesses (heaviest category). Kept tiny and
#: explicit — everything else just takes the total budget. ``exec`` is the only
#: subprocess-spawning tool today (design §5 / process_service uses a separate
#: lane). Extend deliberately if a new subprocess tool is added.
_EXEC_BUCKET_TOOLS = frozenset({"exec"})


class _NullCtx:
    """A no-op async context manager (unbounded budget)."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class ToolConcurrencyManager:
    """Shared total + per-category semaphore budget for one chat process.

    Constructed once in DI and injected into BOTH the main agent and the
    sub-agent handler so they draw from the SAME budget.
    """

    __slots__ = ("_total", "_exec", "_total_n", "_exec_n")

    def __init__(self, *, total: int = 8, exec_budget: int = 2) -> None:
        self._total_n = total
        self._exec_n = exec_budget
        # A non-positive budget means "unbounded" → no semaphore (no-op slot).
        self._total: asyncio.Semaphore | None = (
            asyncio.Semaphore(total) if total > 0 else None
        )
        self._exec: asyncio.Semaphore | None = (
            asyncio.Semaphore(exec_budget) if exec_budget > 0 else None
        )

    @property
    def total_budget(self) -> int:
        return self._total_n

    @property
    def exec_budget(self) -> int:
        return self._exec_n

    @asynccontextmanager
    async def slot(self, tool_name: str) -> AsyncIterator[None]:
        """Acquire the total budget (+ the tool's bucket) for one tool call.

        Order: total → bucket; release reverse (no cross-pair deadlock). A
        ``None`` semaphore (budget <= 0) is skipped, so an unbounded manager is
        a true no-op with zero overhead.
        """
        bucket = self._exec if tool_name in _EXEC_BUCKET_TOOLS else None
        if self._total is None and bucket is None:
            # Fully unbounded — avoid even the async-with churn.
            yield None
            return
        total_cm = self._total if self._total is not None else _NullCtx()
        bucket_cm = bucket if bucket is not None else _NullCtx()
        async with total_cm:
            async with bucket_cm:
                yield None

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory registry pairing a tab's pending ``question`` with its answer.

The ``question`` chat tool (a V2 enhancement; V1 has no equivalent) lets the
LLM pause its agentic loop and ask the user a clarifying question, blocking
until the user answers in the frontend.  The blocking is implemented the same
way :mod:`qai.chat.adapters.stream_abort_registry` implements cooperative
cancellation: a process-local registry holds an :class:`asyncio.Future`; the
tool handler ``await``\\s the future and a separate route-layer endpoint
resolves it when the user submits an answer.

**Keyed by** :class:`~qai.chat.domain.ids.TabId` (not an opaque question id):
a single tab can only ever have ONE question outstanding at a time, because
the agentic loop is *suspended* on the future inside the streaming generator
(it cannot emit a second ``question`` tool call until the first returns).
Keying by tab means the frontend can resolve the answer with the ``tab_id`` it
already holds — no need to thread a server-minted ``question_id`` back out
through the (locked) ``tool_call`` frame.

Why process-local (mirrors the abort-registry rationale): a pending question
is inherently tied to a *live* in-flight stream pinned to one worker process.
There is nothing to durably persist — if the process dies the stream dies with
it.

State-Truth-First (AGENTS.md §铁律5): :meth:`create` installs a future the
caller MUST eventually drain via :meth:`resolve` *or* abandon via
:meth:`cancel` (stream aborted / tab closed).  The tool handler additionally
races the future against the stream's abort handle + a hard timeout so a
never-answered question can never wedge the generator (and its SSE/WS
connection) open forever.
"""

from __future__ import annotations

import asyncio

from qai.chat.domain.ids import TabId

__all__ = ["InMemoryQuestionRegistry"]


class InMemoryQuestionRegistry:
    """Process-local ``tab_id`` -> pending-answer :class:`asyncio.Future`.

    Thread-safety: all methods run on the single asyncio event loop that drives
    both the chat stream and the answer endpoint, so no lock is needed
    (consistent with :class:`InMemoryStreamAbortRegistry`).
    """

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[str]] = {}

    def create(self, tab_id: TabId) -> asyncio.Future[str]:
        """Register a new pending question for ``tab_id`` and return its future.

        Raises :class:`ValueError` if the tab already has a pending question
        (the suspended generator guarantees at most one at a time; a second
        ``create`` signals a logic error rather than a benign race).
        """
        key = tab_id.value
        if key in self._pending and not self._pending[key].done():
            raise ValueError(
                f"tab {key!r} already has a pending question awaiting an answer"
            )
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[key] = future
        return future

    def resolve(self, tab_id: TabId, answer: str) -> bool:
        """Deliver ``answer`` to the tab's waiting question.

        Returns ``True`` iff a pending question was found and resolved,
        ``False`` if there is none / already resolved / already cancelled.
        Idempotent: a second resolve is a no-op ``False``.
        """
        future = self._pending.pop(tab_id.value, None)
        if future is None or future.done():
            return False
        future.set_result(answer)
        return True

    def cancel(self, tab_id: TabId) -> bool:
        """Abandon the tab's pending question (stream aborted / tab closed).

        Returns ``True`` iff a pending question was found and cancelled.
        Idempotent.  The waiting handler observes :class:`asyncio.CancelledError`
        and surfaces a stable "question cancelled" tool result.
        """
        future = self._pending.pop(tab_id.value, None)
        if future is None or future.done():
            return False
        future.cancel()
        return True

    def is_pending(self, tab_id: TabId) -> bool:
        """Return ``True`` iff the tab has a question awaiting an answer."""
        future = self._pending.get(tab_id.value)
        return future is not None and not future.done()

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory registry holding a tab's pending mid-turn user *injections*.

A V2 enhancement (V1 has no equivalent): while a turn is streaming the user
may type a new instruction and click the **inject** button.  Unlike the
Enter-while-streaming *message queue* (which sends a brand-new turn AFTER the
current turn fully ends), an injection is meant to be folded into the SAME
in-flight run — appended as a ``role:user`` message in the gap BETWEEN tool
rounds so the model sees it on the very next round.

The mechanism mirrors :mod:`qai.chat.adapters.question_registry` and
:mod:`qai.chat.adapters.stream_abort_registry`: a process-local, tab-keyed map
that the route-layer control WebSocket writes to (:meth:`inject`) and the
streaming run loop reads from (:meth:`drain`) at its inter-round seam.

**Keyed by** :class:`~qai.chat.domain.ids.TabId`: each chat tab owns at most
one in-flight stream, so all injections for a tab target that one run.  Keying
by tab lets the frontend address an injection with the ``tab_id`` it already
holds — no server-minted id need be threaded back out.

Why process-local (mirrors the abort / question registry rationale): a pending
injection is inherently tied to a *live* in-flight stream pinned to one worker
process.  There is nothing to durably persist — if the process dies the stream
dies with it and any un-drained injection is moot.

Ordering: injections drain FIFO (the order the user clicked), matching the
"every inter-round gap appends ALL pending injections, in order" product
decision.  :meth:`drain` atomically returns and clears the pending list so an
injection is delivered to the model exactly once.

State-Truth-First (AGENTS.md §铁律1): :meth:`drain` is the single read point
and reflects the true pending set at that instant; the run loop never caches a
stale snapshot.  :meth:`clear` is the turn-teardown cleanup so a never-drained
injection (e.g. the turn ended with no tool round) does not leak into the next
turn — the frontend's local fallback re-queues it as a fresh turn instead.
"""

from __future__ import annotations

from collections.abc import Callable

from qai.chat.domain.ids import TabId
from qai.platform.logging import get_logger

__all__ = ["InMemoryInjectionRegistry"]

_log = get_logger(__name__)


class InMemoryInjectionRegistry:
    """Process-local ``tab_id`` -> ordered list of pending injection texts.

    Image parity note: an injection's image(s) ride INSIDE its text as
    ``![](url)`` markdown (the SAME shape a normal submit uses), so the stored
    element stays a plain ``str`` — no separate media field is needed. The
    streaming run loop's inter-round seam extracts the refs from the text and
    resolves them to vision blocks (``streaming.py`` ``_inject_hook``).

    Thread-safety: all methods run on the single asyncio event loop that drives
    both the chat stream and the control WebSocket, so no lock is needed
    (consistent with :class:`InMemoryStreamAbortRegistry` /
    :class:`InMemoryQuestionRegistry`).

    Inject listeners (:meth:`on_inject`) turn the registry from a passive
    mailbox into an event source, so a long-running tool can react to a
    mid-turn injection the moment it lands rather than only at the run
    loop's next inter-round seam.  See
    :class:`qai.chat.adapters.injection_signal_bridge.InjectionSignalBridge`.
    """

    __slots__ = ("_listeners", "_pending")

    def __init__(self) -> None:
        self._pending: dict[str, list[str]] = {}
        self._listeners: list[Callable[[TabId], None]] = []

    def on_inject(
        self, callback: Callable[[TabId], None]
    ) -> Callable[[], None]:
        """Subscribe ``callback`` to every RECORDED injection; returns a disposer.

        Fired synchronously from :meth:`inject`, once per accepted
        injection, with the target tab.  A rejected (empty / whitespace)
        injection fires nothing — there is no pending text to react to.

        A raising callback is logged and skipped: one bad subscriber must
        never make the user's inject click fail (mirrors
        ``AsyncJobManager``'s delivery-sink fan-out).

        The returned disposer is idempotent; calling it after the
        registry moved on is a benign no-op.
        """
        self._listeners.append(callback)

        def _dispose() -> None:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

        return _dispose

    def inject(self, tab_id: TabId, text: str) -> bool:
        """Append ``text`` to the tab's pending-injection list.

        Returns ``True`` iff a non-empty injection was recorded; an
        empty / whitespace-only ``text`` is rejected (``False``) so a stray
        blank control frame can never inject an empty ``role:user`` turn.

        On acceptance every :meth:`on_inject` listener is notified AFTER
        the text is recorded, so a listener that immediately calls
        :meth:`has_pending` / :meth:`drain` observes the new state.
        """
        trimmed = text.strip()
        if not trimmed:
            return False
        self._pending.setdefault(tab_id.value, []).append(trimmed)
        for listener in tuple(self._listeners):
            try:
                listener(tab_id)
            except Exception:  # noqa: BLE001 — one bad listener must not
                # fail the user's inject, nor starve the other listeners.
                _log.warning(
                    "injection_registry.listener_failed",
                    tab_id=tab_id.value,
                    exc_info=True,
                )
        return True

    def drain(self, tab_id: TabId) -> list[str]:
        """Atomically return AND clear the tab's pending injections (FIFO).

        Called by the streaming run loop at its inter-round seam.  Returns an
        empty list when nothing is pending.  After this call the tab has no
        pending injections, so each injection is delivered to the model exactly
        once.
        """
        return self._pending.pop(tab_id.value, [])

    def has_pending(self, tab_id: TabId) -> bool:
        """Return ``True`` iff the tab has at least one pending injection."""
        return bool(self._pending.get(tab_id.value))

    def withdraw(self, tab_id: TabId, text: str) -> bool:
        """Remove the FIRST pending injection matching ``text`` (FIFO).

        The "inject" button shows the queued text as a still-editable /
        still-cancellable bubble until the run loop folds it in. When the user
        edits or cancels that bubble BEFORE it is drained, the frontend calls
        this (via the ``inject_cancel`` control frame) so the run loop does NOT
        also fold the withdrawn text into the wire -- otherwise the same text
        would be both re-edited in the composer AND injected by the backend
        (double submission). Matching is by trimmed text (the frontend sends
        trimmed text and :meth:`inject` stores trimmed text).

        Returns ``True`` iff a pending injection was removed; ``False`` when
        nothing matched (already drained / never pending / wrong tab) -- a
        benign no-op, mirroring the idempotency of the answer/stop paths.
        """
        trimmed = text.strip()
        pending = self._pending.get(tab_id.value)
        if not pending:
            return False
        for i, existing in enumerate(pending):
            if existing == trimmed:
                del pending[i]
                if not pending:
                    self._pending.pop(tab_id.value, None)
                return True
        return False

    def clear(self, tab_id: TabId) -> None:
        """Drop the tab's pending (un-drained) injections (turn teardown).

        Idempotent.  Used in the run loop's ``finally`` as a BACKSTOP for the
        abort/error termination paths.  It only affects texts still sitting in
        ``_pending`` — never the already-drained injections, which were minted
        into ``_TurnBodyState.injected_messages`` and are durably persisted to
        the conversation on every termination path (normal + interrupt/abort)
        BEFORE this clear runs.

        On the NORMAL-completion path this is a no-op: the run loop drains any
        still-pending texts itself and emits an ``inject_as_new_turn`` frame
        (``streaming.py`` — search ``chat.inject_as_new_turn.emitted``), which
        the frontend inserts at the HEAD of the tab's send queue so the text
        reaches the model ahead of the ordinary backlog.  An un-drained
        injection on a completed turn is therefore NOT discarded — the user
        asked for it, so it is delivered on the next turn with priority.

        Abort/error exits still discard: they are semantically "user
        cancelled", where carrying the text into a fresh turn would resurrect
        an instruction the user just stopped.
        """
        self._pending.pop(tab_id.value, None)

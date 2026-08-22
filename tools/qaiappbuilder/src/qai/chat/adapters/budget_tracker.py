# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Budget-tracker adapters for the chat bounded context.

Implements :class:`~qai.chat.application.ports.BudgetTrackerPort` — the
per-conversation TOKEN-budget observer + enforcement gate for the
``max_budget_tokens`` feature.

Two adapters:

* :class:`ConversationBackedBudgetTracker` — the real implementation. Reads /
  writes the running counter in :attr:`Conversation.meta` ``["budget"]`` via
  the :class:`~qai.chat.application.ports.ConversationRepositoryPort`, so the
  sub-agent handler and the streaming use case can enforce a shared budget
  WITHOUT either importing / touching the ``Conversation`` aggregate directly
  (they hold only the port — clean layering, AGENTS.md §3.2).
* :class:`NullBudgetTracker` — a no-op default (always ``exceeded=False``,
  ``observe`` / ``reset`` do nothing) so callers that do not wire a real
  tracker (unit stubs / the budget-disabled deployment) see byte-for-byte
  unchanged behaviour.

State-Truth-First (AGENTS.md 铁律 1)
------------------------------------
``observe`` only ever GROWS the counter, and only by the caller-supplied,
provider-measured delta. A non-positive delta (a round with no authoritative
usage — local model / no measurement) is a no-op: the counter never advances
on an estimate and never regresses.

Best-effort persistence
------------------------
A missing conversation or a repository hiccup during ``observe`` / ``check`` /
``reset`` is swallowed (logged at DEBUG) — a budget-bookkeeping failure must
never break or strand a live turn. ``check`` degrades to a disabled result
(``max_tokens=None`` ⇒ never blocks) on such a failure.
"""

from __future__ import annotations

from qai.chat.application.ports import (
    BudgetCheckResult,
    ConversationRepositoryPort,
)
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.ids import ConversationId
from qai.platform.logging import get_logger
from qai.platform.time import Clock

_log = get_logger(__name__)


def _read_budget(conv: Conversation) -> tuple[int | None, int]:
    """Return ``(max_tokens, used_tokens)`` from ``conv.meta["budget"]``.

    Missing / malformed values normalise to ``(None, 0)`` (disabled). Never
    raises — a corrupt sub-dict degrades to disabled rather than crashing a
    turn.
    """
    meta = conv.meta
    if not isinstance(meta, dict):
        return (None, 0)
    budget = meta.get("budget")
    if not isinstance(budget, dict):
        return (None, 0)
    raw_max = budget.get("max_tokens")
    try:
        max_tokens = int(raw_max) if raw_max is not None else None
    except (TypeError, ValueError):
        max_tokens = None
    # A non-positive cap is treated as "disabled" (a 0 / negative cap would
    # otherwise block every turn instantly, which is never the intent).
    if max_tokens is not None and max_tokens <= 0:
        max_tokens = None
    try:
        used_tokens = int(budget.get("used_tokens") or 0)
    except (TypeError, ValueError):
        used_tokens = 0
    if used_tokens < 0:
        used_tokens = 0
    return (max_tokens, used_tokens)


def _write_budget(
    conv: Conversation,
    *,
    max_tokens: int | None,
    used_tokens: int,
) -> None:
    """Store ``{"max_tokens", "used_tokens"}`` into ``conv.meta["budget"]``.

    Preserves every other ``meta`` key (channel source / workspace / flags).
    A ``None`` cap is stored explicitly (rather than dropping the sub-key) so a
    reset-to-disabled is durable and unambiguous.
    """
    meta = dict(conv.meta) if isinstance(conv.meta, dict) else {}
    meta["budget"] = {
        "max_tokens": max_tokens,
        "used_tokens": max(0, int(used_tokens)),
    }
    conv.meta = meta


class NullBudgetTracker:
    """No-op :class:`BudgetTrackerPort` (budget disabled).

    ``check`` always returns a disabled result (``max_tokens=None`` ⇒
    ``exceeded=False``); ``observe`` / ``reset`` do nothing. The default so a
    deployment / test that does not opt into budgeting is byte-for-byte
    unchanged.
    """

    async def observe(
        self,
        conversation_id: ConversationId,
        delta_tokens: int,
    ) -> None:
        return None

    async def check(
        self,
        conversation_id: ConversationId,
    ) -> BudgetCheckResult:
        return BudgetCheckResult(used=0, max_tokens=None, exceeded=False)

    async def reset(self, conversation_id: ConversationId) -> None:
        return None

    async def set_max_tokens(
        self,
        conversation_id: ConversationId,
        max_tokens: int | None,
    ) -> BudgetCheckResult:
        # No-op adapter: budgeting disabled ⇒ nothing persisted, disabled result.
        return BudgetCheckResult(used=0, max_tokens=None, exceeded=False)


class ConversationBackedBudgetTracker:
    """:class:`BudgetTrackerPort` backed by ``Conversation.meta["budget"]``.

    Persists the running counter through the :class:`ConversationRepositoryPort`
    on every :meth:`observe`, so a shared budget pool (parent conversation +
    all its sub-agents) is a single source of truth even across process
    restarts. ``clock`` bumps ``updated_at`` on write (parity with the other
    ``meta`` mutators).
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        clock: Clock,
    ) -> None:
        self._conversations = conversations
        self._clock = clock

    async def observe(
        self,
        conversation_id: ConversationId,
        delta_tokens: int,
    ) -> None:
        # State-Truth-First: only grow on a real, positive measured delta.
        if delta_tokens <= 0:
            return
        try:
            conv = await self._conversations.find(conversation_id)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not break a turn
            _log.debug(
                "chat.budget_observe_find_failed",
                conversation_id=conversation_id.value,
                error=str(exc),
            )
            return
        if conv is None:
            return
        max_tokens, used_tokens = _read_budget(conv)
        # Advance the counter EVEN when the cap is currently disabled: if the
        # user enables a cap later the accumulated usage is already truthful.
        new_used = used_tokens + int(delta_tokens)
        _write_budget(conv, max_tokens=max_tokens, used_tokens=new_used)
        try:
            # ``save`` writes the conversation HEADER (incl. meta). We just
            # re-fetched, so the window for clobbering a concurrent header edit
            # is minimal (parity with the pinned/favorite/workspace setters).
            await self._conversations.save(conv)
        except Exception as exc:  # noqa: BLE001 — never break a turn on a write hiccup
            _log.debug(
                "chat.budget_observe_save_failed",
                conversation_id=conversation_id.value,
                error=str(exc),
            )

    async def check(
        self,
        conversation_id: ConversationId,
    ) -> BudgetCheckResult:
        try:
            conv = await self._conversations.find(conversation_id)
        except Exception as exc:  # noqa: BLE001 — a read hiccup must not strand a turn
            _log.debug(
                "chat.budget_check_find_failed",
                conversation_id=conversation_id.value,
                error=str(exc),
            )
            return BudgetCheckResult(used=0, max_tokens=None, exceeded=False)
        if conv is None:
            return BudgetCheckResult(used=0, max_tokens=None, exceeded=False)
        max_tokens, used_tokens = _read_budget(conv)
        exceeded = max_tokens is not None and used_tokens >= max_tokens
        return BudgetCheckResult(
            used=used_tokens,
            max_tokens=max_tokens,
            exceeded=exceeded,
        )

    async def reset(self, conversation_id: ConversationId) -> None:
        try:
            conv = await self._conversations.find(conversation_id)
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "chat.budget_reset_find_failed",
                conversation_id=conversation_id.value,
                error=str(exc),
            )
            return
        if conv is None:
            return
        max_tokens, _used = _read_budget(conv)
        _write_budget(conv, max_tokens=max_tokens, used_tokens=0)
        try:
            await self._conversations.save(conv)
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "chat.budget_reset_save_failed",
                conversation_id=conversation_id.value,
                error=str(exc),
            )

    # ── Route-layer config entry point (BudgetTrackerPort.set_max_tokens) ──
    async def set_max_tokens(
        self,
        conversation_id: ConversationId,
        max_tokens: int | None,
    ) -> BudgetCheckResult:
        """Set (or clear) the conversation's cap; return the fresh snapshot.

        Used by ``PATCH /conversations/{id}/budget``. ``max_tokens=None`` (or a
        non-positive value) disables the budget. Preserves the running
        ``used_tokens`` counter (changing the cap does not zero usage — call
        :meth:`reset` for that). Raises ``ConversationNotFoundError`` (via
        ``get``) when the conversation is missing so the route returns 404.
        """
        conv = await self._conversations.get(conversation_id)
        _max, used = _read_budget(conv)
        normalised = (
            int(max_tokens) if max_tokens is not None and int(max_tokens) > 0 else None
        )
        _write_budget(conv, max_tokens=normalised, used_tokens=used)
        conv.updated_at = self._clock.now()
        await self._conversations.save(conv)
        _log.info(
            "chat.budget_max_tokens_set",
            conversation_id=conversation_id.value,
            max_tokens=normalised,
        )
        exceeded = normalised is not None and used >= normalised
        return BudgetCheckResult(
            used=used, max_tokens=normalised, exceeded=exceeded
        )


__all__ = [
    "ConversationBackedBudgetTracker",
    "NullBudgetTracker",
]

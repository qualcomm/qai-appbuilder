# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-conversation token-budget value objects for the chat context.

A conversation may carry an optional **token budget cap** so a long agentic
turn (many follow-up rounds + sub-agents) cannot run away and burn an
unbounded number of tokens.  The cap is renamed from the CC SDK's
``max_budget_usd`` to a **token** count (``max_budget_tokens``) because this
project routes many providers with no shared USD pricing table, but it DOES
have accurate provider-measured cloud token counts (``_extract_usage`` in
``infrastructure/llm_stream.py``) — so tokens are the honest, cross-provider
unit to enforce against.

State-Truth-First (AGENTS.md 铁律 1)
------------------------------------
The budget is enforced *only* from provider-authoritative usage — never an
estimate.  A round that produced no measured prompt size (a local / on-device
model, or a Gemini round that reported ``0/0``) contributes NOTHING to the
counter and is never blocked, because there is no truth to enforce against
(see :class:`~qai.chat.application.ports.BudgetTrackerPort` semantics).

Persistence
-----------
There is **no new entity field / DB column**: the budget lives in the already
persisted :attr:`Conversation.meta` dict under the ``"budget"`` sub-key
(AGENTS.md §3.1 — reuse the ``meta_json`` carrier, no migration)::

    meta["budget"] = {"max_tokens": int | None, "used_tokens": int}

``max_tokens=None`` / absent ⇒ the feature is **disabled** for that
conversation (backward-compatible default).  ``used_tokens`` is a running,
monotonically-growing counter (0 on a fresh conversation).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BudgetCheckResult:
    """Immutable snapshot of a conversation's budget check.

    Returned by :meth:`~qai.chat.application.ports.BudgetTrackerPort.check`.

    * ``used`` — tokens already consumed by this conversation (>= 0).
    * ``max_tokens`` — the configured cap, or ``None`` when the budget is
      disabled for the conversation (the common default).
    * ``exceeded`` — ``True`` iff a cap is set AND ``used >= max_tokens``.
      When ``max_tokens`` is ``None`` this is ALWAYS ``False`` (a disabled
      budget never blocks a turn).

    ``remaining`` is derived (never negative): ``max(0, max_tokens - used)``
    when a cap is set, else ``None`` (unbounded).
    """

    used: int
    max_tokens: int | None
    exceeded: bool

    @property
    def enabled(self) -> bool:
        """Return ``True`` iff a positive cap is configured."""
        return self.max_tokens is not None

    @property
    def remaining(self) -> int | None:
        """Return remaining tokens before the cap (``None`` = unbounded)."""
        if self.max_tokens is None:
            return None
        rem = self.max_tokens - self.used
        return rem if rem > 0 else 0


__all__ = ["BudgetCheckResult"]

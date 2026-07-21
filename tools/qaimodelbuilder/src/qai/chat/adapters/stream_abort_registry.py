# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory :class:`StreamAbortRegistryPort` adapter (PR-042).

Promoted out of ``apps/api/_chat_di.py`` so the regex
``class _Fake\\w+`` no longer matches the chat DI module.

In-memory state is fine for the registry: in-flight stream handles are
inherently process-local (they wrap an :class:`asyncio.Event`), so
durability is not required. Distinct conversations partition by
:class:`TabId`; a second ``register`` for the same tab raises
:class:`ConversationLockedError` per the port contract.

PR-043 may extend this with sqlite-backed observability if a future use
case wants to inspect across processes; until then the in-memory map is
the production implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from qai.chat.application.ports import ActiveStreamSnapshot, StreamAbortHandle
from qai.chat.domain.errors import ConversationLockedError
from qai.chat.domain.ids import TabId


__all__ = ["InMemoryStreamAbortRegistry"]


@dataclass(slots=True)
class _AbortRecord:
    handle: StreamAbortHandle
    started_at: datetime
    reason: str | None = None


class InMemoryStreamAbortRegistry:
    """Process-local :class:`StreamAbortRegistryPort` implementation."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: dict[str, _AbortRecord] = {}

    def register(
        self, *, tab_id: TabId, handle: StreamAbortHandle
    ) -> None:
        if tab_id.value in self._records:
            raise ConversationLockedError(
                conversation_id="<unknown>",
                held_by_tab_id=tab_id.value,
                message=(
                    f"abort handle already registered for tab "
                    f"{tab_id.value!r}"
                ),
            )
        self._records[tab_id.value] = _AbortRecord(
            handle=handle,
            started_at=datetime.now(UTC),
        )

    def unregister(self, tab_id: TabId) -> None:
        self._records.pop(tab_id.value, None)

    def abort(
        self, tab_id: TabId, *, reason: str = "user_requested"
    ) -> bool:
        record = self._records.get(tab_id.value)
        if record is None:
            return False
        record.reason = reason
        record.handle.signal(reason=reason)
        return True

    def is_streaming(self, tab_id: TabId) -> bool:
        return tab_id.value in self._records

    def cancel_tool(self, tab_id: TabId, call_id: str) -> bool:
        """Record a per-tool cancel request on the tab's handle.

        Additive helper (AGENTS.md §3.1: append-only). Independent of
        :meth:`abort` — it does NOT signal the handle (the turn keeps
        running); it only marks ONE tool call (``call_id``) for cancellation so
        the dispatcher stops just that tool and synthesizes a ``[cancelled]``
        result while the round keeps draining the others. Returns ``False``
        when no handle is registered, or when the handle predates the
        capability.
        """
        record = self._records.get(tab_id.value)
        if record is None:
            return False
        request = getattr(record.handle, "request_cancel_tool", None)
        if request is None:
            return False
        request(call_id)
        return True

    def request_retry_now(self, tab_id: TabId) -> bool:
        """Cut short a tab's network-retry backoff so it re-opens now.

        Additive helper (AGENTS.md §3.1: append-only). Independent of
        :meth:`abort` — it does not tear the turn down, it just signals the
        handle's retry-now flag so the in-flight ``_abortable_sleep`` returns
        early and the retry loop re-opens the LLM stream at once (the "立即重试"
        button). Returns ``False`` when no handle is registered, or when the
        handle predates the retry-now capability.
        """
        record = self._records.get(tab_id.value)
        if record is None:
            return False
        request = getattr(record.handle, "request_retry_now", None)
        if request is None:
            return False
        request()
        return True

    def is_aborted(self, tab_id: TabId) -> bool:
        """Return ``True`` iff a handle is registered AND has been signalled.

        Additive helper (AGENTS.md §3.1: append-only) used by the blocking
        ``question`` tool to detect a cooperative-cancellation request while it
        waits for the user's answer — the stream handle stays *registered* for
        the whole turn, so :meth:`is_streaming` alone cannot distinguish
        "still streaming" from "aborted but not yet unwound".  Returns
        ``False`` when no handle is registered (nothing to abort).
        """
        record = self._records.get(tab_id.value)
        return record is not None and record.handle.is_set()

    def list_active(self) -> tuple[ActiveStreamSnapshot, ...]:
        """Return process-local active stream handles in start order."""
        items = [
            ActiveStreamSnapshot(
                tab_id=TabId.of(tab_id),
                started_at=record.started_at,
                aborted=record.handle.is_set(),
                reason=record.reason,
            )
            for tab_id, record in self._records.items()
        ]
        return tuple(sorted(items, key=lambda item: item.started_at))

# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Startup recovery of the coordinator's pending-SYSTEM_NOTICE state.

Why this exists
===============

:class:`~qai.chat.adapters.agent_session_coordinator.AgentSessionCoordinator`
holds "does tab X still owe the user an integration of a background
result?" in a plain in-process dict.  That dict dies with the process,
but the thing it tracks does NOT: the ``SYSTEM_NOTICE`` row a settling
sub-agent produced was committed to ``chat_message`` by
:class:`~qai.chat.adapters.background_job_dispatcher.BackgroundJobDispatcher`
BEFORE the coordinator ever ran a follow-up turn.

So a restart in that window strands the notice permanently: the row is
on the conversation, but nothing remembers that a follow-up still owes
the user a summary of it.  The user asked a sub-agent for work, the work
finished, and the answer never arrives — no error, no retry, nothing.

This rebuilder closes that hole by DERIVING the pending set from the
durable transcript at startup, so recovery needs no extra bookkeeping
table that could itself go stale.

The derivation
==============

``chat_message.position`` is a 0-based, per-conversation, monotonically
increasing column (``UNIQUE (conversation_id, position)``; allocated as
``MAX(position) + 1`` by ``append_message_atomic``).  It advances across
ALL roles, so it totally orders the transcript — which makes "did an
integration happen after this notice?" answerable with two queries:

1. **The watermark.**  The highest ``position`` of an assistant message
   carrying ``meta.headless_followup`` — i.e. the last turn that existed
   purely to integrate background notices (written by §N.41 in
   ``build_assistant_meta``).  ``-1`` when the conversation has never had
   one.
2. **The un-integrated notices.**  Every ``role='system_notice'`` row
   ABOVE that watermark.  A notice BELOW it was already on the wire when
   that follow-up ran (the wire is rebuilt from the full transcript at
   turn open), so it is provably integrated.

``meta.dedup_key`` of each surviving notice is the exact key the
coordinator's pending map is keyed by, so recovery is a replay of
:meth:`AgentSessionCoordinator.notify` — never a write to its private
state (see "Recovery path" below).

Stale keys need no explicit sweep: the pending map starts EMPTY in a
fresh process, and this rebuilder only ever adds keys it just read out
of the DB.  A key whose row no longer exists is therefore unreachable by
construction, which is strictly stronger than reconciling after the fact.

``tab_id`` derivation
=====================

The pending map is keyed by :class:`TabId`, and a tab id is NOT derivable
from a conversation id: it is an independent identifier minted per opened
tab (``TabId(value=container.ids.new_id())`` in the REST layer).  Guessing
one would be worse than doing nothing — pending work would be filed under
an id no tab will ever consume, and the recovery would report success
while silently doing nothing.

So the mapping is read from the authoritative place that already persists
it: ``chat_conversation_tab`` (a durable table with a ``conversation_id``
FK), via :meth:`TabSessionStorePort.list_active`.  Tab rows survive a
restart — the startup ``streaming -> idle`` reset in the API lifespan
exists precisely because they do — so an open tab is recoverable, and a
conversation may legitimately map to SEVERAL tabs (multiple windows on
one conversation), in which case every one of them is notified.

A conversation with un-integrated notices but NO active tab is COUNTED
AS SKIPPED, not recovered: filing pending work under a closed tab is a
write nobody reads.  That case is already covered without us — when the
user next opens the conversation, the wire rebuild folds the notices into
the history, so the next turn sees them.

Recovery path — ``notify`` rather than ``_states``
=================================================

Recovery goes through the public :meth:`AgentSessionCoordinator.notify`,
which is idempotent per ``dedup_key``.  Writing ``_states`` directly
would skip the two decisions ``notify`` owns: the ``is_tab_streaming``
check, and starting the headless drain task.  Those are exactly the
behaviours we want at startup, and duplicating them here would fork the
"how does pending work get drained" contract across two modules.

Kicking the drain immediately is CORRECT here, not a side effect to be
suppressed: at startup no tab can be streaming (the process just came
up), which is the same "tab was never opened in this process" condition
the design requires before auto-triggering — so the notice is integrated
without waiting for the user to type something. The coordinator's own
ceilings (``_MAX_ITER`` and the drain sliding window) bound the work if
many conversations recover at once.

Runs ONCE per process, from the API lifespan after migrations and before
requests are accepted.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from qai.chat.domain.ids import ConversationId
from qai.chat.domain.message import SYSTEM_NOTICE_META_DEDUP_KEY
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.adapters.agent_session_coordinator import (
        AgentSessionCoordinator,
    )
    from qai.chat.application.ports import TabSessionStorePort
    from qai.chat.domain.ids import TabId
    from qai.platform.persistence import Database


__all__ = ["PendingStateRebuilder"]

_log = get_logger(__name__)

#: Highest ``position`` per conversation of an assistant message marked
#: ``meta.headless_followup`` — the "everything below is integrated"
#: watermark.
#:
#: The predicate is ``= 1``, NOT ``= true``: ``json_extract`` decodes a
#: JSON boolean to SQLite's integer ``1`` (verified against a migrated
#: database — ``typeof(...) = 'integer'``), and ``= 1`` is the口径 the
#: conversation repository already uses for its own boolean meta flags
#: (``json_extract(c.meta_json, '$.favorite') = 1``).
_SQL_WATERMARKS = (
    "SELECT conversation_id, MAX(position) FROM chat_message "
    "WHERE role = 'assistant' "
    "AND json_extract(meta_json, '$.headless_followup') = 1 "
    "GROUP BY conversation_id"
)

#: Every persisted notice row, transcript-ordered.  Served by the partial
#: index ``ix_chat_message_role_system_notice`` on
#: ``(conversation_id, position) WHERE role = 'system_notice'`` — the
#: ``WHERE`` + ``ORDER BY`` here are shaped to match it exactly.
_SQL_NOTICES = (
    "SELECT conversation_id, position, meta_json FROM chat_message "
    "WHERE role = 'system_notice' "
    "ORDER BY conversation_id, position"
)


class PendingStateRebuilder:
    """Re-derive un-integrated ``SYSTEM_NOTICE`` pending work from the DB."""

    __slots__ = ("_coordinator", "_db", "_tab_store")

    def __init__(
        self,
        *,
        tab_store: "TabSessionStorePort",
        coordinator: "AgentSessionCoordinator",
        db: "Database",
    ) -> None:
        """
        Args:
            tab_store: Source of the durable ``conversation -> tab(s)``
                mapping.  Only :meth:`list_active` is used; closed tabs
                are deliberately excluded (nothing would consume pending
                work filed under them).
            coordinator: Receives the recovered keys through its public
                :meth:`notify`.
            db: Read-only here — the derivation is two SELECTs, and
                recovery mutates in-memory coordinator state only.
        """
        self._tab_store = tab_store
        self._coordinator = coordinator
        self._db = db

    async def rebuild_from_db(self) -> int:
        """Push un-integrated notice keys back into the coordinator.

        Returns the number of ``(tab, dedup_key)`` pairs this call newly
        filed.  A key the coordinator already held is NOT counted (a
        second run reports 0, so the number never overstates what
        recovery achieved), and a conversation whose notices have no
        active tab contributes 0 and is reported via
        ``skipped_no_active_tab``.

        Raises whatever the DB / tab store raises; the caller (the API
        lifespan) treats recovery as an optimisation and must never let a
        failure here abort startup.
        """
        tabs_by_conv = await self._active_tabs_by_conversation()
        async with self._db.connection() as conn:
            cur = await conn.execute(_SQL_WATERMARKS)
            watermarks = {
                str(row[0]): int(row[1])
                for row in await cur.fetchall()
                if row[1] is not None
            }
            await cur.close()
            cur = await conn.execute(_SQL_NOTICES)
            notice_rows = await cur.fetchall()
            await cur.close()

        restored = 0
        scanned = 0
        skipped_convs: set[str] = set()
        for row in notice_rows:
            conv_id_str = str(row[0])
            position = int(row[1])
            # Watermark default -1: a conversation that never ran a
            # headless follow-up has integrated nothing, so position 0
            # already counts as pending.
            if position <= watermarks.get(conv_id_str, -1):
                continue
            dedup_key = _dedup_key_of(row[2])
            if dedup_key is None:
                # A notice with no dedup_key cannot be acked, so pushing
                # it would create a pending entry no turn can ever clear.
                _log.warning(
                    "pending_rebuild.notice_without_dedup_key",
                    conv_id=conv_id_str,
                    position=position,
                )
                continue
            scanned += 1
            tab_ids = tabs_by_conv.get(conv_id_str)
            if not tab_ids:
                skipped_convs.add(conv_id_str)
                continue
            conversation_id = ConversationId.of(conv_id_str)
            for tab_id in tab_ids:
                # ``notify`` is idempotent per (tab, key), so a
                # conversation open in two tabs files once per tab and a
                # repeat run folds.  Because a fold is invisible from the
                # return value, compare the pending set across the call
                # and count only a genuine addition — otherwise a second
                # rebuild would report the same keys as "restored" again.
                before = self._coordinator.snapshot(tab_id).pending_keys
                await self._coordinator.notify(
                    tab_id=tab_id,
                    conversation_id=conversation_id,
                    dedup_key=dedup_key,
                )
                after = self._coordinator.snapshot(tab_id).pending_keys
                if len(after) > len(before):
                    restored += 1
        _log.info(
            "pending_rebuild.scan_completed",
            notice_rows=len(notice_rows),
            un_integrated=scanned,
            restored=restored,
            watermarked_convs=len(watermarks),
            skipped_no_active_tab=len(skipped_convs),
        )
        return restored

    async def _active_tabs_by_conversation(self) -> dict[str, list["TabId"]]:
        """Group every non-closed tab by its conversation id."""
        by_conv: dict[str, list[TabId]] = {}
        for tab in await self._tab_store.list_active():
            by_conv.setdefault(tab.conversation_id.value, []).append(tab.id)
        return by_conv


def _dedup_key_of(meta_json: Any) -> str | None:
    """Extract ``meta.dedup_key`` from a raw ``meta_json`` column value.

    Returns ``None`` for NULL / unparseable / non-object / missing-key
    rows: a notice we cannot key is one we must not queue.
    """
    if not meta_json:
        return None
    try:
        meta = json.loads(str(meta_json))
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    key = meta.get(SYSTEM_NOTICE_META_DEDUP_KEY)
    return key if isinstance(key, str) and key else None

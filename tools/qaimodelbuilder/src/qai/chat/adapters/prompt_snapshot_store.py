# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory prompt-snapshot ring buffer (PR-403 / S7.5 lane L4).

Migrates ``backend/main.py:_prompt_snapshots`` (module-level dict +
FIFO eviction at 200 entries) into the chat bounded context as a
proper :class:`PromptSnapshotStorePort` adapter.

Storage characteristics:

* In-memory only — debugging artefacts are never durable
  (single-uvicorn-worker assumption mirrors legacy);
* FIFO eviction at :data:`DEFAULT_SNAPSHOT_CAPACITY` (200) entries —
  matches legacy ``_MAX_PROMPT_SNAPSHOTS``;
* asyncio-safe — uses ``asyncio.Lock`` so concurrent saves cannot
  corrupt the order; reads are lock-free (dict get is atomic).

The legacy implementation used insertion order from a plain ``dict``
and ``next(iter(d))`` for eviction; we keep that semantics
(Python 3.7+ dicts are ordered) and add the lock for safety.

Shared-prefix storage (O(N) optimisation)
------------------------------------------
Every agentic *round* of one *turn* sends a ``wire_messages`` list that
is a strict *prefix* of the turn's longest round (each round only
appends ``assistant{tool_calls}`` + ``role:tool`` blocks).  Storing a
full deep-copy per round is O(N²) in total messages; instead
:meth:`InMemoryPromptSnapshotStore.save_shared_prefix` keeps **one**
shared list per ``turn_ref`` and records each ``request_id`` as a
``prefix_len`` boundary.  :meth:`get` slices ``shared[:prefix_len]`` to
rebuild the exact per-round payload (identical shape to the legacy
full-copy path).  Capacity is counted by ``request_id``; a turn's
shared list is released once its last referencing id is evicted.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from qai.chat.application.ports import (
    PromptSnapshot,
    PromptSnapshotStorePort,
)


DEFAULT_SNAPSHOT_CAPACITY: int = 200
"""Maximum number of snapshots retained in the ring buffer; mirrors
``backend/main.py:_MAX_PROMPT_SNAPSHOTS``."""


DEFAULT_TOOL_CONTENT_MAX_CHARS: int = 8 * 1024
"""Per-``role:tool`` content cap for snapshot *display*.

Large tool outputs (exec stdout / file reads) bloat the captured
``messages``; the snapshot is a debug artefact, not the real prompt, so
oversized tool content is truncated with a ``…[truncated N chars]``
marker.  This does NOT affect what is actually sent to the model (the
truncation happens only on the stored copy)."""


def _truncate_tool_content(text: str, *, limit: int) -> str:
    """Cap ``text`` to ``limit`` chars, appending a truncation marker."""
    if limit <= 0 or len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}\u2026[truncated {dropped} chars]"


def _truncate_messages(
    messages: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return a shallow copy with oversized ``role:tool`` content capped.

    Only entries whose ``role`` is ``"tool"`` and whose ``content`` is a
    long string are rewritten (a fresh dict so the caller's list is never
    mutated in place); everything else is referenced as-is.
    """
    out: list[dict[str, Any]] = []
    for entry in messages:
        content = entry.get("content")
        if (
            entry.get("role") == "tool"
            and isinstance(content, str)
            and len(content) > limit
        ):
            new_entry = dict(entry)
            new_entry["content"] = _truncate_tool_content(content, limit=limit)
            out.append(new_entry)
        else:
            out.append(entry)
    return out


def _is_prefix_compatible(
    a: list[dict[str, Any]],
    b: list[dict[str, Any]],
) -> bool:
    """Return ``True`` iff ``a`` and ``b`` agree on their common prefix.

    Shared-prefix storage relies on the invariant that every round of one
    turn is a strict *prefix* of the longest round (each round only appends
    ``assistant{tool_calls}`` + ``role:tool`` blocks).  This compares the two
    lists entry-by-entry over ``min(len(a), len(b))`` and returns ``False``
    the moment any position differs — i.e. neither is a prefix of the other.
    A ``False`` result means overwriting the shared list with the longer of
    the two would let :meth:`InMemoryPromptSnapshotStore.get` slice WRONG
    content for an earlier round, so the caller must NOT merge them.

    The invariant should always hold in normal operation (a prefix break
    forces a fresh ``turn_ref`` upstream in the use case), so this is a
    defensive guard against accidental misuse / future regressions.
    """
    for left, right in zip(a, b):
        if left != right:
            return False
    return True


@dataclass(slots=True)
class _SharedTurn:
    """One turn's shared message list + the ids referencing it.

    ``messages`` is the longest ``wire_messages`` seen for this turn (a
    superset prefix of every round); ``ref_ids`` tracks which
    ``request_id`` boundaries point into it so the turn can be released
    when its last reference is evicted.
    """

    messages: list[dict[str, Any]]
    ref_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _SnapshotEntry:
    """A stored snapshot — either shared-prefix or a standalone copy."""

    # Shared-prefix mode: a turn ref + prefix boundary into its list.
    turn_ref: str | None = None
    prefix_len: int = 0
    model_id: str = ""
    tool_mode: str = ""
    timestamp: str = ""
    # Non-message wire fields (resolved tools / sampling / session_id) for
    # the debug dialog — additive (v2.7 §3.1). ``None`` when not captured.
    request_options: dict[str, Any] | None = None
    # Standalone mode (legacy full-copy ``save``): the payload dict stored
    # verbatim (callers pass the already-shaped V1 payload; we only cap
    # oversized ``role:tool`` content in any ``messages`` list).
    payload: dict[str, Any] | None = None


@dataclass(slots=True)
class InMemoryPromptSnapshotStore(PromptSnapshotStorePort):
    """Default :class:`PromptSnapshotStorePort` — in-process ring buffer."""

    capacity: int = DEFAULT_SNAPSHOT_CAPACITY
    tool_content_max_chars: int = DEFAULT_TOOL_CONTENT_MAX_CHARS
    _store: OrderedDict[str, _SnapshotEntry] = field(default_factory=OrderedDict)
    _turns: dict[str, _SharedTurn] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError(f"capacity must be > 0 (got {self.capacity!r})")

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def save(self, snapshot: PromptSnapshot) -> None:
        """Standalone full-copy save (legacy path / non-turn captures).

        Stores the payload **verbatim** (callers pass the already-shaped V1
        ``{model_id, tool_mode, messages, timestamp}`` payload — round-0
        capture / instrumentation), only capping oversized ``role:tool``
        content in any ``messages`` list for debug display.
        """
        if not snapshot.request_id:
            return
        payload = dict(snapshot.payload or {})
        raw_msgs = payload.get("messages")
        if isinstance(raw_msgs, list):
            payload["messages"] = _truncate_messages(
                [m for m in raw_msgs if isinstance(m, dict)],
                limit=self.tool_content_max_chars,
            )
        entry = _SnapshotEntry(payload=payload)
        async with self._lock:
            self._insert_locked(snapshot.request_id, entry)

    async def save_shared_prefix(
        self,
        *,
        request_id: str,
        turn_ref: str,
        shared_messages: list[dict[str, Any]],
        prefix_len: int,
        model_id: str,
        tool_mode: str,
        timestamp: str,
        request_options: dict[str, Any] | None = None,
    ) -> None:
        """Store one round as a prefix of its turn's shared list.

        See :meth:`PromptSnapshotStorePort.save_shared_prefix`.  Adds a
        **defensive guard**: if the incoming list and the already-stored
        shared list disagree on their common prefix (a broken prefix
        invariant — should never happen because the use case forces a fresh
        ``turn_ref`` after any inter-round compression), this round is stored
        as a *standalone verbatim copy* of its own messages instead of being
        merged into the shared list, so :meth:`get` can never slice another
        round's content for it.
        """
        if not request_id or not turn_ref:
            return
        async with self._lock:
            turn = self._turns.get(turn_ref)
            # Only the longest round's list needs to be retained: a later
            # (longer) round extends/replaces the stored base; earlier,
            # shorter rounds remain valid prefixes of the longer list.
            truncated = _truncate_messages(
                [m for m in shared_messages if isinstance(m, dict)],
                limit=self.tool_content_max_chars,
            )
            if turn is None:
                turn = _SharedTurn(messages=truncated)
                self._turns[turn_ref] = turn
            elif not _is_prefix_compatible(truncated, turn.messages):
                # Defensive guard (should never trigger in normal operation —
                # a prefix break forces a fresh ``turn_ref`` upstream): the new
                # list disagrees with the stored shared list on their common
                # prefix, so neither is a prefix of the other.  Merging would
                # let ``get`` slice WRONG content for some round.  Store THIS
                # request_id as a standalone verbatim copy instead (its own
                # messages, no shared slicing) so it can never serve another
                # round's content.  The shared list is left untouched.
                entry = _SnapshotEntry(
                    payload={
                        "model_id": model_id,
                        "tool_mode": tool_mode,
                        "messages": truncated[: max(0, prefix_len)]
                        if prefix_len < len(truncated)
                        else truncated,
                        "timestamp": timestamp,
                        **(
                            {"request_options": request_options}
                            if request_options
                            else {}
                        ),
                    },
                )
                self._insert_locked(request_id, entry)
                return
            elif len(truncated) > len(turn.messages):
                turn.messages = truncated
            # Clamp the boundary to the shared list length (defensive).
            bound = max(0, min(prefix_len, len(turn.messages)))
            entry = _SnapshotEntry(
                model_id=model_id,
                tool_mode=tool_mode,
                timestamp=timestamp,
                turn_ref=turn_ref,
                prefix_len=bound,
                request_options=request_options or None,
            )
            turn.ref_ids.add(request_id)
            self._insert_locked(request_id, entry)

    def _insert_locked(self, request_id: str, entry: _SnapshotEntry) -> None:
        """Insert/replace one entry + enforce FIFO capacity (lock held)."""
        # Re-insert moves to the tail (most-recent end), so a repeated save
        # with the same id behaves like a touch.
        if request_id in self._store:
            self._release_ref(request_id, self._store.pop(request_id))
        self._store[request_id] = entry
        while len(self._store) > self.capacity:
            evicted_id, evicted = self._store.popitem(last=False)
            self._release_ref(evicted_id, evicted)

    def _release_ref(self, request_id: str, entry: _SnapshotEntry) -> None:
        """Drop ``request_id``'s turn reference; free the turn if orphaned."""
        if entry.turn_ref is None:
            return
        turn = self._turns.get(entry.turn_ref)
        if turn is None:
            return
        turn.ref_ids.discard(request_id)
        if not turn.ref_ids:
            # Last reference gone — release the shared messages list.
            self._turns.pop(entry.turn_ref, None)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get(self, request_id: str) -> PromptSnapshot | None:
        if not request_id:
            return None
        entry = self._store.get(request_id)
        if entry is None:
            return None
        if entry.turn_ref is None:
            # Standalone: return the stored payload verbatim (a fresh dict so
            # callers cannot mutate the stored copy).
            return PromptSnapshot(
                request_id=request_id,
                payload=dict(entry.payload or {}),
            )
        turn = self._turns.get(entry.turn_ref)
        messages = (
            list(turn.messages[: entry.prefix_len]) if turn is not None else []
        )
        payload: dict[str, Any] = {
            "model_id": entry.model_id,
            "tool_mode": entry.tool_mode,
            "messages": messages,
            "timestamp": entry.timestamp,
        }
        if entry.request_options:
            payload["request_options"] = entry.request_options
        return PromptSnapshot(request_id=request_id, payload=payload)

    # ------------------------------------------------------------------
    # Test / introspection helpers (not on the Port)
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._store)

    def _turn_count(self) -> int:
        """Number of live shared-turn lists (introspection for tests)."""
        return len(self._turns)

    def _shared_message_total(self) -> int:
        """Total stored messages across all shared turns + standalone.

        Used by tests to prove O(N) storage: with shared prefixes the
        total is ≈ the longest round's length (one list per turn), not the
        Σ of every round's full history (O(N²)).
        """
        total = sum(len(t.messages) for t in self._turns.values())
        total += sum(
            len(e.payload.get("messages") or ())
            for e in self._store.values()
            if e.turn_ref is None and isinstance(e.payload, dict)
        )
        return total


__all__ = [
    "InMemoryPromptSnapshotStore",
    "DEFAULT_SNAPSHOT_CAPACITY",
    "DEFAULT_TOOL_CONTENT_MAX_CHARS",
]

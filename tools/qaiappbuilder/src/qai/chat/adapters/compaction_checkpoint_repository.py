# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`CompactionCheckpointStorePort` (CCD-5 + Task R).

Schema reference: ``qai-db-schema.md`` / migrations 044 / 059 / 060 / 062 /
065 / 066 / 067. The checkpoint is a **three-table aggregate** keyed by
``conversation_id``:

* ``chat_compaction_checkpoint`` (core, migration 044 + 065) — carries the
  compacted wire, the reference ledger, and the ``generation`` counter that
  is the SINGLE authority for Compare-And-Swap writes.
* ``chat_compaction_checkpoint_digest`` (fragment, migration 066) — one row
  per conversation holding the async P2 digest plus the ``core_generation``
  it was written against.
* ``chat_compaction_checkpoint_turn_prefix`` (fragment, migration 067) —
  same shape for the P3 turn-prefix summaries payload.

The fragment split (Task R) eliminates the P2 vs P3 whole-row overwrite
race that Task P documented: each background use case now writes to its
OWN row, and every writer carries the ``core_generation`` snapshot it read
so a stale writer is refused at the DB level rather than blindly stomping
the neighbouring fragment.

Each write returns a ``bool``:

* ``save_core`` succeeds iff the incoming generation is strictly greater
  than the stored one (or the row is fresh). Concurrent compactions racing
  to the same conversation therefore serialise deterministically — the
  first commit wins, the loser observes ``False`` and MUST NOT touch the
  in-memory cache.
* ``save_digest`` / ``save_turn_prefix`` succeed iff the incoming
  ``core_generation`` matches the CURRENT core row's generation. A stale
  save (its core has since been overwritten OR dropped by
  ``/compact clear``) is refused.

The legacy columns ``digest_text`` / ``digest_updated_at`` /
``turn_prefix_summaries_json`` on the core table are DEPRECATED but not
dropped (AGENTS.md §8 additive-only). This repository no longer reads or
writes them — the fragment tables are the single source of truth. The
backfill inside migrations 066 / 067 seeds fragment rows from the legacy
columns on upgrade so no data is lost.

The checkpoint follows the conversation's lifecycle: the DB-level
``ON DELETE CASCADE`` on ``conversation_id`` removes it when the parent
conversation row is deleted (``foreign_keys=ON``), and :meth:`delete` lets a
caller drop it explicitly (idempotent). Deleting the core row cascades to
both fragment tables via their own foreign keys.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.chat.application.use_cases._agentic_kernel import CompactionCheckpoint
from qai.chat.domain.ids import ConversationId
from qai.chat.domain.reference_ledger import ReferenceLedger
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = ["SqliteCompactionCheckpointRepository"]


# Core-table columns (single SELECT projection reused by :meth:`load`).
# Digest / turn-prefix live in their own fragment tables now and are joined
# in via LEFT JOIN so a missing fragment collapses to ``None``.
_CORE_COLUMNS = (
    "c.conversation_id, c.anchor_index, c.compacted_wire_json, "
    "c.estimated_tokens, c.last_eff_prompt, c.created_at, "
    "c.anchor_message_id, c.reference_ledger_json, c.generation, "
    "d.digest_text, d.digest_updated_at, d.core_generation, "
    "t.summaries_json, t.core_generation"
)


class SqliteCompactionCheckpointRepository:
    """aiosqlite implementation of :class:`CompactionCheckpointStorePort`.

    Task R three-table CAS aggregate; see module docstring.
    """

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Core write path (Task R CAS)
    # ------------------------------------------------------------------
    async def save_core(
        self,
        conversation_id: ConversationId,
        checkpoint: CompactionCheckpoint,
    ) -> bool:
        """Insert or CAS-update the core checkpoint row.

        Returns ``True`` iff the row was written (generation strictly
        advanced) — the caller is then safe to replace its in-memory
        checkpoint cache. Returns ``False`` when the incoming generation
        is <= the stored one; the caller MUST NOT swap in the loser
        checkpoint (a peer already wrote a strictly-newer state).

        The write is scoped to the CORE columns only (compacted wire,
        reference ledger, generation, bookkeeping). Digest and turn-prefix
        fragments live in their own tables and MUST be written through
        :meth:`save_digest` / :meth:`save_turn_prefix`.
        """
        cid = conversation_id.value
        compacted_wire_json = json.dumps(
            checkpoint.compacted_wire, ensure_ascii=False
        )
        estimated_tokens = (
            int(checkpoint.estimated_tokens)
            if checkpoint.estimated_tokens is not None
            else None
        )
        last_eff_prompt = (
            int(checkpoint.last_eff_prompt)
            if checkpoint.last_eff_prompt is not None
            else None
        )
        anchor_message_id = checkpoint.anchor_message_id
        ledger = checkpoint.reference_ledger
        if ledger is not None and not ledger.is_empty():
            reference_ledger_json: str | None = json.dumps(
                ledger.to_json(), ensure_ascii=False
            )
        else:
            reference_ledger_json = None
        generation = max(1, int(checkpoint.generation))
        updated_at = datetime.now(timezone.utc).isoformat()
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = await conn.execute(
                        "INSERT INTO chat_compaction_checkpoint ("
                        "conversation_id, anchor_index, compacted_wire_json, "
                        "estimated_tokens, last_eff_prompt, created_at, "
                        "anchor_message_id, reference_ledger_json, "
                        "generation, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(conversation_id) DO UPDATE SET "
                        " anchor_index=excluded.anchor_index, "
                        " compacted_wire_json=excluded.compacted_wire_json, "
                        " estimated_tokens=excluded.estimated_tokens, "
                        " last_eff_prompt=excluded.last_eff_prompt, "
                        " created_at=excluded.created_at, "
                        " anchor_message_id=excluded.anchor_message_id, "
                        " reference_ledger_json=excluded.reference_ledger_json, "
                        " generation=excluded.generation, "
                        " updated_at=excluded.updated_at "
                        "WHERE excluded.generation > "
                        "chat_compaction_checkpoint.generation",
                        (
                            cid,
                            int(checkpoint.anchor_index),
                            compacted_wire_json,
                            estimated_tokens,
                            last_eff_prompt,
                            float(checkpoint.created_at),
                            anchor_message_id,
                            reference_ledger_json,
                            generation,
                            updated_at,
                        ),
                    )
                    written = cur.rowcount > 0
                    await cur.close()
                    await conn.commit()
                    return written
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.compaction_checkpoint.save_core_failed",
                f"failed to save core checkpoint for {cid!r}: {exc}",
                operation="compaction_checkpoint.save_core",
                cause=exc,
            ) from exc


    # ------------------------------------------------------------------
    # Fragment writes (P2 digest / P3 turn-prefix) with CAS
    # ------------------------------------------------------------------
    async def save_digest(
        self,
        conversation_id: ConversationId,
        *,
        digest_text: str,
        digest_updated_at: str,
        core_generation: int,
    ) -> bool:
        """CAS write of the P2 digest fragment.

        Succeeds iff the CURRENT core row's ``generation`` equals
        ``core_generation`` — the generation the caller observed at
        ``load`` time. A mismatch (or missing core row) means either
        ``/compact clear`` dropped the checkpoint OR a fresh compaction
        advanced the core past the writer's snapshot — the fragment write
        is stale and refused.

        Returns ``True`` when the fragment row was inserted / updated,
        ``False`` when the CAS refused the write. Empty / whitespace-only
        ``digest_text`` collapses to a refused write (there is nothing to
        persist) — same door as a stale-generation refuse from the caller's
        perspective.
        """
        cid = conversation_id.value
        if not digest_text or not digest_text.strip():
            return False
        expected_gen = int(core_generation)
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = await conn.execute(
                        "SELECT generation FROM chat_compaction_checkpoint "
                        "WHERE conversation_id = ?",
                        (cid,),
                    )
                    row = await cur.fetchone()
                    await cur.close()
                    if row is None:
                        # Core row is gone (concurrent /compact clear). The
                        # FK on the fragment table would refuse the insert
                        # anyway; short-circuit here to skip the extra
                        # round-trip and log a stable refusal signal.
                        await conn.commit()
                        return False
                    current_gen = int(row[0])
                    if current_gen != expected_gen:
                        await conn.commit()
                        return False
                    cur = await conn.execute(
                        "INSERT INTO chat_compaction_checkpoint_digest ("
                        "conversation_id, digest_text, digest_updated_at, "
                        "core_generation) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(conversation_id) DO UPDATE SET "
                        " digest_text=excluded.digest_text, "
                        " digest_updated_at=excluded.digest_updated_at, "
                        " core_generation=excluded.core_generation "
                        "WHERE excluded.core_generation >= "
                        "chat_compaction_checkpoint_digest.core_generation",
                        (
                            cid,
                            digest_text,
                            digest_updated_at,
                            expected_gen,
                        ),
                    )
                    written = cur.rowcount > 0
                    await cur.close()
                    await conn.commit()
                    return written
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.compaction_checkpoint.save_digest_failed",
                f"failed to save digest fragment for {cid!r}: {exc}",
                operation="compaction_checkpoint.save_digest",
                cause=exc,
            ) from exc

    async def save_turn_prefix(
        self,
        conversation_id: ConversationId,
        *,
        summaries_json: str,
        core_generation: int,
    ) -> bool:
        """CAS write of the P3 turn-prefix fragment.

        Same semantics as :meth:`save_digest`: succeeds iff the core row's
        generation matches ``core_generation``. Empty / whitespace-only
        payloads are refused (there is nothing to persist).
        """
        cid = conversation_id.value
        if not summaries_json or not summaries_json.strip():
            return False
        expected_gen = int(core_generation)
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = await conn.execute(
                        "SELECT generation FROM chat_compaction_checkpoint "
                        "WHERE conversation_id = ?",
                        (cid,),
                    )
                    row = await cur.fetchone()
                    await cur.close()
                    if row is None:
                        await conn.commit()
                        return False
                    current_gen = int(row[0])
                    if current_gen != expected_gen:
                        await conn.commit()
                        return False
                    cur = await conn.execute(
                        "INSERT INTO chat_compaction_checkpoint_turn_prefix ("
                        "conversation_id, summaries_json, core_generation) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(conversation_id) DO UPDATE SET "
                        " summaries_json=excluded.summaries_json, "
                        " core_generation=excluded.core_generation "
                        "WHERE excluded.core_generation >= "
                        "chat_compaction_checkpoint_turn_prefix.core_generation",
                        (
                            cid,
                            summaries_json,
                            expected_gen,
                        ),
                    )
                    written = cur.rowcount > 0
                    await cur.close()
                    await conn.commit()
                    return written
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.compaction_checkpoint.save_turn_prefix_failed",
                f"failed to save turn-prefix fragment for {cid!r}: {exc}",
                operation="compaction_checkpoint.save_turn_prefix",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Back-compat convenience wrapper
    # ------------------------------------------------------------------
    async def save(
        self,
        conversation_id: ConversationId,
        checkpoint: CompactionCheckpoint,
    ) -> None:
        """Save a whole checkpoint by dispatching to the three CAS writers.

        Back-compat convenience wrapper for callers that hand in a
        complete :class:`CompactionCheckpoint` aggregate. To keep the
        legacy "idempotent single-row upsert" contract this wrapper
        ADVANCES ``generation`` past the stored value when the incoming
        instance's generation lags behind (or matches) the persisted row —
        without the bump the CAS in :meth:`save_core` would refuse a
        re-save of an unchanged checkpoint. Fragment writes are then
        dispatched against the effective generation.

        Callers that need CAS semantics (concurrent compactors) MUST use
        :meth:`save_core` directly and observe its ``bool`` return.
        """
        cid_value = conversation_id.value
        # Determine the effective generation: the greater of the incoming
        # generation and (stored + 1). This preserves idempotency for
        # non-concurrent callers while never REGRESSING the counter.
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT generation FROM chat_compaction_checkpoint "
                    "WHERE conversation_id = ?",
                    (cid_value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.compaction_checkpoint.save_failed",
                f"failed to peek generation for {cid_value!r}: {exc}",
                operation="compaction_checkpoint.save",
                cause=exc,
            ) from exc
        stored_gen = int(row[0]) if row is not None else 0
        effective_gen = max(int(checkpoint.generation), stored_gen + 1)
        if effective_gen != int(checkpoint.generation):
            import dataclasses as _dc
            checkpoint = _dc.replace(checkpoint, generation=effective_gen)
        await self.save_core(conversation_id, checkpoint)
        if checkpoint.digest_text and checkpoint.digest_text.strip():
            updated_at = (
                checkpoint.digest_updated_at
                or datetime.now(timezone.utc).isoformat()
            )
            await self.save_digest(
                conversation_id,
                digest_text=checkpoint.digest_text,
                digest_updated_at=updated_at,
                core_generation=effective_gen,
            )
        raw_prefix = checkpoint.turn_prefix_summaries_json
        if raw_prefix and raw_prefix.strip():
            await self.save_turn_prefix(
                conversation_id,
                summaries_json=raw_prefix,
                core_generation=effective_gen,
            )

    async def delete(self, conversation_id: ConversationId) -> None:
        """Remove the conversation's checkpoint; idempotent (no error if absent).

        Deletes only the core row — the ``ON DELETE CASCADE`` on both
        fragment tables' FKs wipes the digest / turn-prefix rows in the
        same transaction. ``PRAGMA foreign_keys = ON`` is set globally by
        ``Database._INIT_PRAGMAS`` so the cascade is always live.
        """
        cid = conversation_id.value
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_compaction_checkpoint "
                    "WHERE conversation_id = ?",
                    (cid,),
                )
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.compaction_checkpoint.delete_failed",
                f"failed to delete compaction checkpoint for {cid!r}: {exc}",
                operation="compaction_checkpoint.delete",
                cause=exc,
            ) from exc


    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------
    async def load(
        self,
        conversation_id: ConversationId,
    ) -> CompactionCheckpoint | None:
        """Return the conversation's checkpoint or ``None`` if none persisted.

        LEFT JOINs the two fragment tables so a missing digest / turn-prefix
        gracefully collapses to ``None`` on the returned dataclass. Fragments
        whose stored ``core_generation`` diverges from the CURRENT core
        generation are treated as STALE and discarded (view rebuilt from the
        core only) — this shields readers from ever seeing a digest that
        belongs to an older compaction than the core they are looking at.
        """
        cid = conversation_id.value
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_CORE_COLUMNS} FROM chat_compaction_checkpoint c "
                    "LEFT JOIN chat_compaction_checkpoint_digest d "
                    "  ON d.conversation_id = c.conversation_id "
                    "LEFT JOIN chat_compaction_checkpoint_turn_prefix t "
                    "  ON t.conversation_id = c.conversation_id "
                    "WHERE c.conversation_id = ?",
                    (cid,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.compaction_checkpoint.load_failed",
                f"failed to load compaction checkpoint for {cid!r}: {exc}",
                operation="compaction_checkpoint.load",
                cause=exc,
            ) from exc
        if row is None:
            return None
        return self._row_to_checkpoint(row)

    # ------------------------------------------------------------------
    # Row -> domain
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_checkpoint(row: tuple[object, ...]) -> CompactionCheckpoint:
        # Column order matches ``_CORE_COLUMNS``:
        #  0 c.conversation_id, 1 c.anchor_index, 2 c.compacted_wire_json,
        #  3 c.estimated_tokens, 4 c.last_eff_prompt, 5 c.created_at,
        #  6 c.anchor_message_id, 7 c.reference_ledger_json, 8 c.generation,
        #  9 d.digest_text, 10 d.digest_updated_at, 11 d.core_generation,
        # 12 t.summaries_json, 13 t.core_generation.
        wire_raw = str(row[2] or "[]")
        try:
            wire = json.loads(wire_raw)
            if not isinstance(wire, list):
                wire = []
        except (TypeError, ValueError):
            wire = []
        compacted_wire: list[dict[str, Any]] = [
            entry for entry in wire if isinstance(entry, dict)
        ]
        estimated_tokens: int | None = None
        if row[3] is not None:
            try:
                estimated_tokens = int(row[3])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                estimated_tokens = None
        last_eff_prompt: int | None = None
        if row[4] is not None:
            try:
                last_eff_prompt = int(row[4])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                last_eff_prompt = None
        try:
            created_at = float(row[5])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            created_at = 0.0
        anchor_message_id = (
            str(row[6]) if row[6] is not None else None
        )
        # P1.b: rehydrate the reference ledger; NULL / empty / malformed all
        # collapse to ``None`` so a pre-migration-059 row surfaces the same
        # dataclass shape as a fresh row.
        reference_ledger: ReferenceLedger | None = None
        if row[7] is not None:
            try:
                payload = json.loads(str(row[7]))
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                reference_ledger = ReferenceLedger.from_json(payload)
        # Task R: the core generation counter. Rows written before migration
        # 065 default to 1 (SQL column DEFAULT) — mapping the same value on
        # the dataclass side keeps a fresh writer's CAS comparable against
        # legacy rows.
        try:
            generation = int(row[8]) if row[8] is not None else 1
        except (TypeError, ValueError):
            generation = 1
        if generation < 1:
            generation = 1
        # P2 digest fragment (migration 066). A LEFT JOIN miss surfaces as
        # ``None`` on both slots. If the fragment's ``core_generation``
        # diverges from the CURRENT core generation the fragment is stale
        # (a fresh compaction advanced the core past this row) — DROP it
        # so the reader never sees a digest that belongs to an old
        # checkpoint.
        digest_text: str | None = None
        digest_updated_at: str | None = None
        if row[9] is not None:
            frag_core_gen: int | None
            try:
                frag_core_gen = int(row[11]) if row[11] is not None else None
            except (TypeError, ValueError):
                frag_core_gen = None
            if frag_core_gen is not None and frag_core_gen == generation:
                raw_digest = str(row[9])
                if raw_digest.strip():
                    digest_text = raw_digest
                    digest_updated_at = (
                        str(row[10]) if row[10] is not None else None
                    )
        # P3 turn-prefix fragment (migration 067). Same stale-drop rule as
        # the digest fragment above.
        turn_prefix_summaries_json: str | None = None
        if row[12] is not None:
            try:
                tp_core_gen: int | None = (
                    int(row[13]) if row[13] is not None else None
                )
            except (TypeError, ValueError):
                tp_core_gen = None
            if tp_core_gen is not None and tp_core_gen == generation:
                raw_prefix = str(row[12])
                if raw_prefix.strip():
                    try:
                        parsed = json.loads(raw_prefix)
                    except (TypeError, ValueError):
                        parsed = None
                    if isinstance(parsed, list):
                        turn_prefix_summaries_json = raw_prefix
        return CompactionCheckpoint(
            anchor_index=int(row[1]),  # type: ignore[arg-type]
            compacted_wire=compacted_wire,
            estimated_tokens=estimated_tokens,
            last_eff_prompt=last_eff_prompt,
            created_at=created_at,
            anchor_message_id=anchor_message_id,
            reference_ledger=reference_ledger,
            digest_text=digest_text,
            digest_updated_at=digest_updated_at,
            turn_prefix_summaries_json=turn_prefix_summaries_json,
            generation=generation,
        )

    # ------------------------------------------------------------------
    # Best-effort last_eff_prompt bookkeeping (does NOT advance generation)
    # ------------------------------------------------------------------
    async def update_last_eff_prompt(
        self,
        conversation_id: ConversationId,
        *,
        last_eff_prompt: int,
        core_generation: int,
    ) -> bool:
        """Advance the TPP-1 delta baseline on the CORE row without bumping ``generation``.

        The ``last_eff_prompt`` field is a per-turn diagnostic baseline used
        to grow ``conv.full_history_tokens`` by the measured delta. It is
        NOT a state advance — a race that overwrites it in the wrong order
        merely nudges the baseline for the current turn and self-heals on
        the NEXT turn (the next measurement dominates the delta anyway).
        Bypassing the CAS keeps the checkpoint's authoritative
        ``generation`` reserved for compaction advances, so a stale digest
        write can still be refused by :meth:`save_digest`.

        Guarded by ``core_generation`` to avoid writing this baseline to a
        row that has since advanced past the caller's snapshot (a peer
        compaction landed) — that check is best-effort defensive, not
        load-bearing.
        """
        cid = conversation_id.value
        expected_gen = int(core_generation)
        value = int(last_eff_prompt)
        updated_at = datetime.now(timezone.utc).isoformat()
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "UPDATE chat_compaction_checkpoint "
                    "SET last_eff_prompt = ?, updated_at = ? "
                    "WHERE conversation_id = ? AND generation = ?",
                    (value, updated_at, cid, expected_gen),
                )
                written = cur.rowcount > 0
                await cur.close()
                await conn.commit()
                return written
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.compaction_checkpoint.update_last_eff_prompt_failed",
                f"failed to update last_eff_prompt for {cid!r}: {exc}",
                operation="compaction_checkpoint.update_last_eff_prompt",
                cause=exc,
            ) from exc

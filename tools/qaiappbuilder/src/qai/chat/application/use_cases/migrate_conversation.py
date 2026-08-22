# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``MigrateConversationUseCase`` -- the P5 handoff escape hatch.

CONTEXT-COMPRESSION-NEXT v3.1 §7.2: when the compaction engine cannot keep
a single conversation converging inside the window (``escalation`` returns
``"suggest_handoff"``: three or more mid-turn compactions inside one turn
AND a P2 session digest already exists), this use case creates a **brand
new conversation** whose FIRST user message is a structured "handoff"
document seeded from the source conversation's digest + reference ledger.

Two contracts (v3.1 §7.2 / D12):

* **Source is READ-ONLY.** The source conversation is loaded but never
  mutated: no messages appended, no title changed, no ``meta`` rewritten,
  no timestamps bumped. A migrator that touched the source would defeat
  the entire escape-hatch premise (you should still be able to open the
  original tab and see its full trail). Persistence goes through
  :meth:`ConversationRepositoryPort.save` ONLY for the NEW conversation.

* **Gated on ``digest_text`` presence.** No P2 digest ⇒ no useful state
  to carry over, so the migration is refused (``ValueError``). This
  mirrors the ``should_suggest_handoff`` gate in the compaction engine
  so the two sides never diverge: the UI never surfaces the entry when
  the digest is missing, and even a hand-crafted API call is rejected.

Layering (AGENTS.md §3.2 / §3.5): imports only ``application.ports`` +
``domain`` + platform ``Clock`` / ``IdGenerator``. No adapters, no
interfaces, no cross-context. Purely an application-layer coordinator
over already-existing ports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from qai.chat.application.ports import (
    CompactionCheckpointStorePort,
    ConversationRepositoryPort,
)
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.conversation import Conversation, ConversationStatus
from qai.chat.domain.ids import ConversationId, MessageId
from qai.chat.domain.message import Message
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock, ensure_aware_utc

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.domain.reference_ledger import ReferenceLedger

_log = get_logger(__name__)

# Title of the freshly-minted destination conversation. Kept short so the
# sidebar can render it without truncation; still uniquely identifiable so
# the user can find it after a migration. The source's title is captured
# inside the handoff document for traceability.
_MIGRATED_TITLE_TEMPLATE: str = "Continued from {source_title}"

# Hard title cap enforced by :class:`Conversation` (256 chars). Keep in
# sync with ``conversation._MAX_TITLE_LENGTH`` — a longer template would
# raise :class:`InvalidConversationTitleError` at construction time.
_MAX_TITLE_LENGTH: int = 256


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrateConversationInput:
    """Inputs for :meth:`MigrateConversationUseCase.execute`."""

    source_conversation_id: ConversationId


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrateConversationResult:
    """Outcome of a successful migration.

    Both ids are returned so a caller (HTTP route / channel bridge) can
    redirect the client to the new conversation while still linking back
    to the source for auditing.
    """

    new_conversation_id: ConversationId
    source_conversation_id: ConversationId


def _resolve_title(source_title: str) -> str:
    """Compose + truncate the destination conversation title.

    The template embeds the source title so the sidebar can distinguish
    two migrations from different sources without opening either. Over-
    long titles get ``...``-suffixed at the domain cap to sidestep
    :class:`InvalidConversationTitleError`.
    """
    title = _MIGRATED_TITLE_TEMPLATE.format(source_title=source_title)
    if len(title) > _MAX_TITLE_LENGTH:
        title = title[: _MAX_TITLE_LENGTH - 3] + "..."
    return title


def _render_ledger_lines(ledger: "ReferenceLedger | None") -> tuple[str, str]:
    """Return ``(files_line, urls_line)`` for the handoff document.

    Both are bullet-list bodies (one entry per line, ``- `` prefix), or
    ``"- (none)"`` when nothing is recorded. ``ledger is None`` (pre-P1.d
    checkpoint) collapses to ``(none)`` for both — the handoff still lands,
    but with no "recently touched" hint. Execs are intentionally NOT
    surfaced: they are transient shell invocations and rarely useful
    across a fresh session.
    """
    if ledger is None:
        return ("- (none)", "- (none)")
    files_entries = list(ledger.files.keys())
    urls_entries = list(ledger.urls.keys())
    files_line = (
        "\n".join(f"- {p}" for p in files_entries) if files_entries else "- (none)"
    )
    urls_line = (
        "\n".join(f"- {u}" for u in urls_entries) if urls_entries else "- (none)"
    )
    return files_line, urls_line


def _build_handoff_document(
    *,
    source_conversation_id: str,
    digest_text: str | None,
    ledger: "ReferenceLedger | None",
    recent_transcript: str | None = None,
) -> str:
    """Render the ``<handoff>`` document that seeds the new conversation.

    Template mirrors CONTEXT-COMPRESSION-NEXT v3.1 §7.2: an opening frame
    telling the model this is a resumed session, the source id (for the
    audit trail), the digest verbatim (already structured markdown from
    :class:`RefreshDigestUseCase`), and a "recently touched" section
    listing the ledger's files + urls so the model has concrete anchors
    to resume from.

    DEGRADED MODE: ``digest_text`` may be ``None`` when the source has no
    session summary (never compacted, or the P2 summariser is unavailable).
    Migration must still work — refusing outright left the user with no way
    forward at exactly the moment the context was too big to continue. In
    that case the summary section is replaced by ``recent_transcript`` (the
    tail of the real conversation), which carries less structure but is
    strictly better than nothing, and the frame says so explicitly so the
    model does not assume it has a curated summary.
    """
    files_line, urls_line = _render_ledger_lines(ledger)
    has_digest = bool(digest_text and digest_text.strip())
    if has_digest:
        body_heading = (
            "## Goal / Constraints / Progress / Key Decisions / "
            "Critical Context / Next Steps"
        )
        body_text = (digest_text or "").strip()
        provenance = ""
    else:
        body_heading = "## Recent Conversation (verbatim tail)"
        body_text = (
            (recent_transcript or "").strip()
            or "(no transcript available)"
        )
        provenance = (
            "NOTE: no curated session summary was available for this "
            "handoff, so the section below is the RAW tail of the previous "
            "conversation rather than a structured digest. Earlier context "
            "is NOT included — ask the user to restate anything you need.\n"
            "\n"
        )
    return (
        "<handoff>\n"
        "You are continuing work from a previous session that grew too "
        "large to fit in one context window. You have full authority to "
        "resume seamlessly.\n"
        "\n"
        f"Previous session id: {source_conversation_id}\n"
        "\n"
        f"{provenance}"
        f"{body_heading}\n"
        f"{body_text}\n"
        "\n"
        "## Recently Touched\n"
        "### Files\n"
        f"{files_line}\n"
        "### URLs\n"
        f"{urls_line}\n"
        "</handoff>\n"
        "\n"
        "Continue from here."
    )


def _render_recent_transcript(
    messages: "tuple[Any, ...] | list[Any]",
    *,
    max_messages: int = 12,
    max_chars: int = 12_000,
) -> str:
    """Render the tail of a conversation as a plain ``role: text`` transcript.

    The degraded-mode substitute for a missing session summary. Walks the
    LAST ``max_messages`` entries, skips empty bodies, and stops once the
    accumulated text would exceed ``max_chars`` (counted in characters, not
    tokens — this is a bounded-size guard, not a budget calculation, and the
    caller has no model context to reason about). Tool scaffolding is left
    out: only user / assistant prose survives, which is what a resuming
    model can actually act on.
    """
    if not messages:
        return ""
    tail = list(messages)[-max_messages:]
    lines: list[str] = []
    budget = max_chars
    for msg in tail:
        role = getattr(getattr(msg, "role", None), "value", None)
        if role not in ("user", "assistant"):
            continue
        content = getattr(msg, "content", None)
        text = getattr(content, "text", None) if content is not None else None
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        entry = f"{role}: {text}"
        if len(entry) > budget:
            entry = entry[: max(0, budget)] + "…"
            if entry.strip():
                lines.append(entry)
            break
        lines.append(entry)
        budget -= len(entry)
    return "\n\n".join(lines)


class MigrateConversationUseCase:
    """Fork a NEW conversation seeded with the source's P2 digest.

    Contract (see module docstring): the source conversation is only
    READ; the ``ConversationRepositoryPort`` is only asked to
    :meth:`~ConversationRepositoryPort.save` the freshly-minted new
    conversation. The compaction checkpoint store is read to fetch the
    source's digest; nothing is written to it. Any error path — missing
    source, missing checkpoint, empty digest — raises BEFORE the new
    conversation is saved, so a rejected migration leaves the world
    byte-for-byte unchanged.
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        checkpoint_store: CompactionCheckpointStorePort,
        ids: IdGenerator,
        clock: Clock,
        compaction_engine: "Any | None" = None,
        digest_wait_seconds: float = 60.0,
    ) -> None:
        self._conversations = conversations
        self._checkpoint_store = checkpoint_store
        self._ids = ids
        self._clock = clock
        # Optional :class:`CompactionCheckpointEngine`. When wired, ``execute``
        # can WAIT for an in-flight P2 summariser instead of refusing the
        # migration: ``/compact`` returns in milliseconds but its background
        # digest task takes 20-40s (growing with history), so a user who runs
        # ``/compact`` then ``/compact migrate`` — the documented handoff
        # sequence — used to hit a guaranteed refusal. ``None`` keeps the
        # engine-less behaviour (no wait; fall straight through to the
        # degraded handoff below).
        self._compaction_engine = compaction_engine
        self._digest_wait_seconds = float(digest_wait_seconds)

    async def execute(
        self, request: MigrateConversationInput,
    ) -> MigrateConversationResult:
        # 1. Load the source. Missing ⇒ ``ConversationNotFoundError`` from
        #    the port (see :meth:`ConversationRepositoryPort.get`). We do
        #    NOT catch it: the caller (HTTP route / test) sees the exact
        #    same not-found signal any other conversation read raises.
        source = await self._conversations.get(request.source_conversation_id)

        # 2. Wait out an in-flight P2 summariser before reading the
        #    checkpoint. The digest is what makes a handoff useful, and it
        #    lands 20-40s AFTER the ``/compact`` reply the user just saw, so
        #    racing it would refuse a migration that is about to be possible.
        #    Bounded by ``digest_wait_seconds`` and never fatal: a timeout /
        #    cancellation / crashed task all fall through to the degraded
        #    handoff below rather than failing the migration.
        await self._await_inflight_digest(request.source_conversation_id)

        # 3. Load the source's compaction checkpoint. A missing checkpoint or
        #    an empty digest no longer refuses the migration — it selects the
        #    DEGRADED handoff (verbatim conversation tail instead of a curated
        #    summary). Refusing here used to strand the user at exactly the
        #    moment their context was too large to continue, which is the
        #    opposite of an escape hatch.
        checkpoint = await self._checkpoint_store.load(
            request.source_conversation_id,
        )
        digest_text = (
            checkpoint.digest_text if checkpoint is not None else None
        )
        ledger = checkpoint.reference_ledger if checkpoint is not None else None
        recent_transcript: str | None = None
        if not (digest_text and digest_text.strip()):
            # No summary available — carry the real tail of the conversation
            # so the new session still starts with concrete context.
            recent_transcript = _render_recent_transcript(
                getattr(source, "messages", ()) or (),
            )
            _log.info(
                "chat.compaction.migrate_degraded",
                conversation_id=request.source_conversation_id.value,
                had_checkpoint=checkpoint is not None,
                transcript_chars=len(recent_transcript),
            )

        # 4. Compose the handoff document. The ledger may still be
        #    ``None`` on a pre-P1.d row — that is fine, the doc just
        #    reports "(none)" for touched files/urls.
        handoff_text = _build_handoff_document(
            source_conversation_id=source.id.value,
            digest_text=digest_text,
            ledger=ledger,
            recent_transcript=recent_transcript,
        )

        # 5. Mint the destination conversation. Timestamps come from the
        #    injected :class:`Clock` (V2 rule: no direct ``datetime.now()``
        #    in application code, so ``FrozenClock`` in tests produces
        #    deterministic ids/timestamps). The seed user message is the
        #    first turn of the fresh conversation.
        now = ensure_aware_utc(self._clock.now())
        new_conv_id = ConversationId.generate(self._ids)
        title = _resolve_title(source.title)

        # ``meta.migrated_from`` records the source conversation id + title
        # so the sidebar can render a "continued from …" badge and future
        # audit / analytics can walk the migration chain without touching
        # message bodies. Kept under the shared ``meta_json`` carrier
        # (AGENTS.md §3.1 — no new column / migration).
        meta: dict = {
            "migrated_from": {
                "conversation_id": source.id.value,
                "title": source.title,
            },
        }

        seed_message = Message(
            id=MessageId.generate(self._ids),
            role=MessageRole.USER,
            content=MessageContent(text=handoff_text),
            created_at=now,
        )

        new_conv = Conversation(
            id=new_conv_id,
            title=title,
            created_at=now,
            updated_at=now,
            status=ConversationStatus.ACTIVE,
            messages=[seed_message],
            meta=meta,
        )

        # 6. Persist ONLY the new conversation. The source is untouched —
        #    this is the contract that makes the escape hatch safe: even
        #    if a user changes their mind and closes the migrated tab,
        #    the original conversation is still available in the sidebar
        #    exactly as it was.
        await self._conversations.save(new_conv)

        _log.info(
            "chat.conversation_migrated",
            source_conversation_id=source.id.value,
            new_conversation_id=new_conv_id.value,
            digest_bytes=len(digest_text or ""),
            has_ledger=ledger is not None,
            degraded=recent_transcript is not None,
        )

        return MigrateConversationResult(
            new_conversation_id=new_conv_id,
            source_conversation_id=source.id,
        )

    async def _await_inflight_digest(
        self, conversation_id: ConversationId,
    ) -> None:
        """Block until an in-flight P2 digest task settles (bounded, never raises).

        ``/compact`` replies as soon as the 4-phase drop finishes, then keeps
        summarising in the background for 20-40s. A migration issued in that
        window would read a digest-less checkpoint and fall back to the
        degraded handoff even though a real summary was seconds away — so we
        wait for it first.

        Every failure mode is swallowed on purpose: no engine wired, the engine
        predates the accessor, no task for this key, the task raised, it was
        cancelled, or the wait timed out. In all of those the caller proceeds
        with whatever the store holds (digest or degraded tail). A handoff must
        never fail because its optional enrichment did.
        """
        engine = self._compaction_engine
        if engine is None or self._digest_wait_seconds <= 0:
            return
        # Public accessor: it applies the engine's own ``key_prefix`` and
        # returns ``None`` for an already-settled task, so we never await
        # something that cannot progress. Reaching into the private task
        # registry instead would silently miss every entry on a prefixed
        # (multi-agent) engine.
        getter = getattr(engine, "digest_refresh_task", None)
        if not callable(getter):
            return
        try:
            task = getter(conversation_id.value)
        except Exception:  # noqa: BLE001 — probe must never break a migration
            return
        if task is None:
            return
        _log.info(
            "chat.compaction.migrate_awaiting_digest",
            conversation_id=conversation_id.value,
            wait_seconds=self._digest_wait_seconds,
        )
        try:
            # ``shield`` so a cancellation of the DIGEST task (a newer kick
            # superseded it, or ``/compact clear`` invalidated the key) cannot
            # propagate into THIS coroutine and abort the migration.
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self._digest_wait_seconds,
            )
        except TimeoutError:
            _log.info(
                "chat.compaction.migrate_digest_wait_timeout",
                conversation_id=conversation_id.value,
                wait_seconds=self._digest_wait_seconds,
            )
        except asyncio.CancelledError:
            # The shielded task was cancelled — not our failure. Re-raising
            # here would abort the migration for a reason the user never
            # caused, so we log and fall through to the degraded handoff.
            _log.info(
                "chat.compaction.migrate_digest_cancelled",
                conversation_id=conversation_id.value,
            )
        except Exception as exc:  # noqa: BLE001 — enrichment is optional
            _log.info(
                "chat.compaction.migrate_digest_wait_failed",
                conversation_id=conversation_id.value,
                error=str(exc),
                error_type=type(exc).__name__,
            )


__all__ = [
    "MigrateConversationInput",
    "MigrateConversationResult",
    "MigrateConversationUseCase",
]

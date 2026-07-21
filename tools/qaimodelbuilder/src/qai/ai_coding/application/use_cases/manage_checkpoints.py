# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: create / list / rewind to checkpoints for a session.

Backs the legacy
``GET /api/cc/sessions/{id}/checkpoints`` (list — not in PR-105 scope
but the use case is generic enough to support it),
``POST /api/oc/sessions/{id}/checkpoint`` (create),
``POST /api/oc/sessions/{id}/rewind`` (rewind to a target checkpoint)
routes.

Implementation note (PR-105)
----------------------------
The legacy CC backend backed checkpoints with the SDK's
``replay-user-messages`` + ``user_message_checkpoints`` mapping; the
legacy OC backend relied on OpenCode's native ``revert`` API.  PR-105
ships a **minimal** implementation: a KV-table-backed
:class:`CheckpointRepositoryPort` adapter that stores history
snapshots under the per-session key
``ai_coding.checkpoints.<session_id>``.  This preserves the wire
surface and lets the WebUI exercise the round-trip; PR-108c (Agent
Harness Phase 2D-2E) will swap in a real OpenCode-CLI-backed adapter
when the SDK enhancement work lands.

The aggregate is NOT mutated by checkpoint create — checkpoints are
out-of-band audit metadata.  Rewind, on the other hand, calls the
existing :meth:`CodingSession.truncate_history_after` (PR-104a) so
the rewind semantics are unified with the existing truncate path.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    CheckpointRecord,
    CheckpointRepositoryPort,
    CodingProviderPort,
    CodingSessionRepositoryPort,
)
from qai.ai_coding.domain import CodingSessionId
from qai.platform.events import EventBus
from qai.platform.logging import get_logger

logger = get_logger(__name__)


__all__ = [
    "CheckpointInfo",
    "CheckpointResult",
    "CreateCheckpointCommand",
    "CreateCheckpointUseCase",
    "ListCheckpointsQuery",
    "ListCheckpointsResult",
    "ListCheckpointsUseCase",
    "RewindCheckpointCommand",
    "RewindCheckpointResult",
    "RewindCheckpointUseCase",
]


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckpointInfo:
    """Wire-friendly snapshot of a :class:`CheckpointRecord`.

    The dataclass keeps the use-case return shape stable while leaving
    the underlying record class slot-only (no dataclass overhead).
    """

    checkpoint_id: str
    created_at: str
    label: str | None
    message_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateCheckpointCommand:
    """Input for :class:`CreateCheckpointUseCase`."""

    session_id: CodingSessionId
    label: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckpointResult:
    """Return shape of :class:`CreateCheckpointUseCase`."""

    checkpoint: CheckpointInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class ListCheckpointsQuery:
    """Input for :class:`ListCheckpointsUseCase`."""

    session_id: CodingSessionId


@dataclass(frozen=True, slots=True, kw_only=True)
class ListCheckpointsResult:
    """Return shape of :class:`ListCheckpointsUseCase`."""

    checkpoints: tuple[CheckpointInfo, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RewindCheckpointCommand:
    """Input for :class:`RewindCheckpointUseCase`.

    ``checkpoint_id`` selects the snapshot to restore.  The use case
    truncates the aggregate's history to the snapshot's recorded
    length and emits a :class:`MessageHistoryTruncatedEvent` via the
    existing PR-104a truncate path.
    """

    session_id: CodingSessionId
    checkpoint_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RewindCheckpointResult:
    """Return shape of :class:`RewindCheckpointUseCase`.

    ``files_rewound`` (2-H3) reports whether the provider performed a
    native on-disk file restoration (CC ``rewind_files`` / OC
    ``revert``) in addition to the message-history truncate.  ``False``
    means a message-only rewind ran (no native support wired / unknown
    session) — never an error.
    """

    checkpoint_id: str
    removed: int
    remaining: int
    files_rewound: bool = False


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


def _record_to_info(record: CheckpointRecord) -> CheckpointInfo:
    snapshot = record.snapshot or {}
    raw_history = snapshot.get("messages") if isinstance(snapshot, dict) else None
    if isinstance(raw_history, list):
        message_count = len(raw_history)
    else:
        message_count = 0
    return CheckpointInfo(
        checkpoint_id=record.checkpoint_id,
        created_at=record.created_at,
        label=record.label,
        message_count=message_count,
    )


class CreateCheckpointUseCase:
    """Application service for ``POST /api/oc/sessions/{id}/checkpoint``."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        checkpoint_repository: CheckpointRepositoryPort,
    ) -> None:
        self._repository = repository
        self._checkpoint_repository = checkpoint_repository

    async def execute(self, command: CreateCheckpointCommand) -> CheckpointResult:
        # Load the session so a missing id surfaces 422 (DomainError).
        session = await self._repository.get(command.session_id)
        snapshot = {
            "session_id": str(session.session_id),
            "title": session.title,
            "workspace": session.workspace.path,
            "messages": [
                {"text": msg.text} for msg in session.messages
            ],
        }
        record = await self._checkpoint_repository.create(
            session_id=command.session_id,
            snapshot=snapshot,
            label=command.label,
        )
        logger.info(
            "ai_coding.checkpoint.created",
            session_id=str(command.session_id),
            checkpoint_id=record.checkpoint_id,
        )
        return CheckpointResult(checkpoint=_record_to_info(record))


class ListCheckpointsUseCase:
    """Application service for listing per-session checkpoints."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        checkpoint_repository: CheckpointRepositoryPort,
    ) -> None:
        self._repository = repository
        self._checkpoint_repository = checkpoint_repository

    async def execute(
        self, query: ListCheckpointsQuery
    ) -> ListCheckpointsResult:
        # Validate the session exists so a missing id surfaces 422.
        await self._repository.get(query.session_id)
        records = await self._checkpoint_repository.list_for_session(
            query.session_id
        )
        return ListCheckpointsResult(
            checkpoints=tuple(_record_to_info(r) for r in records),
        )


class RewindCheckpointUseCase:
    """Application service for ``POST /api/oc/sessions/{id}/rewind``.

    Truncates the session's history to the message count recorded in
    the target checkpoint snapshot and emits the existing
    :class:`MessageHistoryTruncatedEvent` so audit subscribers see
    the rewind as a structured truncate.

    2-H3 (rewind file restoration): when a ``provider_port`` is wired,
    the use case additionally asks the provider to roll back the
    on-disk workspace files to the rewind anchor — Claude Code's native
    ``rewind_files`` or OpenCode's native ``revert`` — restoring V1's
    "rewind also reverts project files" behaviour.  The provider hook
    is best-effort (never raises) and provider-native, so the whole
    flow stays inside the ai_coding context (no cross-context
    project_snapshot import).  ``provider_port=None`` keeps the
    message-only rewind for hand-rolled containers / tests.
    """

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        checkpoint_repository: CheckpointRepositoryPort,
        event_bus: EventBus,
        provider_port: CodingProviderPort | None = None,
    ) -> None:
        self._repository = repository
        self._checkpoint_repository = checkpoint_repository
        self._event_bus = event_bus
        self._provider_port = provider_port

    async def execute(
        self, command: RewindCheckpointCommand
    ) -> RewindCheckpointResult:
        session = await self._repository.get(command.session_id)
        record = await self._checkpoint_repository.get(
            session_id=command.session_id,
            checkpoint_id=command.checkpoint_id,
        )

        # Snapshot's recorded message count is the rewind target.
        snapshot = record.snapshot or {}
        recorded_messages = snapshot.get("messages") if isinstance(snapshot, dict) else []
        target_count = (
            len(recorded_messages) if isinstance(recorded_messages, list) else 0
        )
        current_count = len(session.messages)

        if target_count >= current_count:
            # Nothing to truncate — checkpoint is at or after current head.
            return RewindCheckpointResult(
                checkpoint_id=record.checkpoint_id,
                removed=0,
                remaining=current_count,
                files_rewound=False,
            )

        # Use the existing PR-104a aggregate method so the event flow
        # is unified.  ``marker_index`` is the last index to KEEP;
        # truncate_history_after drops everything strictly after.
        marker_index = target_count - 1
        if marker_index < 0:
            # Special case: rewind to "nothing" — drop all messages.
            # truncate_history_after expects a valid index, so we use
            # marker_index=0 + include_self=True to also drop the
            # head message.
            removed = session.truncate_history_after(
                marker_index=0, include_self=True
            )
        else:
            removed = session.truncate_history_after(
                marker_index=marker_index, include_self=False
            )

        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)

        # 2-H3: ask the provider to natively roll back project files to
        # the rewind anchor (CC ``rewind_files`` / OC ``revert``).  The
        # anchor is the index of the last KEPT message (``target_count -
        # 1``); clamp to 0 for the "rewind to nothing" case.  Best-effort
        # — a missing port / native-unsupported provider degrades to the
        # message-only rewind already performed above.
        files_rewound = await self._maybe_rewind_files(
            command.session_id,
            marker_index=max(0, target_count - 1),
        )

        logger.info(
            "ai_coding.checkpoint.rewound",
            session_id=str(command.session_id),
            checkpoint_id=record.checkpoint_id,
            removed=removed,
            files_rewound=files_rewound,
        )
        return RewindCheckpointResult(
            checkpoint_id=record.checkpoint_id,
            removed=removed,
            remaining=len(session.messages),
            files_rewound=files_rewound,
        )

    async def _maybe_rewind_files(
        self, session_id: CodingSessionId, *, marker_index: int
    ) -> bool:
        """Best-effort provider-native file rewind (2-H3).

        Returns ``True`` only when the provider issued a native file
        restoration; any missing port, unsupported provider, or
        provider-side failure returns ``False`` so the rewind degrades
        to message-only without raising.
        """
        if self._provider_port is None:
            return False
        rewind = getattr(self._provider_port, "rewind_files", None)
        if not callable(rewind):
            return False
        try:
            return bool(
                await rewind(session_id=session_id, marker_index=marker_index)
            )
        except Exception as exc:  # noqa: BLE001 — never abort the rewind.
            logger.warning(
                "ai_coding.checkpoint.rewind_files_failed",
                session_id=str(session_id),
                error=repr(exc),
            )
            return False

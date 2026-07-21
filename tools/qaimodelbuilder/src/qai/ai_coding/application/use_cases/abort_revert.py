# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: hard-abort + revert-to-message for a coding session.

PR-105 lifts the legacy OC-only ``abort`` and ``revert`` routes into
the new architecture and exposes them on **both** the CC and OC
sub-routers (the prompt explicitly directs adding the CC twin).

Semantic differences from PR-104a's :class:`InterruptSessionUseCase`
-------------------------------------------------------------------
* ``InterruptSessionUseCase`` (PR-104a) is a **soft** interrupt:
  the aggregate flips to ``IDLE``, the in-flight stream is
  abandoned, and the user can immediately resume.  The legacy
  CC backend treats this as ``interrupt`` and the legacy OC
  backend tries it first before falling through to abort.
* :class:`AbortSessionUseCase` (here) is a **hard** abort: the
  provider's terminate path is invoked unconditionally and the
  aggregate flips to ``IDLE`` (mirroring the legacy
  ``oc_manager.abort_session`` semantic).  The route layer
  surfaces ``{"ok": true, "session_id": "..."}``.

``RevertMessageUseCase`` is the per-message history rewind: the
caller supplies the legacy ``message_id`` (or the new index) and
the use case truncates the trailing slice past it.  The legacy
OC backend delegates this to OpenCode's native ``revert`` API;
PR-105 ships a minimal aggregate-side rewind so the route surface
is preserved without an OpenCode integration.  PR-108c may swap
in the real OC delegation.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    CodingProviderPort,
    CodingSessionRepositoryPort,
)
from qai.ai_coding.domain import (
    CodingSessionAlreadyTerminatedError,
    CodingSessionId,
    SessionStatus,
)
from qai.platform.errors import ValidationError
from qai.platform.events import EventBus
from qai.platform.logging import get_logger

logger = get_logger(__name__)


__all__ = [
    "AbortSessionCommand",
    "AbortSessionResult",
    "AbortSessionUseCase",
    "RevertMessageCommand",
    "RevertMessageResult",
    "RevertMessageUseCase",
]


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class AbortSessionCommand:
    """Input for :class:`AbortSessionUseCase`."""

    session_id: CodingSessionId


@dataclass(frozen=True, slots=True, kw_only=True)
class AbortSessionResult:
    """Return shape of :class:`AbortSessionUseCase`."""

    aborted: bool
    reason: str | None = None


class AbortSessionUseCase:
    """Application service for hard-abort.

    Backs ``POST /api/cc/sessions/{id}/abort`` and
    ``POST /api/oc/sessions/{id}/abort``.

    Behaviour
    ---------
    * On a TERMINATED session: raises
      :class:`CodingSessionAlreadyTerminatedError` (route → 422).
    * On any other session: invokes
      :meth:`CodingProviderPort.terminate` (best-effort; provider
      errors are logged but do not block the abort) and flips the
      aggregate to IDLE via the existing
      :meth:`CodingSession.interrupt` mutator.  Returns
      ``aborted=True``.
    """

    def __init__(
        self,
        *,
        provider_port: CodingProviderPort,
        repository: CodingSessionRepositoryPort,
        event_bus: EventBus,
    ) -> None:
        self._provider_port = provider_port
        self._repository = repository
        self._event_bus = event_bus

    async def execute(self, command: AbortSessionCommand) -> AbortSessionResult:
        session = await self._repository.get(command.session_id)

        if session.status is SessionStatus.TERMINATED:
            raise CodingSessionAlreadyTerminatedError(
                message=(
                    f"coding session {command.session_id} is terminated; "
                    "cannot abort"
                ),
                details={"session_id": str(command.session_id)},
            )

        # Best-effort provider abort.  Unlike interrupt, abort ALWAYS
        # asks the provider regardless of current status — the legacy
        # ``oc_manager.abort_session`` flushed the live stream even when
        # the aggregate had already drifted to IDLE.  2-H4: prefer the
        # provider's native ``abort`` (OpenCode ``POST /session/{id}/abort``)
        # over the generic ``terminate``; ``abort`` is a duck-typed
        # optional hook (the base/CC adapter falls back to terminate),
        # so older provider stubs without it degrade to ``terminate``.
        try:
            abort = getattr(self._provider_port, "abort", None)
            if callable(abort):
                await abort(session_id=command.session_id)
            else:
                await self._provider_port.terminate(
                    session_id=command.session_id
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ai_coding.abort_session.provider_error",
                session_id=str(command.session_id),
                error=repr(exc),
            )

        # Only flip the aggregate when there is something to flip;
        # ``interrupt()`` itself no-ops on an already-IDLE session
        # via the route-level "no in-flight turn" branch in
        # :class:`InterruptSessionUseCase`, but the abort surface
        # mandates a status flip when not idle.  We mirror the same
        # guard here for consistency.
        if session.status is not SessionStatus.IDLE:
            session.interrupt()
            await self._repository.save(session)
            for event in session.drain_events():
                await self._event_bus.publish(event)

        logger.info(
            "ai_coding.abort_session.ok",
            session_id=str(command.session_id),
        )
        return AbortSessionResult(aborted=True)


# ---------------------------------------------------------------------------
# Revert
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RevertMessageCommand:
    """Input for :class:`RevertMessageUseCase`.

    The legacy OC client passes ``message_id`` (and optionally
    ``part_id``); the new architecture stores only the user-message
    text on the aggregate so we resolve the legacy id to a 0-based
    index.  The route layer accepts both shapes:

    * ``message_id`` matching the synthetic ``oc-user-<n>`` /
      ``cc-user-<n>`` projection exposed by ``GET /history``.
    * ``after_index`` directly (new clients).

    The first-found resolution is used; an unresolvable id raises
    :class:`ValidationError`.
    """

    session_id: CodingSessionId
    marker_index: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RevertMessageResult:
    """Return shape of :class:`RevertMessageUseCase`."""

    removed: int
    remaining: int


class RevertMessageUseCase:
    """Application service for per-message history revert.

    Backs ``POST /api/cc/sessions/{id}/revert`` and
    ``POST /api/oc/sessions/{id}/revert``.

    Behaviour
    ---------
    * Always performs the aggregate-side history rewind via
      :meth:`CodingSession.truncate_history_after` (PR-104a) — the rewind
      point becomes the message at ``marker_index`` and everything after
      it is dropped.
    * RE-OC-7: when a ``provider_port`` is wired AND the session is an
      OpenCode session, the use case additionally asks the provider to
      perform OpenCode's **native** revert (``POST /session/{id}/revert``)
      using the persisted native ``messageID`` for ``marker_index`` — so
      the OpenCode server's workspace + message state roll back too, even
      after a daemon restart (V1 forwarded the frontend messageID directly
      and thus worked for any historical turn,
      ``opencode_session_manager.py:1138-1169``).  The provider hook is
      best-effort (never raises) and provider-native, so a missing port /
      CC session degrades to the message-only rewind.
    """

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        event_bus: EventBus,
        provider_port: CodingProviderPort | None = None,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus
        self._provider_port = provider_port

    async def execute(
        self, command: RevertMessageCommand
    ) -> RevertMessageResult:
        session = await self._repository.get(command.session_id)

        if command.marker_index < 0 or command.marker_index >= len(session.messages):
            raise ValidationError(
                code="ai_coding.invalid_revert_marker",
                message=(
                    f"revert marker {command.marker_index} is out of bounds "
                    f"(session has {len(session.messages)} messages)"
                ),
                field_errors={"marker_index": [str(command.marker_index)]},
            )

        # RE-OC-7: OpenCode native revert (best-effort, OC-only).  Seed the
        # provider handle with the persisted native message ids so
        # ``rewind_files`` resolves ``marker_index`` to the right
        # ``messageID`` even after a restart, then ask the provider to roll
        # back the OpenCode server state.  Done BEFORE the aggregate
        # truncate so a provider failure does not leave the aggregate
        # rewound without the server rewound (the truncate still runs —
        # message-only degrade — matching the pre-RE-OC-7 behaviour).
        await self._maybe_native_revert(session, command.marker_index)

        # ``include_self=True`` so the targeted message itself is
        # also dropped — legacy revert semantics treat the targeted
        # turn as "to be re-issued by the caller".
        removed = session.truncate_history_after(
            marker_index=command.marker_index,
            include_self=True,
        )
        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)

        logger.info(
            "ai_coding.revert_message.ok",
            session_id=str(command.session_id),
            marker_index=command.marker_index,
            removed=removed,
        )
        return RevertMessageResult(
            removed=removed,
            remaining=len(session.messages),
        )

    async def _maybe_native_revert(
        self, session: object, marker_index: int
    ) -> bool:
        """Best-effort OpenCode-native revert (RE-OC-7).

        Returns ``True`` only when the provider issued a native revert.
        Any missing port, non-OpenCode session, missing persisted message
        id, or provider-side failure returns ``False`` so the caller's
        message-only rewind still runs.
        """
        if self._provider_port is None:
            return False
        provider = getattr(session, "provider", None)
        # Duck-typed OC check: only OpenCode sessions carry native
        # message ids + a ``rewind_files`` hook keyed on them.
        if getattr(provider, "value", None) != "open_code":
            return False
        message_ids = list(getattr(session, "oc_message_ids", ()) or ())
        if not message_ids:
            return False
        session_id = getattr(session, "session_id", None)
        if session_id is None:
            return False
        # Seed the provider handle from the persisted ids so a post-restart
        # process (which never streamed the turn) can still resolve them.
        seed = getattr(self._provider_port, "seed_oc_message_ids", None)
        if callable(seed):
            try:
                seed(session_id=session_id, message_ids=message_ids)
            except Exception:  # noqa: BLE001 — best-effort.
                pass
        rewind = getattr(self._provider_port, "rewind_files", None)
        if not callable(rewind):
            return False
        try:
            return bool(
                await rewind(session_id=session_id, marker_index=marker_index)
            )
        except Exception as exc:  # noqa: BLE001 — never abort the revert.
            logger.warning(
                "ai_coding.revert_message.native_revert_failed",
                session_id=str(getattr(session, "session_id", "")),
                error=repr(exc),
            )
            return False

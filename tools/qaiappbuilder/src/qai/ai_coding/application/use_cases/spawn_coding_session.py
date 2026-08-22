# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: spawn a new :class:`CodingSession`.

Sequence (happy path)
---------------------
1. Verify the requested provider is registered with
   :class:`CodingProviderPort` — otherwise raise
   :class:`ProviderNotAvailableError`.
2. Acquire the workspace lock via :class:`WorkspaceLockPort`; failure
   to acquire surfaces as :class:`WorkspaceLockedError`.
3. Spawn the backend agent through :class:`CodingProviderPort.spawn`.
4. Construct the aggregate via :class:`CodingSession.spawn`.
5. Persist the aggregate and publish queued domain events.

If step 3 or step 4 fails, the workspace lock is released so the
caller can retry without rebooting the application.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qai.ai_coding.application.ports import (
    ClaudeMdInjectorPort,
    CodingProviderPort,
    CodingSessionRepositoryPort,
    WorkspaceLockPort,
)
from qai.ai_coding.domain import (
    CodingSession,
    CodingSessionConfig,
    CodingSessionId,
    MessageContent,
    Provider,
    ProviderNotAvailableError,
    Workspace,
)
from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class SpawnCodingSessionCommand:
    """Input parameters for :class:`SpawnCodingSessionUseCase`."""

    provider: Provider
    workspace: Workspace
    initial_prompt: MessageContent | None = None
    title: str | None = None
    # PR-107: SDK 12-item enhancements (mcp_servers / hooks /
    # fallback_model / …).  Defaults to ``None`` so the historical
    # PR-046 spawn shape keeps working without changes.
    config: CodingSessionConfig | None = None


class SpawnCodingSessionUseCase:
    """Application service for spawning a coding session."""

    def __init__(
        self,
        *,
        provider_port: CodingProviderPort,
        repository: CodingSessionRepositoryPort,
        workspace_lock: WorkspaceLockPort,
        clock: Clock,
        ids: IdGenerator,
        event_bus: EventBus,
        claude_md_injector: ClaudeMdInjectorPort | None = None,
    ) -> None:
        self._provider_port = provider_port
        self._repository = repository
        self._workspace_lock = workspace_lock
        self._clock = clock
        self._ids = ids
        self._event_bus = event_bus
        # PR-095 / S9 H-12 — workspace bootstrap collaborator.  ``None``
        # disables CLAUDE.md injection entirely (used by tests / fixtures
        # / deployments that opt out of the bundled template).  The
        # production DI wires
        # :class:`qai.ai_coding.infrastructure.claude_md_injector.ClaudeMdInjector`
        # so the use case stays clean of the
        # ``layered-ai_coding`` ``application -> infrastructure``
        # import-linter violation.
        self._claude_md_injector = claude_md_injector

    async def execute(self, command: SpawnCodingSessionCommand) -> CodingSession:
        if command.provider not in self._provider_port.available_providers():
            raise ProviderNotAvailableError(
                message=f"provider {command.provider.value} is not registered",
                details={"provider": command.provider.value},
            )

        # Acquire the workspace lock first; rejecting on contention keeps
        # the spawn flow strictly sequential per directory.
        await self._workspace_lock.acquire(command.workspace)

        # Pre-allocate the session id so the provider can stash the
        # PR-107 ``config`` under that key inside its handle dict.
        # ``CodingSession.spawn`` below uses the same id.
        pre_session_id = CodingSessionId(value=self._ids.new_id())
        try:
            await self._provider_port.spawn(
                provider=command.provider,
                workspace=command.workspace,
                initial_prompt=command.initial_prompt,
                session_id=pre_session_id,
                config=command.config,
            )
        except BaseException:
            # Release the lock on any failure to spawn so the caller
            # can retry; we explicitly do NOT swallow the exception.
            await self._workspace_lock.release(command.workspace)
            raise

        session = CodingSession.spawn(
            session_id=pre_session_id,
            provider=command.provider,
            workspace=command.workspace,
            now=self._clock.now(),
            title=command.title,
            config=command.config,
        )
        if command.initial_prompt is not None:
            session.append_message(command.initial_prompt)

        # PR-095 / S9 H-12: drop the project's CLAUDE.md template into
        # the workspace so the upstream agent picks up house rules
        # (path conventions, dependency discipline, testing etiquette)
        # without per-turn prompting.  Idempotent — existing CLAUDE.md
        # is preserved.  Disk failures are absorbed inside the adapter
        # so a read-only workspace does not abort spawn.  The injector
        # is optional; ``None`` disables injection entirely.
        if self._claude_md_injector is not None:
            try:
                workspace_path = Path(command.workspace.path)
            except (TypeError, ValueError):
                workspace_path = None
            if workspace_path is not None:
                self._claude_md_injector.copy_to(workspace_path)

        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)
        logger.info(
            "ai_coding.spawn_coding_session.ok",
            session_id=str(session.session_id),
            provider=command.provider.value,
        )
        return session


__all__ = ["SpawnCodingSessionCommand", "SpawnCodingSessionUseCase"]

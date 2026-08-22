# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""The async-job bookkeeping behind one sub-agent's lifetime.

Split out of :mod:`qai.chat.adapters.subagent_bridge` because "which job
belongs to which sub-agent, and what terminal state did it reach" is a
distinct concern from "how a sub-agent is started".  Every interaction with
:class:`AsyncJobManager` on the spawn path goes through here, which is what
makes the one-terminal-transition-per-run guarantee checkable in a single
place instead of spread across the spawn code.

The ledger also owns the ``subagent_id -> job_id`` index.  The job manager
stamps the sub-agent id onto ``AsyncJob.agent_id`` so a job is discoverable
from the sub-agent alone, but that is a scan; this index makes the common
per-sub-agent lookup (status, cancel) direct.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.chat.adapters.async_job_manager import AsyncJobKind
from qai.chat.domain.sub_agent_error import SubAgentError, SubAgentErrorKind
from qai.platform.logging import get_logger

if TYPE_CHECKING:
    from qai.chat.adapters.async_job_manager import AsyncJob, AsyncJobManager

__all__ = ["SubAgentJobLedger"]

_log = get_logger(__name__)


class SubAgentJobLedger:
    """Registers, settles and cancels the jobs tracking sub-agent runs."""

    __slots__ = ("_ajm", "_job_ids")

    def __init__(self, async_job_manager: AsyncJobManager) -> None:
        self._ajm = async_job_manager
        self._job_ids: dict[str, str] = {}

    async def register(self, *, conv_id: str, subagent_id: str) -> str:
        """Open a job for ``subagent_id``; returns its job id.

        ``owner_id`` is the CONVERSATION, not the tab: a conversation outlives
        its tab, so a completion always has somewhere to be delivered.

        Raises:
            SubAgentError: kind ``ISOLATION`` — the run was never launched, so
                the spawn must fail rather than silently go untracked.
        """
        try:
            job_id = await self._ajm.register_job(
                kind=AsyncJobKind.SUBAGENT,
                owner_id=conv_id,
                agent_id=subagent_id,
            )
        except Exception as exc:
            _log.warning(
                "subagent.bridge.spawn_failed",
                extra={"subagent_id": subagent_id, "error": str(exc)},
            )
            raise SubAgentError(
                kind=SubAgentErrorKind.ISOLATION,
                message=f"Cannot spawn: job registration failed ({exc})",
                subagent_id=subagent_id,
                recovery_hint=(
                    "Retry; if it persists the job registry is unhealthy."
                ),
            ) from exc
        self._job_ids[subagent_id] = job_id
        return job_id

    async def complete(self, job_id: str, result: str | None) -> None:
        """Settle ``job_id`` successfully with the run's final text."""
        await self._ajm.complete_job(job_id, result=result)

    async def fail(
        self,
        job_id: str,
        *,
        subagent_id: str,
        kind: SubAgentErrorKind,
        message: str,
    ) -> None:
        """Settle ``job_id`` as failed, carrying a typed error."""
        await self._ajm.complete_job(
            job_id,
            error=SubAgentError(
                kind=kind, message=message, subagent_id=subagent_id
            ),
        )

    async def fail_with(self, job_id: str, error: SubAgentError) -> None:
        """Settle ``job_id`` with an already-typed error."""
        await self._ajm.complete_job(job_id, error=error)

    def job_of(self, subagent_id: str) -> AsyncJob | None:
        """The job tracking ``subagent_id``, or ``None`` if not tracked.

        The index entry deliberately outlives the run — a just-finished
        sub-agent is exactly what callers ask about — so ``None`` means either
        "never registered here" or "already dead-letter evicted".  An evicted
        key is dropped on the way out so the index cannot outgrow the registry
        it mirrors.
        """
        job_id = self._job_ids.get(subagent_id)
        if job_id is None:
            return None
        job = self._ajm.get(job_id)
        if job is None:
            self._job_ids.pop(subagent_id, None)
        return job

    async def cancel(self, subagent_id: str) -> bool:
        """Cancel just this sub-agent's job; ``True`` when it transitioned.

        Siblings sharing the conversation are untouched, which is why this is
        a per-job cancellation rather than an owner-wide one.
        """
        job_id = self._job_ids.get(subagent_id)
        if job_id is None:
            return False
        return await self._ajm.cancel_job(job_id)

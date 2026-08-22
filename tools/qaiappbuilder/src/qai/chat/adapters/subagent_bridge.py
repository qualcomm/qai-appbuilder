# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``SubAgentBridge`` — the ONE way a sub-agent is spawned.

Spawning is asynchronous by construction: :meth:`SubAgentBridge.spawn`
preflights, opens a tracking job, launches the run on a background task and
returns a :class:`SubAgentHandle` — it never awaits the sub-agent.  The parent
agent therefore gets its tool result at t=0 and keeps streaming, and the
result arrives later through the job manager's completion fan-out.  This
replaces a synchronous drain that consumed the sub-agent's event iterator
inline and raced a park gate against it to fake non-blocking behaviour —
hence no park gate, no while-loop and no synthetic "done" frame here.

Responsibilities are split so each moves for its own reason: admission policy
in :class:`SpawnPreflight`, execution in :class:`SubAgentRunner`, job
bookkeeping in :class:`SubAgentJobLedger`.  What remains here is the
composition — mint the id, admit, launch, settle exactly once, cancel by id.
Live progress belongs to none of them: ``iter_events`` already fans events out
to the sub-agent stream broadcaster, so the standalone sub-agent tab keeps
working untouched.

``SubAgentHandle.subagent_id`` is minted HERE, before the run starts, and
handed down so the persisted session adopts it — one id serves as handle id,
session id, broadcaster key and abort key, so a caller holding a handle can
cancel, inspect and subscribe with no further lookup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from qai.chat.adapters.subagent_job_ledger import SubAgentJobLedger
from qai.chat.adapters.subagent_preflight import SpawnPreflight
from qai.chat.adapters.subagent_runner import (
    SubAgentRun,
    SubAgentRunner,
    classify_failure,
)
from qai.chat.domain.sub_agent_error import SubAgentError, SubAgentErrorKind
from qai.chat.domain.sub_agent_spawn_request import (
    SubAgentHandle,
    SubAgentSpawnRequest,
)
from qai.platform.ids.ports import IdGenerator, UlidGenerator
from qai.platform.logging import get_logger

if TYPE_CHECKING:
    from qai.chat.adapters.async_job_manager import AsyncJob, AsyncJobManager
    from qai.chat.adapters.sub_agent_abort_registry import (
        InMemorySubAgentAbortRegistry,
    )

__all__ = ["SubAgentBridge"]

_log = get_logger(__name__)


class SubAgentBridge:
    """Spawn, track and cancel background sub-agent runs.

    Args:
        async_job_manager: registry owning admission control and completion
            delivery for every async job.
        agent_tool_handler_factory: zero-arg callable returning the
            ``AgentToolHandler`` that runs a sub-agent.  A FACTORY rather than
            the handler itself, because the handler is constructed with a
            reference to this bridge — the callable breaks that cycle.
        subagent_abort_registry: cooperative-abort registry :meth:`cancel`
            signals so a running task notices immediately.
        ids: mints the sub-agent id before the run starts.
        chat_settings: read by the preflight for the recursion ceiling.
        max_depth_reader: optional async ceiling override, per spawn.
    """

    __slots__ = ("_abort_registry", "_ids", "_jobs", "_preflight", "_runner", "_tasks")

    def __init__(
        self,
        *,
        async_job_manager: AsyncJobManager,
        agent_tool_handler_factory: Callable[[], Any],
        subagent_abort_registry: InMemorySubAgentAbortRegistry | None = None,
        ids: IdGenerator | None = None,
        chat_settings: Any | None = None,
        max_depth_reader: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        self._abort_registry = subagent_abort_registry
        self._ids: IdGenerator = ids if ids is not None else UlidGenerator()
        self._jobs = SubAgentJobLedger(async_job_manager)
        self._preflight = SpawnPreflight(
            chat_settings=chat_settings, max_depth_reader=max_depth_reader
        )
        self._runner = SubAgentRunner(agent_tool_handler_factory)
        #: Strong references to in-flight runs.  The event loop keeps only a
        #: weak one, so without this a GC pass can collect a running
        #: sub-agent mid-flight.
        self._tasks: set[asyncio.Task[None]] = set()

    async def spawn(
        self,
        *,
        conv_id: str,
        parent_depth: int,
        parent_agent_type: str | None,
        spawn_request: SubAgentSpawnRequest,
        tab_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SubAgentHandle:
        """Admit a sub-agent, start it in the background, return its handle.

        Returns as soon as the job is registered — the sub-agent's own
        runtime never contributes to this call's latency.

        Args:
            conv_id: owning conversation (the job's owner: it outlives tabs).
            parent_depth: the spawning agent's depth; the child gets one more.
            parent_agent_type: spawning agent's type, ``None`` for the main
                agent.
            spawn_request: what to run.
            tab_id: tab for display; defaults to ``conv_id``.
            extra: forwarded verbatim as ``iter_events`` kwargs.

        Raises:
            SubAgentError: ``PREFLIGHT`` when refused — no job is registered —
                or ``ISOLATION`` when the run could not be launched at all.
        """
        depth = parent_depth + 1
        await self._preflight.check(
            child_depth=depth,
            parent_agent_type=parent_agent_type,
            spawn_request=spawn_request,
        )
        subagent_id = self._ids.new_id()
        job_id = await self._jobs.register(
            conv_id=conv_id, subagent_id=subagent_id
        )
        run = SubAgentRun(
            job_id=job_id,
            subagent_id=subagent_id,
            conv_id=conv_id,
            tab_id=tab_id or conv_id,
            depth=depth,
            request=spawn_request,
            extra=dict(extra) if extra else {},
        )
        task = asyncio.create_task(self._settle(run), name=f"sa-{job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        _log.info(
            "subagent.bridge.spawn",
            extra={
                "subagent_id": subagent_id,
                "job_id": job_id,
                "depth": depth,
            },
        )
        return SubAgentHandle(subagent_id=subagent_id, job_id=job_id)

    async def _settle(self, run: SubAgentRun) -> None:
        """Run one sub-agent and drive its job to a terminal state.

        Every exit path settles the job.  An unsettled job would stay
        ``RUNNING`` forever, holding an admission slot that starves queued
        work and never ageing into a dead letter.
        """
        try:
            result = await self._runner.run(run)
        except asyncio.CancelledError:
            # A cancelled run is still a settled job: record the interruption
            # before honouring the cancellation.
            await self._jobs.fail(
                run.job_id,
                subagent_id=run.subagent_id,
                kind=SubAgentErrorKind.INTERRUPTED,
                message="Sub-agent was interrupted before it finished.",
            )
            raise
        except SubAgentError as exc:
            await self._jobs.fail_with(run.job_id, exc)
        except Exception as exc:  # noqa: BLE001 — every failure gets a kind
            await self._jobs.fail(
                run.job_id,
                subagent_id=run.subagent_id,
                kind=classify_failure(exc),
                message=str(exc) or type(exc).__name__,
            )
        else:
            await self._jobs.complete(run.job_id, result)

    def status(self, subagent_id: str) -> AsyncJob | None:
        """The tracked job of ``subagent_id``, or ``None`` if not tracked."""
        return self._jobs.job_of(subagent_id)

    async def cancel(self, subagent_id: str) -> bool:
        """Stop ONE sub-agent; ``True`` when anything was actually stopped.

        Cancels the job so the parent stops expecting a result, AND signals
        the cooperative-abort flag so the running task unwinds promptly.  Both
        halves are required: cancelling only the job would leave the task
        running while its metadata claimed ``CANCELLED``.  Siblings sharing
        the conversation are untouched.
        """
        cancelled = await self._jobs.cancel(subagent_id)
        aborted = (
            self._abort_registry.abort(subagent_id)
            if self._abort_registry is not None
            else False
        )
        _log.info(
            "subagent.bridge.cancel",
            extra={
                "subagent_id": subagent_id,
                "success": cancelled or aborted,
            },
        )
        return cancelled or aborted
